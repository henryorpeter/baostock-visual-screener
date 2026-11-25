"""KLineRadar · 原生 Web 终端后端（Starlette + Uvicorn，零新增依赖）。

直接复用 db.py / sync.py / engine.py / charts.py 全部核心引擎，提供秒级 JSON API 与离线前端静态服务。
启动：python web_server.py --port 8506
"""
import argparse
from datetime import date, datetime, timedelta
import math
from pathlib import Path
import threading
import time
import webbrowser

import numpy as np
import pandas as pd
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, Response
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
        out.append({
            "code": code,
            "name": name,
            "date": dt_s or "",
            "close": _clean_num(cl, 2),
            "pctChg": _clean_num(pct, 2),
            "turn": _clean_num(trn, 2),
            "amount_yi": _clean_num(float(amt) / 1e8, 2) if amt is not None else None,
            "added_at": added_at,
        })
    return out


async def api_index(_request):
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def api_bootstrap(_request):
    local_status = db.cache_status()
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


async def api_screen(request):
    body = await request.json()
    today_dt = date.today()
    start_str = str(body.get("start") or (today_dt - timedelta(days=180)).strftime("%Y-%m-%d"))[:10]
    end_str = str(body.get("end") or today_dt.strftime("%Y-%m-%d"))[:10]
    strategies = [s for s in (body.get("strategies") or []) if s in engine.STRATEGIES]
    if not strategies:
        return JSONResponse({"error": "请至少选择 1 套选股策略"}, status_code=400)
    if start_str >= end_str:
        return JSONResponse({"error": "开始日期必须早于结束日期"}, status_code=400)

    min_change = float(body.get("min_change", 0.0))
    max_turn = float(body.get("max_turn", 100.0))
    min_amount = float(body.get("min_amount", 0.0))
    exclude_st = bool(body.get("exclude_st", True))
    exclude_gem_star = bool(body.get("exclude_gem_star", False))
    max_stocks = max(10, min(6000, int(body.get("max_stocks", 6000))))

    started = time.perf_counter()
    universe = _get_universe_cached()
    if not universe:
        return JSONResponse({"error": "本地还没有股票列表，请先点击「同步行情」"}, status_code=400)

    limit = min(max_stocks, len(universe))
    stride = max(1, len(universe) // limit)
    selected = universe[::stride][:limit]

    try:
        start_dt = pd.Timestamp(start_str).date()
        end_dt = pd.Timestamp(end_str).date()
    except (ValueError, TypeError):
        return JSONResponse({"error": "日期格式无效，请使用 YYYY-MM-DD"}, status_code=400)
    if start_dt >= end_dt:
        return JSONResponse({"error": "开始日期必须早于结束日期"}, status_code=400)
    lookback_start = min(start_dt, end_dt - timedelta(days=210))
    db_rows, db_newest, db_rev = db._cache_version()
    frames = engine.load_market_arrays(
        lookback_start.strftime("%Y-%m-%d"), end_str, db_rows, db_newest, db_rev
    )
    if not frames:
        return JSONResponse({"error": "本地没有所选日期区间的行情缓存，请先同步行情"}, status_code=400)

    result_df, scan_stats = engine.screen(
        selected, frames, strategies,
        min_change=min_change, min_turn=0.0, max_turn=max_turn,
        on_progress=lambda *a, **k: None,
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

    return JSONResponse({
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
    })


async def api_kline(request):
    code = request.path_params["code"].strip()
    interval = request.query_params.get("interval", "日线")
    if interval not in ("日线", "周线", "月线"):
        interval = "日线"
    today_dt = date.today()
    start_str = request.query_params.get("start") or (today_dt - timedelta(days=180)).strftime("%Y-%m-%d")
    end_str = request.query_params.get("end") or today_dt.strftime("%Y-%m-%d")

    if interval in ("周线", "月线"):
        fetch_start = "1990-01-01"
        display_start = None
    else:
        fetch_start = (pd.Timestamp(start_str) - pd.Timedelta(days=180)).strftime("%Y-%m-%d")
        display_start = start_str

    frame = db._cache_read(code, fetch_start, end_str)
    if frame.empty:
        return JSONResponse({"error": f"本地暂无 {code} 在该区间的日线缓存"}, status_code=404)

    # 名称查询
    name_map = dict(_get_universe_cached())
    name = name_map.get(code, code)

    # 最新日行情卡片摘要（基于原始日线）
    latest = frame.iloc[-1]
    prior = frame.iloc[-2].close if len(frame) > 1 else latest.close
    change = (float(latest.close) / float(prior) - 1.0) * 100.0 if prior else 0.0
    amp = ((float(latest.high) - float(latest.low)) / float(prior) * 100.0) if prior else 0.0

    # 全套 14 项指标（复用 charts._resample_kline）
    kdf = charts._resample_kline(frame, interval)
    if display_start is not None and not kdf.empty:
        sliced = kdf[kdf["date"] >= pd.Timestamp(display_start)].reset_index(drop=True)
        if not sliced.empty:
            kdf = sliced

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
        "in_watchlist": code in wl_codes,
        "ticker": {
            "date": pd.Timestamp(latest.date).strftime("%Y-%m-%d"),
            "open": _clean_num(latest.open, 2),
            "high": _clean_num(latest.high, 2),
            "low": _clean_num(latest.low, 2),
            "close": _clean_num(latest.close, 2),
            "pctChg": _clean_num(change, 2),
            "amp": _clean_num(amp, 2),
            "turn": _clean_num(latest.turn, 2),
            "amount": _clean_num(latest.amount, 0),
            "amount_str": charts._fmt_amount(latest.amount),
            "volume_str": charts._fmt_volume(latest.volume),
            "rsi6": last_bar.get("rsi6"),
            "macd_trend": macd_trend,
        },
        "bars": bars,
    })


async def api_watchlist_list(_request):
    return JSONResponse({"items": _watchlist_with_quotes()})


async def api_watchlist_add(request):
    body = await request.json()
    code = str(body.get("code") or "").strip()
    name = str(body.get("name") or "").strip()
    if not code:
        return JSONResponse({"error": "缺少股票代码"}, status_code=400)
    if not name:
        name = dict(_get_universe_cached()).get(code, code)
    db.watchlist_add(code, name)
    return JSONResponse({"ok": True, "items": _watchlist_with_quotes()})


async def api_watchlist_delete(request):
    code = request.path_params["code"].strip()
    db.watchlist_remove(code)
    return JSONResponse({"ok": True, "items": _watchlist_with_quotes()})


async def api_sync_start(_request):
    mgr = sync.get_sync_manager_singleton()
    started = mgr.trigger_now()
    return JSONResponse({"started": started, "state": mgr.snapshot()})


async def api_sync_status(_request):
    local_status = db.cache_status()
    mgr = sync.get_sync_manager_singleton()
    state = mgr.snapshot(local_status=local_status)
    return JSONResponse({"status": local_status, "auto_sync": state})


async def api_sync_config(request):
    body = await request.json()
    enabled = bool(body.get("enabled", True))
    sync_time = str(body.get("sync_time") or "15:35")
    db.save_auto_sync_config(enabled, sync_time)
    mgr = sync.get_sync_manager_singleton()
    return JSONResponse({"ok": True, "auto_sync": mgr.snapshot()})


async def api_export(request):
    body = await request.json()
    fmt = str(body.get("format") or "csv").lower()
    codes = body.get("codes") or []
    rows = body.get("signals") or []
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
    # Default: CSV
    df = pd.DataFrame([
        {
            "策略": r.get("strategy", ""),
            "代码": r.get("code", ""),
            "名称": r.get("name", ""),
            "收盘": r.get("close"),
            "涨跌幅%": r.get("pctChg"),
            "换手率%": r.get("turn"),
            "成交额(亿)": r.get("amount_yi"),
            "信号说明": r.get("reason", ""),
        }
        for r in rows
    ])
    return Response(
        content=df.to_csv(index=False).encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="klineradar_screen_{today_tag}.csv"'},
    )


routes = [
    Route("/", api_index),
    Route("/api/bootstrap", api_bootstrap),
    Route("/api/search", api_search),
    Route("/api/screen", api_screen, methods=["POST"]),
    Route("/api/kline/{code:path}", api_kline),
    Route("/api/watchlist", api_watchlist_list, methods=["GET"]),
    Route("/api/watchlist", api_watchlist_add, methods=["POST"]),
    Route("/api/watchlist/{code:path}", api_watchlist_delete, methods=["DELETE"]),
    Route("/api/sync/start", api_sync_start, methods=["POST"]),
    Route("/api/sync/status", api_sync_status, methods=["GET"]),
    Route("/api/sync/config", api_sync_config, methods=["POST"]),
    Route("/api/export", api_export, methods=["POST"]),
    Mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static"),
]

web_app = Starlette(routes=routes)


def run_server(host="0.0.0.0", port=8506, open_browser=False):
    db._init_cache()
    sync.get_sync_manager_singleton()
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    uvicorn.run(web_app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KLineRadar Native Web Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8506)
    parser.add_argument("--open", action="store_true", help="自动打开浏览器")
    args = parser.parse_args()
    print(f"KLineRadar running at http://localhost:{args.port}")
    run_server(host=args.host, port=args.port, open_browser=args.open)
