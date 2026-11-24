"""Baostock A股可视化选股工具。

运行：streamlit run baostock_screener.py
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from html import escape
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import requests
from requests.adapters import HTTPAdapter

import baostock as bs
import numpy as np
import pandas as pd
import streamlit as st
import streamlit_antd_components as sac
from lightweight_charts_v5 import lightweight_charts_v5_component


DAILY_FIELDS = "date,code,open,high,low,close,volume,amount,turn,pctChg,isST"
BAOSTOCK_AVAILABLE = None
CACHE_DB = Path(__file__).with_name("stock_cache.sqlite3")
SYNC_LOCK = threading.Lock()
_INIT_LOCK = threading.Lock()
_CACHE_INITIALIZED = False
_STATUS_LOCK = threading.Lock()
_STATUS_CACHE = {"ts": 0.0, "data": None}
_THREAD_LOCAL = threading.local()
AUTO_SYNC_TIMES = ["15:15", "15:30", "15:35", "16:00", "17:00", "18:00", "20:00"]


def _invalidate_status_cache():
    with _STATUS_LOCK:
        _STATUS_CACHE["ts"] = 0.0
        _STATUS_CACHE["data"] = None


def _http_session():
    session = getattr(_THREAD_LOCAL, "http_session", None)
    if session is None:
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=16, pool_maxsize=16)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({"User-Agent": "Mozilla/5.0"})
        _THREAD_LOCAL.http_session = session
    return session


def _init_cache():
    global _CACHE_INITIALIZED
    if _CACHE_INITIALIZED and CACHE_DB.exists():
        return
    with _INIT_LOCK:
        if _CACHE_INITIALIZED and CACHE_DB.exists():
            return
        with sqlite3.connect(CACHE_DB, timeout=30) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS daily (
                code TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL, low REAL,
                close REAL, volume REAL, amount REAL, turn REAL, pctChg REAL, isST TEXT,
                PRIMARY KEY (code, date))""")
            conn.execute("CREATE TABLE IF NOT EXISTS sync_meta (code TEXT PRIMARY KEY, start_date TEXT NOT NULL, through_date TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS stock_names (code TEXT PRIMARY KEY, name TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS watchlist (code TEXT PRIMARY KEY, name TEXT NOT NULL, added_at TEXT NOT NULL)")
            # 主键 (code, date) 已覆盖该组合索引，移除冗余索引并补建 date 索引以加速区间查询与 MAX(date)
            conn.execute("DROP INDEX IF EXISTS idx_daily_code_date")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_date ON daily(date)")
        _CACHE_INITIALIZED = True


def _setting_get(key, default=None):
    _init_cache()
    with sqlite3.connect(CACHE_DB, timeout=30) as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _setting_set(key, value):
    _init_cache()
    with sqlite3.connect(CACHE_DB, timeout=30) as conn:
        conn.execute("INSERT OR REPLACE INTO app_settings(key, value) VALUES (?, ?)", (key, str(value)))


def get_auto_sync_config():
    enabled = _setting_get("auto_sync_enabled", "1") == "1"
    sync_time = _setting_get("auto_sync_time", "15:35")
    if sync_time not in AUTO_SYNC_TIMES:
        sync_time = "15:35"
    last_date = _setting_get("auto_sync_last_date", "")
    raw_info = _setting_get("auto_sync_last_info", "")
    try:
        last_info = json.loads(raw_info) if raw_info else {}
    except Exception:
        last_info = {}
    return {
        "enabled": enabled,
        "sync_time": sync_time,
        "last_date": last_date,
        "last_info": last_info,
    }


def save_auto_sync_config(enabled, sync_time):
    _setting_set("auto_sync_enabled", "1" if enabled else "0")
    if sync_time in AUTO_SYNC_TIMES:
        _setting_set("auto_sync_time", sync_time)


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
    _invalidate_status_cache()


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
    "金三角策略": "均线多头价托初成 + 放量阳线确认",
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
    session = _http_session()
    for attempt in range(3):
        try:
            response = session.get(url, params=params, headers={"Referer": referer}, timeout=12)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            error = exc
            if attempt < 2:
                time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"HTTPS 行情备用源请求失败：{error}")


def _tonghuashun_stock_list():
    """同花顺公开 A 股分页页，作为 Baostock/腾讯不可用时的列表源。"""
    from html import unescape
    rows = {}
    session = _http_session()
    headers = {"Referer": "https://q.10jqka.com.cn/"}
    page_count = 280
    for page in range(1, page_count + 1):
        url = f"https://q.10jqka.com.cn/index/index/board/all/field/zdf/order/desc/page/{page}/"
        response = session.get(url, headers=headers, timeout=15)
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
    """用腾讯批量实时报价并发核验候选代码并极速构建全市场股票列表。"""
    ranges = [(600000, 600999), (601000, 601999), (603000, 603999),
              (605000, 605999), (688000, 689999), (0, 3999),
              (300000, 301999)]
    codes = []
    for first, last in ranges:
        codes.extend(f"{n:06d}" for n in range(first, last + 1))
    batches = [codes[offset:offset + 300] for offset in range(0, len(codes), 300)]

    def _fetch_batch(batch):
        symbols = [f"{'sh' if code[0] in '68' else 'sz'}{code}" for code in batch]
        response = _http_session().get("https://qt.gtimg.cn/q=" + ",".join(symbols), timeout=15)
        response.raise_for_status()
        response.encoding = "gbk"
        parsed = []
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
            name = fields[1].strip()
            if "退" not in name and not any(word in name for word in ("债", "基金", "ETF", "LOF", "指数")):
                parsed.append((code, name))
        return parsed

    rows = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for batch_rows in pool.map(_fetch_batch, batches):
            for code, name in batch_rows:
                rows[code] = name
    if len(rows) < 1000:
        raise RuntimeError(f"腾讯 HTTPS 仅核验到 {len(rows)} 只股票，列表不完整")
    return pd.DataFrame([{"code": code, "tradeStatus": "1", "code_name": name} for code, name in rows.items()])


def _fetch_stock_list(day):
    global BAOSTOCK_AVAILABLE
    frame, actual_day, source = pd.DataFrame(), day, ""
    errors = []

    try:
        frame = _tencent_stock_list()
        source = "腾讯批量接口"
    except Exception as qq_error:
        errors.append(f"腾讯列表失败：{qq_error}")

    if frame.empty and BAOSTOCK_AVAILABLE is not False:
        def run():
            requested = pd.Timestamp(day)
            for offset in range(11):
                q_day = (requested - pd.Timedelta(days=offset)).strftime("%Y-%m-%d")
                res = _rows(bs.query_all_stock(day=q_day))
                if not res.empty:
                    return res, q_day
            return pd.DataFrame(), ""
        try:
            frame, actual_day = _query(run)
            source = "Baostock"
        except Exception as bs_error:
            BAOSTOCK_AVAILABLE = False
            errors.append(f"Baostock 失败：{bs_error}")

    if frame.empty:
        try:
            frame = _tonghuashun_stock_list()
            source = "同花顺 HTTPS"
            actual_day = day
        except Exception as ths_error:
            errors.append(f"同花顺备用列表失败：{ths_error}")
            raise RuntimeError("；".join(errors)) from ths_error

    if frame.empty:
        return frame, actual_day, source
    frame = frame[frame.code.str.match(r"^(sh\.(600|601|603|605|688|689)|sz\.(000|001|002|003|300|301))\d{3}$")].copy()
    return frame, actual_day, source


@st.cache_data(ttl=3600, show_spinner=False)
def stock_list(day):
    return _fetch_stock_list(day)


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


def _history_http(code, start, end, name=""):
    market, symbol = code.split(".")
    days = min(1000, max(60, (pd.Timestamp(end) - pd.Timestamp(start)).days + 30))
    payload = _https_json(
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        {"param": f"{market}{symbol},day,,,{days},qfq"},
        "https://gu.qq.com/",
    )
    payload = (payload.get("data") or {}).get(f"{market}{symbol}", {})
    rows = [row[:6] for row in payload.get("qfqday", []) if len(row) >= 6]
    frame = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume"])
    if frame.empty:
        raise RuntimeError(f"腾讯 HTTPS 备用源未返回 {code} 的日线数据")
    frame["code"] = code
    frame["turn"] = float("nan")
    frame["pctChg"] = pd.to_numeric(frame["close"], errors="coerce").pct_change().mul(100)
    frame["isST"] = "1" if "ST" in (name or "").upper() else "0"
    frame = frame[(frame.date >= start) & (frame.date <= end)].copy()
    for col in ("open", "high", "low", "close", "volume"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    # 腾讯 fqkline：科创板(688/689)成交量单位已是股，其余板块单位为手(需×100转为股)
    if not symbol.startswith(("688", "689")):
        frame["volume"] = frame["volume"] * 100.0
    frame["amount"] = frame["close"] * frame["volume"]
    frame["date"] = pd.to_datetime(frame["date"])
    return frame.dropna(subset=["close"]).sort_values("date")


def _history_eastmoney(code, start, end, name=""):
    """东方财富前复权日线接口：返回含换手率、成交额的完整日线（所有板块成交量均以手为单位）。"""
    market, symbol = code.split(".")
    secid = f"{'1' if market == 'sh' else '0'}.{symbol}"
    payload = _https_json(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        {
            "secid": secid,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101",
            "fqt": "1",
            "beg": start.replace("-", ""),
            "end": end.replace("-", ""),
        },
        "https://quote.eastmoney.com/",
    )
    data = payload.get("data")
    if data is None:
        raise RuntimeError(f"东方财富未返回 {code} 数据")
    klines = data.get("klines") or []
    is_st = "1" if "ST" in (name or "").upper() else "0"
    parsed = []
    for line in klines:
        parts = line.split(",")
        if len(parts) < 11:
            continue
        d_str = parts[0]
        if d_str < start or d_str > end:
            continue
        try:
            open_p = float(parts[1])
            close_p = float(parts[2])
            high_p = float(parts[3])
            low_p = float(parts[4])
            vol_lots = float(parts[5])
            amt_yuan = float(parts[6])
            pct_chg = float(parts[8])
        except ValueError:
            continue
        try:
            turn = float(parts[10])
        except ValueError:
            turn = float("nan")
        parsed.append({
            "date": d_str,
            "code": code,
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close_p,
            "volume": vol_lots * 100.0,
            "amount": amt_yuan,
            "turn": turn,
            "pctChg": pct_chg,
            "isST": is_st,
        })
    cols = ["date", "code", "open", "high", "low", "close", "volume", "amount", "turn", "pctChg", "isST"]
    frame = pd.DataFrame(parsed, columns=cols)
    if not frame.empty:
        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame.dropna(subset=["close"]).sort_values("date")
    return frame


def cache_status(force=False):
    now = time.monotonic()
    with _STATUS_LOCK:
        if not force and _STATUS_CACHE["data"] is not None and (now - _STATUS_CACHE["ts"]) < 10.0:
            return dict(_STATUS_CACHE["data"])
    _init_cache()
    with sqlite3.connect(CACHE_DB, timeout=30) as conn:
        stocks, rows, earliest, newest = conn.execute(
            "SELECT COUNT(DISTINCT code), COUNT(*), MIN(date), MAX(date) FROM daily"
        ).fetchone()
        named = conn.execute("SELECT COUNT(*) FROM stock_names").fetchone()[0]
    size_mb = CACHE_DB.stat().st_size / (1024 * 1024) if CACHE_DB.exists() else 0.0
    data = {
        "stocks": stocks or 0,
        "rows": rows or 0,
        "earliest": earliest,
        "newest": newest,
        "named": named or 0,
        "size_mb": size_mb,
    }
    with _STATUS_LOCK:
        _STATUS_CACHE["ts"] = now
        _STATUS_CACHE["data"] = data
    return dict(data)


def watchlist_load():
    _init_cache()
    with sqlite3.connect(CACHE_DB, timeout=30) as conn:
        return conn.execute("SELECT code, name, added_at FROM watchlist ORDER BY added_at DESC, code").fetchall()


def watchlist_add(code, name):
    _init_cache()
    with sqlite3.connect(CACHE_DB, timeout=30) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO watchlist(code, name, added_at) VALUES (?, ?, ?)",
            (code, name, datetime.now().strftime("%Y-%m-%d %H:%M")),
        )


def watchlist_remove(code):
    _init_cache()
    with sqlite3.connect(CACHE_DB, timeout=30) as conn:
        conn.execute("DELETE FROM watchlist WHERE code=?", (code,))


def cache_universe_save(stocks):
    _init_cache()
    with sqlite3.connect(CACHE_DB, timeout=30) as conn:
        conn.execute("DELETE FROM stock_names")
        conn.executemany("INSERT OR REPLACE INTO stock_names(code,name) VALUES (?,?)",
                         stocks[["code", "code_name"]].itertuples(index=False, name=None))
    _setting_set("stock_list_updated_date", date.today().strftime("%Y-%m-%d"))
    _invalidate_status_cache()


def cache_universe_load():
    _init_cache()
    with sqlite3.connect(CACHE_DB) as conn:
        return conn.execute("SELECT code,name FROM stock_names ORDER BY code").fetchall()


def _get_universe_for_sync(day_str, force=False):
    cached = cache_universe_load()
    updated_date = _setting_get("stock_list_updated_date", "")
    if not force and len(cached) >= 4000 and updated_date:
        try:
            age_days = (date.today() - pd.Timestamp(updated_date).date()).days
            if 0 <= age_days <= 7:
                return cached, updated_date, "本地股票池缓存"
        except Exception:
            pass
    stocks, list_date, list_source = _fetch_stock_list(day_str)
    if stocks.empty:
        return [], list_date, list_source
    cache_universe_save(stocks)
    all_codes = list(stocks[["code", "code_name"]].itertuples(index=False, name=None))
    return all_codes, list_date, list_source


def _recent_trade_days(end_str, now=None):
    """查询截止 end_str 的最近已收盘交易日及前一交易日（未满15:00排除当日盘中未收盘Bar）。"""
    global BAOSTOCK_AVAILABLE
    days = []
    try:
        resp = _http_session().get(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": "sh000001,day,,,20,qfq"},
            headers={"Referer": "https://gu.qq.com/"},
            timeout=10,
        )
        resp.raise_for_status()
        node = (resp.json().get("data") or {}).get("sh000001") or {}
        rows = node.get("qfqday") or node.get("day") or []
        days = sorted({row[0] for row in rows if len(row) >= 1 and row[0] <= end_str})
    except Exception:
        days = []

    if not days and BAOSTOCK_AVAILABLE is not False:
        try:
            start_probe = (pd.Timestamp(end_str) - pd.Timedelta(days=45)).strftime("%Y-%m-%d")
            def run_idx():
                return _rows(bs.query_history_k_data_plus("sh.000001", "date", start_date=start_probe, end_date=end_str, frequency="d"))
            idx_frame = _query(run_idx)
            if not idx_frame.empty:
                days = sorted({str(d) for d in idx_frame["date"].tolist() if str(d) <= end_str})
        except Exception:
            pass

    now = now or datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    if days and days[-1] == today_str and now.strftime("%H:%M") < "15:00":
        days.pop()
    if not days:
        return end_str, None
    return days[-1], (days[-2] if len(days) >= 2 else None)


def _probe_snapshot_date():
    """探测腾讯批量行情接口当前截面所属日期（YYYY-MM-DD）。"""
    try:
        resp = _http_session().get("https://qt.gtimg.cn/q=sh000001", timeout=8)
        resp.raise_for_status()
        resp.encoding = "gbk"
        match = re.search(r'v_sh000001="([^"]*)"', resp.text)
        if not match:
            return ""
        parts = match.group(1).split("~")
        if len(parts) > 30:
            ts = parts[30].strip()
            if len(ts) >= 8 and ts[:8].isdigit():
                return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
    except Exception:
        pass
    return ""


def _fetch_tencent_snapshots(codes, progress=None, progress_range=(0.05, 0.55)):
    """8 路并发批量拉取全市场最新交易日截面行情（每次 300 只）。"""
    code_list = [item[0] if isinstance(item, (tuple, list)) else item for item in codes]
    batches = [code_list[offset:offset + 300] for offset in range(0, len(code_list), 300)]

    def _fetch_batch(batch):
        symbols = [f"{code[:2]}{code[3:]}" for code in batch]
        url = "https://qt.gtimg.cn/q=" + ",".join(symbols)
        resp = None
        for attempt in range(2):
            try:
                resp = _http_session().get(url, timeout=15)
                resp.raise_for_status()
                break
            except Exception:
                if attempt == 1:
                    raise
                time.sleep(0.4)
        resp.encoding = "gbk"
        parsed = {}
        for symbol, content in re.findall(r'v_((?:sh|sz)\d{6})="([^"]*)"', resp.text):
            parts = content.split("~")
            if len(parts) < 39:
                continue
            ts = parts[30].strip()
            if len(ts) < 8 or not ts[:8].isdigit():
                continue
            try:
                close_p = float(parts[3])
                prev_close = float(parts[4])
                open_p = float(parts[5])
                vol_raw = float(parts[6])
                pct_chg = float(parts[32])
                high_p = float(parts[33])
                low_p = float(parts[34])
                amt_wan = float(parts[37])
            except ValueError:
                continue
            try:
                turn = float(parts[38])
            except ValueError:
                turn = float("nan")
            symbol_num = symbol[2:]
            # 腾讯批量快照：科创板(688/689)成交量单位为股，其余板块单位为手(需×100转为股)
            volume = vol_raw if symbol_num.startswith(("688", "689")) else vol_raw * 100.0
            amount = amt_wan * 10000.0
            name = parts[1].strip()
            code = f"{symbol[:2]}.{symbol_num}"
            parsed[code] = {
                "date": f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}",
                "code": code,
                "name": name,
                "open": open_p,
                "high": high_p,
                "low": low_p,
                "close": close_p,
                "prev_close": prev_close,
                "volume": volume,
                "amount": amount,
                "turn": turn,
                "pctChg": pct_chg,
                "isST": "1" if "ST" in name.upper() else "0",
            }
        return parsed

    snapshots = {}
    total_batches = max(1, len(batches))
    p_start, p_end = progress_range
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_fetch_batch, b) for b in batches]
        for idx, future in enumerate(as_completed(futures), 1):
            try:
                snapshots.update(future.result())
            except Exception:
                pass
            if progress is not None:
                ratio = p_start + (p_end - p_start) * (idx / total_batches)
                progress.progress(ratio, text=f"批量行情截面 {idx}/{total_batches} 批（共 {len(snapshots):,} 只）")
    return snapshots


def update_cache(codes, start, end, progress):
    """方案1：交易日预检 + 批量截面极速日更 + 除权自动校验 + 连接池并发回补 + Baostock 兜底。"""
    global BAOSTOCK_AVAILABLE
    _init_cache()
    if progress is not None:
        progress.progress(0.02, text="正在核验最新已收盘交易日…")
    effective_end, prev_trade_day = _recent_trade_days(end)
    if effective_end < start:
        return {
            "updated": 0, "skipped": len(codes), "failed": 0,
            "failures": [], "source": "无需更新（暂无已收盘数据）",
            "effective_end": effective_end, "snapshot_updated": 0, "backfill_updated": 0, "ex_div": 0,
        }

    with sqlite3.connect(CACHE_DB, timeout=30) as conn:
        conn.execute("CREATE TEMP TABLE wanted (code TEXT PRIMARY KEY)")
        conn.executemany("INSERT OR IGNORE INTO wanted VALUES (?)", ((code,) for code, _ in codes))
        state = {
            row[0]: row[1:]
            for row in conn.execute(
                """
                WITH agg AS (
                    SELECT w.code, MIN(d.date) AS first_d, MAX(d.date) AS last_d
                    FROM wanted w
                    LEFT JOIN daily d ON d.code = w.code
                    GROUP BY w.code
                )
                SELECT a.code, a.first_d, a.last_d, d.close
                FROM agg a
                LEFT JOIN daily d ON d.code = a.code AND d.date = a.last_d
                """
            )
        }

    skipped = 0
    snapshot_candidates = []
    backfill_tasks = []
    for code, name in codes:
        first, last, last_close = state.get(code, (None, None, None))
        if first and last and first <= start and last >= effective_end:
            skipped += 1
            continue
        if first and first <= start and prev_trade_day and last == prev_trade_day:
            snapshot_candidates.append((code, name, last_close))
        else:
            fetch_start = start
            if first and first <= start and last:
                fetch_start = max(start, (pd.Timestamp(last) - pd.Timedelta(days=7)).strftime("%Y-%m-%d"))
            backfill_tasks.append((code, name, fetch_start))

    successful, changed, failures = set(), set(), {}
    snapshot_updated, backfill_updated, ex_div_count = 0, 0, 0
    used_snapshot = False
    snap_p_end = 0.05

    # 1. 批量截面极速更新：仅缺最新 1 个已收盘交易日的股票，18 次 HTTP 请求即可全部补齐
    if snapshot_candidates:
        snap_date = _probe_snapshot_date()
        if snap_date == effective_end:
            used_snapshot = True
            snap_p_end = 0.55 if backfill_tasks else 0.95
            snaps = _fetch_tencent_snapshots(snapshot_candidates, progress=progress, progress_range=(0.05, snap_p_end))
            snap_rows = []
            name_updates = []
            for code, name, last_close in snapshot_candidates:
                snap = snaps.get(code)
                if not snap:
                    backfill_tasks.append((code, name, max(start, prev_trade_day)))
                    continue
                if snap["name"] and snap["name"] != name:
                    name_updates.append((code, snap["name"]))
                # 停牌或无成交标的跳过当日写入，但视为同步成功
                if snap["date"] != effective_end or snap["open"] <= 0 or snap["volume"] <= 0 or snap["close"] <= 0:
                    skipped += 1
                    successful.add(code)
                    continue
                # 除权/复权连续性校验：比对批量接口昨收价与本地前一交易日收盘价
                if last_close is None or last_close <= 0:
                    continuity_ok = False
                else:
                    tol = max(0.02, abs(float(last_close)) * 0.002)
                    continuity_ok = abs(snap["prev_close"] - float(last_close)) <= tol
                if continuity_ok:
                    snap_rows.append(snap)
                    snapshot_updated += 1
                    changed.add(code)
                    successful.add(code)
                else:
                    ex_div_count += 1
                    backfill_tasks.append((code, name, start))

            if snap_rows:
                _cache_write(pd.DataFrame(snap_rows))
            if name_updates:
                with sqlite3.connect(CACHE_DB, timeout=30) as conn:
                    conn.executemany("INSERT OR REPLACE INTO stock_names(code, name) VALUES (?, ?)", name_updates)
        else:
            # 盘中时段快照已切换至今日，而 effective_end 为前一收盘日：转入快速并发历史回补最近几日
            recent_start = max(start, (pd.Timestamp(prev_trade_day) - pd.Timedelta(days=7)).strftime("%Y-%m-%d"))
            for code, name, _ in snapshot_candidates:
                backfill_tasks.append((code, name, recent_start))

    # 2. 连接池并发历史回补：东方财富前复权（含换手率/成交额）-> 腾讯 fqkline 回退，每 100 只批量写库
    if backfill_tasks:
        em_lock = threading.Lock()
        em_fail_streak = [0]
        total_bf = len(backfill_tasks)
        base_p = snap_p_end if used_snapshot else 0.05

        def _fetch_one_backfill(task):
            code, name, fetch_start = task
            with em_lock:
                skip_em = em_fail_streak[0] >= 12
            if not skip_em:
                try:
                    frame = _history_eastmoney(code, fetch_start, effective_end, name=name)
                    with em_lock:
                        em_fail_streak[0] = 0
                    return frame
                except Exception:
                    with em_lock:
                        em_fail_streak[0] += 1
            return _history_http(code, fetch_start, effective_end, name=name)

        batch_frames = []
        with ThreadPoolExecutor(max_workers=10) as pool:
            future_map = {pool.submit(_fetch_one_backfill, t): t for t in backfill_tasks}
            for i, future in enumerate(as_completed(future_map), 1):
                code, name, fetch_start = future_map[future]
                try:
                    frame = future.result()
                    if not frame.empty:
                        batch_frames.append(frame)
                        changed.add(code)
                        backfill_updated += 1
                    successful.add(code)
                    failures.pop(code, None)
                except Exception as exc:
                    failures[code] = str(exc)

                if len(batch_frames) >= 100:
                    _cache_write(pd.concat(batch_frames, ignore_index=True))
                    batch_frames.clear()
                if progress is not None and (i % 10 == 0 or i == total_bf):
                    ratio = base_p + (0.95 - base_p) * (i / total_bf)
                    progress.progress(ratio, text=f"并发回补历史 {i}/{total_bf}（10 路连接池）")
        if batch_frames:
            _cache_write(pd.concat(batch_frames, ignore_index=True))

    # 3. 仅针对 HTTPS 均失败的个别残余标的启用 Baostock 兜底
    remaining = [(code, fetch_start) for code, _, fetch_start in backfill_tasks if code not in successful]
    if remaining and BAOSTOCK_AVAILABLE is not False:
        def fetch_baostock():
            batch = []
            tot_rem = len(remaining)
            for i, (code, fetch_start) in enumerate(remaining, 1):
                try:
                    frame = _history(code, fetch_start, effective_end)
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
                if progress is not None and (i % 10 == 0 or i == tot_rem):
                    progress.progress(0.95 + 0.05 * (i / max(1, tot_rem)), text=f"Baostock 兜底 {i}/{tot_rem}")
            if batch:
                _cache_write(pd.concat(batch, ignore_index=True))
        try:
            _query(fetch_baostock)
        except Exception:
            BAOSTOCK_AVAILABLE = False

    if successful and effective_end >= start:
        with sqlite3.connect(CACHE_DB, timeout=30) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO sync_meta(code,start_date,through_date) VALUES (?,?,?)",
                ((code, start, effective_end) for code in successful),
            )

    if used_snapshot and backfill_tasks:
        source = "腾讯批量快照 + 并发历史回补"
    elif used_snapshot:
        source = "腾讯批量快照"
    elif backfill_tasks:
        source = "东方财富/腾讯 HTTPS 并发"
    else:
        source = "无需更新（已是最新）"
    return {
        "updated": len(changed),
        "skipped": skipped,
        "failed": len(failures),
        "failures": list(failures.items())[:10],
        "source": source,
        "effective_end": effective_end,
        "snapshot_updated": snapshot_updated,
        "backfill_updated": backfill_updated,
        "ex_div": ex_div_count,
    }


class AutoSyncManager:
    """进程级单例：每个交易日收盘后自动增量更新一次全市场行情缓存。"""

    def __init__(self):
        self._state_lock = threading.Lock()
        self._running = False
        self._ratio = 0.0
        self._text = ""
        self._started_at = 0.0
        self._error = ""
        self._retry_after = 0.0
        self._thread = None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="klineradar-auto-sync", daemon=True)
        self._thread.start()

    def _set_state(self, **kwargs):
        with self._state_lock:
            for k, v in kwargs.items():
                setattr(self, f"_{k}", v)

    def should_run_today(self, now=None, local_status=None):
        now = now or datetime.now()
        cfg = get_auto_sync_config()
        if not cfg["enabled"]:
            return False, "已关闭"
        if now.weekday() >= 5:
            return False, "周末休市"
        today_str = now.strftime("%Y-%m-%d")
        if cfg["last_date"] == today_str:
            return False, "今日已自动更新"
        local = local_status if local_status is not None else cache_status()
        if local["newest"] and local["newest"] >= today_str:
            return False, "今日行情已是最新"
        if now.strftime("%H:%M") < cfg["sync_time"]:
            return False, f"等待收盘后 {cfg['sync_time']} 触发"
        return True, "待自动更新"

    def _loop(self):
        # 启动后稍作延迟，让页面完成首次渲染
        time.sleep(2.5)
        while True:
            try:
                now = datetime.now()
                should_run, _ = self.should_run_today(now)
                if should_run and time.monotonic() >= self._retry_after:
                    self._execute_sync(trigger_label="收盘后自动更新")
            except Exception as exc:
                self._set_state(error=str(exc))
            time.sleep(30)

    def trigger_now(self):
        if self._running or SYNC_LOCK.locked():
            return False
        t = threading.Thread(
            target=self._execute_sync,
            kwargs={"trigger_label": "后台手动触发"},
            name="klineradar-manual-bg-sync",
            daemon=True,
        )
        t.start()
        return True

    def _execute_sync(self, trigger_label="收盘后自动更新"):
        if not SYNC_LOCK.acquire(blocking=False):
            return
        today_dt = date.today()
        today_str = today_dt.strftime("%Y-%m-%d")
        start_str = (today_dt - timedelta(days=210)).strftime("%Y-%m-%d")
        self._set_state(running=True, ratio=0.02, text=f"{trigger_label}：正在获取最新 A 股股票列表…", started_at=time.monotonic(), error="")

        class _BgProgress:
            def progress(_, ratio, text=""):
                self._set_state(ratio=0.12 + 0.86 * min(1.0, max(0.0, ratio)), text=f"{trigger_label}：{text}")

        try:
            all_codes, list_date, list_source = _get_universe_for_sync(today_str)
            if not all_codes:
                raise RuntimeError("未获取到可用股票列表")
            self._set_state(ratio=0.12, text=f"{trigger_label}：已就绪 {len(all_codes):,} 只标的（{list_source}），正在核对并补齐行情…")
            stats = update_cache(all_codes, start_str, today_str, _BgProgress())
            finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            eff_end = stats.get("effective_end", today_str)
            info = {
                "time": finished_at,
                "date": eff_end,
                "trigger": trigger_label,
                "updated": stats.get("updated", 0),
                "skipped": stats.get("skipped", 0),
                "failed": stats.get("failed", 0),
                "snapshot_updated": stats.get("snapshot_updated", 0),
                "ex_div": stats.get("ex_div", 0),
                "effective_end": eff_end,
                "source": stats.get("source", "腾讯批量快照"),
                "status": "warning" if stats.get("failed") else "success",
            }
            # 若已完成当日收盘更新，或已过 15:00 且今天是法定节假日休市，则记录今日已完成避免重复循环
            if eff_end >= today_str or datetime.now().strftime("%H:%M") >= "15:00":
                _setting_set("auto_sync_last_date", today_str)
            _setting_set("auto_sync_last_info", json.dumps(info, ensure_ascii=False))
            self._set_state(ratio=1.0, text=f"{trigger_label}完成：更新 {info['updated']} 只，跳过 {info['skipped']} 只")
        except Exception as exc:
            self._set_state(error=str(exc), retry_after=time.monotonic() + 1800)
            info = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "date": today_str,
                "trigger": trigger_label,
                "error": str(exc),
                "status": "error",
            }
            _setting_set("auto_sync_last_info", json.dumps(info, ensure_ascii=False))
        finally:
            self._set_state(running=False)
            SYNC_LOCK.release()

    def snapshot(self, local_status=None):
        cfg = get_auto_sync_config()
        _, today_status = self.should_run_today(local_status=local_status)
        with self._state_lock:
            return {
                "enabled": cfg["enabled"],
                "sync_time": cfg["sync_time"],
                "last_date": cfg["last_date"],
                "last_info": cfg["last_info"],
                "running": self._running,
                "ratio": self._ratio,
                "text": self._text,
                "elapsed": (time.monotonic() - self._started_at) if self._running and self._started_at else 0.0,
                "error": self._error,
                "today_status": "后台同步中…" if self._running else today_status,
            }


@st.cache_resource
def get_auto_sync_manager():
    mgr = AutoSyncManager()
    mgr.start()
    return mgr


def _cache_version():
    _init_cache()
    with sqlite3.connect(CACHE_DB, timeout=30) as conn:
        rows, newest = conn.execute("SELECT COUNT(*), COALESCE(MAX(date), '') FROM daily").fetchone()
    return int(rows or 0), str(newest or "")


@st.cache_resource(max_entries=3, show_spinner=False)
def _load_market_arrays_cached(start, end, db_rows, db_newest):
    """一次性从 SQLite 载入全市场行情并按股票拆分为只读 NumPy 数组字典（带版本感知内存缓存）。"""
    _init_cache()
    with sqlite3.connect(CACHE_DB, timeout=30) as conn:
        rows = conn.execute(
            "SELECT code,date,open,high,low,close,volume,amount,turn,pctChg "
            "FROM daily WHERE date BETWEEN ? AND ? AND close IS NOT NULL ORDER BY code,date",
            (start, end),
        ).fetchall()
    if not rows:
        return {}

    market = {}
    cur_code = None
    d_buf, o_buf, h_buf, l_buf, c_buf, v_buf, a_buf, t_buf, p_buf = [], [], [], [], [], [], [], [], []

    def _flush():
        if not cur_code or not c_buf:
            return
        arrs = tuple(
            np.asarray(buf, dtype=np.float64)
            for buf in (o_buf, h_buf, l_buf, c_buf, v_buf, a_buf, t_buf, p_buf)
        )
        for arr in arrs:
            arr.setflags(write=False)
        market[cur_code] = (tuple(d_buf), *arrs)

    for code, dt, op, hi, lo, cl, vol, amt, turn, pct in rows:
        if code != cur_code:
            _flush()
            cur_code = code
            d_buf, o_buf, h_buf, l_buf, c_buf, v_buf, a_buf, t_buf, p_buf = [], [], [], [], [], [], [], [], []
        d_buf.append(dt)
        o_buf.append(op)
        h_buf.append(hi)
        l_buf.append(lo)
        c_buf.append(cl)
        v_buf.append(vol)
        a_buf.append(amt)
        t_buf.append(turn)
        p_buf.append(pct)
    _flush()
    return market


def _limit_pct_threshold(code):
    """按板块返回涨跌停幅度阈值：双创 20cm(19.5%)、北交所 30cm(29.5%)、主板 10cm(9.5%)。"""
    if code.startswith(("sh.688", "sh.689", "sz.300", "sz.301")):
        return 19.5
    if code.startswith("bj."):
        return 29.5
    return 9.5


def _is_gem_or_star(code):
    return code.startswith(("sh.688", "sh.689", "sz.300", "sz.301"))


def _rolling_mean_1d(arr, window):
    n = len(arr)
    out = np.full(n, np.nan, dtype=np.float64)
    if n < window:
        return out
    if np.isnan(arr).any():
        return pd.Series(arr).rolling(window).mean().to_numpy(dtype=np.float64)
    cs = np.empty(n + 1, dtype=np.float64)
    cs[0] = 0.0
    np.cumsum(arr, out=cs[1:])
    out[window - 1:] = (cs[window:] - cs[:-window]) / float(window)
    return out


def _ema_1d(arr, span):
    n = len(arr)
    if n == 0:
        return np.empty(0, dtype=np.float64)
    if np.isnan(arr).any():
        return pd.Series(arr).ewm(span=span, adjust=False).mean().to_numpy(dtype=np.float64)
    alpha = 2.0 / (span + 1.0)
    one_minus = 1.0 - alpha
    out = np.empty(n, dtype=np.float64)
    val = float(arr[0])
    out[0] = val
    for idx in range(1, n):
        val = alpha * float(arr[idx]) + one_minus * val
        out[idx] = val
    return out


def _fast_indicators_np(o, h, l, c, v, amt, pct):
    """纯 NumPy 计算技术指标，结果与 Pandas indicators() 严格一致。"""
    n = len(c)
    if n < 27:
        return None

    ma5 = _rolling_mean_1d(c, 5)
    ma10 = _rolling_mean_1d(c, 10)
    ma20 = _rolling_mean_1d(c, 20)
    ma25 = _rolling_mean_1d(c, 25)
    ma60 = _rolling_mean_1d(c, 60)

    # RSI(14): delta[0] 为 NaN，故滚动均值从第 14 个索引开始有效
    if np.isnan(c).any():
        delta_s = pd.Series(c).diff()
        gain_s, loss_s = delta_s.clip(lower=0), -delta_s.clip(upper=0)
        ag = gain_s.rolling(14).mean().to_numpy(dtype=np.float64)
        al = loss_s.rolling(14).mean().to_numpy(dtype=np.float64)
    else:
        diff = np.diff(c)
        gain = np.where(diff > 0.0, diff, 0.0)
        loss = np.where(diff < 0.0, -diff, 0.0)
        ag = np.full(n, np.nan, dtype=np.float64)
        al = np.full(n, np.nan, dtype=np.float64)
        if n >= 15:
            g_cs = np.empty(n, dtype=np.float64)
            l_cs = np.empty(n, dtype=np.float64)
            g_cs[0] = 0.0
            l_cs[0] = 0.0
            np.cumsum(gain, out=g_cs[1:])
            np.cumsum(loss, out=l_cs[1:])
            ag[14:] = (g_cs[14:] - g_cs[:-14]) / 14.0
            al[14:] = (l_cs[14:] - l_cs[:-14]) / 14.0

    rsi = np.full(n, np.nan, dtype=np.float64)
    has_r = (~np.isnan(ag)) & (~np.isnan(al))
    pos_l = has_r & (al > 0.0)
    rsi[pos_l] = 100.0 - 100.0 / (1.0 + ag[pos_l] / al[pos_l])
    rsi[has_r & (al == 0.0) & (ag > 0.0)] = 100.0
    rsi[has_r & (al == 0.0) & (ag == 0.0)] = 50.0

    ema12 = _ema_1d(c, 12)
    ema26 = _ema_1d(c, 26)
    macd = ema12 - ema26
    sig = _ema_1d(macd, 9)

    if np.isnan(l).any() or np.isnan(h).any():
        low9 = pd.Series(l).rolling(9).min().to_numpy(dtype=np.float64)
        high9 = pd.Series(h).rolling(9).max().to_numpy(dtype=np.float64)
    else:
        low9 = np.full(n, np.nan, dtype=np.float64)
        high9 = np.full(n, np.nan, dtype=np.float64)
        low9[8:] = np.min(np.lib.stride_tricks.sliding_window_view(l, 9), axis=1)
        high9[8:] = np.max(np.lib.stride_tricks.sliding_window_view(h, 9), axis=1)

    diff_hl = high9 - low9
    k_arr = np.full(n, np.nan, dtype=np.float64)
    valid_hl = (~np.isnan(diff_hl)) & (diff_hl != 0.0) & (~np.isnan(c))
    k_arr[valid_hl] = 100.0 * (c[valid_hl] - low9[valid_hl]) / diff_hl[valid_hl]

    d_arr = np.full(n, np.nan, dtype=np.float64)
    if n >= 11:
        d_arr[10:] = (k_arr[8:-2] + k_arr[9:-1] + k_arr[10:]) / 3.0

    vol_ma5 = _rolling_mean_1d(v, 5)
    vol_ma20 = _rolling_mean_1d(v, 20)

    valid = ~(np.isnan(ma25) | np.isnan(rsi) | np.isnan(macd) | np.isnan(sig) | np.isnan(k_arr) | np.isnan(d_arr))
    if valid[24:].all():
        if n - 24 < 3:
            return None
        sl = slice(24, None)
        return (
            o[sl], h[sl], l[sl], c[sl], v[sl], amt[sl], pct[sl],
            ma5[sl], ma10[sl], ma20[sl], ma25[sl], ma60[sl],
            rsi[sl], macd[sl], sig[sl], k_arr[sl], d_arr[sl],
            vol_ma5[sl], vol_ma20[sl],
        )

    if int(valid.sum()) < 3:
        return None
    return (
        o[valid], h[valid], l[valid], c[valid], v[valid], amt[valid], pct[valid],
        ma5[valid], ma10[valid], ma20[valid], ma25[valid], ma60[valid],
        rsi[valid], macd[valid], sig[valid], k_arr[valid], d_arr[valid],
        vol_ma5[valid], vol_ma20[valid],
    )


def _eval_strategies_np(feat, strategies, limit_pct=9.5):
    """按需只计算用户勾选的策略（排除 RPS），避免无效分支开销。"""
    o, h, l, c, v, amt, pct, ma5, ma10, ma20, ma25, ma60, rsi, macd, sig, k_arr, d_arr, vol_ma5, vol_ma20 = feat
    matched = []
    cross_ma = None
    cross_macd = None
    cross_kdj = None
    recent_limit = None
    lim_up_ratio = 1.0 + limit_pct / 100.0
    lim_dn_ratio = 1.0 - limit_pct / 100.0

    for name in strategies:
        ok = False
        if name == "底部趋势拐点":
            ok = rsi[-1] < 38 and c[-1] > o[-1] and macd[-1] > macd[-2] and v[-1] > vol_ma5[-1]
        elif name == "涨停回马枪策略":
            if recent_limit is None:
                ret = np.nan_to_num(pct, nan=0.0)
                recent_limit = bool((ret[-11:-1] >= limit_pct).any())
            ok = recent_limit and c[-1] > o[-1] and c[-1] > ma5[-1] and v[-1] < vol_ma20[-1]
        elif name == "涨停横盘策略":
            if recent_limit is None:
                ret = np.nan_to_num(pct, nan=0.0)
                recent_limit = bool((ret[-11:-1] >= limit_pct).any())
            ok = (
                recent_limit
                and len(c) >= 6
                and (np.std(c[-5:], ddof=1) / c[-1] < 0.04)
                and c[-1] > np.nanmax(h[-6:-1])
            )
        elif name == "启明星策略":
            ok = (
                c[-3] < o[-3]
                and abs(c[-2] - o[-2]) < (o[-3] - c[-3]) * 0.5
                and c[-1] > (o[-3] + c[-3]) / 2.0
            )
        elif name == "多金叉共振":
            if cross_ma is None:
                cross_ma = bool(ma5[-2] <= ma10[-2] and ma5[-1] > ma10[-1])
            if cross_macd is None:
                cross_macd = bool(macd[-2] <= sig[-2] and macd[-1] > sig[-1])
            if cross_kdj is None:
                cross_kdj = bool(k_arr[-2] <= d_arr[-2] and k_arr[-1] > d_arr[-1])
            ok = cross_ma and cross_macd and cross_kdj
        elif name == "金三角策略":
            # 均线价托初成：MA5>MA10>MA20 当日成型（前一日尚未完成多头排列）且放量收阳
            ok = (
                ma5[-1] > ma10[-1] > ma20[-1]
                and (ma5[-2] <= ma10[-2] or ma10[-2] <= ma20[-2])
                and c[-1] > o[-1]
                and v[-1] > vol_ma5[-1]
            )
        elif name == "多方炮策略":
            ok = c[-3] > o[-3] and c[-2] < o[-2] and c[-1] > o[-1] and c[-1] > c[-3]
        elif name == "阻力位突破策略":
            ok = h[-21:-1].size > 0 and c[-1] > np.nanmax(h[-21:-1]) and v[-1] > vol_ma20[-1] * 1.5
        elif name == "强势洗盘弱转强":
            ok = c[-1] > o[-1] and c[-1] > h[-2] and c[-2] < o[-2] and ma5[-1] > ma20[-1]
        elif name == "趋势加速拐点":
            ok = ma5[-1] > ma5[-2] > ma5[-3] and pct[-1] > 3 and v[-1] > vol_ma20[-1] * 1.3
        elif name == "仙人指路策略":
            ok = (h[-1] - max(o[-1], c[-1])) > abs(c[-1] - o[-1]) * 1.8 and c[-1] > ma20[-1]
        elif name == "W底策略":
            ok = (
                l[-10:-5].size > 0
                and l[-5:-1].size > 0
                and h[-10:-1].size > 0
                and abs(np.nanmin(l[-10:-5]) - np.nanmin(l[-5:-1])) / c[-1] < 0.04
                and c[-1] > np.nanmax(h[-10:-1])
            )
        elif name == "趋势起点策略":
            if cross_ma is None:
                cross_ma = bool(ma5[-2] <= ma10[-2] and ma5[-1] > ma10[-1])
            ok = cross_ma and ma20[-1] > ma20[-2]
        elif name == "2560战法":
            ok = c[-1] > ma25[-1] and v[-1] > vol_ma5[-1] > vol_ma20[-1]
        elif name == "金叉不绿策略":
            if cross_ma is None:
                cross_ma = bool(ma5[-2] <= ma10[-2] and ma5[-1] > ma10[-1])
            ok = cross_ma and c[-1] >= o[-1]
        elif name == "海龟突破策略":
            ok = (
                h[-21:-1].size > 0
                and c[-1] > np.nanmax(h[-21:-1])
                and amt[-1] > 100_000_000
                and c[-1] > o[-1]
                and c[-1] > c[-2]
            )
        elif name == "高旗形整理策略":
            ok = (
                l[-40:].size > 0
                and l[-10:].size > 0
                and v[-21:-1].size > 0
                and np.nanmax(h[-40:]) / np.nanmin(l[-40:]) > 1.6
                and np.nanmax(h[-10:]) / np.nanmin(l[-10:]) < 1.15
                and np.nanmin(l[-10:]) >= np.nanmax(h[-40:]) * 0.8
                and v[-1] < np.nanmean(v[-21:-1]) * 0.6
            )
        elif name == "涨停洗盘策略":
            ok = c[-2] >= c[-3] * lim_up_ratio and c[-1] < o[-1] and v[-1] > v[-2] * 2 and l[-1] >= c[-2]
        elif name == "上升趋势跌停反包":
            ok = ma20[-2] > ma60[-2] and c[-1] <= c[-2] * lim_dn_ratio and v[-1] > vol_ma20[-1] * 2

        if bool(ok):
            matched.append((name, STRATEGIES[name]))
    return matched


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


def screen(
    codes, frames, strategies, min_change, min_turn, max_turn,
    on_progress=None, window_start=None,
    exclude_st=True, exclude_gem_star=False, min_amount=0.0,
):
    rows = []
    sparks = {}
    stats = {"ready": 0, "missing": 0, "short": 0, "filtered": 0}
    fallback_progress = None if on_progress else st.progress(0, text="正在读取本地行情…")
    window_start_str = pd.Timestamp(window_start).strftime("%Y-%m-%d") if window_start is not None else None
    non_rps = [s for s in strategies if s != "RPS强势突破"]
    need_rps = "RPS强势突破" in strategies
    min_amt_yuan = float(min_amount) * 1e8 if min_amount and min_amount > 0 else 0.0

    def _emit(ratio, stage, detail, hits=0, scanned=0, total=0):
        if on_progress:
            on_progress(ratio, stage=stage, detail=detail, hits=hits, scanned=scanned, total=total)
        elif fallback_progress is not None:
            fallback_progress.progress(min(1.0, max(0.0, ratio)), text=detail)

    def _record_spark(code, close_arr):
        if code not in sparks:
            tail = close_arr[-20:]
            sparks[code] = [round(float(v), 2) for v in tail if not np.isnan(v)]

    def _append_row(strategy, code, name, last_close, last_pct, last_turn, last_amt, reason):
        rows.append({
            "策略": strategy, "代码": code, "名称": name,
            "收盘": last_close, "涨跌幅%": last_pct,
            "换手率%": last_turn if not np.isnan(last_turn) else float("nan"),
            "成交额(亿)": round(last_amt / 1e8, 2) if not np.isnan(last_amt) else float("nan"),
            "信号说明": reason,
        })

    def run():
        rps_pool = {}
        total_codes = max(1, len(codes))
        last_tick = 0.0
        for i, (code, name) in enumerate(codes, 1):
            if exclude_st and "ST" in str(name).upper():
                stats["filtered"] += 1
                continue
            if exclude_gem_star and _is_gem_or_star(code):
                stats["filtered"] += 1
                continue
            item = frames.get(code)
            if item is None:
                stats["missing"] += 1
            elif isinstance(item, pd.DataFrame):
                if item.empty:
                    stats["missing"] += 1
                else:
                    last_dt = item["date"].iloc[-1]
                    last_dt_str = last_dt.strftime("%Y-%m-%d") if hasattr(last_dt, "strftime") else str(last_dt)[:10]
                    if window_start_str is not None and last_dt_str < window_start_str:
                        stats["missing"] += 1
                    elif len(item) < 70:
                        stats["short"] += 1
                    else:
                        stats["ready"] += 1
                        o = item["open"].to_numpy(dtype=np.float64)
                        h = item["high"].to_numpy(dtype=np.float64)
                        l = item["low"].to_numpy(dtype=np.float64)
                        c = item["close"].to_numpy(dtype=np.float64)
                        v = item["volume"].to_numpy(dtype=np.float64)
                        amt = item["amount"].to_numpy(dtype=np.float64)
                        t = item["turn"].to_numpy(dtype=np.float64)
                        pct = item["pctChg"].to_numpy(dtype=np.float64)
                        last_close = float(c[-1])
                        last_turn = float(t[-1])
                        last_pct = float(pct[-1])
                        last_amt = float(amt[-1])
                        if need_rps and len(c) >= 121:
                            rps_pool[code] = (name, c, h, last_close, last_turn, last_pct, last_amt)
                        turn_ok = np.isnan(last_turn) or (min_turn <= last_turn <= max_turn)
                        pct_ok = (not np.isnan(last_pct)) and (last_pct >= min_change)
                        amt_ok = (min_amt_yuan <= 0.0) or ((not np.isnan(last_amt)) and last_amt >= min_amt_yuan)
                        if non_rps and pct_ok and turn_ok and amt_ok:
                            feat = _fast_indicators_np(o, h, l, c, v, amt, pct)
                            if feat is not None:
                                lim = _limit_pct_threshold(code)
                                for strategy, reason in _eval_strategies_np(feat, non_rps, limit_pct=lim):
                                    _record_spark(code, c)
                                    _append_row(strategy, code, name, last_close, last_pct, last_turn, last_amt, reason)
            else:
                dates, o, h, l, c, v, amt, t, pct = item
                n_bars = len(c)
                if n_bars == 0:
                    stats["missing"] += 1
                elif window_start_str is not None and dates[-1] < window_start_str:
                    stats["missing"] += 1
                elif n_bars < 70:
                    stats["short"] += 1
                else:
                    stats["ready"] += 1
                    last_close = float(c[-1])
                    last_turn = float(t[-1])
                    last_pct = float(pct[-1])
                    last_amt = float(amt[-1])
                    if need_rps and n_bars >= 121:
                        rps_pool[code] = (name, c, h, last_close, last_turn, last_pct, last_amt)
                    turn_ok = np.isnan(last_turn) or (min_turn <= last_turn <= max_turn)
                    pct_ok = (not np.isnan(last_pct)) and (last_pct >= min_change)
                    amt_ok = (min_amt_yuan <= 0.0) or ((not np.isnan(last_amt)) and last_amt >= min_amt_yuan)
                    if non_rps and pct_ok and turn_ok and amt_ok:
                        feat = _fast_indicators_np(o, h, l, c, v, amt, pct)
                        if feat is not None:
                            lim = _limit_pct_threshold(code)
                            for strategy, reason in _eval_strategies_np(feat, non_rps, limit_pct=lim):
                                _record_spark(code, c)
                                _append_row(strategy, code, name, last_close, last_pct, last_turn, last_amt, reason)

            now = time.monotonic()
            if i == total_codes or i % 500 == 0 or (now - last_tick) >= 0.25:
                last_tick = now
                pct_done = 0.22 + 0.72 * (i / total_codes)
                _emit(
                    pct_done,
                    stage=2,
                    detail=f"正在极速计算指标与匹配策略（当前：{code} {name}）",
                    hits=len(rows),
                    scanned=i,
                    total=total_codes,
                )

        if rps_pool:
            _emit(0.96, stage=3, detail=f"正在对 {len(rps_pool):,} 只标的执行 120 日 RPS 相对强度排名…",
                  hits=len(rows), scanned=total_codes, total=total_codes)
            returns = pd.Series({code: vals[1][-1] / vals[1][-121] - 1.0 for code, vals in rps_pool.items()})
            ranks = returns.rank(pct=True) * 100.0
            for code, rank in ranks.items():
                name, c, h, last_close, last_turn, last_pct, last_amt = rps_pool[code]
                breakout = last_close >= float(np.nanmax(h[-120:])) * 0.9
                turn_ok = np.isnan(last_turn) or (min_turn <= last_turn <= max_turn)
                pct_ok = (not np.isnan(last_pct)) and (last_pct >= min_change)
                amt_ok = (min_amt_yuan <= 0.0) or ((not np.isnan(last_amt)) and last_amt >= min_amt_yuan)
                if rank >= 90 and breakout and pct_ok and turn_ok and amt_ok:
                    _record_spark(code, c)
                    _append_row(
                        "RPS强势突破", code, name, last_close, last_pct, last_turn, last_amt,
                        f"本次扫描样本 RPS {rank:.1f}，收盘处于 120 日高点 90% 内",
                    )

        _emit(0.99, stage=3, detail="正在排序信号榜并生成迷你走势数据…",
              hits=len(rows), scanned=total_codes, total=total_codes)
        return pd.DataFrame(rows).sort_values("涨跌幅%", ascending=False).reset_index(drop=True) if rows else pd.DataFrame()

    result = run()
    if fallback_progress is not None:
        fallback_progress.empty()
    stats["sparks"] = sparks
    return result, stats


def compute_forward_returns(result_df, signal_end_str):
    """若本地 SQLite 存在 signal_end_str 之后的交易日，快速统计各策略后续 5 日 / 10 日收益与胜率。"""
    if result_df is None or result_df.empty:
        return pd.DataFrame()
    codes = result_df["代码"].unique().tolist()
    _init_cache()
    with sqlite3.connect(CACHE_DB, timeout=30) as conn:
        conn.execute("CREATE TEMP TABLE fwd_codes (code TEXT PRIMARY KEY)")
        conn.executemany("INSERT OR IGNORE INTO fwd_codes VALUES (?)", ((c,) for c in codes))
        rows = conn.execute(
            "SELECT d.code, d.date, d.close FROM daily d "
            "JOIN fwd_codes f ON d.code = f.code "
            "WHERE d.date > ? AND d.close IS NOT NULL ORDER BY d.code, d.date",
            (signal_end_str,),
        ).fetchall()
    if not rows:
        return pd.DataFrame()

    future_closes = {}
    for code, _dt, cl in rows:
        future_closes.setdefault(code, []).append(float(cl))

    # 以信号当日收盘价为基准计算 T+5 / T+10 涨跌幅
    base_close = dict(result_df[["代码", "收盘"]].drop_duplicates("代码").itertuples(index=False, name=None))
    ret5_map, ret10_map = {}, {}
    for code, closes in future_closes.items():
        base = base_close.get(code)
        if not base or base <= 0:
            continue
        if len(closes) >= 5:
            ret5_map[code] = (closes[4] / base - 1.0) * 100.0
        if len(closes) >= 10:
            ret10_map[code] = (closes[9] / base - 1.0) * 100.0
    if not ret5_map and not ret10_map:
        return pd.DataFrame()

    summary = []
    for strat, grp in result_df.groupby("策略", sort=False):
        c_list = grp["代码"].unique().tolist()
        r5 = [ret5_map[c] for c in c_list if c in ret5_map]
        r10 = [ret10_map[c] for c in c_list if c in ret10_map]
        summary.append({
            "策略": strat,
            "样本数": len(c_list),
            "5日胜率%": round(sum(1 for v in r5 if v > 0) / len(r5) * 100.0, 1) if r5 else float("nan"),
            "5日均涨幅%": round(float(np.mean(r5)), 2) if r5 else float("nan"),
            "10日胜率%": round(sum(1 for v in r10 if v > 0) / len(r10) * 100.0, 1) if r10 else float("nan"),
            "10日均涨幅%": round(float(np.mean(r10)), 2) if r10 else float("nan"),
        })
    return pd.DataFrame(summary)


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


def chart_panes(frame, title, interval, show_ma=True, show_macd=False, display_start=None):
    d = _resample_kline(frame, interval)
    if display_start is not None and not d.empty:
        sliced = d[d["date"] >= pd.Timestamp(display_start)].reset_index(drop=True)
        if not sliced.empty:
            d = sliced
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

/* Tablet & Mobile Responsive Adaptations */
@media (max-width: 768px) {
    [data-testid="stMainBlockContainer"] {
        padding-left: 0.9rem;
        padding-right: 0.9rem;
        padding-top: 0.9rem;
    }
    .hero-bar {
        padding: 14px 16px;
        gap: 10px;
    }
    .ticker-stats {
        grid-template-columns: repeat(4, minmax(0, 1fr));
    }
}

@media (max-width: 640px) {
    [data-testid="stMainBlockContainer"] {
        padding: 0.7rem 0.65rem 2.4rem 0.65rem;
    }
    [data-testid="stSidebar"] {
        max-width: 86vw;
    }
    .hero-bar {
        padding: 12px 14px;
        border-radius: 13px;
        margin-bottom: 10px;
    }
    .hero-left {
        gap: 10px;
        align-items: flex-start;
    }
    .hero-logo {
        width: 36px;
        height: 36px;
        border-radius: 10px;
    }
    .hero-title {
        font-size: 1.05rem;
        line-height: 1.28;
    }
    .hero-sub {
        font-size: 0.74rem;
    }
    .hero-right {
        width: 100%;
        justify-content: flex-start;
        gap: 6px;
    }
    .status-pill {
        font-size: 0.72rem;
        padding: 4px 9px;
    }
    .kpi-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 8px;
        margin-bottom: 10px;
    }
    .kpi-card {
        padding: 11px 12px;
        border-radius: 12px;
    }
    .kpi-label {
        font-size: 0.72rem;
    }
    .kpi-value {
        font-size: 1.22rem;
    }
    .kpi-sub {
        font-size: 0.7rem;
    }
    .sec-head {
        flex-direction: column;
        align-items: flex-start;
        gap: 4px;
        margin: 12px 0 8px 0;
    }
    .sec-title {
        font-size: 0.94rem;
    }
    .sec-meta {
        font-size: 0.73rem;
    }
    .ticker-card {
        padding: 12px 14px;
        border-radius: 13px;
    }
    .ticker-top {
        flex-direction: column;
        align-items: flex-start;
        gap: 8px;
    }
    .ticker-name {
        font-size: 1.08rem;
    }
    .ticker-price {
        font-size: 1.38rem;
    }
    .ticker-stats {
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 8px;
    }
    .t-stat {
        padding: 7px 9px;
    }
    .ma-legend {
        gap: 8px;
        padding: 6px 10px;
        font-size: 0.73rem;
    }
    .scan-float-card {
        width: 94vw;
        padding: 16px 16px;
        border-radius: 14px;
    }
    .scan-title {
        font-size: 0.95rem;
    }
    .scan-pct {
        font-size: 1.24rem;
    }
    .scan-steps {
        grid-template-columns: 1fr;
        gap: 6px;
    }
    .scan-step {
        text-align: left;
        padding: 6px 10px;
    }
    .empty-hero {
        padding: 22px 16px 18px 16px;
    }
    .empty-title {
        font-size: 1.06rem;
    }
    .empty-desc {
        font-size: 0.8rem;
    }
    .stButton > button {
        min-height: 38px;
    }
}
</style>
""", unsafe_allow_html=True)


def _render_hero(local_status, selected_strategies, auto_sync=None):
    newest = local_status["newest"] or "待同步"
    stocks_cnt = local_status["stocks"]
    is_ready = stocks_cnt > 0
    pill_cls = "status-pill live" if is_ready else "status-pill"
    status_text = "离线引擎就绪" if is_ready else "尚未建立缓存"

    sync_pill = ""
    if auto_sync:
        if auto_sync["running"]:
            pct = int(round(auto_sync["ratio"] * 100))
            sync_pill = f'<span class="status-pill" style="border-color:rgba(56,189,248,0.45);background:rgba(56,189,248,0.12);color:#7dd3fc;">⟳ 自动更新 {pct}%</span>'
        elif auto_sync["enabled"]:
            sync_pill = f'<span class="status-pill">收盘自动更新 {escape(auto_sync["sync_time"])}</span>'

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
        {sync_pill}
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
    if not SYNC_LOCK.acquire(blocking=False):
        st.info("后台正在执行自动行情更新，请稍候完成后再试。")
        return
    end_str = end.strftime("%Y-%m-%d")
    tracker = FloatingProgress("正在更新全市场行情缓存", ("校验交易日与股票池", "批量截面/并发拉取", "写入本地库"))
    try:
        tracker.update(0.05, stage=1, detail="正在加载股票池并核对最近已收盘交易日…")
        try:
            all_codes, list_date, list_source = _get_universe_for_sync(end_str)
        except Exception as exc:
            tracker.close()
            st.error(f"获取股票列表失败：{exc}")
            return
        if not all_codes:
            tracker.close()
            st.error("所选日期及之前 10 天内都没有可用的股票列表，请稍后重试。")
            return
        tracker.update(0.15, stage=2, detail=f"已就绪 {len(all_codes):,} 只股票（来源：{list_source}），正在检查本地缓存区间…")
        update_stats = update_cache(all_codes, start.strftime("%Y-%m-%d"), end_str, tracker)
        tracker.update(1.0, stage=3, detail="全市场行情同步完成！")
        time.sleep(0.2)
    finally:
        tracker.close()
        SYNC_LOCK.release()

    eff_end = update_stats.get("effective_end", end_str)
    today_str = date.today().strftime("%Y-%m-%d")
    if eff_end >= today_str:
        _setting_set("auto_sync_last_date", today_str)
        _setting_set("auto_sync_last_info", json.dumps({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "date": eff_end,
            "trigger": "手动全市场更新",
            "updated": update_stats.get("updated", 0),
            "skipped": update_stats.get("skipped", 0),
            "failed": update_stats.get("failed", 0),
            "snapshot_updated": update_stats.get("snapshot_updated", 0),
            "ex_div": update_stats.get("ex_div", 0),
            "source": update_stats.get("source", "腾讯批量快照"),
            "status": "warning" if update_stats.get("failed") else "success",
        }, ensure_ascii=False))

    if eff_end < end_str:
        st.info(f"当前仍在交易时段或 {end_str} 非交易日，已自动同步至最近已收盘交易日：{eff_end}。")
    if list_source not in ("Baostock", "本地股票池缓存"):
        st.info(f"股票列表来源：{list_source}（下次 7 天内将直接复用本地股票池缓存）。")
    st.session_state["update_stats"] = update_stats
    for key in ("result", "scan_count", "scan_stats", "history_source", "sparks"):
        st.session_state.pop(key, None)

    detail_parts = [f"新增/更新 {update_stats['updated']} 只"]
    if update_stats.get("snapshot_updated"):
        detail_parts.append(f"极速截面 {update_stats['snapshot_updated']} 只")
    if update_stats.get("ex_div"):
        detail_parts.append(f"除权自动回补 {update_stats['ex_div']} 只")
    detail_parts.append(f"跳过 {update_stats['skipped']} 只")
    summary_text = "，".join(detail_parts)

    if update_stats["failed"]:
        st.warning(f"缓存更新完成（{summary_text}），但 {update_stats['failed']} 只股票更新失败；示例：{update_stats['failures'][:3]}")
    else:
        st.toast(f"行情缓存更新完成：{summary_text}", icon="✅")
        st.success(f"行情缓存已更新：{summary_text}（通道：{update_stats.get('source', '自动')}）。")


def _run_screen_action(
    start, end, strategies, min_change, min_turn, max_turn, max_stocks,
    exclude_st=True, exclude_gem_star=False, min_amount=0.0,
):
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
        # 为保证 MA60/MACD/RSI/RPS 等指标充分预热且数据精准，后台自动向前多读取前置历史行情
        lookback_start = min(start, end - timedelta(days=210))
        db_rows, db_newest = _cache_version()
        tracker.update(0.08, stage=1, detail=f"正在从本地缓存加载全市场行情（共 {db_rows:,} 条记录）…")
        frames = _load_market_arrays_cached(
            lookback_start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), db_rows, db_newest
        )
        if not frames:
            tracker.close()
            st.error("本地没有所选日期范围的行情数据，请先点击「更新全市场行情缓存」。")
            return
        tracker.update(0.22, stage=2, detail=f"已就绪 {len(frames):,} 只标的行情，开始极速策略扫描…")
        result, scan_stats = screen(
            selected, frames, strategies, min_change, min_turn, max_turn,
            on_progress=tracker.update, window_start=start,
            exclude_st=exclude_st, exclude_gem_star=exclude_gem_star, min_amount=min_amount,
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
    min_turn = 0.0
    max_turn = st.slider("最高换手率 (%)", 0.0, 100.0, 100.0, step=0.5, help="左侧固定 0%，拖动滑块限制最大换手率（100% 表示不限）")
    min_amount = st.number_input("最低日成交额 (亿元)", 0.0, 100.0, 0.0, 0.5, format="%.1f", help="设为 0 表示不限制；建议设 1.0 亿以上过滤僵尸股")
    f_c1, f_c2 = st.columns(2)
    with f_c1:
        exclude_st = st.checkbox("排除 ST", value=True, help="过滤名称含 ST / *ST 的风险警示标的")
    with f_c2:
        exclude_gem_star = st.checkbox("排除双创", value=False, help="过滤创业板(300/301)与科创板(688/689)")
    mobile_compact = st.toggle("手机紧凑视图", value=False, help="手机竖屏建议开启：精简信号榜列数免横滑，并调低 K 线图高度防误触")
    max_stocks = st.number_input("最多扫描股票数 (只)", 10, 6000, 6000, 200)

    sac.divider(label="执行操作", icon="lightning-charge", align="start", size="xs", color="gray")
    run_btn = st.button("开始本地选股", type="primary", use_container_width=True, icon=":material/radar:")
    update_btn = st.button("更新全市场行情缓存", use_container_width=True, icon=":material/cloud_sync:")

    local_status = cache_status()
    sync_mgr = get_auto_sync_manager()
    auto_cfg = get_auto_sync_config()
    auto_toggle = st.toggle(
        f"收盘后自动更新 ({auto_cfg['sync_time']})",
        value=auto_cfg["enabled"],
        help="每个交易日（周一至周五）收盘后自动在后台增量更新一次全市场日线，可在「数据源与缓存中心」自定义时间。",
        key="sb_auto_sync_toggle",
    )
    if auto_toggle != auto_cfg["enabled"]:
        save_auto_sync_config(auto_toggle, auto_cfg["sync_time"])
        st.rerun()

    auto_state = sync_mgr.snapshot(local_status=local_status)
    st.caption(f"自动更新状态：{auto_state['today_status']} · 日常选股全程离线读取 SQLite。")


# ========================== Main Header & Navigation ==========================
_render_hero(local_status, strategies, auto_sync=auto_state)

if auto_state["running"]:
    b1, b2 = st.columns([5, 1], vertical_alignment="center")
    with b1:
        st.progress(min(1.0, max(0.02, auto_state["ratio"])), text=f"{auto_state['text']}（已耗时 {auto_state['elapsed']:.0f}s，不阻塞本地选股）")
    with b2:
        if st.button("刷新进度", use_container_width=True, key="refresh_bg_sync_btn"):
            st.rerun()

valid_params = True
if start >= end:
    sac.alert(label="日期区间无效", description="开始日期必须早于结束日期，请在左侧栏调整日期范围。", color="error", icon=True, variant="light")
    valid_params = False
elif not strategies:
    sac.alert(label="尚未选择策略", description="请在左侧栏至少选择 1 个策略，或在「策略百科」中一键应用流派组合。", color="warning", icon=True, variant="light")
    valid_params = False

if update_btn and valid_params:
    _run_update_action(start, end)
    local_status = cache_status(force=True)
    auto_state = sync_mgr.snapshot(local_status=local_status)

should_scan = run_btn or st.session_state.pop("trigger_scan", False)
if should_scan and valid_params:
    _run_screen_action(
        start, end, strategies, min_change, min_turn, max_turn, max_stocks,
        exclude_st=exclude_st, exclude_gem_star=exclude_gem_star, min_amount=min_amount,
    )

result = st.session_state.get("result")
hit_badge = sac.Tag(f"{len(result)}", color="red", bordered=False) if result is not None and not result.empty else None
watchlist_rows = watchlist_load()
watchlist_codes = {r[0] for r in watchlist_rows}
wl_badge = sac.Tag(f"{len(watchlist_rows)}", color="gold", bordered=False) if watchlist_rows else None

active_tab = sac.tabs(
    items=[
        sac.TabsItem(label="策略选股工作台", icon="graph-up-arrow", tag=hit_badge),
        sac.TabsItem(label="自选观察池", icon="star", tag=wl_badge),
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
        bar_c1, bar_c2, bar_c3, bar_c4 = st.columns([2.1, 1.1, 1.0, 0.8], vertical_alignment="bottom")
        with bar_c1:
            if len(hit_strategies) > 1:
                chip_items = ["全部策略"] + hit_strategies
                if st.session_state.get("result_strategy_chip") not in chip_items:
                    st.session_state["result_strategy_chip"] = "全部策略"
                filter_strategy = sac.chip(
                    items=chip_items,
                    index=0,
                    size="sm",
                    radius="md",
                    variant="light",
                    color="red",
                    key="result_strategy_chip",
                )
            else:
                st.session_state.pop("result_strategy_chip", None)
                filter_strategy = "全部策略"
                sac.tags([sac.Tag(hit_strategies[0], color="red", bordered=False)], size="sm")
        with bar_c2:
            view_mode = sac.segmented(
                items=[
                    sac.SegmentedItem(label="按信号展开"),
                    sac.SegmentedItem(label="按股票聚合"),
                ],
                index=0,
                size="xs",
                radius="md",
                color="red",
                use_container_width=True,
                key="signal_view_mode",
            )
        with bar_c3:
            keyword = st.text_input("快速搜索", placeholder="输入代码或名称过滤…", label_visibility="collapsed")
        with bar_c4:
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
            if view_mode == "按股票聚合":
                agg_rows = []
                for code, grp in filtered.groupby("代码", sort=False):
                    first = grp.iloc[0]
                    strats = grp["策略"].unique().tolist()
                    agg_rows.append({
                        "代码": code,
                        "名称": first["名称"],
                        "命中数": len(strats),
                        "命中策略": " · ".join(strats),
                        "收盘": first["收盘"],
                        "涨跌幅%": first["涨跌幅%"],
                        "近20日走势": sparks.get(code, []),
                        "换手率%": first["换手率%"],
                        "成交额(亿)": first["成交额(亿)"] if "成交额(亿)" in first else float("nan"),
                    })
                display_df = pd.DataFrame(agg_rows).sort_values(
                    ["命中数", "涨跌幅%"], ascending=[False, False]
                ).reset_index(drop=True)
                col_cfg = {
                    "代码": st.column_config.TextColumn("代码", width="small"),
                    "名称": st.column_config.TextColumn("股票名称", width="small"),
                    "命中数": st.column_config.NumberColumn("共振数", format="%d", width="small"),
                    "命中策略": st.column_config.TextColumn("命中策略组合", width="large"),
                    "收盘": st.column_config.NumberColumn("收盘价", format="¥%.2f", width="small"),
                    "涨跌幅%": st.column_config.NumberColumn("涨跌幅", format="%+.2f%%", width="small"),
                    "近20日走势": st.column_config.LineChartColumn("近20日走势", width="medium"),
                    "换手率%": st.column_config.ProgressColumn("换手率", format="%.2f%%", min_value=0, max_value=25, width="small"),
                    "成交额(亿)": st.column_config.NumberColumn("成交额", format="%.2f亿", width="small"),
                }
            else:
                display_df = filtered.copy()
                display_df.insert(5, "近20日走势", display_df["代码"].map(lambda c: sparks.get(c, [])))
                col_cfg = {
                    "策略": st.column_config.TextColumn("命中策略", width="medium"),
                    "代码": st.column_config.TextColumn("代码", width="small"),
                    "名称": st.column_config.TextColumn("股票名称", width="small"),
                    "收盘": st.column_config.NumberColumn("收盘价", format="¥%.2f", width="small"),
                    "涨跌幅%": st.column_config.NumberColumn("涨跌幅", format="%+.2f%%", width="small"),
                    "近20日走势": st.column_config.LineChartColumn("近20日走势", width="medium"),
                    "换手率%": st.column_config.ProgressColumn("换手率", format="%.2f%%", min_value=0, max_value=25, width="small"),
                    "成交额(亿)": st.column_config.NumberColumn("成交额", format="%.2f亿", width="small"),
                    "信号说明": st.column_config.TextColumn("触发逻辑", width="large"),
                }

            if mobile_compact:
                keep_cols = (
                    ["代码", "名称", "命中数", "收盘", "涨跌幅%"]
                    if view_mode == "按股票聚合"
                    else ["代码", "名称", "收盘", "涨跌幅%", "策略"]
                )
                table_df = display_df[[c for c in keep_cols if c in display_df.columns]]
            else:
                table_df = display_df

            styled_df = table_df.style.map(_style_pct, subset=["涨跌幅%"])
            table_event = st.dataframe(
                styled_df,
                use_container_width=True,
                hide_index=True,
                height=280 if mobile_compact else 340,
                on_select="rerun",
                selection_mode="single-row",
                key=f"signal_table_{view_mode}_{mobile_compact}",
                column_config=col_cfg,
            )

            # ---- Optional Historical Forward-Return Quick Check ----
            scan_end_dt = st.session_state.get("range", (start, end))[1]
            scan_end_str = scan_end_dt.strftime("%Y-%m-%d")
            newest_db_str = str(local_status.get("newest") or "")
            if newest_db_str and scan_end_str < newest_db_str:
                fwd_df = compute_forward_returns(filtered, scan_end_str)
                with st.expander(f"📊 历史胜率速验（信号日 {scan_end_str} 后 5 / 10 个交易日表现）", expanded=not fwd_df.empty):
                    if fwd_df.empty:
                        st.caption("信号日之后的本地交易日不足 5 天，暂无法统计后续收益。")
                    else:
                        st.dataframe(
                            fwd_df,
                            use_container_width=True,
                            hide_index=True,
                            column_config={
                                "5日胜率%": st.column_config.NumberColumn("5日胜率", format="%.1f%%"),
                                "5日均涨幅%": st.column_config.NumberColumn("5日均涨幅", format="%+.2f%%"),
                                "10日胜率%": st.column_config.NumberColumn("10日胜率", format="%.1f%%"),
                                "10日均涨幅%": st.column_config.NumberColumn("10日均涨幅", format="%+.2f%%"),
                            },
                        )
            else:
                st.caption("💡 提示：将左侧「结束日期」调至历史某交易日后重新选股，可在此自动统计命中标的后续 5 日 / 10 日胜率与平均收益。")

            # ---- Section B: Stock Technical Analysis Terminal ----
            stocks_df = display_df[["代码", "名称"]].drop_duplicates("代码")
            name_by_code = dict(stocks_df.itertuples(index=False, name=None))
            stock_codes = list(name_by_code.keys())

            # Row click selection takes precedence when changed
            selected_rows = table_event.selection.rows if table_event and hasattr(table_event, "selection") else []
            if selected_rows and selected_rows[0] < len(display_df):
                clicked_code = display_df.iloc[selected_rows[0]]["代码"]
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

            nav1, nav2, nav3, nav_wl, nav4 = st.columns([2.2, 0.55, 0.55, 0.85, 2.0], vertical_alignment="bottom")
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
            with nav_wl:
                cur_code = st.session_state["active_stock"]
                in_wl = cur_code in watchlist_codes
                if st.button("★ 已自选" if in_wl else "☆ 加自选", use_container_width=True, key=f"wl_toggle_{cur_code}"):
                    if in_wl:
                        watchlist_remove(cur_code)
                        st.toast(f"已移出自选：{name_by_code[cur_code]} ({cur_code})")
                    else:
                        watchlist_add(cur_code, name_by_code[cur_code])
                        st.toast(f"已加入自选：{name_by_code[cur_code]} ({cur_code})", icon="⭐")
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
            chart_fetch_start = start_date - timedelta(days=150)
            frame = _cache_read(choice, chart_fetch_start.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))

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

                panes, kdf = chart_panes(
                    frame, f"{name_by_code[choice]} · {choice}", interval,
                    show_ma=show_ma, show_macd=show_macd, display_start=start_date,
                )
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

                chart_height = (460 if show_macd else 360) if mobile_compact else (705 if show_macd else 565)
                lightweight_charts_v5_component(
                    name=f"{name_by_code[choice]} {choice}",
                    charts=panes,
                    height=chart_height,
                    zoom_level=65 if mobile_compact else 85,
                    key=f"chart_{choice}_{interval}_{show_ma}_{show_macd}_{mobile_compact}",
                )


# ========================== Tab 2: 自选观察池 ==========================
elif active_tab == "自选观察池":
    st.markdown("""
    <div class="sec-head">
        <div class="sec-title"><span class="sec-dot"></span>本地自选股观察池</div>
        <div class="sec-meta">持久化保存在本地 stock_cache.sqlite3，点击表格行可联动下方 K 线图复盘</div>
    </div>
    """, unsafe_allow_html=True)

    if not watchlist_rows:
        sac.alert(
            label="自选观察池为空",
            description="在「策略选股工作台」选中任意个股后，点击「☆ 加自选」即可加入此处跟踪复盘。",
            color="info",
            icon=True,
        )
    else:
        wl_codes_list = [r[0] for r in watchlist_rows]
        _init_cache()
        with sqlite3.connect(CACHE_DB, timeout=30) as conn:
            conn.execute("CREATE TEMP TABLE wl_tmp (code TEXT PRIMARY KEY)")
            conn.executemany("INSERT OR IGNORE INTO wl_tmp VALUES (?)", ((c,) for c in wl_codes_list))
            latest_map = {
                row[0]: row[1:]
                for row in conn.execute(
                    """
                    WITH mx AS (
                        SELECT w.code, MAX(d.date) AS last_d
                        FROM wl_tmp w LEFT JOIN daily d ON d.code = w.code
                        GROUP BY w.code
                    )
                    SELECT m.code, d.date, d.close, d.pctChg, d.turn, d.amount
                    FROM mx m LEFT JOIN daily d ON d.code = m.code AND d.date = m.last_d
                    """
                )
            }
        wl_data = []
        for code, name, added_at in watchlist_rows:
            dt_s, cl, pct, trn, amt = latest_map.get(code, (None, None, None, None, None))
            wl_data.append({
                "代码": code,
                "名称": name,
                "最新日期": dt_s or "—",
                "收盘": float(cl) if cl is not None else float("nan"),
                "涨跌幅%": float(pct) if pct is not None else float("nan"),
                "换手率%": float(trn) if trn is not None else float("nan"),
                "成交额(亿)": round(float(amt) / 1e8, 2) if amt is not None else float("nan"),
                "加入时间": added_at,
            })
        wl_df = pd.DataFrame(wl_data)
        wl_event = st.dataframe(
            wl_df.style.map(_style_pct, subset=["涨跌幅%"]),
            use_container_width=True,
            hide_index=True,
            height=280,
            on_select="rerun",
            selection_mode="single-row",
            key="watchlist_table",
            column_config={
                "收盘": st.column_config.NumberColumn("最新收盘", format="¥%.2f"),
                "涨跌幅%": st.column_config.NumberColumn("涨跌幅", format="%+.2f%%"),
                "换手率%": st.column_config.NumberColumn("换手率", format="%.2f%%"),
                "成交额(亿)": st.column_config.NumberColumn("成交额", format="%.2f亿"),
            },
        )
        wl_sel = wl_event.selection.rows if wl_event and hasattr(wl_event, "selection") else []
        if wl_sel and wl_sel[0] < len(wl_df):
            st.session_state["wl_active_stock"] = wl_df.iloc[wl_sel[0]]["代码"]
        wl_name_map = {r[0]: r[1] for r in watchlist_rows}
        if st.session_state.get("wl_active_stock") not in wl_name_map:
            st.session_state["wl_active_stock"] = wl_codes_list[0]

        wc1, wc2, wc3 = st.columns([2.2, 0.9, 1.9], vertical_alignment="bottom")
        with wc1:
            wl_choice = st.selectbox(
                "选择自选标的",
                options=wl_codes_list,
                index=wl_codes_list.index(st.session_state["wl_active_stock"]),
                format_func=lambda c: f"{wl_name_map[c]} ({c})",
                label_visibility="collapsed",
                key="wl_stock_select",
            )
            st.session_state["wl_active_stock"] = wl_choice
        with wc2:
            if st.button("移出自选", use_container_width=True, key="wl_remove_btn"):
                watchlist_remove(wl_choice)
                st.toast(f"已移出自选：{wl_name_map[wl_choice]} ({wl_choice})")
                st.rerun()
        with wc3:
            wl_interval = sac.segmented(
                items=[
                    sac.SegmentedItem(label="日线", icon="bar-chart-line"),
                    sac.SegmentedItem(label="周线", icon="calendar-week"),
                    sac.SegmentedItem(label="月线", icon="calendar-month"),
                ],
                index=0,
                size="sm",
                radius="md",
                color="red",
                use_container_width=True,
                key="wl_interval_seg",
            )
        wl_frame = _cache_read(wl_choice, (start - timedelta(days=150)).strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        if not wl_frame.empty:
            wl_panes, _ = chart_panes(wl_frame, f"{wl_name_map[wl_choice]} · {wl_choice}", wl_interval, show_ma=True, show_macd=True, display_start=start)
            lightweight_charts_v5_component(
                name=f"wl {wl_name_map[wl_choice]} {wl_choice}",
                charts=wl_panes,
                height=440 if mobile_compact else 640,
                zoom_level=65 if mobile_compact else 85,
                key=f"wl_chart_{wl_choice}_{wl_interval}_{mobile_compact}",
            )


# ========================== Tab 3: 策略百科矩阵 ==========================
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
        <div class="sec-title"><span class="sec-dot"></span>收盘后自动更新配置</div>
        <div class="sec-meta">每个交易日（周一至周五）收盘后后台自动增量更新一次，当天仅执行一次，绝不重复拉取</div>
    </div>
    """, unsafe_allow_html=True)

    ac1, ac2, ac3, ac4 = st.columns([1.2, 1.3, 1.8, 1.3], vertical_alignment="bottom")
    with ac1:
        tab_enabled = st.toggle("启用收盘后自动更新", value=auto_state["enabled"], key="tab_auto_sync_toggle")
    with ac2:
        time_idx = AUTO_SYNC_TIMES.index(auto_state["sync_time"]) if auto_state["sync_time"] in AUTO_SYNC_TIMES else 2
        tab_time = st.selectbox("每日收盘后触发时间", options=AUTO_SYNC_TIMES, index=time_idx, key="tab_auto_sync_time")
    if tab_enabled != auto_state["enabled"] or tab_time != auto_state["sync_time"]:
        save_auto_sync_config(tab_enabled, tab_time)
        st.toast(f"已保存自动更新设置：{'开启' if tab_enabled else '关闭'}（每日 {tab_time}）", icon="✅")
        st.rerun()
    with ac3:
        st.info(f"今日状态：**{auto_state['today_status']}**（上次自动更新日：{auto_state['last_date'] or '暂无'}）")
    with ac4:
        if st.button("立即后台更新一次", use_container_width=True, disabled=auto_state["running"], icon=":material/sync:"):
            if sync_mgr.trigger_now():
                st.toast("已启动后台增量更新任务，可在顶部查看实时进度", icon="🚀")
                st.rerun()
            else:
                st.warning("当前已有同步任务正在运行中。")

    last_auto = auto_state.get("last_info") or {}
    if last_auto:
        if last_auto.get("status") == "error":
            sac.alert(
                label=f"最近一次自动更新失败（{last_auto.get('time', '')}）",
                description=f"原因：{last_auto.get('error', '未知错误')}（后台将在 30 分钟后自动重试，也可手动重试）",
                color="error",
                icon=True,
            )
        else:
            sac.alert(
                label=f"最近一次同步记录：{last_auto.get('trigger', '自动更新')} · {last_auto.get('time', '')}（通道：{last_auto.get('source', 'Baostock')}）",
                description=f"新增/更新 {last_auto.get('updated', 0)} 只 · 跳过已缓存 {last_auto.get('skipped', 0)} 只 · 失败 {last_auto.get('failed', 0)} 只",
                color="warning" if last_auto.get("failed") else "success",
                icon=True,
            )

    st.markdown("""
    <div class="sec-head">
        <div class="sec-title"><span class="sec-dot"></span>三级行情数据源与容灾链路</div>
        <div class="sec-meta">自动故障切换，缓存落盘至本地 stock_cache.sqlite3，支持跨机拷贝复用</div>
    </div>
    <div class="pipe-grid">
        <div class="pipe-card">
            <div class="pipe-step">STAGE 01 · BATCH SNAPSHOT</div>
            <div class="pipe-title">腾讯批量截面极速日更 + 除权校验</div>
            <div class="pipe-desc">日常收盘后仅需 18 次并发请求（每次 300 只）即可秒级补齐全市场 OHLCV 与换手率，自动比对昨收价识别除权并触发重拉。</div>
        </div>
        <div class="pipe-card">
            <div class="pipe-step">STAGE 02 · SMART PRECHECK</div>
            <div class="pipe-title">交易日预检 + 股票池 7 天缓存</div>
            <div class="pipe-desc">更新前先探测上证指数最近已收盘交易日，盘中/休市不误写、不空跑；全市场证券列表缓存 7 天复用，告别反复全量查询。</div>
        </div>
        <div class="pipe-card">
            <div class="pipe-step">STAGE 03 · 10x POOLED BACKFILL</div>
            <div class="pipe-title">东方财富 / 腾讯连接池回补 + Baostock 兜底</div>
            <div class="pipe-desc">多日缺口或除权标的走 10 路连接池并发拉取前复权历史（每 100 只批量写库），残余异常项自动由 Baostock 兜底补齐。</div>
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
