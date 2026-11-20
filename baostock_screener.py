"""Baostock A股可视化选股工具。

运行：streamlit run baostock_screener.py
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from html import escape
from pathlib import Path
import sqlite3
import time
import requests

import baostock as bs
import pandas as pd
import streamlit as st
import streamlit_antd_components as sac
from lightweight_charts_v5 import lightweight_charts_v5_component


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

STRATEGY_GROUPS = {
    "趋势突破": {
        "color": "#38bdf8",
        "bg": "rgba(56, 189, 248, 0.12)",
        "tag_color": "cyan",
        "desc": "顺势捕捉主升浪、平台突破与相对强度领涨标的",
        "items": ["海龟突破策略", "阻力位突破策略", "高旗形整理策略", "趋势加速拐点", "趋势起点策略", "RPS强势突破"],
    },
    "涨停战法": {
        "color": "#fb7185",
        "bg": "rgba(251, 113, 133, 0.13)",
        "tag_color": "red",
        "desc": "围绕涨停基因、强势换手洗盘与二次启动机会",
        "items": ["涨停回马枪策略", "涨停横盘策略", "涨停洗盘策略", "强势洗盘弱转强", "上升趋势跌停反包"],
    },
    "均线共振": {
        "color": "#f59e0b",
        "bg": "rgba(245, 158, 11, 0.13)",
        "tag_color": "gold",
        "desc": "均线系统与量能、MACD、KDJ 多指标协同确认",
        "items": ["多金叉共振", "2560战法", "金三角策略", "金叉不绿策略"],
    },
    "形态反转": {
        "color": "#a78bfa",
        "bg": "rgba(167, 139, 250, 0.13)",
        "tag_color": "purple",
        "desc": "经典 K 线组合与底部结构反转形态识别",
        "items": ["底部趋势拐点", "启明星策略", "多方炮策略", "仙人指路策略", "W底策略"],
    },
}

STRATEGY_TO_GROUP = {
    name: group_name
    for group_name, meta in STRATEGY_GROUPS.items()
    for name in meta["items"]
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
        stocks, rows, earliest, newest = conn.execute(
            "SELECT COUNT(DISTINCT code), COUNT(*), MIN(date), MAX(date) FROM daily"
        ).fetchone()
        named = conn.execute("SELECT COUNT(*) FROM stock_names").fetchone()[0]
    size_mb = CACHE_DB.stat().st_size / (1024 * 1024) if CACHE_DB.exists() else 0.0
    return {
        "stocks": stocks or 0,
        "rows": rows or 0,
        "earliest": earliest,
        "newest": newest,
        "named": named or 0,
        "size_mb": size_mb,
    }


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


def load_cache(codes, start, end, on_progress=None):
    _init_cache()
    if on_progress:
        on_progress(0.06, stage=1, detail=f"正在从 SQLite 读取 {len(codes):,} 只标的的区间行情（{start} ~ {end}）…")
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
    if on_progress:
        on_progress(0.16, stage=1, detail=f"已读取 {len(frame):,} 条日线记录，正在按标的构建内存索引…")
    frame["date"] = pd.to_datetime(frame["date"])
    grouped = {code: group.reset_index(drop=True) for code, group in frame.groupby("code", sort=False)}
    if on_progress:
        on_progress(0.22, stage=2, detail=f"已载入 {len(grouped):,} 只标的行情，开始计算技术指标与策略匹配…")
    return grouped


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


def screen(codes, frames, strategies, min_change, min_turn, max_turn, on_progress=None):
    rows = []
    sparks = {}
    stats = {"ready": 0, "missing": 0, "short": 0}
    fallback_progress = None if on_progress else st.progress(0, text="正在读取本地行情…")

    def _emit(ratio, stage, detail, hits=0, scanned=0, total=0):
        if on_progress:
            on_progress(ratio, stage=stage, detail=detail, hits=hits, scanned=scanned, total=total)
        elif fallback_progress is not None:
            fallback_progress.progress(min(1.0, max(0.0, ratio)), text=detail)

    def _record_spark(code, frame):
        if code not in sparks:
            sparks[code] = [round(float(v), 2) for v in frame.close.tail(20).dropna().tolist()]

    def run():
        rps_frames = {}
        total_codes = max(1, len(codes))
        last_tick = 0.0
        for i, (code, name) in enumerate(codes, 1):
            frame = frames.get(code)
            if frame is None or frame.empty:
                stats["missing"] += 1
            elif len(frame) < 70:
                stats["short"] += 1
            else:
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
                        _record_spark(code, frame)
                        rows.append({"策略": strategy, "代码": code, "名称": name,
                                     "收盘": float(last.close), "涨跌幅%": float(last.pctChg),
                                     "换手率%": float(turn) if pd.notna(turn) else float("nan"),
                                     "信号说明": reason})

            now = time.monotonic()
            if i == total_codes or i % 60 == 0 or (now - last_tick) >= 0.14:
                last_tick = now
                pct = 0.22 + 0.72 * (i / total_codes)
                _emit(
                    pct,
                    stage=2,
                    detail=f"正在逐股计算指标与匹配策略（当前：{code} {name}）",
                    hits=len(rows),
                    scanned=i,
                    total=total_codes,
                )

        if rps_frames:
            _emit(0.96, stage=3, detail=f"正在对 {len(rps_frames):,} 只标的执行 120 日 RPS 相对强度排名…",
                  hits=len(rows), scanned=total_codes, total=total_codes)
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
                    _record_spark(code, frame)
                    rows.append({"策略": "RPS强势突破", "代码": code, "名称": name,
                                 "收盘": float(last.close), "涨跌幅%": float(last.pctChg),
                                 "换手率%": float(turn) if pd.notna(turn) else float("nan"),
                                 "信号说明": f"本次扫描样本 RPS {rank:.1f}，收盘处于 120 日高点 90% 内"})

        _emit(0.99, stage=3, detail="正在排序信号榜并生成迷你走势数据…",
              hits=len(rows), scanned=total_codes, total=total_codes)
        return pd.DataFrame(rows).sort_values("涨跌幅%", ascending=False).reset_index(drop=True) if rows else pd.DataFrame()

    result = run()
    if fallback_progress is not None:
        fallback_progress.empty()
    stats["sparks"] = sparks
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


def _resample_kline(frame, interval):
    d = frame.copy().sort_values("date").dropna(subset=["open", "high", "low", "close"])
    d["volume"] = pd.to_numeric(d.volume, errors="coerce").fillna(0)
    if interval == "周线":
        period = d.date.dt.to_period("W-FRI")
    elif interval == "月线":
        period = d.date.dt.to_period("M")
    else:
        period = None
    if period is not None:
        d = d.groupby(period, sort=True).agg(
            date=("date", "last"), open=("open", "first"), high=("high", "max"),
            low=("low", "min"), close=("close", "last"), volume=("volume", "sum"),
        ).reset_index(drop=True)
    for window in (5, 10, 20, 30):
        d[f"ma{window}"] = d.close.rolling(window).mean()
    ema12 = d.close.ewm(span=12, adjust=False).mean()
    ema26 = d.close.ewm(span=26, adjust=False).mean()
    d["dif"] = ema12 - ema26
    d["dea"] = d["dif"].ewm(span=9, adjust=False).mean()
    d["macd_hist"] = (d["dif"] - d["dea"]) * 2
    d["time"] = pd.to_datetime(d.date).dt.strftime("%Y-%m-%d")
    return d


def chart_panes(frame, title, interval, show_ma=True, show_macd=False):
    d = _resample_kline(frame, interval)
    up, down = "#ef5350", "#26a69a"
    theme = {
        "layout": {
            "background": {"color": "#0c121c"},
            "textColor": "#8a9bb0",
            "fontFamily": "'Inter', 'Microsoft YaHei', sans-serif",
            "fontSize": 12,
        },
        "grid": {
            "vertLines": {"color": "#152030"},
            "horzLines": {"color": "#152030"},
        },
        "rightPriceScale": {"borderColor": "#1f2e42"},
        "timeScale": {
            "borderColor": "#1f2e42",
            "timeVisible": False,
            "rightOffset": 6,
            "barSpacing": 9,
        },
        "crosshair": {
            "mode": "normal",
            "vertLine": {"color": "#64748b", "style": 2, "width": 1},
            "horzLine": {"color": "#64748b", "style": 2, "width": 1},
        },
    }
    candles = d[["time", "open", "high", "low", "close"]].to_dict("records")
    price_series = [{
        "type": "Candlestick",
        "data": candles,
        "options": {
            "upColor": up,
            "downColor": down,
            "borderVisible": False,
            "wickUpColor": up,
            "wickDownColor": down,
            "priceLineVisible": True,
        },
    }]
    if show_ma:
        ma_colors = {5: "#f5c542", 10: "#38bdf8", 20: "#c084fc", 30: "#fb923c"}
        for window, color in ma_colors.items():
            values = d[["time", f"ma{window}"]].dropna()
            price_series.append({
                "type": "Line",
                "data": values.rename(columns={f"ma{window}": "value"}).to_dict("records"),
                "options": {
                    "color": color,
                    "lineWidth": 1,
                    "title": "",
                    "priceLineVisible": False,
                    "lastValueVisible": False,
                    "crosshairMarkerVisible": False,
                },
            })
    volumes = [{
        "time": row.time,
        "value": float(row.volume or 0),
        "color": f"{up}99" if row.close >= row.open else f"{down}99",
    } for row in d.itertuples()]

    panes = [
        {"chart": theme, "height": 420, "title": f"{title} · {interval}", "series": price_series},
        {
            "chart": theme,
            "height": 135,
            "title": "成交量 VOL",
            "series": [{
                "type": "Histogram",
                "data": volumes,
                "options": {"priceFormat": {"type": "volume"}, "priceLineVisible": False, "lastValueVisible": False},
            }],
        },
    ]

    if show_macd:
        macd_bars = [{
            "time": row.time,
            "value": round(float(row.macd_hist), 4),
            "color": f"{up}bb" if row.macd_hist >= 0 else f"{down}bb",
        } for row in d.dropna(subset=["macd_hist"]).itertuples()]
        dif_data = d[["time", "dif"]].dropna().rename(columns={"dif": "value"}).to_dict("records")
        dea_data = d[["time", "dea"]].dropna().rename(columns={"dea": "value"}).to_dict("records")
        panes.append({
            "chart": theme,
            "height": 135,
            "title": "MACD (12, 26, 9)",
            "series": [
                {"type": "Histogram", "data": macd_bars, "options": {"title": "", "priceLineVisible": False, "lastValueVisible": False}},
                {"type": "Line", "data": dif_data, "options": {"color": "#f5c542", "lineWidth": 1, "title": "", "priceLineVisible": False, "lastValueVisible": False, "crosshairMarkerVisible": False}},
                {"type": "Line", "data": dea_data, "options": {"color": "#38bdf8", "lineWidth": 1, "title": "", "priceLineVisible": False, "lastValueVisible": False, "crosshairMarkerVisible": False}},
            ],
        })
    return panes, d


def _fmt_amount(amount):
    if pd.isna(amount) or amount <= 0:
        return "—"
    if amount >= 1e8:
        return f"{amount / 1e8:.2f} 亿"
    if amount >= 1e4:
        return f"{amount / 1e4:.0f} 万"
    return f"{amount:,.0f}"


def _fmt_volume(volume):
    if pd.isna(volume) or volume <= 0:
        return "—"
    if volume >= 1e6:
        return f"{volume / 1e6:.2f} M"
    if volume >= 1e4:
        return f"{volume / 1e4:.1f} 万"
    return f"{volume:,.0f}"


def _inject_custom_css():
    st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap');

:root {
    --bg-base: #090e16;
    --bg-panel: #101826;
    --bg-panel-hover: #152033;
    --border-subtle: rgba(148, 163, 184, 0.14);
    --border-strong: rgba(148, 163, 184, 0.26);
    --text-primary: #eef4fb;
    --text-secondary: #93a4ba;
    --text-muted: #64748b;
    --up-red: #f05252;
    --up-red-bg: rgba(240, 82, 82, 0.14);
    --down-green: #26a69a;
    --down-green-bg: rgba(38, 166, 154, 0.14);
    --accent-blue: #38bdf8;
    --accent-gold: #f5c542;
}

html, body, [class*="css"] {
    font-family: 'Inter', 'Microsoft YaHei', 'PingFang SC', -apple-system, sans-serif;
}

.stApp {
    background:
        radial-gradient(920px 360px at 18% -8%, rgba(56, 189, 248, 0.09), transparent 65%),
        radial-gradient(760px 340px at 88% -10%, rgba(229, 72, 77, 0.10), transparent 62%),
        linear-gradient(180deg, #0b111c 0%, #090e16 100%);
    color: var(--text-primary);
}

[data-testid="stAppViewContainer"] > .main {
    background: transparent;
}

[data-testid="stMainBlockContainer"] {
    max-width: 1560px;
    padding-top: 1.35rem;
    padding-bottom: 3rem;
}

[data-testid="stHeader"] {
    background: rgba(9, 14, 22, 0.72);
    backdrop-filter: blur(12px);
    border-bottom: 1px solid rgba(148, 163, 184, 0.08);
}

/* Sidebar Styling */
[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #0d1421 0%, #0a101a 100%);
    border-right: 1px solid var(--border-subtle);
}
[data-testid="stSidebar"] > div:first-child {
    padding-top: 1.1rem;
}
.sb-brand {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    margin-bottom: 10px;
    background: linear-gradient(135deg, rgba(229, 72, 77, 0.12), rgba(56, 189, 248, 0.08));
    border: 1px solid rgba(148, 163, 184, 0.16);
    border-radius: 12px;
}
.sb-brand-title {
    font-size: 0.96rem;
    font-weight: 700;
    color: #f1f5f9;
    line-height: 1.2;
}
.sb-brand-sub {
    font-size: 0.72rem;
    color: #8ea1b8;
    font-family: 'JetBrains Mono', monospace;
}

/* Hero Banner */
.hero-bar {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 14px;
    padding: 18px 22px;
    margin-bottom: 14px;
    border-radius: 16px;
    background: linear-gradient(135deg, rgba(17, 25, 40, 0.92) 0%, rgba(12, 18, 30, 0.88) 100%);
    border: 1px solid var(--border-subtle);
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.28);
}
.hero-left {
    display: flex;
    align-items: center;
    gap: 14px;
}
.hero-logo {
    width: 44px;
    height: 44px;
    border-radius: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: linear-gradient(135deg, #e5484d 0%, #b91c1c 100%);
    box-shadow: 0 6px 18px rgba(229, 72, 77, 0.34);
    flex-shrink: 0;
}
.hero-title {
    margin: 0;
    font-size: 1.48rem;
    font-weight: 700;
    letter-spacing: -0.02em;
    color: #f8fafc;
    line-height: 1.2;
}
.hero-subtitle {
    margin: 3px 0 0 0;
    font-size: 0.82rem;
    color: #8ea1b8;
}
.hero-badges {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
}
.status-pill {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 6px 12px;
    border-radius: 999px;
    font-size: 0.78rem;
    font-weight: 500;
    background: rgba(15, 23, 38, 0.9);
    border: 1px solid rgba(148, 163, 184, 0.2);
    color: #cbd5e1;
}
.status-pill.live {
    border-color: rgba(38, 166, 154, 0.38);
    background: rgba(38, 166, 154, 0.10);
    color: #5eead4;
}
.pulse-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: #2dd4bf;
    box-shadow: 0 0 0 4px rgba(45, 212, 191, 0.18);
}
.mono {
    font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, monospace;
}

/* KPI Cards */
.kpi-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 12px;
    margin: 10px 0 18px 0;
}
@media (max-width: 980px) {
    .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
.kpi-card {
    position: relative;
    overflow: hidden;
    padding: 15px 18px;
    border-radius: 14px;
    background: linear-gradient(155deg, rgba(19, 29, 46, 0.92), rgba(12, 18, 28, 0.94));
    border: 1px solid var(--border-subtle);
    transition: border-color 0.2s ease, transform 0.2s ease;
}
.kpi-card:hover {
    border-color: rgba(148, 163, 184, 0.3);
    transform: translateY(-1px);
}
.kpi-card::before {
    content: "";
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 2px;
    background: var(--kpi-accent, linear-gradient(90deg, #38bdf8, transparent));
}
.kpi-label {
    font-size: 0.78rem;
    color: #8ea1b8;
    font-weight: 500;
    margin-bottom: 6px;
    display: flex;
    align-items: center;
    justify-content: space-between;
}
.kpi-value {
    font-family: 'JetBrains Mono', monospace;
    font-size: 1.48rem;
    font-weight: 700;
    color: #f8fafc;
    letter-spacing: -0.02em;
    line-height: 1.25;
}
.kpi-sub {
    margin-top: 5px;
    font-size: 0.76rem;
    color: #7c90a8;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

/* Section Headers */
.sec-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    margin: 6px 0 10px 0;
}
.sec-title {
    font-size: 1.06rem;
    font-weight: 700;
    color: #f1f5f9;
    display: inline-flex;
    align-items: center;
    gap: 8px;
}
.sec-dot {
    width: 4px;
    height: 16px;
    border-radius: 3px;
    background: linear-gradient(180deg, #f05252, #f59e0b);
    display: inline-block;
}
.sec-meta {
    font-size: 0.78rem;
    color: #7c90a8;
}

/* Ticker Header Card */
.ticker-card {
    padding: 18px 20px;
    border-radius: 15px;
    background: linear-gradient(145deg, rgba(17, 26, 40, 0.95), rgba(11, 17, 28, 0.96));
    border: 1px solid var(--border-subtle);
    margin-bottom: 12px;
}
.ticker-top {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 14px;
    padding-bottom: 14px;
    border-bottom: 1px solid rgba(148, 163, 184, 0.12);
}
.ticker-name-wrap {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 10px;
}
.ticker-name {
    font-size: 1.42rem;
    font-weight: 700;
    color: #f8fafc;
}
.ticker-code {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.84rem;
    padding: 3px 9px;
    border-radius: 6px;
    background: rgba(148, 163, 184, 0.12);
    color: #cbd5e1;
    border: 1px solid rgba(148, 163, 184, 0.18);
}
.ticker-price-wrap {
    display: flex;
    align-items: baseline;
    gap: 12px;
}
.ticker-price {
    font-family: 'JetBrains Mono', monospace;
    font-size: 1.72rem;
    font-weight: 700;
    letter-spacing: -0.02em;
}
.ticker-chg {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.92rem;
    font-weight: 600;
    padding: 4px 10px;
    border-radius: 8px;
}
.is-up { color: #f87171; }
.is-down { color: #34d399; }
.is-flat { color: #94a3b8; }
.chg-up { color: #fca5a5; background: rgba(240, 82, 82, 0.16); border: 1px solid rgba(240, 82, 82, 0.35); }
.chg-down { color: #6ee7b7; background: rgba(38, 166, 154, 0.16); border: 1px solid rgba(38, 166, 154, 0.35); }
.chg-flat { color: #cbd5e1; background: rgba(148, 163, 184, 0.14); border: 1px solid rgba(148, 163, 184, 0.25); }

.ticker-stats {
    display: grid;
    grid-template-columns: repeat(8, minmax(0, 1fr));
    gap: 10px;
    padding-top: 13px;
}
@media (max-width: 1100px) {
    .ticker-stats { grid-template-columns: repeat(4, minmax(0, 1fr)); }
}
.t-stat {
    background: rgba(9, 14, 22, 0.52);
    border: 1px solid rgba(148, 163, 184, 0.09);
    border-radius: 10px;
    padding: 8px 10px;
}
.t-stat-k {
    font-size: 0.72rem;
    color: #7c90a8;
    margin-bottom: 3px;
}
.t-stat-v {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.9rem;
    font-weight: 600;
    color: #e2e8f0;
}

/* MA Legend Bar */
.ma-legend {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 14px;
    padding: 8px 14px;
    border-radius: 10px 10px 0 0;
    background: #0c121c;
    border: 1px solid rgba(148, 163, 184, 0.16);
    border-bottom: none;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.76rem;
    color: #94a3b8;
}
.ma-item { display: inline-flex; align-items: center; gap: 5px; }
.ma-dot { width: 8px; height: 3px; border-radius: 2px; display: inline-block; }

/* Strategy Encyclopedia Cards */
.strat-card {
    padding: 15px 16px;
    border-radius: 13px;
    background: linear-gradient(155deg, rgba(17, 25, 39, 0.92), rgba(11, 17, 27, 0.94));
    border: 1px solid var(--border-subtle);
    margin-bottom: 12px;
    min-height: 112px;
    transition: border-color 0.2s ease, background 0.2s ease;
}
.strat-card.active {
    border-color: rgba(229, 72, 77, 0.55);
    background: linear-gradient(155deg, rgba(36, 22, 32, 0.85), rgba(14, 19, 30, 0.95));
}
.strat-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
}
.strat-name {
    font-size: 0.96rem;
    font-weight: 700;
    color: #f1f5f9;
}
.strat-badge {
    font-size: 0.71rem;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 999px;
}
.strat-desc {
    font-size: 0.82rem;
    color: #93a4ba;
    line-height: 1.5;
}
.strat-active-mark {
    margin-top: 8px;
    font-size: 0.73rem;
    color: #fca5a5;
    font-weight: 600;
}

/* Source Pipeline Card */
.pipe-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 12px;
    margin: 10px 0 16px 0;
}
@media (max-width: 900px) {
    .pipe-grid { grid-template-columns: 1fr; }
}
.pipe-card {
    padding: 16px 18px;
    border-radius: 13px;
    background: linear-gradient(150deg, rgba(17, 25, 39, 0.94), rgba(11, 17, 27, 0.96));
    border: 1px solid var(--border-subtle);
}
.pipe-step {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.72rem;
    color: #38bdf8;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 6px;
}
.pipe-title {
    font-size: 0.98rem;
    font-weight: 700;
    color: #f1f5f9;
    margin-bottom: 5px;
}
.pipe-desc {
    font-size: 0.81rem;
    color: #8ea1b8;
    line-height: 1.5;
}

/* Native Streamlit Controls Polish */
[data-testid="stDataFrame"] {
    border: 1px solid var(--border-subtle);
    border-radius: 12px;
    overflow: hidden;
    background: #0d1420;
}
.stButton > button, .stDownloadButton > button {
    border-radius: 10px;
    border: 1px solid rgba(148, 163, 184, 0.24);
    background: rgba(17, 25, 39, 0.9);
    color: #e2e8f0;
    font-weight: 600;
    min-height: 2.55rem;
    transition: all 0.18s ease;
}
.stButton > button:hover, .stDownloadButton > button:hover {
    border-color: rgba(56, 189, 248, 0.5);
    background: rgba(24, 35, 54, 0.96);
    color: #ffffff;
}
.stButton > button[kind="primary"] {
    background: linear-gradient(135deg, #e5484d 0%, #c8333a 100%);
    border: 1px solid rgba(255, 130, 135, 0.35);
    color: #ffffff;
    box-shadow: 0 6px 18px rgba(229, 72, 77, 0.28);
}
.stButton > button[kind="primary"]:hover {
    background: linear-gradient(135deg, #f0565b 0%, #d83b42 100%);
    border-color: rgba(255, 160, 165, 0.55);
    box-shadow: 0 8px 22px rgba(229, 72, 77, 0.38);
}
[data-testid="stExpander"] {
    background: rgba(16, 24, 38, 0.85);
    border: 1px solid var(--border-subtle);
    border-radius: 12px;
}
.stMultiSelect [data-baseweb="tag"] {
    background: rgba(229, 72, 77, 0.18) !important;
    border: 1px solid rgba(229, 72, 77, 0.42) !important;
    border-radius: 6px !important;
}
.stProgress > div > div > div > div {
    background: linear-gradient(90deg, #38bdf8, #e5484d);
}

/* Floating Real-Time Scan Progress Overlay */
.scan-float-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(6, 10, 17, 0.52);
    backdrop-filter: blur(5px);
    z-index: 99998;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
}
.scan-float-card {
    width: min(620px, 92vw);
    padding: 24px 26px;
    border-radius: 18px;
    background: linear-gradient(155deg, rgba(19, 29, 46, 0.98) 0%, rgba(11, 17, 29, 0.98) 100%);
    border: 1px solid rgba(56, 189, 248, 0.34);
    box-shadow: 0 24px 64px rgba(0, 0, 0, 0.62), 0 0 0 1px rgba(255, 255, 255, 0.04) inset;
    color: #eef4fb;
}
.scan-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    margin-bottom: 14px;
}
.scan-title-wrap {
    display: flex;
    align-items: center;
    gap: 10px;
}
.scan-spinner {
    width: 14px;
    height: 14px;
    border-radius: 50%;
    border: 2.5px solid rgba(56, 189, 248, 0.28);
    border-top-color: #38bdf8;
    animation: scan-spin 0.8s linear infinite;
    flex-shrink: 0;
}
@keyframes scan-spin {
    to { transform: rotate(360deg); }
}
.scan-title {
    font-size: 1.06rem;
    font-weight: 700;
    color: #f8fafc;
}
.scan-pct {
    font-family: 'JetBrains Mono', monospace;
    font-size: 1.52rem;
    font-weight: 700;
    color: #38bdf8;
    letter-spacing: -0.02em;
}
.scan-bar-track {
    width: 100%;
    height: 10px;
    border-radius: 999px;
    background: rgba(148, 163, 184, 0.14);
    overflow: hidden;
    margin-bottom: 14px;
}
.scan-bar-fill {
    height: 100%;
    border-radius: 999px;
    background: linear-gradient(90deg, #38bdf8 0%, #818cf8 52%, #f05252 100%);
    box-shadow: 0 0 14px rgba(56, 189, 248, 0.45);
    transition: width 0.18s ease-out;
}
.scan-steps {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 8px;
    margin-bottom: 14px;
}
.scan-step {
    font-size: 0.76rem;
    padding: 6px 10px;
    border-radius: 8px;
    text-align: center;
    background: rgba(15, 23, 38, 0.72);
    border: 1px solid rgba(148, 163, 184, 0.12);
    color: #64748b;
    font-weight: 500;
}
.scan-step.active {
    background: rgba(56, 189, 248, 0.14);
    border-color: rgba(56, 189, 248, 0.44);
    color: #e0f2fe;
    font-weight: 600;
}
.scan-step.done {
    background: rgba(38, 166, 154, 0.14);
    border-color: rgba(38, 166, 154, 0.36);
    color: #5eead4;
}
.scan-foot {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    font-size: 0.81rem;
    color: #93a4ba;
}
.scan-metrics {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-family: 'JetBrains Mono', monospace;
}
.scan-pill {
    padding: 3px 9px;
    border-radius: 999px;
    font-size: 0.75rem;
    background: rgba(148, 163, 184, 0.12);
    color: #cbd5e1;
}
.scan-pill.hit {
    background: rgba(240, 82, 82, 0.18);
    border: 1px solid rgba(240, 82, 82, 0.4);
    color: #fca5a5;
    font-weight: 600;
}

/* Ready / Empty State Hero Card */
.empty-hero {
    margin: 12px 0 18px 0;
    padding: 34px 28px 26px 28px;
    border-radius: 16px;
    text-align: center;
    background: linear-gradient(155deg, rgba(18, 28, 44, 0.92), rgba(11, 17, 28, 0.94));
    border: 1px solid rgba(148, 163, 184, 0.18);
    box-shadow: 0 10px 28px rgba(0, 0, 0, 0.24);
}
.empty-icon {
    width: 56px;
    height: 56px;
    margin: 0 auto 14px auto;
    border-radius: 16px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: linear-gradient(135deg, rgba(56, 189, 248, 0.16), rgba(229, 72, 77, 0.18));
    border: 1px solid rgba(148, 163, 184, 0.22);
}
.empty-title {
    font-size: 1.24rem;
    font-weight: 700;
    color: #f8fafc;
    margin-bottom: 6px;
}
.empty-desc {
    font-size: 0.88rem;
    color: #9bb0c9;
    max-width: 620px;
    margin: 0 auto 14px auto;
    line-height: 1.6;
}
.empty-pills {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 8px;
}
</style>
""", unsafe_allow_html=True)


def _render_hero(local_status, selected_strategies):
    newest = local_status["newest"] or "待同步"
    stocks_cnt = local_status["stocks"]
    is_ready = stocks_cnt > 0
    pill_cls = "status-pill live" if is_ready else "status-pill"
    status_text = "离线引擎就绪" if is_ready else "尚未建立缓存"
    st.markdown(f"""
<div class="hero-bar">
    <div class="hero-left">
        <div class="hero-logo">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
                <path d="M5 17V11M5 11H3.5M5 11H6.5M5 17H3.5M5 17H6.5" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>
                <rect x="3.5" y="12.2" width="3" height="3.6" rx="0.8" fill="#fff"/>
                <path d="M12 18V7" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>
                <rect x="10.2" y="9" width="3.6" height="6.2" rx="0.9" fill="#fde047"/>
                <path d="M19 14V5" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>
                <rect x="17.2" y="6.6" width="3.6" height="5.2" rx="0.9" fill="#fff"/>
            </svg>
        </div>
        <div>
            <h1 class="hero-title">KLineRadar · A股技术形态选股终端</h1>
            <p class="hero-subtitle">Baostock 本地行情引擎 · 20 套技术形态策略 · TradingView 专业多周期图表</p>
        </div>
    </div>
    <div class="hero-badges">
        <span class="{pill_cls}"><span class="pulse-dot"></span>{status_text}</span>
        <span class="status-pill mono">{stocks_cnt:,} 标的</span>
        <span class="status-pill mono">最新 {escape(str(newest))}</span>
        <span class="status-pill">已选 {len(selected_strategies)} 策略</span>
    </div>
</div>
""", unsafe_allow_html=True)


def _render_kpi_cards(local_status, result, scan_count):
    if result is not None:
        hit_cnt = f"{len(result):,} 条"
        unique_cnt = result["代码"].nunique() if not result.empty else 0
        if not result.empty:
            avg_chg = result["涨跌幅%"].mean()
            limit_up = int((result["涨跌幅%"] >= 9.5).sum())
            hit_sub = f"去重 {unique_cnt} 只 · 均涨幅 {avg_chg:+.2f}% · 涨停 {limit_up} 只"
        else:
            hit_sub = "当前条件下暂无命中信号，可放宽过滤条件"
        hit_accent = "linear-gradient(90deg, #f05252, #f59e0b)"
    else:
        hit_cnt = "待扫描"
        hit_sub = "点击「开始本地选股」即可秒级扫描全市场"
        hit_accent = "linear-gradient(90deg, #64748b, transparent)"

    if scan_count:
        scanned, attempted, total = scan_count
        pool_sub = f"本次有效扫描 {scanned:,} / {attempted:,}（池 {total:,}）"
    else:
        pool_sub = f"股票名称库已登记 {local_status['named']:,} 只标的"

    earliest = local_status["earliest"] or "—"
    newest = local_status["newest"] or "—"
    st.markdown(f"""
<div class="kpi-grid">
    <div class="kpi-card" style="--kpi-accent: linear-gradient(90deg, #38bdf8, transparent);">
        <div class="kpi-label"><span>本地缓存标的</span><span class="mono">SQLite</span></div>
        <div class="kpi-value">{local_status['stocks']:,} <span style="font-size:0.88rem;color:#8ea1b8;font-weight:500;">只</span></div>
        <div class="kpi-sub">{escape(pool_sub)}</div>
    </div>
    <div class="kpi-card" style="--kpi-accent: linear-gradient(90deg, #818cf8, transparent);">
        <div class="kpi-label"><span>日线行情记录</span><span class="mono">{local_status['size_mb']:.1f} MB</span></div>
        <div class="kpi-value">{local_status['rows']:,} <span style="font-size:0.88rem;color:#8ea1b8;font-weight:500;">条</span></div>
        <div class="kpi-sub">覆盖区间：{escape(str(earliest))} ~ {escape(str(newest))}</div>
    </div>
    <div class="kpi-card" style="--kpi-accent: linear-gradient(90deg, #26a69a, transparent);">
        <div class="kpi-label"><span>最新交易日期</span><span class="mono">EOD</span></div>
        <div class="kpi-value">{escape(str(newest))}</div>
        <div class="kpi-sub">离线选股全程 0 网络请求，毫秒级响应</div>
    </div>
    <div class="kpi-card" style="--kpi-accent: {hit_accent};">
        <div class="kpi-label"><span>策略命中信号</span><span class="mono">SIGNALS</span></div>
        <div class="kpi-value">{hit_cnt}</div>
        <div class="kpi-sub">{escape(hit_sub)}</div>
    </div>
</div>
""", unsafe_allow_html=True)


def _apply_strategy_preset(names):
    st.session_state["pending_strategies"] = list(names)


def _apply_date_window(days):
    st.session_state["pending_window_days"] = days


def _trigger_scan():
    st.session_state["trigger_scan"] = True


class FloatingProgress:
    """全屏居中悬浮实时进度卡片，确保无论页面滚动到哪都能第一时间看到选股/同步进度。"""

    def __init__(self, title, steps):
        self.title = title
        self.steps = steps
        self.started = time.monotonic()
        self.slot = st.empty()

    def update(self, ratio, stage=1, detail="", hits=None, scanned=None, total=None):
        pct = int(round(min(1.0, max(0.0, ratio)) * 100))
        elapsed = time.monotonic() - self.started
        step_html = []
        for idx, label in enumerate(self.steps, 1):
            if idx < stage:
                cls, prefix = "scan-step done", "✓ "
            elif idx == stage:
                cls, prefix = "scan-step active", "● "
            else:
                cls, prefix = "scan-step", f"{idx}. "
            step_html.append(f'<div class="{cls}">{prefix}{escape(label)}</div>')

        pills = [f'<span class="scan-pill">耗时 {elapsed:.1f}s</span>']
        if scanned is not None and total:
            pills.append(f'<span class="scan-pill">{scanned:,} / {total:,} 只</span>')
        if hits is not None:
            pills.append(f'<span class="scan-pill hit">已命中 {hits:,} 条</span>')

        self.slot.markdown(f"""
        <div class="scan-float-backdrop">
            <div class="scan-float-card">
                <div class="scan-head">
                    <div class="scan-title-wrap">
                        <span class="scan-spinner"></span>
                        <span class="scan-title">{escape(self.title)}</span>
                    </div>
                    <div class="scan-pct">{pct}%</div>
                </div>
                <div class="scan-bar-track">
                    <div class="scan-bar-fill" style="width: {max(3, pct)}%;"></div>
                </div>
                <div class="scan-steps">{"".join(step_html)}</div>
                <div class="scan-foot">
                    <span>{escape(detail)}</span>
                    <span class="scan-metrics">{"".join(pills)}</span>
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)

    # 兼容 st.progress(ratio, text=...) 接口，方便 update_cache 直接复用
    def progress(self, ratio, text=""):
        stage = 2 if ratio < 0.98 else 3
        self.update(0.15 + 0.85 * ratio, stage=stage, detail=text)

    def close(self):
        self.slot.empty()


def _run_update_action(start, end):
    tracker = FloatingProgress("正在更新全市场行情缓存", ("获取股票列表", "同步历史日线", "写入本地库"))
    try:
        tracker.update(0.05, stage=1, detail="正在连接数据源获取最新 A 股股票列表…")
        try:
            stocks, list_date, list_source = stock_list(end.strftime("%Y-%m-%d"))
        except Exception as exc:
            tracker.close()
            st.error(f"获取股票列表失败：{exc}")
            return
        if stocks.empty:
            tracker.close()
            st.error("所选日期及之前 10 天内都没有可用的股票列表，请稍后重试。")
            return
        cache_universe_save(stocks)
        all_codes = list(stocks[["code", "code_name"]].itertuples(index=False, name=None))
        tracker.update(0.15, stage=2, detail=f"已获取 {len(all_codes):,} 只股票（来源：{list_source}），正在检查本地缓存区间…")
        update_stats = update_cache(all_codes, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), tracker)
        tracker.update(1.0, stage=3, detail="全市场行情同步完成！")
        time.sleep(0.2)
    finally:
        tracker.close()

    if list_date != end.strftime("%Y-%m-%d"):
        st.info(f"{end:%Y-%m-%d} 的股票列表尚未发布，已使用最近可用列表日期：{list_date}。行情仍按所选结束日期查询。")
    if list_source != "Baostock":
        st.info(f"Baostock 登录不可用，股票列表来源已自动切换：{list_source}。")
    st.session_state["update_stats"] = update_stats
    for key in ("result", "scan_count", "scan_stats", "history_source", "sparks"):
        st.session_state.pop(key, None)
    if update_stats["failed"]:
        st.warning(f"缓存更新完成，但 {update_stats['failed']} 只股票更新失败；示例：{update_stats['failures'][:3]}")
    else:
        st.toast(f"行情缓存更新完成：新增/更新 {update_stats['updated']} 只，跳过 {update_stats['skipped']} 只", icon="✅")
        st.success(f"行情缓存已更新：新增/更新 {update_stats['updated']} 只，已跳过 {update_stats['skipped']} 只。")


def _run_screen_action(start, end, strategies, min_change, min_turn, max_turn, max_stocks):
    tracker = FloatingProgress(
        f"正在执行本地策略选股（{len(strategies)} 套策略）",
        ("读取 SQLite 缓存", "技术指标与策略匹配", "生成信号与走势"),
    )
    started = time.monotonic()
    try:
        tracker.update(0.02, stage=1, detail="正在加载本地股票池…")
        universe = cache_universe_load()
        if not universe:
            tracker.close()
            st.error("本地还没有股票列表，请先点击「更新全市场行情缓存」。")
            return
        limit = min(int(max_stocks), len(universe))
        stride = max(1, len(universe) // limit)
        selected = universe[::stride][:limit]
        start_text, end_text = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
        frames = load_cache(selected, start_text, end_text, on_progress=tracker.update)
        if not frames:
            tracker.close()
            st.error("本地没有所选日期范围的行情数据，请先点击「更新全市场行情缓存」。")
            return
        result, scan_stats = screen(
            selected, frames, strategies, min_change, min_turn, max_turn, on_progress=tracker.update
        )
        sparks = scan_stats.pop("sparks", {})
        tracker.update(
            1.0,
            stage=3,
            detail=f"扫描完成！共命中 {len(result):,} 条信号，即将呈现结果…",
            hits=len(result),
            scanned=len(selected),
            total=len(selected),
        )
        time.sleep(0.18)
    finally:
        tracker.close()

    elapsed = time.monotonic() - started
    st.session_state["scan_count"] = (scan_stats["ready"], len(selected), len(universe))
    st.session_state["scan_elapsed"] = elapsed
    st.session_state["result"] = result
    st.session_state["sparks"] = sparks
    st.session_state["range"] = (start, end)
    st.session_state["strategies"] = list(strategies)
    st.session_state["history_source"] = "本地 SQLite 缓存"
    st.session_state["scan_stats"] = scan_stats
    if not result.empty:
        st.session_state["active_stock"] = result.iloc[0]["代码"]
        st.toast(f"选股完成！共命中 {len(result):,} 条信号（耗时 {elapsed:.1f}s）", icon="🎯")
    else:
        st.toast(f"扫描完成（耗时 {elapsed:.1f}s），当前条件下暂无命中标的", icon="ℹ️")


def _style_pct(val):
    if pd.isna(val):
        return "color: #64748b;"
    if val >= 9.5:
        return "color: #fecaca; background-color: rgba(239, 68, 68, 0.28); font-weight: 700;"
    if val > 0:
        return "color: #f87171; font-weight: 600;"
    if val < 0:
        return "color: #34d399; font-weight: 600;"
    return "color: #94a3b8;"


# ========================== Page Setup ==========================
st.set_page_config(
    page_title="KLineRadar · A股技术形态选股终端",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)
_inject_custom_css()

today = date.today()
if "selected_strategies" not in st.session_state:
    st.session_state["selected_strategies"] = [next(iter(STRATEGIES))]
if "end_date" not in st.session_state:
    st.session_state["end_date"] = today
if "start_date" not in st.session_state:
    st.session_state["start_date"] = today - timedelta(days=180)

# Handle pending state updates safely before widget instantiation
if "pending_strategies" in st.session_state:
    st.session_state["selected_strategies"] = st.session_state.pop("pending_strategies")
if "pending_window_days" in st.session_state:
    days = st.session_state.pop("pending_window_days")
    st.session_state["start_date"] = st.session_state["end_date"] - timedelta(days=days)

# ========================== Sidebar ==========================
with st.sidebar:
    st.markdown("""
    <div class="sb-brand">
        <div style="font-size:1.35rem;line-height:1;">⚡</div>
        <div>
            <div class="sb-brand-title">选股控制台</div>
            <div class="sb-brand-sub">SCREENER PARAMETERS</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    sac.divider(label="行情区间", icon="calendar-range", align="start", size="xs", color="gray")
    c_start, c_end = st.columns(2)
    with c_start:
        start = st.date_input("开始日期", key="start_date")
    with c_end:
        end = st.date_input("结束日期", key="end_date")

    w1, w2, w3 = st.columns(3)
    w1.button("近 90 天", use_container_width=True, on_click=_apply_date_window, args=(90,))
    w2.button("近 180 天", use_container_width=True, on_click=_apply_date_window, args=(180,))
    w3.button("近 1 年", use_container_width=True, on_click=_apply_date_window, args=(365,))

    sac.divider(label="策略组合", icon="sliders", align="start", size="xs", color="gray")
    p1, p2 = st.columns(2)
    p1.button("趋势突破", use_container_width=True, on_click=_apply_strategy_preset, args=(STRATEGY_GROUPS["趋势突破"]["items"],))
    p2.button("涨停战法", use_container_width=True, on_click=_apply_strategy_preset, args=(STRATEGY_GROUPS["涨停战法"]["items"],))
    p3, p4 = st.columns(2)
    p3.button("均线共振", use_container_width=True, on_click=_apply_strategy_preset, args=(STRATEGY_GROUPS["均线共振"]["items"],))
    p4.button("形态反转", use_container_width=True, on_click=_apply_strategy_preset, args=(STRATEGY_GROUPS["形态反转"]["items"],))

    strategies = st.multiselect(
        "已选策略（支持多选）",
        options=list(STRATEGIES.keys()),
        key="selected_strategies",
        placeholder="请选择至少一个选股策略…",
    )

    sac.divider(label="行情过滤", icon="funnel", align="start", size="xs", color="gray")
    min_change = st.number_input("最低涨跌幅 (%)", -10.0, 20.0, 0.0, 0.5, format="%.1f")
    min_turn, max_turn = st.slider("换手率范围 (%)", 0.0, 100.0, (0.0, 100.0), step=0.5)
    max_stocks = st.number_input("最多扫描股票数 (只)", 10, 6000, 6000, 200)

    sac.divider(label="执行操作", icon="lightning-charge", align="start", size="xs", color="gray")
    run_btn = st.button("开始本地选股", type="primary", use_container_width=True, icon=":material/radar:")
    update_btn = st.button("更新全市场行情缓存", use_container_width=True, icon=":material/cloud_sync:")
    st.caption("提示：日常选股全程离线读取 SQLite，仅在需要补齐最新交易日行情时点击更新。")


# ========================== Main Header & Navigation ==========================
local_status = cache_status()
_render_hero(local_status, strategies)

valid_params = True
if start >= end:
    sac.alert(label="日期区间无效", description="开始日期必须早于结束日期，请在左侧栏调整日期范围。", color="error", icon=True, variant="light")
    valid_params = False
elif not strategies:
    sac.alert(label="尚未选择策略", description="请在左侧栏至少选择 1 个策略，或在「策略百科」中一键应用流派组合。", color="warning", icon=True, variant="light")
    valid_params = False

if update_btn and valid_params:
    _run_update_action(start, end)
    local_status = cache_status()

should_scan = run_btn or st.session_state.pop("trigger_scan", False)
if should_scan and valid_params:
    _run_screen_action(start, end, strategies, min_change, min_turn, max_turn, max_stocks)

result = st.session_state.get("result")
hit_badge = sac.Tag(f"{len(result)}", color="red", bordered=False) if result is not None and not result.empty else None

active_tab = sac.tabs(
    items=[
        sac.TabsItem(label="策略选股工作台", icon="graph-up-arrow", tag=hit_badge),
        sac.TabsItem(label="策略百科矩阵", icon="grid-1x2", tag=sac.Tag("20", color="blue", bordered=False)),
        sac.TabsItem(label="数据源与缓存中心", icon="database-check"),
    ],
    align="start",
    variant="outline",
    size="md",
    color="red",
    key="main_nav_tabs",
)


# ========================== Tab 1: 策略选股工作台 ==========================
if active_tab == "策略选股工作台":
    _render_kpi_cards(local_status, result, st.session_state.get("scan_count"))

    if strategies:
        tag_items = [
            sac.Tag(s, color=STRATEGY_GROUPS[STRATEGY_TO_GROUP[s]]["tag_color"], bordered=False)
            for s in strategies
        ]
        with st.expander(f"当前启用策略逻辑（已选 {len(strategies)} 项）", expanded=False):
            sac.tags(tag_items, align="start", size="sm")
            for s in strategies:
                st.markdown(f"- **{s}**：{STRATEGIES[s]}")

    if result is None:
        pills_html = "".join(
            f'<span class="strat-badge" style="color:{STRATEGY_GROUPS[STRATEGY_TO_GROUP[s]]["color"]};'
            f'background:{STRATEGY_GROUPS[STRATEGY_TO_GROUP[s]]["bg"]};'
            f'border:1px solid {STRATEGY_GROUPS[STRATEGY_TO_GROUP[s]]["color"]}44;">{escape(s)}</span>'
            for s in strategies
        )
        st.markdown(f"""
        <div class="empty-hero">
            <div class="empty-icon">
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none">
                    <circle cx="12" cy="12" r="9" stroke="#38bdf8" stroke-width="1.6" stroke-dasharray="3 3"/>
                    <circle cx="12" cy="12" r="5" stroke="#f05252" stroke-width="1.6"/>
                    <circle cx="12" cy="12" r="1.8" fill="#f8fafc"/>
                    <path d="M12 12L17.5 6.5" stroke="#38bdf8" stroke-width="1.8" stroke-linecap="round"/>
                </svg>
            </div>
            <div class="empty-title">KLineRadar 本地选股引擎已就绪</div>
            <div class="empty-desc">
                已从本地 SQLite 就绪 <strong style="color:#f8fafc;">{local_status['stocks']:,}</strong> 只 A 股日线行情（最新至 <span class="mono" style="color:#38bdf8;">{escape(str(local_status['newest'] or '暂无'))}</span>）。
                点击下方或左侧「开始本地选股」，将实时弹出扫描进度面板并呈现信号榜与专业 K 线图表。
            </div>
            <div class="empty-pills">{pills_html}</div>
        </div>
        """, unsafe_allow_html=True)
        c_l, c_mid, c_r = st.columns([2, 1.4, 2])
        with c_mid:
            st.button(
                "立即开始本地选股",
                type="primary",
                use_container_width=True,
                disabled=not valid_params,
                icon=":material/play_arrow:",
                on_click=_trigger_scan,
            )
    elif result.empty:
        st.markdown("""
        <div class="empty-hero">
            <div class="empty-title">本次扫描未发现符合条件的标的</div>
            <div class="empty-desc">
                建议在左侧栏适当放宽「最低涨跌幅」或「换手率范围」，或者多勾选几个策略流派后重新扫描。
            </div>
        </div>
        """, unsafe_allow_html=True)
    else:
        # ---- Section A: Interactive Signal Table ----
        st.markdown(f"""
        <div class="sec-head">
            <div class="sec-title"><span class="sec-dot"></span>策略信号榜（共 {len(result)} 条 · 去重 {result['代码'].nunique()} 只）</div>
            <div class="sec-meta">行情源：{escape(st.session_state.get('history_source', '本地 SQLite 缓存'))} · 点击表格任意行可联动下方 K 线图</div>
        </div>
        """, unsafe_allow_html=True)

        hit_strategies = list(result["策略"].unique())
        bar_c1, bar_c2, bar_c3 = st.columns([2.4, 1.2, 0.9], vertical_alignment="bottom")
        with bar_c1:
            if len(hit_strategies) > 1:
                filter_strategy = sac.chip(
                    items=["全部策略"] + hit_strategies,
                    index=0,
                    size="sm",
                    radius="md",
                    variant="light",
                    color="red",
                    key="result_strategy_chip",
                )
            else:
                filter_strategy = "全部策略"
                sac.tags([sac.Tag(hit_strategies[0], color="red", bordered=False)], size="sm")
        with bar_c2:
            keyword = st.text_input("快速搜索", placeholder="输入代码或名称过滤…", label_visibility="collapsed")
        with bar_c3:
            st.download_button(
                "导出 CSV",
                data=result.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"baostock_screen_{date.today():%Y%m%d}.csv",
                mime="text/csv",
                use_container_width=True,
                icon=":material/download:",
            )

        filtered = result.copy()
        if filter_strategy and filter_strategy != "全部策略":
            filtered = filtered[filtered["策略"] == filter_strategy]
        if keyword.strip():
            kw = keyword.strip().lower()
            filtered = filtered[
                filtered["代码"].str.lower().str.contains(kw, na=False)
                | filtered["名称"].str.lower().str.contains(kw, na=False)
            ]
        filtered = filtered.reset_index(drop=True)

        if filtered.empty:
            sac.alert(label="过滤后暂无匹配标的", description="请清空搜索关键词或切换策略标签。", color="info", icon=True)
        else:
            sparks = st.session_state.get("sparks", {})
            display_df = filtered.copy()
            display_df.insert(5, "近20日走势", display_df["代码"].map(lambda c: sparks.get(c, [])))

            styled_df = display_df.style.map(_style_pct, subset=["涨跌幅%"])
            table_event = st.dataframe(
                styled_df,
                use_container_width=True,
                hide_index=True,
                height=340,
                on_select="rerun",
                selection_mode="single-row",
                key="signal_table",
                column_config={
                    "策略": st.column_config.TextColumn("命中策略", width="medium"),
                    "代码": st.column_config.TextColumn("代码", width="small"),
                    "名称": st.column_config.TextColumn("股票名称", width="small"),
                    "收盘": st.column_config.NumberColumn("收盘价", format="¥%.2f", width="small"),
                    "涨跌幅%": st.column_config.NumberColumn("涨跌幅", format="%+.2f%%", width="small"),
                    "近20日走势": st.column_config.LineChartColumn("近20日走势", width="medium"),
                    "换手率%": st.column_config.ProgressColumn("换手率", format="%.2f%%", min_value=0, max_value=25, width="small"),
                    "信号说明": st.column_config.TextColumn("触发逻辑", width="large"),
                },
            )

            # ---- Section B: Stock Technical Analysis Terminal ----
            stocks_df = filtered[["代码", "名称"]].drop_duplicates("代码")
            name_by_code = dict(stocks_df.itertuples(index=False, name=None))
            stock_codes = list(name_by_code.keys())

            # Row click selection takes precedence when changed
            selected_rows = table_event.selection.rows if table_event and hasattr(table_event, "selection") else []
            if selected_rows:
                clicked_code = filtered.iloc[selected_rows[0]]["代码"]
                if clicked_code != st.session_state.get("last_clicked_code"):
                    st.session_state["last_clicked_code"] = clicked_code
                    st.session_state["active_stock"] = clicked_code

            if st.session_state.get("active_stock") not in stock_codes:
                st.session_state["active_stock"] = stock_codes[0]

            current_idx = stock_codes.index(st.session_state["active_stock"])

            st.markdown("""
            <div class="sec-head" style="margin-top: 18px;">
                <div class="sec-title"><span class="sec-dot"></span>个股深度行情终端</div>
                <div class="sec-meta">支持拖拽缩放 · 十字光标精确查价 · 多周期与 MACD 联动</div>
            </div>
            """, unsafe_allow_html=True)

            nav1, nav2, nav3, nav4 = st.columns([2.6, 0.55, 0.55, 2.2], vertical_alignment="bottom")
            with nav1:
                choice = st.selectbox(
                    "选择分析标的",
                    options=stock_codes,
                    index=current_idx,
                    format_func=lambda code: f"{name_by_code[code]}  ({code})",
                    label_visibility="collapsed",
                )
                if choice != st.session_state["active_stock"]:
                    st.session_state["active_stock"] = choice
                    current_idx = stock_codes.index(choice)
            with nav2:
                if st.button("上一只", use_container_width=True, disabled=current_idx <= 0):
                    st.session_state["active_stock"] = stock_codes[current_idx - 1]
                    st.rerun()
            with nav3:
                if st.button("下一只", use_container_width=True, disabled=current_idx >= len(stock_codes) - 1):
                    st.session_state["active_stock"] = stock_codes[current_idx + 1]
                    st.rerun()
            with nav4:
                interval = sac.segmented(
                    items=[
                        sac.SegmentedItem(label="日线", icon="bar-chart-line"),
                        sac.SegmentedItem(label="周线", icon="calendar-week"),
                        sac.SegmentedItem(label="月线", icon="calendar-month"),
                    ],
                    index=0,
                    size="sm",
                    radius="md",
                    color="red",
                    align="end",
                    use_container_width=True,
                    key="kline_interval_seg",
                )

            choice = st.session_state["active_stock"]
            start_date, end_date = st.session_state["range"]
            frame = _cache_read(choice, start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))

            if not frame.empty:
                latest = frame.iloc[-1]
                prior = frame.iloc[-2].close if len(frame) > 1 else latest.close
                change = (latest.close / prior - 1) * 100 if prior else 0.0
                amp = ((latest.high - latest.low) / prior * 100) if prior else 0.0
                feat = indicators(frame)
                rsi_val = f"{feat.iloc[-1].rsi:.1f}" if not feat.empty else "—"
                macd_val = feat.iloc[-1].macd - feat.iloc[-1].signal if not feat.empty else float("nan")
                macd_text = ("多头金叉区" if macd_val > 0 else "空头整理区") if pd.notna(macd_val) else "—"
                turn_text = f"{latest.turn:.2f}%" if pd.notna(latest.turn) else "—"

                color_cls = "is-up" if change > 0 else ("is-down" if change < 0 else "is-flat")
                chg_cls = "chg-up" if change > 0 else ("chg-down" if change < 0 else "chg-flat")

                hit_list = result[result["代码"] == choice]["策略"].unique().tolist()
                hit_pills = "".join(
                    f'<span class="strat-badge" style="color:{STRATEGY_GROUPS[STRATEGY_TO_GROUP[s]]["color"]};'
                    f'background:{STRATEGY_GROUPS[STRATEGY_TO_GROUP[s]]["bg"]};'
                    f'border:1px solid {STRATEGY_GROUPS[STRATEGY_TO_GROUP[s]]["color"]}44;">{escape(s)}</span>'
                    for s in hit_list
                )

                st.markdown(f"""
                <div class="ticker-card">
                    <div class="ticker-top">
                        <div class="ticker-name-wrap">
                            <span class="ticker-name">{escape(name_by_code[choice])}</span>
                            <span class="ticker-code">{escape(choice)}</span>
                            <span style="display:inline-flex;gap:6px;flex-wrap:wrap;">{hit_pills}</span>
                        </div>
                        <div class="ticker-price-wrap">
                            <span class="ticker-price {color_cls}">¥{latest.close:.2f}</span>
                            <span class="ticker-chg {chg_cls}">{change:+.2f}%</span>
                            <span class="mono" style="font-size:0.78rem;color:#64748b;">{pd.Timestamp(latest.date):%Y-%m-%d}</span>
                        </div>
                    </div>
                    <div class="ticker-stats">
                        <div class="t-stat"><div class="t-stat-k">今开</div><div class="t-stat-v">{latest.open:.2f}</div></div>
                        <div class="t-stat"><div class="t-stat-k">最高</div><div class="t-stat-v is-up">{latest.high:.2f}</div></div>
                        <div class="t-stat"><div class="t-stat-k">最低</div><div class="t-stat-v is-down">{latest.low:.2f}</div></div>
                        <div class="t-stat"><div class="t-stat-k">振幅</div><div class="t-stat-v">{amp:.2f}%</div></div>
                        <div class="t-stat"><div class="t-stat-k">换手率</div><div class="t-stat-v">{turn_text}</div></div>
                        <div class="t-stat"><div class="t-stat-k">成交额</div><div class="t-stat-v">{_fmt_amount(latest.amount)}</div></div>
                        <div class="t-stat"><div class="t-stat-k">RSI (14)</div><div class="t-stat-v">{rsi_val}</div></div>
                        <div class="t-stat"><div class="t-stat-k">MACD 趋势</div><div class="t-stat-v">{macd_text}</div></div>
                    </div>
                </div>
                """, unsafe_allow_html=True)

                opt1, opt2, opt3 = st.columns([1.2, 1.2, 3.6])
                with opt1:
                    show_ma = st.toggle("叠加 MA5/10/20/30", value=True)
                with opt2:
                    show_macd = st.toggle("显示 MACD 副图", value=True)
                with opt3:
                    st.caption(f"当前展示第 {current_idx + 1} / {len(stock_codes)} 只 · 区间 {start_date:%Y-%m-%d} ~ {end_date:%Y-%m-%d}")

                panes, kdf = chart_panes(frame, f"{name_by_code[choice]} · {choice}", interval, show_ma=show_ma, show_macd=show_macd)
                last_k = kdf.iloc[-1]
                legend_parts = []
                if show_ma:
                    for w, col in ((5, "#f5c542"), (10, "#38bdf8"), (20, "#c084fc"), (30, "#fb923c")):
                        val = last_k.get(f"ma{w}")
                        val_str = f"{val:.2f}" if pd.notna(val) else "—"
                        legend_parts.append(
                            f'<span class="ma-item"><span class="ma-dot" style="background:{col};"></span>'
                            f'<span style="color:{col};">MA{w}</span> {val_str}</span>'
                        )
                if show_macd:
                    dif_v = last_k.get("dif")
                    dea_v = last_k.get("dea")
                    hist_v = last_k.get("macd_hist")
                    dif_s = f"{dif_v:.3f}" if pd.notna(dif_v) else "—"
                    dea_s = f"{dea_v:.3f}" if pd.notna(dea_v) else "—"
                    hist_s = f"{hist_v:+.3f}" if pd.notna(hist_v) else "—"
                    sep = '<span style="color:#334155;">|</span>' if legend_parts else ""
                    legend_parts.append(
                        f'{sep}<span class="ma-item"><span class="ma-dot" style="background:#f5c542;"></span>'
                        f'<span style="color:#f5c542;">DIF</span> {dif_s}</span>'
                        f'<span class="ma-item"><span class="ma-dot" style="background:#38bdf8;"></span>'
                        f'<span style="color:#38bdf8;">DEA</span> {dea_s}</span>'
                        f'<span class="ma-item"><span style="color:#94a3b8;">MACD</span> {hist_s}</span>'
                    )
                if legend_parts:
                    st.markdown(f'<div class="ma-legend">{"".join(legend_parts)}</div>', unsafe_allow_html=True)

                chart_height = 705 if show_macd else 565
                lightweight_charts_v5_component(
                    name=f"{name_by_code[choice]} {choice}",
                    charts=panes,
                    height=chart_height,
                    zoom_level=85,
                    key=f"chart_{choice}_{interval}_{show_ma}_{show_macd}",
                )


# ========================== Tab 2: 策略百科矩阵 ==========================
elif active_tab == "策略百科矩阵":
    st.markdown("""
    <div class="sec-head">
        <div class="sec-title"><span class="sec-dot"></span>20 套 A 股技术形态策略矩阵</div>
        <div class="sec-meta">所有策略均基于本地日线 OHLCV 与量价/均线/MACD/KDJ/RSI 因子实时计算</div>
    </div>
    """, unsafe_allow_html=True)

    group_choice = sac.chip(
        items=["全部流派 (20)"] + [f"{g} ({len(meta['items'])})" for g, meta in STRATEGY_GROUPS.items()],
        index=0,
        size="sm",
        radius="md",
        variant="light",
        color="red",
        key="wiki_group_chip",
    )

    selected_group_name = None if not group_choice or group_choice.startswith("全部") else group_choice.split(" ")[0]
    groups_to_show = (
        [(selected_group_name, STRATEGY_GROUPS[selected_group_name])]
        if selected_group_name
        else list(STRATEGY_GROUPS.items())
    )

    for g_name, meta in groups_to_show:
        gh1, gh2 = st.columns([4, 1.1], vertical_alignment="center")
        with gh1:
            st.markdown(
                f'<div style="margin:10px 0 6px 0;"><span style="font-size:1.02rem;font-weight:700;color:{meta["color"]};">● {escape(g_name)}</span>'
                f'<span style="margin-left:10px;font-size:0.82rem;color:#8ea1b8;">{escape(meta["desc"])}</span></div>',
                unsafe_allow_html=True,
            )
        with gh2:
            st.button(
                f"应用「{g_name}」",
                key=f"apply_grp_{g_name}",
                use_container_width=True,
                on_click=_apply_strategy_preset,
                args=(meta["items"],),
            )

        cols = st.columns(3)
        for idx, s_name in enumerate(meta["items"]):
            is_active = s_name in strategies
            card_cls = "strat-card active" if is_active else "strat-card"
            active_html = '<div class="strat-active-mark">✓ 已加入左侧选股组合</div>' if is_active else ""
            with cols[idx % 3]:
                st.markdown(f"""
                <div class="{card_cls}">
                    <div class="strat-top">
                        <span class="strat-name">{escape(s_name)}</span>
                        <span class="strat-badge" style="color:{meta['color']};background:{meta['bg']};border:1px solid {meta['color']}44;">{escape(g_name)}</span>
                    </div>
                    <div class="strat-desc">{escape(STRATEGIES[s_name])}</div>
                    {active_html}
                </div>
                """, unsafe_allow_html=True)


# ========================== Tab 3: 数据源与缓存中心 ==========================
elif active_tab == "数据源与缓存中心":
    _render_kpi_cards(local_status, result, st.session_state.get("scan_count"))

    st.markdown("""
    <div class="sec-head">
        <div class="sec-title"><span class="sec-dot"></span>三级行情数据源与容灾链路</div>
        <div class="sec-meta">自动故障切换，缓存落盘至本地 stock_cache.sqlite3，支持跨机拷贝复用</div>
    </div>
    <div class="pipe-grid">
        <div class="pipe-card">
            <div class="pipe-step">STAGE 01 · PRIMARY</div>
            <div class="pipe-title">Baostock 证券宝主通道</div>
            <div class="pipe-desc">单连接拉取前复权日线（含开高低收、成交量、成交额、换手率、涨跌幅与 ST 标识），字段最完整。</div>
        </div>
        <div class="pipe-card">
            <div class="pipe-step">STAGE 02 · LIST FALLBACK</div>
            <div class="pipe-title">同花顺 / 腾讯实时证券列表</div>
            <div class="pipe-desc">当 Baostock 10030 端口受限时，自动切换同花顺公开列表或腾讯实时报价接口核验全市场 A 股代码。</div>
        </div>
        <div class="pipe-card">
            <div class="pipe-step">STAGE 03 · 6x CONCURRENT</div>
            <div class="pipe-title">腾讯财经 HTTPS 6 路并发补齐</div>
            <div class="pipe-desc">对主通道失败或缺失标的自动启动 6 线程 HTTPS 并发回补（换手率缺省项在选股时自动温和跳过）。</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    c_left, c_right = st.columns([1.4, 1])
    with c_left:
        st.markdown('<div class="sec-head"><div class="sec-title"><span class="sec-dot"></span>本地数据库概况</div></div>', unsafe_allow_html=True)
        info_df = pd.DataFrame([
            {"项目": "SQLite 路径", "数值": str(CACHE_DB)},
            {"项目": "文件体积", "数值": f"{local_status['size_mb']:.2f} MB"},
            {"项目": "已缓存标的数", "数值": f"{local_status['stocks']:,} 只"},
            {"项目": "证券名称字典", "数值": f"{local_status['named']:,} 条"},
            {"项目": "历史日线总行数", "数值": f"{local_status['rows']:,} 条"},
            {"项目": "数据起止日期", "数值": f"{local_status['earliest'] or '暂无'} 至 {local_status['newest'] or '暂无'}"},
        ])
        st.dataframe(info_df, use_container_width=True, hide_index=True)

    with c_right:
        st.markdown('<div class="sec-head"><div class="sec-title"><span class="sec-dot"></span>最近一次同步与扫描日志</div></div>', unsafe_allow_html=True)
        u = st.session_state.get("update_stats")
        q = st.session_state.get("scan_stats")
        if u:
            sac.alert(
                label=f"上次缓存更新（通道：{u.get('source', '未知')}）",
                description=f"新增/更新 {u.get('updated', 0)} 只 · 跳过已缓存 {u.get('skipped', 0)} 只 · 失败 {u.get('failed', 0)} 只",
                color="warning" if u.get("failed") else "success",
                icon=True,
            )
            if u.get("failures"):
                fail_df = pd.DataFrame(u["failures"], columns=["代码", "失败原因"])
                st.dataframe(fail_df, use_container_width=True, hide_index=True, height=180)
        else:
            sac.alert(
                label="本次会话尚未执行增量更新",
                description="现有 SQLite 缓存可直接用于离线选股；如需补充最新交易日，请点击左侧「更新全市场行情缓存」。",
                color="info",
                icon=True,
            )

        if q:
            st.caption(f"最近扫描统计：有效数据 {q.get('ready', 0):,} 只 · 无缓存 {q.get('missing', 0):,} 只 · 不足 70 根 K 线 {q.get('short', 0):,} 只。")
