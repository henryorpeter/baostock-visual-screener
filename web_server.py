"""KLineRadar · 原生 Web 终端后端（Starlette + Uvicorn，零新增依赖）。

直接复用 db.py / sync.py / engine.py / charts.py 全部核心引擎，提供秒级 JSON API 与离线前端静态服务。
启动：python web_server.py --port 8506
"""
import argparse
import asyncio
from datetime import date, datetime, timedelta
import json
import math
from pathlib import Path
import re
import sys
import threading
import time
import traceback
from urllib.parse import urlparse
import webbrowser

import numpy as np
import pandas as pd
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
import uvicorn

import charts
import db
import engine
import sync


STATIC_DIR = Path(__file__).with_name("static")
_UNIVERSE_CACHE = {"ts": 0.0, "data": []}
_UNIVERSE_LOCK = threading.Lock()
_SCREEN_SEM = threading.BoundedSemaphore(2)
_CODE_RE = re.compile(r"^(?:sh|sz|bj)\.\d{6}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MB 请求体上限，防止大包内存耗尽
_MAX_EXPORT_ROWS = 10000
_MAX_WATCHLIST_SIZE = 500
_CSP_HEADER = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'"
)


def _valid_code(code):
    return isinstance(code, str) and bool(_CODE_RE.fullmatch(code))


def _valid_date_str(s):
    if not isinstance(s, str) or not _DATE_RE.fullmatch(s):
        return False
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _parse_finite_float(val, default, lo, hi, field_name):
    if val is None or val == "":
        return default, None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None, f"参数 {field_name} 必须是有效数字"
    if not math.isfinite(f):
        return None, f"参数 {field_name} 不能为 NaN 或无穷大"
    return max(lo, min(hi, f)), None


def _parse_bounded_int(val, default, lo, hi, field_name):
    if val is None or val == "":
        return default, None
    try:
        n = int(val)
    except (TypeError, ValueError):
        return None, f"参数 {field_name} 必须是整数"
    return max(lo, min(hi, n)), None


def _csv_safe_cell(val):
    """中和 Excel/WPS CSV 公式注入（以 =/+/-/@/Tab/CR 开头的文本单元格加前置单引号）。"""
    if val is None:
        return ""
    s = str(val)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


async def _read_json_dict(request, max_bytes=_MAX_BODY_BYTES):
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > max_bytes:
                return None, ("请求体过大", 413)
        except ValueError:
            return None, ("无效的 Content-Length", 400)
    raw = await request.body()
    if len(raw) > max_bytes:
        return None, ("请求体过大", 413)
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None, ("请求 JSON 格式错误", 400)
    if not isinstance(parsed, dict):
        return None, ("请求体必须为 JSON 对象", 400)
    return parsed, None


def _clean_num(val, ndigits=None):
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, ndigits) if ndigits is not None else f


def _get_universe_cached(force=False):
    now = time.monotonic()
    with _UNIVERSE_LOCK:
        if not force and _UNIVERSE_CACHE["data"] and (now - _UNIVERSE_CACHE["ts"]) < 30.0:
            return _UNIVERSE_CACHE["data"]
    data = db.cache_universe_load()
    with _UNIVERSE_LOCK:
        _UNIVERSE_CACHE["ts"] = now
        _UNIVERSE_CACHE["data"] = data
    return data


def _watchlist_with_quotes():
    rows = db.watchlist_load()
    if not rows:
        return []
    codes = [r[0] for r in rows]
    db._init_cache()
    with db.connect(timeout=30) as conn:
        conn.execute("CREATE TEMP TABLE wl_q (code TEXT PRIMARY KEY)")
        conn.executemany("INSERT OR IGNORE INTO wl_q VALUES (?)", ((c,) for c in codes))
        latest_map = {
            r[0]: r[1:]
            for r in conn.execute(
                """
                WITH mx AS (
                    SELECT w.code, MAX(d.date) AS last_d
                    FROM wl_q w LEFT JOIN daily d ON d.code = w.code
                    GROUP BY w.code
                )
                SELECT m.code, d.date, d.close, d.pctChg, d.turn, d.amount
                FROM mx m LEFT JOIN daily d ON d.code = m.code AND d.date = m.last_d
                """
            )
        }
    out = []
    for code, name, added_at in rows:
        dt_s, cl, pct, trn, amt = latest_map.get(code, (None, None, None, None, None))
        amt_num = _clean_num(amt)
        out.append({
            "code": code,
            "name": name,
            "date": dt_s or "",
            "close": _clean_num(cl, 2),
            "pctChg": _clean_num(pct, 2),
            "turn": _clean_num(trn, 2),
            "amount_yi": round(amt_num / 1e8, 2) if amt_num is not None else None,
            "added_at": added_at,
        })
    return out


async def api_index(_request):
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def api_bootstrap(_request):
    local_status = db.cache_status()
    local_status["db_name"] = db.CACHE_DB.name
    mgr = sync.get_sync_manager_singleton()
    auto_state = mgr.snapshot(local_status=local_status)
    return JSONResponse({
        "status": local_status,
        "auto_sync": auto_state,
        "auto_sync_times": db.AUTO_SYNC_TIMES,
        "strategies": engine.STRATEGIES,
        "strategy_groups": engine.STRATEGY_GROUPS,
        "strategy_to_group": engine.STRATEGY_TO_GROUP,
        "main_overlays": list(charts.MAIN_OVERLAYS),
        "sub_indicators": list(charts.SUB_INDICATORS),
        "watchlist": _watchlist_with_quotes(),
        "today": date.today().strftime("%Y-%m-%d"),
        "default_start": (date.today() - timedelta(days=180)).strftime("%Y-%m-%d"),
    })


async def api_search(request):
    q = (request.query_params.get("q") or "").strip().lower()
    universe = _get_universe_cached()
    if not q:
        items = [{"code": c, "name": n} for c, n in universe[:20]]
        return JSONResponse({"items": items})
    exact, prefix, contains = [], [], []
    for code, name in universe:
        c_low = code.lower()
        sym = c_low.split(".", 1)[-1]
        n_low = name.lower()
        if q == sym or q == c_low or q == n_low:
            exact.append({"code": code, "name": name})
        elif sym.startswith(q) or n_low.startswith(q):
            prefix.append({"code": code, "name": name})
        elif q in c_low or q in n_low:
            contains.append({"code": code, "name": name})
        if len(exact) + len(prefix) + len(contains) >= 60:
            break
    return JSONResponse({"items": (exact + prefix + contains)[:25]})


def _run_screen_core(body, emit=None):
    """同步执行全市场选股核心逻辑；可选通过 emit(pct, detail, scanned, total, hits) 汇报实时进度。"""
    def _report(pct, detail, scanned=0, total=0, hits=0):
        if emit is not None:
            emit({
                "type": "progress",
                "pct": max(0, min(100, int(round(pct)))),
                "detail": detail,
                "scanned": scanned,
                "total": total,
                "hits": hits,
            })

    today_dt = date.today()
    start_str = str(body.get("start") or (today_dt - timedelta(days=180)).strftime("%Y-%m-%d"))[:10]
    end_str = str(body.get("end") or today_dt.strftime("%Y-%m-%d"))[:10]
    raw_strats = body.get("strategies")
    if not isinstance(raw_strats, list):
        return None, ("策略列表格式无效", 400)
    strategies = [s for s in raw_strats if isinstance(s, str) and s in engine.STRATEGIES]
    if not strategies:
        return None, ("请至少选择 1 套选股策略", 400)

    # 留空时不限最低涨幅，确保「上升趋势跌停反包」与「涨停洗盘策略」等可正常命中
    min_change, err_msg = _parse_finite_float(body.get("min_change"), -35.0, -35.0, 35.0, "min_change")
    if err_msg:
        return None, (err_msg, 400)
    max_turn, err_msg = _parse_finite_float(body.get("max_turn"), 100.0, 0.0, 100.0, "max_turn")
    if err_msg:
        return None, (err_msg, 400)
    min_amount, err_msg = _parse_finite_float(body.get("min_amount"), 0.0, 0.0, 10000.0, "min_amount")
    if err_msg:
        return None, (err_msg, 400)
    max_stocks, err_msg = _parse_bounded_int(body.get("max_stocks"), 6000, 10, 6000, "max_stocks")
    if err_msg:
        return None, (err_msg, 400)
    exclude_st = bool(body.get("exclude_st", True))
    exclude_gem_star = bool(body.get("exclude_gem_star", False))

    if not (_valid_date_str(start_str) and _valid_date_str(end_str)):
        return None, ("日期格式无效，请使用 YYYY-MM-DD", 400)

    started = time.perf_counter()
    universe = _get_universe_cached()
    if not universe:
        return None, ("本地还没有股票列表，请先点击「同步行情」", 400)

    limit = min(max_stocks, len(universe))
    stride = max(1, len(universe) // limit)
    selected = universe[::stride][:limit]

    start_dt = pd.Timestamp(start_str).date()
    end_dt = pd.Timestamp(end_str).date()
    if start_dt >= end_dt:
        return None, ("开始日期必须早于结束日期", 400)

    _report(10, f"正在加载本地行情缓存（{len(selected):,} 只股票）…", 0, len(selected), 0)
    lookback_start = min(start_dt, end_dt - timedelta(days=210))
    db_rows, db_newest, db_rev = db._cache_version()
    frames = engine.load_market_arrays(
        lookback_start.strftime("%Y-%m-%d"), end_str, db_rows, db_newest, db_rev
    )
    if not frames:
        return None, ("本地没有所选日期区间的行情缓存，请先同步行情", 400)

    _report(22, f"缓存加载完成，开始扫描 {len(selected):,} 只股票（{len(strategies)} 套策略）…", 0, len(selected), 0)

    def _on_progress(pct_frac, stage=2, detail="", hits=0, scanned=0, total=0, **_extra):
        _report(pct_frac * 100.0, detail or "正在扫描策略…", scanned=scanned, total=total, hits=hits)

    result_df, scan_stats = engine.screen(
        selected, frames, strategies,
        min_change=min_change, min_turn=0.0, max_turn=max_turn,
        on_progress=_on_progress,
        window_start=start_str,
        exclude_st=exclude_st, exclude_gem_star=exclude_gem_star, min_amount=min_amount,
    )
    sparks = scan_stats.pop("sparks", {})
    elapsed = round(time.perf_counter() - started, 3)

    signals = []
    aggregated = []
    forward_returns = []
    hit_strategies = []

    if not result_df.empty:
        hit_strategies = list(result_df["策略"].unique())
        for r in result_df.to_dict("records"):
            code = r["代码"]
            strat = r["策略"]
            signals.append({
                "strategy": strat,
                "group": engine.STRATEGY_TO_GROUP.get(strat, ""),
                "code": code,
                "name": r["名称"],
                "close": _clean_num(r.get("收盘"), 2),
                "pctChg": _clean_num(r.get("涨跌幅%"), 2),
                "turn": _clean_num(r.get("换手率%"), 2),
                "amount_yi": _clean_num(r.get("成交额(亿)"), 2),
                "reason": r.get("信号说明", ""),
                "spark": sparks.get(code, []),
            })
        for code, grp in result_df.groupby("代码", sort=False):
            first = grp.iloc[0]
            strats = grp["策略"].unique().tolist()
            aggregated.append({
                "code": code,
                "name": first["名称"],
                "count": len(strats),
                "strategies": strats,
                "close": _clean_num(first["收盘"], 2),
                "pctChg": _clean_num(first["涨跌幅%"], 2),
                "turn": _clean_num(first["换手率%"], 2),
                "amount_yi": _clean_num(first["成交额(亿)"], 2) if "成交额(亿)" in first else None,
                "spark": sparks.get(code, []),
            })
        aggregated.sort(key=lambda x: (x["count"], x["pctChg"] if x["pctChg"] is not None else -999.0), reverse=True)

        if db_newest and end_str < db_newest:
            _report(97, "正在计算历史 5 / 10 日胜率速验…", len(selected), len(selected), len(signals))
            fwd_df = engine.compute_forward_returns(result_df, end_str)
            if not fwd_df.empty:
                for _, r in fwd_df.iterrows():
                    forward_returns.append({
                        "strategy": r["策略"],
                        "samples": int(r["样本数"]),
                        "win5": _clean_num(r["5日胜率%"], 1),
                        "avg5": _clean_num(r["5日均涨幅%"], 2),
                        "win10": _clean_num(r["10日胜率%"], 1),
                        "avg10": _clean_num(r["10日均涨幅%"], 2),
                    })

    avg_chg = _clean_num(result_df["涨跌幅%"].mean(), 2) if not result_df.empty else None
    limit_up = int((result_df["涨跌幅%"] >= 9.5).sum()) if not result_df.empty else 0

    return {
        "signals": signals,
        "aggregated": aggregated,
        "hit_strategies": hit_strategies,
        "forward_returns": forward_returns,
        "range": {"start": start_str, "end": end_str},
        "stats": {
            **scan_stats,
            "elapsed": elapsed,
            "hit_count": len(signals),
            "unique_count": len(aggregated),
            "avg_chg": avg_chg,
            "limit_up": limit_up,
            "newest": db_newest,
        },
    }, None


async def api_screen(request):
    body, body_err = await _read_json_dict(request)
    if body_err is not None:
        return JSONResponse({"error": body_err[0]}, status_code=body_err[1])
    if not _SCREEN_SEM.acquire(blocking=False):
        return JSONResponse({"error": "当前选股任务繁忙，请稍后再试"}, status_code=429)
    try:
        data, err = await asyncio.to_thread(_run_screen_core, body)
    finally:
        _SCREEN_SEM.release()
    if err is not None:
        return JSONResponse({"error": err[0]}, status_code=err[1])
    return JSONResponse(data)


async def api_screen_stream(request):
    """流式 NDJSON 选股接口，实时推送扫描进度与最终信号榜单。"""
    body, body_err = await _read_json_dict(request)
    if body_err is not None:
        return JSONResponse({"error": body_err[0]}, status_code=body_err[1])
    if not _SCREEN_SEM.acquire(blocking=False):
        return JSONResponse({"error": "当前选股任务繁忙，请稍后再试"}, status_code=429)

    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()

    def _emit(payload):
        loop.call_soon_threadsafe(queue.put_nowait, payload)

    def _worker():
        try:
            data, err = _run_screen_core(body, emit=_emit)
            if err is not None:
                _emit({"type": "error", "error": err[0], "status": err[1]})
            else:
                _emit({"type": "result", "data": data})
        except Exception as exc:
            print(f"[web_server] screen worker error: {exc}\n{traceback.format_exc()}", file=sys.stderr)
            _emit({"type": "error", "error": f"选股计算发生内部异常（{type(exc).__name__}），请查看启动黑窗日志", "status": 500})
        finally:
            _SCREEN_SEM.release()
            loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=_worker, daemon=True).start()

    async def _gen():
        while True:
            item = await queue.get()
            if item is None:
                break
            yield (json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8")

    return StreamingResponse(
        _gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _merge_live_bar_into_kline(frame, quote, interval, already_resampled):
    """将当日最新实时盘口合成到 K 线 DataFrame 最后一根，避免顶部现价与 K 线收盘价脱节。"""
    if frame is None or frame.empty or not quote:
        return frame
    try:
        op = float(quote["open"])
        hi = float(quote["high"])
        lo = float(quote["low"])
        cl = float(quote["close"])
        vol = float(quote.get("volume") or 0.0)
    except (KeyError, TypeError, ValueError):
        return frame
    if not (op > 0 and hi > 0 and lo > 0 and cl > 0):
        return frame
    q_ts = pd.Timestamp(quote["date"])
    d = frame[["date", "open", "high", "low", "close", "volume"]].copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date").reset_index(drop=True)
    last_idx = len(d) - 1
    last_ts = pd.Timestamp(d.at[last_idx, "date"])
    if q_ts < last_ts:
        return d

    if already_resampled and interval in ("周线", "月线"):
        freq = "W-FRI" if interval == "周线" else "M"
        if q_ts.to_period(freq) == last_ts.to_period(freq):
            d.at[last_idx, "high"] = max(float(d.at[last_idx, "high"]), hi)
            d.at[last_idx, "low"] = min(float(d.at[last_idx, "low"]), lo)
            d.at[last_idx, "close"] = cl
            prev_vol = float(d.at[last_idx, "volume"] or 0.0)
            if q_ts > last_ts:
                d.at[last_idx, "date"] = q_ts
                d.at[last_idx, "volume"] = prev_vol + max(0.0, vol)
            else:
                d.at[last_idx, "volume"] = max(prev_vol, vol)
            return d
    elif q_ts == last_ts:
        d.at[last_idx, "open"] = op
        d.at[last_idx, "high"] = hi
        d.at[last_idx, "low"] = lo
        d.at[last_idx, "close"] = cl
        if vol > 0:
            d.at[last_idx, "volume"] = vol
        return d

    new_row = pd.DataFrame(
        [{"date": q_ts, "open": op, "high": hi, "low": lo, "close": cl, "volume": max(0.0, vol)}]
    )
    return pd.concat([d, new_row], ignore_index=True)


async def api_kline(request):
    code = request.path_params["code"].strip()
    if not _valid_code(code):
        return JSONResponse({"error": "股票代码格式无效（需如 sh.600519 / sz.000001）"}, status_code=400)
    interval = request.query_params.get("interval", "日线")
    if interval not in ("日线", "周线", "月线"):
        interval = "日线"
    today_dt = date.today()
    today_str = today_dt.strftime("%Y-%m-%d")
    end_str = (request.query_params.get("end") or today_str).strip()[:10]
    if not _valid_date_str(end_str):
        return JSONResponse({"error": "截止日期格式无效，请使用 YYYY-MM-DD"}, status_code=400)

    # 1. 本地日线（用于换手率/成交额卡片及离线兜底）
    local_frame = db._cache_read(code, "1990-01-01", end_str)

    # 2. 并发拉取多年期前复权 K 线与当日盘中实时行情（消除串行等待，弱网/离线响应快一倍）
    ext_task = asyncio.to_thread(sync.fetch_kline_history, code, interval, end_str)
    if end_str >= today_str:
        ext_frame, live_info = await asyncio.gather(
            ext_task,
            asyncio.to_thread(sync.fetch_intraday_minute, code),
        )
    else:
        ext_frame, live_info = await ext_task, None

    use_ext = False
    if ext_frame is not None and not ext_frame.empty:
        if interval in ("周线", "月线"):
            use_ext = True
        elif local_frame.empty or len(ext_frame) > len(local_frame):
            use_ext = True

    if not use_ext and local_frame.empty:
        return JSONResponse({"error": f"本地暂无 {code} 在该区间的行情数据"}, status_code=404)

    # 名称查询
    name_map = dict(_get_universe_cached())
    name = name_map.get(code, code)

    local_last_date = pd.Timestamp(local_frame.iloc[-1].date).strftime("%Y-%m-%d") if not local_frame.empty else ""
    ext_last_date = pd.Timestamp(ext_frame.iloc[-1].date).strftime("%Y-%m-%d") if (use_ext and ext_frame is not None and not ext_frame.empty) else ""
    use_live_ticker = (
        live_info is not None
        and live_info.get("quote") is not None
        and live_info.get("date", "") >= local_last_date
        and (live_info.get("is_live") or live_info.get("date", "") > local_last_date)
    )

    if use_live_ticker:
        q = live_info["quote"]
        ticker_date = q["date"]
        t_open, t_high, t_low, t_close = q["open"], q["high"], q["low"], q["close"]
        change, amp = q["pctChg"], q["amp"]
        latest_turn, latest_amt, latest_vol = q["turn"], q["amount"], q["volume"]
        is_live = bool(q.get("is_live"))
        quote_time = q.get("quote_time") or ""
        if live_info.get("name") and name == code:
            name = live_info["name"]
    else:
        # 若分时接口超时但多年历史接口拿到了比本地库更新的交易日，顶部行情条同步采用 ext_frame 避免上下价格打架
        use_ext_ticker = use_ext and (local_frame.empty or ext_last_date > local_last_date)
        ticker_src = ext_frame if use_ext_ticker else local_frame
        latest = ticker_src.iloc[-1]
        prior = ticker_src.iloc[-2].close if len(ticker_src) > 1 else latest.close
        ticker_date = pd.Timestamp(latest.date).strftime("%Y-%m-%d")
        t_open, t_high, t_low, t_close = latest.open, latest.high, latest.low, latest.close
        change = (float(latest.close) / float(prior) - 1.0) * 100.0 if prior else 0.0
        amp = ((float(latest.high) - float(latest.low)) / float(prior) * 100.0) if prior else 0.0
        latest_turn = getattr(latest, "turn", None)
        latest_amt = getattr(latest, "amount", None)
        latest_vol = latest.volume
        if latest_amt is None or (isinstance(latest_amt, float) and math.isnan(latest_amt)):
            latest_amt = float(latest.close) * float(latest.volume)
        is_live = False
        quote_time = ""

    # 全套 14 项指标（不按 180 天截断，保留完整多年历史供左拖/缩放与长周期均线/布林带计算）
    base_kline = ext_frame if use_ext else local_frame
    history_source = "extended" if use_ext else "local"
    if use_live_ticker and live_info is not None:
        base_kline = _merge_live_bar_into_kline(base_kline, live_info["quote"], interval, already_resampled=use_ext)
    kdf = charts._resample_kline(base_kline, interval, already_resampled=use_ext)

    cols_to_export = [
        "time", "open", "high", "low", "close", "volume",
        "ma5", "ma10", "ma20", "ma30",
        "ema12", "ema26", "ema60",
        "boll_up", "boll_mid", "boll_dn",
        "bbi", "sar", "kc_up", "kc_mid", "kc_dn",
        "vol_ma5", "vol_ma10",
        "dif", "dea", "macd_hist",
        "kdj_k", "kdj_d", "kdj_j",
        "rsi6", "rsi12", "rsi24",
        "wr10", "wr6", "cci", "obv", "obv_ma30", "atr14",
    ]
    bars = []
    for row in kdf.itertuples(index=False):
        rec = {"time": row.time}
        for col in cols_to_export[1:]:
            val = getattr(row, col, None)
            if col in ("volume", "vol_ma5", "vol_ma10", "obv", "obv_ma30"):
                rec[col] = _clean_num(val, 0)
            elif col in ("dif", "dea", "macd_hist", "atr14"):
                rec[col] = _clean_num(val, 4)
            elif col in ("kdj_k", "kdj_d", "kdj_j", "rsi6", "rsi12", "rsi24", "wr10", "wr6", "cci"):
                rec[col] = _clean_num(val, 2)
            else:
                rec[col] = _clean_num(val, 3)
        bars.append(rec)

    last_bar = bars[-1] if bars else {}
    dif_v = last_bar.get("dif")
    dea_v = last_bar.get("dea")
    macd_trend = "—"
    if dif_v is not None and dea_v is not None:
        macd_trend = "金叉区" if dif_v >= dea_v else "空头区"

    wl_codes = {r[0] for r in db.watchlist_load()}
    return JSONResponse({
        "code": code,
        "name": name,
        "interval": interval,
        "history_source": history_source,
        "bar_count": len(bars),
        "in_watchlist": code in wl_codes,
        "ticker": {
            "date": ticker_date,
            "open": _clean_num(t_open, 2),
            "high": _clean_num(t_high, 2),
            "low": _clean_num(t_low, 2),
            "close": _clean_num(t_close, 2),
            "pctChg": _clean_num(change, 2),
            "amp": _clean_num(amp, 2),
            "turn": _clean_num(latest_turn, 2),
            "amount": _clean_num(latest_amt, 0),
            "amount_str": charts._fmt_amount(latest_amt),
            "volume_str": charts._fmt_volume(latest_vol),
            "rsi6": last_bar.get("rsi6"),
            "macd_trend": macd_trend,
            "is_live": is_live,
            "quote_time": quote_time,
        },
        "bars": bars,
    })


async def api_intraday(request):
    """返回单只股票当日实时分时图数据（每分钟现价、分时均价VWAP、分时量及盘口摘要）。"""
    code = request.path_params["code"].strip()
    if not _valid_code(code):
        return JSONResponse({"error": "股票代码格式无效（需如 sh.600519 / sz.000001）"}, status_code=400)

    live = await asyncio.to_thread(sync.fetch_intraday_minute, code)
    if live is None:
        return JSONResponse({"error": f"暂无法获取 {code} 的实时分时数据（可能离线或暂不支持该板块）"}, status_code=404)

    name_map = dict(_get_universe_cached())
    name = live.get("name") or name_map.get(code, code)
    q = live["quote"]
    wl_codes = {r[0] for r in db.watchlist_load()}
    return JSONResponse({
        "code": code,
        "name": name,
        "date": live["date"],
        "quote_time": live["quote_time"],
        "is_live": bool(live["is_live"]),
        "prev_close": _clean_num(live["prev_close"], 2),
        "in_watchlist": code in wl_codes,
        "ticker": {
            "date": q["date"],
            "open": _clean_num(q["open"], 2),
            "high": _clean_num(q["high"], 2),
            "low": _clean_num(q["low"], 2),
            "close": _clean_num(q["close"], 2),
            "pctChg": _clean_num(q["pctChg"], 2),
            "amp": _clean_num(q["amp"], 2),
            "turn": _clean_num(q["turn"], 2),
            "amount": _clean_num(q["amount"], 0),
            "amount_str": charts._fmt_amount(q["amount"]),
            "volume_str": charts._fmt_volume(q["volume"]),
            "rsi6": None,
            "macd_trend": f"昨收 {_clean_num(live['prev_close'], 2)}",
            "is_live": bool(live["is_live"]),
            "quote_time": live["quote_time"],
        },
        "points": live["points"],
    })


async def api_watchlist_list(_request):
    return JSONResponse({"items": _watchlist_with_quotes()})


async def api_watchlist_add(request):
    body, body_err = await _read_json_dict(request)
    if body_err is not None:
        return JSONResponse({"error": body_err[0]}, status_code=body_err[1])
    code = str(body.get("code") or "").strip()
    if not _valid_code(code):
        return JSONResponse({"error": "股票代码格式无效（需如 sh.600519）"}, status_code=400)
    raw_name = _CTRL_RE.sub("", str(body.get("name") or "")).strip()[:40]
    name = raw_name or dict(_get_universe_cached()).get(code, code)
    existing = db.watchlist_load()
    if len(existing) >= _MAX_WATCHLIST_SIZE and code not in {r[0] for r in existing}:
        return JSONResponse({"error": f"自选池已达上限（{_MAX_WATCHLIST_SIZE} 只）"}, status_code=400)
    db.watchlist_add(code, name)
    return JSONResponse({"ok": True, "items": _watchlist_with_quotes()})


async def api_watchlist_delete(request):
    code = request.path_params["code"].strip()
    if not _valid_code(code):
        return JSONResponse({"error": "股票代码格式无效"}, status_code=400)
    db.watchlist_remove(code)
    return JSONResponse({"ok": True, "items": _watchlist_with_quotes()})


async def api_sync_start(_request):
    mgr = sync.get_sync_manager_singleton()
    started = mgr.trigger_now()
    return JSONResponse({"started": started, "state": mgr.snapshot()})


async def api_sync_status(_request):
    local_status = db.cache_status()
    local_status["db_name"] = db.CACHE_DB.name
    mgr = sync.get_sync_manager_singleton()
    state = mgr.snapshot(local_status=local_status)
    if not state.get("running") and local_status.get("stocks", 0) > 0:
        _get_universe_cached(force=not _UNIVERSE_CACHE["data"])
    return JSONResponse({"status": local_status, "auto_sync": state})


async def api_sync_config(request):
    body, body_err = await _read_json_dict(request)
    if body_err is not None:
        return JSONResponse({"error": body_err[0]}, status_code=body_err[1])
    enabled = bool(body.get("enabled", True))
    sync_time = str(body.get("sync_time") or "15:35").strip()
    if sync_time not in db.AUTO_SYNC_TIMES:
        return JSONResponse({"error": "不支持的自动同步时间"}, status_code=400)
    db.save_auto_sync_config(enabled, sync_time)
    mgr = sync.get_sync_manager_singleton()
    return JSONResponse({"ok": True, "auto_sync": mgr.snapshot()})


async def api_export(request):
    body, body_err = await _read_json_dict(request)
    if body_err is not None:
        return JSONResponse({"error": body_err[0]}, status_code=body_err[1])
    fmt = str(body.get("format") or "csv").lower()
    if fmt not in ("csv", "ebk", "txt"):
        return JSONResponse({"error": "不支持的导出格式"}, status_code=400)
    raw_codes = body.get("codes") or []
    raw_rows = body.get("signals") or []
    if not isinstance(raw_codes, list) or not isinstance(raw_rows, list):
        return JSONResponse({"error": "导出数据格式无效"}, status_code=400)
    codes = [c for c in raw_codes[:_MAX_EXPORT_ROWS] if _valid_code(c)]
    rows = [r for r in raw_rows[:_MAX_EXPORT_ROWS] if isinstance(r, dict)]
    today_tag = date.today().strftime("%Y%m%d")

    if fmt == "ebk":
        content = engine._format_tdx_ebk(codes)
        return Response(
            content=content,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="klineradar_{today_tag}.ebk"'},
        )
    if fmt == "txt":
        content = engine._format_plain_codes(codes).encode("utf-8")
        return Response(
            content=content,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="klineradar_codes_{today_tag}.txt"'},
        )
    # Default: CSV（文本列中和公式注入风险）
    df = pd.DataFrame([
        {
            "策略": _csv_safe_cell(r.get("strategy", "")),
            "代码": _csv_safe_cell(r.get("code", "")),
            "名称": _csv_safe_cell(r.get("name", "")),
            "收盘": r.get("close"),
            "涨跌幅%": r.get("pctChg"),
            "换手率%": r.get("turn"),
            "成交额(亿)": r.get("amount_yi"),
            "信号说明": _csv_safe_cell(r.get("reason", "")),
        }
        for r in rows
    ])
    return Response(
        content=df.to_csv(index=False).encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="klineradar_screen_{today_tag}.csv"'},
    )


async def _handle_unexpected_error(_request, exc):
    print(f"[web_server] unhandled error: {exc}", file=sys.stderr)
    return JSONResponse({"error": "服务器内部错误，请稍后重试"}, status_code=500)


routes = [
    Route("/", api_index),
    Route("/api/bootstrap", api_bootstrap),
    Route("/api/search", api_search),
    Route("/api/screen", api_screen, methods=["POST"]),
    Route("/api/screen/stream", api_screen_stream, methods=["POST"]),
    Route("/api/kline/{code:path}", api_kline),
    Route("/api/intraday/{code:path}", api_intraday),
    Route("/api/watchlist", api_watchlist_list, methods=["GET"]),
    Route("/api/watchlist", api_watchlist_add, methods=["POST"]),
    Route("/api/watchlist/{code:path}", api_watchlist_delete, methods=["DELETE"]),
    Route("/api/sync/start", api_sync_start, methods=["POST"]),
    Route("/api/sync/status", api_sync_status, methods=["GET"]),
    Route("/api/sync/config", api_sync_config, methods=["POST"]),
    Route("/api/export", api_export, methods=["POST"]),
    Mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static"),
]

class _SecurityMiddleware:
    """纯 ASGI 安全中间件：跨站 CSRF 拦截 + 安全响应头（兼容流式 NDJSON 零缓冲）。"""

    _SEC_HEADERS = [
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"no-referrer"),
        (b"content-security-policy", _CSP_HEADER.encode("latin1")),
    ]

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            hdrs = {k.lower(): v.decode("latin1").strip() for k, v in scope.get("headers", [])}
            if hdrs.get(b"sec-fetch-site", "").lower() == "cross-site":
                resp = JSONResponse({"error": "拒绝跨站请求"}, status_code=403)
                await resp(scope, receive, send)
                return
            origin = hdrs.get(b"origin", "")
            if origin:
                host = hdrs.get(b"host", "").lower()
                origin_netloc = (urlparse(origin).netloc or "").strip().lower()
                if not origin_netloc or origin_netloc != host:
                    resp = JSONResponse({"error": "拒绝异源请求"}, status_code=403)
                    await resp(scope, receive, send)
                    return

        async def _send_with_headers(message):
            if message["type"] == "http.response.start":
                raw_hdrs = list(message.get("headers", []))
                present = {k.lower() for k, _ in raw_hdrs}
                for hk, hv in self._SEC_HEADERS:
                    if hk not in present:
                        raw_hdrs.append((hk, hv))
                message = {**message, "headers": raw_hdrs}
            await send(message)

        await self.app(scope, receive, _send_with_headers)


web_app = Starlette(
    routes=routes,
    middleware=[Middleware(_SecurityMiddleware)],
    exception_handlers={500: _handle_unexpected_error},
)


def _ensure_utf8_stdio():
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def run_server(host="0.0.0.0", port=8506, open_browser=False):
    _ensure_utf8_stdio()
    db._init_cache()
    sync.get_sync_manager_singleton()
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    uvicorn.run(web_app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    _ensure_utf8_stdio()
    parser = argparse.ArgumentParser(description="KLineRadar Native Web Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8506)
    parser.add_argument("--open", action="store_true", help="自动打开浏览器")
    args = parser.parse_args()
    print(f"KLineRadar running at http://localhost:{args.port}")
    run_server(host=args.host, port=args.port, open_browser=args.open)
