"""Baostock A股可视化选股工具。

运行：streamlit run baostock_screener.py
"""
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import sqlite3
import time
import requests

import baostock as bs
import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st


DAILY_FIELDS = "date,code,open,high,low,close,volume,amount,turn,pctChg,isST"
BAOSTOCK_AVAILABLE = None
CACHE_DB = Path(__file__).with_name("stock_cache.sqlite3")


def _init_cache():
    with sqlite3.connect(CACHE_DB) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS daily (
            code TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL, low REAL,
            close REAL, volume REAL, amount REAL, turn REAL, pctChg REAL, isST TEXT,
            PRIMARY KEY (code, date))""")
        conn.execute("CREATE TABLE IF NOT EXISTS sync_meta (code TEXT PRIMARY KEY, start_date TEXT NOT NULL, through_date TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS stock_names (code TEXT PRIMARY KEY, name TEXT NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_code_date ON daily(code,date)")


def _cache_read(code, start, end):
    _init_cache()
    with sqlite3.connect(CACHE_DB) as conn:
        frame = pd.read_sql_query(
            "SELECT date,code,open,high,low,close,volume,amount,turn,pctChg,isST FROM daily "
            "WHERE code=? AND date BETWEEN ? AND ? ORDER BY date", conn,
            params=(code, start, end),
        )
    if not frame.empty:
        frame["date"] = pd.to_datetime(frame["date"])
    return frame


def _cache_write(frame):
    if frame.empty:
        return
    cols = ["date", "code", "open", "high", "low", "close", "volume", "amount", "turn", "pctChg", "isST"]
    data = frame[cols].copy()
    data["date"] = pd.to_datetime(data["date"]).dt.strftime("%Y-%m-%d")
    with sqlite3.connect(CACHE_DB, timeout=30) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO daily (date,code,open,high,low,close,volume,amount,turn,pctChg,isST) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)", data.itertuples(index=False, name=None),
        )


STRATEGIES = {
    "底部趋势拐点": "深跌 + MACD/RSI 底部反转",
    "涨停回马枪策略": "涨停后缩量回调再次启动",
    "涨停横盘策略": "涨停后窄幅横盘突破",
    "启明星策略": "三根 K 线底部反转",
    "多金叉共振": "均线 + KDJ + MACD 金叉",
    "多方炮策略": "两阳夹一阴",
    "阻力位突破策略": "放量突破 20 日阻力",
    "强势洗盘弱转强": "强势股洗盘后反包",
    "趋势加速拐点": "上升趋势放量加速",
    "仙人指路策略": "长上影后收复压力位",
    "W底策略": "双底后颈线突破",
    "趋势起点策略": "短均线向上突破长均线",
    "2560战法": "突破 MA25 + 均量金叉",
    "金三角策略": "均线/MACD/KDJ 三点共振",
    "金叉不绿策略": "均线金叉且阳线确认",
    "海龟突破策略": "突破前 20 日高点、放量并收阳",
    "高旗形整理策略": "强势上涨后高位收敛并缩量",
    "涨停洗盘策略": "昨日涨停后回踩支撑并放量",
    "上升趋势跌停反包": "多头趋势中放量跌停，观察错杀反弹",
    "RPS强势突破": "120 日相对强度前 10%，收盘接近 120 日新高（按本次扫描样本排名）",
}


def _rows(result):
    if result.error_code != "0":
        raise RuntimeError(f"Baostock 错误 {result.error_code}: {result.error_msg}")
    rows = []
    while result.next():
        rows.append(result.get_row_data())
    return pd.DataFrame(rows, columns=result.fields)


def _query(fn):
    global BAOSTOCK_AVAILABLE
    last = None
    for attempt in range(3):
        login = bs.login()
        if login.error_code == "0":
            BAOSTOCK_AVAILABLE = True
            try:
                return fn()
            finally:
                bs.logout()
        last = login
        try:
            bs.logout()
        except Exception:
            pass
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(
        f"登录失败 {last.error_code}: {last.error_msg}。"
        "这是 Baostock 网络连接错误，请检查 public-api.baostock.com:10030 是否可访问后重试。"
    )


def _https_json(url, params, referer):
    error = None
    for attempt in range(3):
        try:
            response = requests.get(
                url, params=params,
                headers={"User-Agent": "Mozilla/5.0", "Referer": referer}, timeout=15,
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            error = exc
            if attempt < 2:
                time.sleep(attempt + 1)
    raise RuntimeError(f"HTTPS 行情备用源请求失败：{error}")


def _tonghuashun_stock_list():
    """同花顺公开 A 股分页页，作为 Baostock/东方财富不可用时的列表源。"""
    import re
    from html import unescape
    rows = {}
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://q.10jqka.com.cn/"}
    page_count = 280
    for page in range(1, page_count + 1):
        url = f"https://q.10jqka.com.cn/index/index/board/all/field/zdf/order/desc/page/{page}/"
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        response.encoding = "gbk"
        page_info = re.search(r'class="page_info">\s*\d+/(\d+)', response.text)
        if page_info:
            page_count = min(280, int(page_info.group(1)))
        for code, name in re.findall(
            r'<a href="https?://stockpage\.10jqka\.com\.cn/(\d{6})/"[^>]*>\d{6}</a>\s*'
            r'<a href="https?://stockpage\.10jqka\.com\.cn/\d{6}/"[^>]*>(.*?)</a>',
            response.text, re.S,
        ):
            name = unescape(re.sub(r"<.*?>", "", name)).strip()
            if code.startswith(("000", "001", "002", "003", "300", "301")):
                market = "sz"
            elif code.startswith(("600", "601", "603", "605", "688", "689")):
                market = "sh"
            else:
                continue
            if name and "退" not in name and not any(x in name for x in ("债", "基", "ETF", "LOF", "指数")):
                rows[f"{market}.{code}"] = name
        if not re.search(r"stockpage\.10jqka\.com\.cn/\d{6}/", response.text):
            raise RuntimeError(f"同花顺第 {page} 页未返回股票数据")
        if page >= page_count:
            break
    if not rows:
        raise RuntimeError("同花顺 HTTPS 未返回股票列表（可能触发反爬）")
    return pd.DataFrame([{"code": code, "tradeStatus": "1", "code_name": name} for code, name in rows.items()])


def _tencent_stock_list():
    """同花顺受限时，用腾讯实时报价核验候选代码并构建真实股票列表。"""
    import re
    ranges = [(600000, 600999), (601000, 601999), (603000, 603999),
              (605000, 605999), (688000, 689999), (0, 3999),
              (300000, 301999)]
    codes = []
    for first, last in ranges:
        codes.extend(f"{n:06d}" for n in range(first, last + 1))
    rows = {}
    headers = {"User-Agent": "Mozilla/5.0"}
    session = requests.Session()
    for offset in range(0, len(codes), 300):
        batch = codes[offset:offset + 300]
        symbols = [f"{'sh' if code[0] in '68' else 'sz'}{code}" for code in batch]
        response = session.get("https://qt.gtimg.cn/q=" + ",".join(symbols), headers=headers, timeout=15)
        response.raise_for_status()
        response.encoding = "gbk"
        for symbol, content in re.findall(r'v_((?:sh|sz)\d{6})="([^"]*)"', response.text):
            fields = content.split("~")
            if len(fields) < 4 or not fields[1] or fields[1] == "N/A":
                continue
            try:
                if float(fields[3]) <= 0:
                    continue
            except ValueError:
                continue
            code = symbol[:2] + "." + symbol[2:]
            name = fields[1]
            if "退" not in name and not any(word in name for word in ("债", "基金", "ETF", "LOF", "指数")):
                rows[code] = name
    if len(rows) < 1000:
        raise RuntimeError(f"腾讯 HTTPS 仅核验到 {len(rows)} 只股票，列表不完整")
    return pd.DataFrame([{"code": code, "tradeStatus": "1", "code_name": name} for code, name in rows.items()])


@st.cache_data(ttl=3600, show_spinner=False)
def stock_list(day):
    global BAOSTOCK_AVAILABLE
    def run():
        requested = pd.Timestamp(day)
        for offset in range(11):
            actual_day = (requested - pd.Timedelta(days=offset)).strftime("%Y-%m-%d")
            frame = _rows(bs.query_all_stock(day=actual_day))
            if not frame.empty:
                return frame, actual_day
        return pd.DataFrame(), ""

    if BAOSTOCK_AVAILABLE is False:
        frame, actual_day = pd.DataFrame(), ""
    else:
        try:
            frame, actual_day = _query(run)
        except Exception:
            BAOSTOCK_AVAILABLE = False
            frame, actual_day = pd.DataFrame(), ""
    source = "Baostock"
    if frame.empty:
        try:
            frame = _tonghuashun_stock_list()
            source = "同花顺 HTTPS"
        except Exception as ths_error:
            try:
                frame = _tencent_stock_list()
                source = "腾讯 HTTPS 备用源"
            except Exception as qq_error:
                raise RuntimeError(
                    f"Baostock 登录失败；同花顺备用列表不可用：{ths_error}；腾讯备用列表不可用：{qq_error}"
                ) from qq_error
        actual_day = day
    if frame.empty:
        return frame, actual_day, source
    frame = frame[frame.code.str.match(r"^(sh\.(600|601|603|605|688|689)|sz\.(000|001|002|003|300|301))\d{3}$")].copy()
    return frame, actual_day, source


def _history(code, start, end):
    result = bs.query_history_k_data_plus(
        code, DAILY_FIELDS, start_date=start, end_date=end,
        frequency="d", adjustflag="2"
    )
    frame = _rows(result)
    if frame.empty:
        return frame
    for col in ("open", "high", "low", "close", "volume", "amount", "turn", "pctChg"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame["date"] = pd.to_datetime(frame["date"])
    return frame.dropna(subset=["close"]).sort_values("date")


def _history_http(code, start, end):
    market, symbol = code.split(".")
    payload = _https_json(
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        {"param": f"{market}{symbol},day,,,{min(1000, max(500, (pd.Timestamp(end) - pd.Timestamp(start)).days + 30))},qfq"},
        "https://gu.qq.com/",
    )
    payload = (payload.get("data") or {}).get(f"{market}{symbol}", {})
    rows = [row[:6] for row in payload.get("qfqday", []) if len(row) >= 6]
    frame = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume"])
    if frame.empty:
        raise RuntimeError(f"腾讯 HTTPS 备用源未返回 {code} 的日线数据")
    frame["code"] = code
    frame["amount"] = 0
    frame["turn"] = float("nan")
    frame["pctChg"] = pd.to_numeric(frame["close"], errors="coerce").pct_change().mul(100)
    frame["isST"] = "0"
    frame = frame[(frame.date >= start) & (frame.date <= end)]
    for col in ("open", "high", "low", "close", "volume", "amount"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    if symbol.startswith(("688", "689")):
        frame["volume"] = frame["volume"] / 100
    frame["amount"] = frame["close"] * frame["volume"] * 100
    frame["date"] = pd.to_datetime(frame["date"])
    return frame.dropna(subset=["close"]).sort_values("date")


def cache_status():
    _init_cache()
    with sqlite3.connect(CACHE_DB) as conn:
        stocks, rows, newest = conn.execute(
            "SELECT COUNT(DISTINCT code), COUNT(*), MAX(date) FROM daily"
        ).fetchone()
    return {"stocks": stocks, "rows": rows, "newest": newest}


def cache_universe_save(stocks):
    _init_cache()
    with sqlite3.connect(CACHE_DB) as conn:
        conn.execute("DELETE FROM stock_names")
        conn.executemany("INSERT OR REPLACE INTO stock_names(code,name) VALUES (?,?)",
                         stocks[["code", "code_name"]].itertuples(index=False, name=None))


def cache_universe_load():
    _init_cache()
    with sqlite3.connect(CACHE_DB) as conn:
        return conn.execute("SELECT code,name FROM stock_names ORDER BY code").fetchall()


def update_cache(codes, start, end, progress):
    """缺失/过期股票先走单连接 Baostock，失败项再以 6 路 HTTPS 补齐。"""
    global BAOSTOCK_AVAILABLE
    _init_cache()
    tasks, skipped = [], 0
    with sqlite3.connect(CACHE_DB) as conn:
        conn.execute("CREATE TEMP TABLE wanted (code TEXT PRIMARY KEY)")
        conn.executemany("INSERT OR IGNORE INTO wanted VALUES (?)", ((code,) for code, _ in codes))
        state = {row[0]: row[1:] for row in conn.execute(
            "SELECT w.code, MIN(d.date), MAX(d.date), m.start_date, m.through_date "
            "FROM wanted w LEFT JOIN daily d ON d.code=w.code "
            "LEFT JOIN sync_meta m ON m.code=w.code GROUP BY w.code"
        )}
    for code, _ in codes:
        first, last, synced_start, synced_end = state.get(code, (None, None, None, None))
        if first and last and first <= start and last >= end:
            skipped += 1
            continue
        if synced_end and synced_start and synced_end >= end and synced_start <= start:
            skipped += 1
            continue
        fetch_start = start
        if first and first <= start and last:
            fetch_start = max(start, (pd.Timestamp(last) - pd.Timedelta(days=7)).strftime("%Y-%m-%d"))
        tasks.append((code, fetch_start))

    successful, changed, failures = set(), set(), {}
    total = len(tasks)
    if BAOSTOCK_AVAILABLE is not False and tasks:
        def fetch_baostock():
            batch = []
            for i, (code, fetch_start) in enumerate(tasks, 1):
                try:
                    frame = _history(code, fetch_start, end)
                except Exception as exc:
                    failures[code] = str(exc)
                else:
                    successful.add(code)
                    failures.pop(code, None)
                    if not frame.empty:
                        batch.append(frame)
                        changed.add(code)
                if len(batch) >= 50:
                    _cache_write(pd.concat(batch, ignore_index=True))
                    batch.clear()
                if i % 10 == 0 or i == total:
                    progress.progress(i / max(1, total), text=f"Baostock 更新 {i}/{total}")
            if batch:
                _cache_write(pd.concat(batch, ignore_index=True))
        try:
            _query(fetch_baostock)
        except Exception:
            BAOSTOCK_AVAILABLE = False

    pending = [(code, fetch_start) for code, fetch_start in tasks if code not in successful]
    if pending:
        with ThreadPoolExecutor(max_workers=6) as pool:
            future_codes = {pool.submit(_history_http, code, fetch_start, end): code
                            for code, fetch_start in pending}
            for i, future in enumerate(as_completed(future_codes), 1):
                code = future_codes[future]
                try:
                    frame = future.result()
                    if not frame.empty:
                        _cache_write(frame)
                        changed.add(code)
                    successful.add(code)
                    failures.pop(code, None)
                except Exception as exc:
                    failures[code] = str(exc)
                progress.progress(i / max(1, len(pending)), text=f"HTTPS 补齐 {i}/{len(pending)}（并发 6）")

    with sqlite3.connect(CACHE_DB) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO sync_meta(code,start_date,through_date) VALUES (?,?,?)",
            ((code, start, end) for code in successful),
        )
    source = "腾讯 HTTPS 并发" if BAOSTOCK_AVAILABLE is False else ("Baostock + HTTPS 回退" if pending else "Baostock")
    return {"updated": len(changed), "skipped": skipped, "failed": len(failures),
            "failures": list(failures.items())[:10], "source": source}


def load_cache(codes, start, end):
    _init_cache()
    with sqlite3.connect(CACHE_DB) as conn:
        conn.execute("CREATE TEMP TABLE wanted (code TEXT PRIMARY KEY)")
        conn.executemany("INSERT OR IGNORE INTO wanted VALUES (?)", ((code,) for code, _ in codes))
        frame = pd.read_sql_query(
            "SELECT d.date,d.code,d.open,d.high,d.low,d.close,d.volume,d.amount,d.turn,d.pctChg,d.isST "
            "FROM daily d JOIN wanted w ON d.code=w.code WHERE d.date BETWEEN ? AND ? ORDER BY d.code,d.date",
            conn, params=(start, end),
        )
    if frame.empty:
        return {}
    frame["date"] = pd.to_datetime(frame["date"])
    return {code: group.reset_index(drop=True) for code, group in frame.groupby("code", sort=False)}


def indicators(frame):
    d = frame.copy()
    for n in (5, 10, 20, 25, 60):
        d[f"ma{n}"] = d.close.rolling(n).mean()
    delta = d.close.diff()
    gain, loss = delta.clip(lower=0), -delta.clip(upper=0)
    avg_gain, avg_loss = gain.rolling(14).mean(), loss.rolling(14).mean()
    d["rsi"] = 100 - 100 / (1 + avg_gain / avg_loss.replace(0, float("nan")))
    d.loc[(avg_loss == 0) & (avg_gain > 0), "rsi"] = 100
    d.loc[(avg_loss == 0) & (avg_gain == 0), "rsi"] = 50
    ema12, ema26 = d.close.ewm(span=12, adjust=False).mean(), d.close.ewm(span=26, adjust=False).mean()
    d["macd"] = ema12 - ema26
    d["signal"] = d.macd.ewm(span=9, adjust=False).mean()
    low9, high9 = d.low.rolling(9).min(), d.high.rolling(9).max()
    d["k"] = 100 * (d.close - low9) / (high9 - low9).replace(0, float("nan"))
    d["d"] = d.k.rolling(3).mean()
    d["vol_ma5"] = d.volume.rolling(5).mean()
    d["vol_ma20"] = d.volume.rolling(20).mean()
    return d.dropna(subset=["ma25", "rsi", "macd", "signal", "k", "d"])


def strategy_signal(frame, name, d=None):
    d = d if d is not None else indicators(frame)
    if len(d) < 3:
        return False, "数据不足"
    x, p, p2 = d.iloc[-1], d.iloc[-2], d.iloc[-3]
    ret = d.pctChg.fillna(0)
    recent_limit = (ret.iloc[-11:-1] >= 9.5).any()
    cross_ma = p.ma5 <= p.ma10 and x.ma5 > x.ma10
    cross_macd = p.macd <= p.signal and x.macd > x.signal
    cross_kdj = p.k <= p.d and x.k > x.d
    rules = {
        "底部趋势拐点": x.rsi < 38 and x.close > x.open and x.macd > p.macd and x.volume > x.vol_ma5,
        "涨停回马枪策略": recent_limit and x.close > x.open and x.close > x.ma5 and x.volume < x.vol_ma20,
        "涨停横盘策略": recent_limit and d.close.iloc[-5:].std() / x.close < .04 and x.close > d.high.iloc[-6:-1].max(),
        "启明星策略": p2.close < p2.open and abs(p.close - p.open) < (p2.open - p2.close) * .5 and x.close > (p2.open + p2.close) / 2,
        "多金叉共振": cross_ma and cross_macd and cross_kdj,
        "多方炮策略": p2.close > p2.open and p.close < p.open and x.close > x.open and x.close > p2.close,
        "阻力位突破策略": x.close > d.high.iloc[-21:-1].max() and x.volume > x.vol_ma20 * 1.5,
        "强势洗盘弱转强": x.close > x.open and x.close > p.high and p.close < p.open and x.ma5 > x.ma20,
        "趋势加速拐点": x.ma5 > p.ma5 > p2.ma5 and x.pctChg > 3 and x.volume > x.vol_ma20 * 1.3,
        "仙人指路策略": (x.high - max(x.open, x.close)) > abs(x.close - x.open) * 1.8 and x.close > x.ma20,
        "W底策略": abs(d.low.iloc[-10:-5].min() - d.low.iloc[-5:-1].min()) / x.close < .04 and x.close > d.high.iloc[-10:-1].max(),
        "趋势起点策略": cross_ma and x.ma20 > p.ma20,
        "2560战法": x.close > x.ma25 and x.volume > x.vol_ma5 > x.vol_ma20,
        "金三角策略": cross_ma and cross_macd and cross_kdj,
        "金叉不绿策略": cross_ma and x.close >= x.open,
        "海龟突破策略": x.close > d.high.iloc[-21:-1].max() and x.amount > 100_000_000 and x.close > x.open and x.close > p.close,
        "高旗形整理策略": (
            d.high.iloc[-40:].max() / d.low.iloc[-40:].min() > 1.6
            and d.high.iloc[-10:].max() / d.low.iloc[-10:].min() < 1.15
            and d.low.iloc[-10:].min() >= d.high.iloc[-40:].max() * .8
            and x.volume < d.volume.iloc[-21:-1].mean() * .6
        ),
        "涨停洗盘策略": p.close >= p2.close * 1.095 and x.close < x.open and x.volume > p.volume * 2 and x.low >= p.close,
        "上升趋势跌停反包": p.ma20 > p.ma60 and x.close <= p.close * .905 and x.volume > x.vol_ma20 * 2,
    }
    return bool(rules.get(name, False)), STRATEGIES[name]


def screen(codes, frames, strategies, min_change, min_turn, max_turn):
    rows = []
    stats = {"ready": 0, "missing": 0, "short": 0}
    progress = st.progress(0, text="正在读取本地行情…")
    def run():
        rps_frames = {}
        for i, (code, name) in enumerate(codes, 1):
            frame = frames.get(code)
            if frame is None or frame.empty:
                stats["missing"] += 1
                progress.progress(i / len(codes), text=f"本地读取 {i}/{len(codes)}")
                continue
            if len(frame) < 70:
                stats["short"] += 1
                progress.progress(i / len(codes), text=f"本地读取 {i}/{len(codes)}")
                continue
            stats["ready"] += 1
            if "RPS强势突破" in strategies and len(frame) >= 121:
                rps_frames[code] = (name, frame)
            last = frame.iloc[-1]
            turn = last.turn
            turn_ok = pd.isna(turn) or min_turn <= turn <= max_turn
            features = indicators(frame)
            for strategy in strategies:
                matched, reason = strategy_signal(frame, strategy, features)
                if matched and last.pctChg >= min_change and turn_ok:
                    rows.append({"策略": strategy, "代码": code, "名称": name,
                                 "收盘": last.close, "涨跌幅%": last.pctChg,
                                 "换手率%": turn, "信号说明": reason})
            progress.progress(i / len(codes), text=f"本地计算 {i}/{len(codes)}")
        if rps_frames:
            returns = pd.Series({code: frame.close.iloc[-1] / frame.close.iloc[-121] - 1
                                 for code, (_, frame) in rps_frames.items()})
            ranks = returns.rank(pct=True) * 100
            for code, rank in ranks.items():
                name, frame = rps_frames[code]
                last = frame.iloc[-1]
                breakout = last.close >= frame.high.iloc[-120:].max() * .9
                turn = last.turn
                turn_ok = pd.isna(turn) or min_turn <= turn <= max_turn
                if rank >= 90 and breakout and last.pctChg >= min_change and turn_ok:
                    rows.append({"策略": "RPS强势突破", "代码": code, "名称": name,
                                 "收盘": last.close, "涨跌幅%": last.pctChg,
                                 "换手率%": turn,
                                 "信号说明": f"本次扫描样本 RPS {rank:.1f}，收盘处于 120 日高点 90% 内"})
        return pd.DataFrame(rows).sort_values("涨跌幅%", ascending=False) if rows else pd.DataFrame()
    result = run()
    progress.empty()
    return result, stats


def push_wechat(result, sendkey, strategy_names):
    """通过 Server酱把结果推送到绑定的微信。"""
    if not sendkey.strip():
        raise ValueError("请先填写 Server酱 SendKey")
    if result.empty:
        raise ValueError("当前没有可推送的选股结果")
    lines = [f"策略：{'、'.join(strategy_names)}", f"共 {len(result)} 条"]
    for _, row in result.head(50).iterrows():
        lines.append(f"{row['代码']} {row['名称']} 收盘 {row['收盘']:.2f} 涨跌 {row['涨跌幅%']:.2f}%")
    response = requests.post(
        f"https://sctapi.ftqq.com/{sendkey.strip()}.send",
        data={"title": f"Baostock选股 {date.today():%Y-%m-%d}", "desp": "\n".join(lines)},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise RuntimeError(payload.get("message", "Server酱推送失败"))


st.set_page_config(page_title="Baostock 选股", layout="wide")
st.title("Baostock A股可视化选股")
st.caption("优先使用 Baostock；连接失败时依次使用同花顺或腾讯股票列表，并以腾讯 HTTPS 获取日线。可多选策略、查看价格走势并导出结果。仅供研究参考。")

today = date.today()
with st.sidebar:
    st.header("筛选条件")
    end = st.date_input("结束日期", today)
    start = st.date_input("开始日期", end - timedelta(days=180))
    strategies = st.multiselect("选股策略（可多选）", list(STRATEGIES), default=[next(iter(STRATEGIES))])
    min_change = st.number_input("最低涨跌幅 (%)", -10.0, 20.0, 0.0, 0.1)
    min_turn = st.number_input("最低换手率 (%)", 0.0, 100.0, 0.0, 0.1)
    max_turn = st.number_input("最高换手率 (%)", 0.0, 100.0, 100.0, 0.1)
    max_stocks = st.number_input("最多扫描股票数", 10, 6000, 6000, 100)
    st.divider()
    st.subheader("微信推送")
    sendkey = st.text_input("Server酱 SendKey", type="password", help="在 sct.ftqq.com 获取，绑定微信后填写")
    update = st.button("更新全市场行情缓存", use_container_width=True)
    run = st.button("开始本地选股", type="primary", use_container_width=True)

st.caption("策略逻辑：" + "；".join(f"{s}：{STRATEGIES[s]}" for s in strategies) + "。基于日线 OHLCV 规则识别。")

if start >= end:
    st.error("开始日期必须早于结束日期")
    st.stop()
if not strategies:
    st.error("请至少选择一种策略")
    st.stop()

if update:
    try:
        with st.spinner("读取股票列表…"):
            stocks, list_date, list_source = stock_list(end.strftime("%Y-%m-%d"))
    except Exception as exc:
        st.error(f"Baostock 获取股票列表失败：{exc}")
        st.stop()
    if stocks.empty:
        st.error("所选日期及之前 10 天内都没有可用的 Baostock 股票列表，请稍后重试。")
        st.stop()
    if list_date != end.strftime("%Y-%m-%d"):
        st.info(f"{end:%Y-%m-%d} 的股票列表尚未发布，已使用最近可用列表日期：{list_date}。行情仍按所选结束日期查询。")
    if list_source != "Baostock":
        st.info(f"Baostock 登录不可用，股票列表来源：{list_source}。")
    cache_universe_save(stocks)
    all_codes = list(stocks[["code", "code_name"]].itertuples(index=False, name=None))
    update_progress = st.progress(0, text="检查本地缓存…")
    update_stats = update_cache(all_codes, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), update_progress)
    update_progress.empty()
    st.session_state["update_stats"] = update_stats
    for key in ("result", "scan_count", "scan_stats", "history_source"):
        st.session_state.pop(key, None)
    if update_stats["failed"]:
        st.warning(f"缓存更新完成，但 {update_stats['failed']} 只股票更新失败；示例：{update_stats['failures'][:3]}")
    else:
        st.success(f"行情缓存已更新：新增/更新 {update_stats['updated']} 只，已跳过 {update_stats['skipped']} 只。")

if run:
    universe = cache_universe_load()
    if not universe:
        st.error("本地还没有股票列表，请先点击“更新全市场行情缓存”。")
        st.stop()
    limit = min(int(max_stocks), len(universe))
    stride = max(1, len(universe) // limit)
    selected = universe[::stride][:limit]
    start_text, end_text = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    frames = load_cache(selected, start_text, end_text)
    if not frames:
        st.error("本地没有所选日期范围的行情数据，请先点击“更新全市场行情缓存”。")
        st.stop()
    result, scan_stats = screen(selected, frames, strategies, min_change, min_turn, max_turn)
    st.session_state["scan_count"] = (scan_stats["ready"], len(selected), len(universe))
    st.session_state["result"] = result
    st.session_state["range"] = (start, end)
    st.session_state["strategies"] = strategies
    st.session_state["history_source"] = "本地 SQLite 缓存"
    st.session_state["scan_stats"] = scan_stats

result = st.session_state.get("result")
local_status = cache_status()
st.caption(f"本地行情缓存：{local_status['stocks']} 只股票 / {local_status['rows']:,} 条日线；最新日期 {local_status['newest'] or '暂无'}。")
if "scan_count" in st.session_state:
    scanned, attempted, total = st.session_state["scan_count"]
    st.caption(f"本次有效数据 {scanned} / 尝试 {attempted} / 全市场 {total} 只。")
if "update_stats" in st.session_state:
    u = st.session_state["update_stats"]
    q = st.session_state.get("scan_stats", {})
    st.caption(f"上次缓存更新：新增/更新 {u.get('updated', 0)}，已跳过 {u.get('skipped', 0)}，失败 {u.get('failed', 0)}；有效数据 {q.get('ready', 0)}，无缓存 {q.get('missing', 0)}，不足 70 根 {q.get('short', 0)}。")
if result is None:
    st.info("首次使用先更新全市场行情缓存；之后选股只读本地数据。")
else:
    st.subheader(f"筛选结果（{len(result)} 只）")
    st.caption(f"行情源：{st.session_state.get('history_source', 'Baostock')}")
    st.dataframe(result, use_container_width=True, hide_index=True)
    if st.button("推送到微信", use_container_width=True):
        try:
            push_wechat(result, sendkey, st.session_state.get("strategies", strategies))
            st.success("微信推送已发送")
        except Exception as exc:
            st.error(f"微信推送失败：{exc}")
    if not result.empty:
        choice = st.selectbox("查看个股走势", result["代码"].tolist())
        start_date, end_date = st.session_state["range"]
        frame = _cache_read(choice, start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))
        frame["MA20"] = frame.close.rolling(20).mean()
        fig, ax = plt.subplots(figsize=(12, 4))
        ax.plot(frame.date, frame.close, label="收盘价")
        ax.plot(frame.date, frame.MA20, label="MA20")
        ax.set_title(f"{choice} · MA20")
        ax.grid(alpha=.25)
        ax.legend()
        st.pyplot(fig, clear_figure=True)
        st.download_button("下载筛选结果 CSV", result.to_csv(index=False).encode("utf-8-sig"), "baostock_screen.csv", "text/csv")
