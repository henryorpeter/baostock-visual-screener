"""全市场行情同步引擎：交易日预检、腾讯批量截面、东财/腾讯/新浪三级并发回补、Baostock 兜底与收盘自动调度。"""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
import json
import re
import socket
import threading
import time
import requests
from requests.adapters import HTTPAdapter

import baostock as bs
import numpy as np
import pandas as pd
import streamlit as st

import db


BAOSTOCK_AVAILABLE = None
SYNC_LOCK = threading.Lock()
_BAOSTOCK_LOCK = threading.Lock()
_THREAD_LOCAL = threading.local()


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



def _rows(result):
    if result.error_code != "0":
        raise RuntimeError(f"Baostock 错误 {result.error_code}: {result.error_msg}")
    rows = []
    while result.next():
        rows.append(result.get_row_data())
    return pd.DataFrame(rows, columns=result.fields)


def _baostock_reachable(timeout=3.0):
    try:
        with socket.create_connection(("public-api.baostock.com", 10030), timeout=timeout):
            return True
    except OSError:
        return False


def _query(fn):
    global BAOSTOCK_AVAILABLE
    if not _baostock_reachable(timeout=3.0):
        BAOSTOCK_AVAILABLE = False
        raise RuntimeError(
            "Baostock 服务不可达（public-api.baostock.com:10030 连接超时），已自动跳过。"
        )
    last = None
    with _BAOSTOCK_LOCK:
        prev_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(10.0)
        try:
            for attempt in range(2):
                try:
                    login = bs.login()
                except Exception as exc:
                    last = type("Err", (), {"error_code": "timeout", "error_msg": str(exc)})()
                    login = None
                if login is not None and login.error_code == "0":
                    BAOSTOCK_AVAILABLE = True
                    try:
                        return fn()
                    finally:
                        try:
                            bs.logout()
                        except Exception:
                            pass
                if login is not None:
                    last = login
                try:
                    bs.logout()
                except Exception:
                    pass
                if attempt < 1:
                    time.sleep(1.0)
        finally:
            socket.setdefaulttimeout(prev_timeout)
    raise RuntimeError(
        f"登录失败 {last.error_code}: {last.error_msg}。"
        "这是 Baostock 网络连接错误，请检查 public-api.baostock.com:10030 是否可访问后重试。"
    )


def _https_json(url, params, referer, timeout=12, retries=3):
    error = None
    session = _http_session()
    for attempt in range(retries):
        try:
            response = session.get(url, params=params, headers={"Referer": referer}, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            error = exc
            if attempt < retries - 1:
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
            # 腾讯快照第 40 位为交易所状态位：'D'=已退市，'S'=临时停牌，''=正常交易；仅剔除退市股，保留停牌股
            if len(fields) > 40 and fields[40].strip() == "D":
                continue
            if len(fields) > 48 and fields[47] == "-1" and fields[48] == "-1":
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
        code, db.DAILY_FIELDS, start_date=start, end_date=end,
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


_KLINE_EXT_LOCK = threading.Lock()
_KLINE_EXT_CACHE = OrderedDict()
_KLINE_EXT_OFFLINE_UNTIL = 0.0
_KLINE_EXT_TTL = 900.0  # 15 分钟内存缓存
_KLINE_PERIOD_MAP = {
    "日线": ("day", 800),    # 腾讯 fqkline 上限约 800 根（约 3.3 年日线；>800 会被服务端回退到 640）
    "周线": ("week", 520),   # 约 10 年周线
    "月线": ("month", 240),  # 约 20 年月线
}


def fetch_kline_history(code, interval="日线", end_str=None):
    """按需拉取单只股票多年期前复权日线/周线/月线（腾讯 fqkline），断网或失败返回 None 回退本地 SQLite。"""
    global _KLINE_EXT_OFFLINE_UNTIL
    if not isinstance(code, str) or not re.fullmatch(r"(?:sh|sz)\.\d{6}", code):
        return None
    market, symbol = code.split(".", 1)
    if interval not in _KLINE_PERIOD_MAP:
        return None

    now = time.monotonic()
    period_key, count = _KLINE_PERIOD_MAP[interval]
    cache_key = (code, interval)
    with _KLINE_EXT_LOCK:
        cached = _KLINE_EXT_CACHE.get(cache_key)
        if cached is not None and (now - cached[0]) < _KLINE_EXT_TTL:
            _KLINE_EXT_CACHE.move_to_end(cache_key)
            base_df = cached[1]
        elif now < _KLINE_EXT_OFFLINE_UNTIL:
            return None
        else:
            base_df = None

    if base_df is None:
        sym = f"{market}{symbol}"
        try:
            payload = _https_json(
                "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                {"param": f"{sym},{period_key},,,{count},qfq"},
                "https://gu.qq.com/",
                timeout=4,
                retries=1,
            )
            node = (payload.get("data") or {}).get(sym) or {}
            raw_rows = node.get(f"qfq{period_key}") or node.get(period_key) or []
            rows = [r[:6] for r in raw_rows if len(r) >= 6]
            if not rows:
                return None
            df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume"])
            for col in ("open", "high", "low", "close", "volume"):
                df[col] = pd.to_numeric(df[col], errors="coerce")
            if not symbol.startswith(("688", "689")):
                df["volume"] = df["volume"] * 100.0
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df = df.dropna(subset=["date", "open", "high", "low", "close"]).sort_values("date").reset_index(drop=True)
            if df.empty:
                return None
            base_df = df
            with _KLINE_EXT_LOCK:
                _KLINE_EXT_CACHE[cache_key] = (now, base_df)
                _KLINE_EXT_CACHE.move_to_end(cache_key)
                while len(_KLINE_EXT_CACHE) > 128:
                    _KLINE_EXT_CACHE.popitem(last=False)
        except Exception:
            with _KLINE_EXT_LOCK:
                _KLINE_EXT_OFFLINE_UNTIL = time.monotonic() + 60.0
            return None

    out = base_df
    if end_str:
        out = out[out["date"] <= pd.Timestamp(end_str)].reset_index(drop=True)
    return out if not out.empty else None


_INTRADAY_LOCK = threading.Lock()
_INTRADAY_CACHE = OrderedDict()
_INTRADAY_OFFLINE_UNTIL = 0.0
_INTRADAY_TTL = 3.0  # 3 秒实时缓存，配合前端 5 秒原地刷新，贴近 A 股 3 秒快照且防限流


def fetch_intraday_minute(code):
    """按需拉取单只 A 股当日实时分时（价格/均价VWAP/分时量）与盘口快照（腾讯 minute/query）。"""
    global _INTRADAY_OFFLINE_UNTIL
    if not isinstance(code, str) or not re.fullmatch(r"(?:sh|sz)\.\d{6}", code):
        return None
    market, symbol = code.split(".", 1)
    sym = f"{market}{symbol}"

    now = time.monotonic()
    with _INTRADAY_LOCK:
        cached = _INTRADAY_CACHE.get(code)
        if cached is not None and (now - cached[0]) < _INTRADAY_TTL:
            _INTRADAY_CACHE.move_to_end(code)
            return cached[1]
        if now < _INTRADAY_OFFLINE_UNTIL:
            return None

    try:
        payload = _https_json(
            "https://web.ifzq.gtimg.cn/appstock/app/minute/query",
            {"code": sym},
            "https://gu.qq.com/",
            timeout=4,
            retries=1,
        )
    except Exception:
        with _INTRADAY_LOCK:
            _INTRADAY_OFFLINE_UNTIL = time.monotonic() + 30.0
        return None

    node = (payload.get("data") or {}).get(sym) or {}
    mnode = node.get("data") or {}
    raw_date = str(mnode.get("date") or "").strip()
    if len(raw_date) == 8 and raw_date.isdigit():
        trade_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
    else:
        trade_date = date.today().strftime("%Y-%m-%d")

    qt = (node.get("qt") or {}).get(sym) or []

    def _qt_float(idx):
        if len(qt) <= idx:
            return None
        try:
            v = float(qt[idx])
            return v if np.isfinite(v) else None
        except (TypeError, ValueError):
            return None

    qt_name = str(qt[1]).strip() if len(qt) > 1 and qt[1] else ""
    price_now = _qt_float(3)
    prev_close = _qt_float(4)
    open_p = _qt_float(5)
    pct_chg = _qt_float(32)
    high_p = _qt_float(33)
    low_p = _qt_float(34)
    amt_wan = _qt_float(37)
    turn = _qt_float(38)
    raw_ts = str(qt[30]).strip() if len(qt) > 30 and qt[30] else ""
    quote_time = f"{raw_ts[8:10]}:{raw_ts[10:12]}:{raw_ts[12:14]}" if len(raw_ts) >= 14 else ""

    # 科创板(688/689)成交量单位已是股，其余板块单位为手(×100转股)
    vol_mult = 1.0 if symbol.startswith(("688", "689")) else 100.0
    raw_mins = mnode.get("data") or []
    points = []
    prev_cum_shares = 0.0
    for row in raw_mins:
        parts = str(row).split()
        if len(parts) < 3:
            continue
        hhmm_raw = parts[0]
        if len(hhmm_raw) != 4 or not hhmm_raw.isdigit():
            continue
        try:
            px = float(parts[1])
            cum_vol_raw = float(parts[2])
            cum_amt = float(parts[3]) if len(parts) >= 4 else None
        except ValueError:
            continue
        if px <= 0:
            continue
        cum_shares = cum_vol_raw * vol_mult
        bar_vol = max(0.0, cum_shares - prev_cum_shares)
        prev_cum_shares = cum_shares
        avg_px = px
        if cum_amt is not None and cum_shares > 0:
            cand = cum_amt / cum_shares
            if 0.5 * px <= cand <= 2.0 * px:
                avg_px = cand
        pct_pt = ((px / prev_close) - 1.0) * 100.0 if prev_close and prev_close > 0 else 0.0
        points.append({
            "hhmm": f"{hhmm_raw[:2]}:{hhmm_raw[2:]}",
            "price": round(px, 3),
            "avg": round(avg_px, 3),
            "vol": round(bar_vol, 0),
            "pct": round(pct_pt, 2),
        })

    if not points and price_now is None:
        return None

    if price_now is None and points:
        price_now = points[-1]["price"]
    if prev_close is None or prev_close <= 0:
        prev_close = open_p or price_now
    if open_p is None and points:
        open_p = points[0]["price"]
    if high_p is None and points:
        high_p = max(p["price"] for p in points)
    if low_p is None and points:
        low_p = min(p["price"] for p in points)
    if pct_chg is None and prev_close and price_now:
        pct_chg = (price_now / prev_close - 1.0) * 100.0
    amp = ((high_p - low_p) / prev_close * 100.0) if (high_p is not None and low_p is not None and prev_close) else 0.0
    amount_yuan = amt_wan * 10000.0 if amt_wan is not None else (prev_cum_shares * (price_now or 0.0))

    now_dt = datetime.now()
    today_s = now_dt.strftime("%Y-%m-%d")
    hm = now_dt.strftime("%H:%M")
    is_live = (trade_date == today_s) and (now_dt.weekday() < 5) and ("09:15" <= hm <= "15:05")

    res = {
        "code": code,
        "name": qt_name,
        "date": trade_date,
        "quote_time": quote_time or (points[-1]["hhmm"] if points else ""),
        "is_live": is_live,
        "prev_close": round(prev_close, 3) if prev_close is not None else None,
        "quote": {
            "date": trade_date,
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": price_now,
            "pctChg": pct_chg,
            "amp": amp,
            "turn": turn,
            "amount": amount_yuan,
            "volume": prev_cum_shares,
            "is_live": is_live,
            "quote_time": quote_time or (points[-1]["hhmm"] if points else ""),
        },
        "points": points,
    }
    with _INTRADAY_LOCK:
        _INTRADAY_CACHE[code] = (now, res)
        _INTRADAY_CACHE.move_to_end(code)
        while len(_INTRADAY_CACHE) > 64:
            _INTRADAY_CACHE.popitem(last=False)
    return res


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


def _history_sina(code, start, end, name=""):
    """新浪财经前复权日线备用源（独立于东财/腾讯，成交量单位为股，叠加 qfq.js 前复权因子）。"""
    market, symbol = code.split(".")
    sym = f"{market}{symbol}"
    days = min(1023, max(120, (pd.Timestamp(end) - pd.Timestamp(start)).days + 45))
    session = _http_session()
    resp = session.get(
        f"https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_{sym}=/CN_MarketDataService.getKLineData",
        params={"symbol": sym, "scale": "240", "ma": "no", "datalen": str(days)},
        headers={"Referer": "https://finance.sina.com.cn/"},
        timeout=12,
    )
    resp.raise_for_status()
    m = re.search(r"\((\[.*\])\)", resp.text, re.S)
    if not m:
        raise RuntimeError(f"新浪日线接口未返回 {code} 数据")
    bars = json.loads(m.group(1))
    if not bars:
        raise RuntimeError(f"新浪日线返回空列表: {code}")

    # 拉取前复权因子（按日期降序，价格 / 对应区间因子 = 前复权价）
    qfq_factors = []
    try:
        qf_resp = session.get(
            f"https://finance.sina.com.cn/realstock/company/{sym}/qfq.js",
            headers={"Referer": "https://finance.sina.com.cn/"},
            timeout=8,
        )
        if qf_resp.status_code == 200:
            qm = re.search(r"(\{.*\})", qf_resp.text, re.S)
            if qm:
                raw_data = json.loads(qm.group(1)).get("data") or []
                for item in raw_data:
                    d_s, f_s = item.get("d"), item.get("f")
                    if d_s and f_s:
                        fv = float(f_s)
                        if fv > 0:
                            qfq_factors.append((str(d_s), fv))
                qfq_factors.sort(key=lambda x: x[0], reverse=True)
    except Exception:
        qfq_factors = []

    def _factor_for(d_str):
        for fd, fv in qfq_factors:
            if d_str >= fd:
                return fv
        return qfq_factors[-1][1] if qfq_factors else 1.0

    parsed = []
    is_st = "1" if "ST" in (name or "").upper() else "0"
    for bar in bars:
        d_str = str(bar.get("day", ""))[:10]
        if not d_str:
            continue
        try:
            op = float(bar["open"])
            hi = float(bar["high"])
            lo = float(bar["low"])
            cl = float(bar["close"])
            vol = float(bar["volume"])
        except (KeyError, ValueError, TypeError):
            continue
        fac = _factor_for(d_str)
        if fac != 1.0:
            op, hi, lo, cl = op / fac, hi / fac, lo / fac, cl / fac
        parsed.append({
            "date": d_str,
            "code": code,
            "open": round(op, 4),
            "high": round(hi, 4),
            "low": round(lo, 4),
            "close": round(cl, 4),
            "volume": vol,
            "amount": cl * vol,
            "turn": float("nan"),
            "isST": is_st,
        })
    if not parsed:
        raise RuntimeError(f"新浪日线解析为空: {code}")
    frame = pd.DataFrame(parsed).sort_values("date")
    frame["pctChg"] = frame["close"].pct_change().mul(100.0)
    frame = frame[(frame["date"] >= start) & (frame["date"] <= end)].copy()
    if not frame.empty:
        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame.dropna(subset=["close"]).sort_values("date")
    return frame



def _get_universe_for_sync(day_str, force=False):
    cached = db.cache_universe_load()
    updated_date = db._setting_get("stock_list_updated_date", "")
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
    db.cache_universe_save(stocks)
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

    if not days:
        # 快速回退：探测 qt.gtimg.cn 截面日期并结合本地 SQLite 已有交易日，避免被 ifzq 限流拖进慢速 Baostock
        snap_d = _probe_snapshot_date()
        local_days = set()
        if snap_d and snap_d <= end_str:
            local_days.add(snap_d)
        try:
            db._init_cache()
            with db.connect(timeout=10) as conn:
                for (d_str,) in conn.execute(
                    "SELECT DISTINCT date FROM daily WHERE date <= ? ORDER BY date DESC LIMIT 5",
                    (end_str,),
                ):
                    if d_str:
                        local_days.add(str(d_str))
        except Exception:
            pass
        days = sorted(local_days)

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
            status_flag = parts[40].strip() if len(parts) > 40 else ""
            is_delisted = (status_flag == "D") or (len(parts) > 48 and parts[47] == "-1" and parts[48] == "-1")
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
                "status": status_flag,
                "is_delisted": is_delisted,
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
    db._init_cache()
    if progress is not None:
        progress.progress(0.02, text="正在核验最新已收盘交易日…")
    effective_end, prev_trade_day = _recent_trade_days(end)
    if effective_end < start:
        return {
            "updated": 0, "skipped": len(codes), "failed": 0,
            "failures": [], "source": "无需更新（暂无已收盘数据）",
            "effective_end": effective_end, "snapshot_updated": 0, "backfill_updated": 0, "ex_div": 0,
        }

    with db.connect(timeout=30) as conn:
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
                SELECT a.code, a.first_d, a.last_d, d.close, d.turn, m.start_date, m.through_date
                FROM agg a
                LEFT JOIN daily d ON d.code = a.code AND d.date = a.last_d
                LEFT JOIN sync_meta m ON m.code = a.code
                """
            )
        }

    skipped = 0
    snapshot_candidates = []
    backfill_tasks = []
    turn_patch_codes = []
    for code, name in codes:
        first, last, last_close, last_turn, synced_start, synced_through = state.get(
            code, (None, None, None, None, None, None)
        )
        # 新股上市日期可能晚于 start、停牌股最后交易日可能早于 effective_end，借助 sync_meta 避免反复回补
        start_covered = bool((first and first <= start) or (synced_start and synced_start <= start))
        end_covered = bool((last and last >= effective_end) or (synced_through and synced_through >= effective_end))
        if start_covered and end_covered:
            if last == effective_end and last_turn is None:
                turn_patch_codes.append((code, name))
            else:
                skipped += 1
            continue
        if start_covered and prev_trade_day and last == prev_trade_day:
            snapshot_candidates.append((code, name, last_close))
        else:
            fetch_start = start
            if start_covered and last:
                fetch_start = max(start, (pd.Timestamp(last) - pd.Timedelta(days=7)).strftime("%Y-%m-%d"))
            backfill_tasks.append((code, name, fetch_start))

    successful, changed, failures = set(), set(), {}
    snapshot_updated, backfill_updated, ex_div_count = 0, 0, 0
    used_snapshot = False
    snap_date = None
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
                db._cache_write(pd.DataFrame(snap_rows))
            if name_updates:
                with db.connect(timeout=30) as conn:
                    conn.executemany("INSERT OR REPLACE INTO stock_names(code, name) VALUES (?, ?)", name_updates)
        else:
            # 盘中时段快照已切换至今日，而 effective_end 为前一收盘日：转入快速并发历史回补最近几日
            recent_start = max(start, (pd.Timestamp(prev_trade_day) - pd.Timedelta(days=7)).strftime("%Y-%m-%d"))
            for code, name, _ in snapshot_candidates:
                backfill_tasks.append((code, name, recent_start))

    # 清理本地股票池里历史遗留的已退市僵尸股（仅检查无日线或严重滞后标的，1~2 次批量请求即可完成）
    no_data_tasks = [(code, name) for code, name, _ in backfill_tasks if state.get(code, (None, None, None, None))[1] is None]
    if no_data_tasks:
        probe_snaps = _fetch_tencent_snapshots(no_data_tasks, progress=None)
        delisted_codes = {c for c, _ in no_data_tasks if (probe_snaps.get(c) or {}).get("is_delisted")}
        if delisted_codes:
            backfill_tasks = [t for t in backfill_tasks if t[0] not in delisted_codes]
            skipped += len(delisted_codes)
            with db.connect(timeout=30) as conn:
                conn.executemany("DELETE FROM stock_names WHERE code=?", ((c,) for c in delisted_codes))
            db._invalidate_status_cache()

    # 2. 连接池并发历史回补：东方财富 -> 腾讯 fqkline -> 新浪前复权 三级 HTTP 容灾，每 100 只批量写库
    if backfill_tasks:
        em_lock = threading.Lock()
        em_fail_streak = [0]
        tx_fail_streak = [0]
        sn_fail_streak = [0]
        total_bf = len(backfill_tasks)
        base_p = snap_p_end if used_snapshot else 0.05

        def _fetch_one_backfill(task):
            code, name, fetch_start = task
            with em_lock:
                skip_em = em_fail_streak[0] >= 12
                skip_tx = tx_fail_streak[0] >= 12
                skip_sn = sn_fail_streak[0] >= 12
            if not skip_em:
                try:
                    frame = _history_eastmoney(code, fetch_start, effective_end, name=name)
                    with em_lock:
                        em_fail_streak[0] = 0
                    return frame
                except Exception:
                    with em_lock:
                        em_fail_streak[0] += 1
            if not skip_tx:
                try:
                    frame = _history_http(code, fetch_start, effective_end, name=name)
                    with em_lock:
                        tx_fail_streak[0] = 0
                    return frame
                except Exception:
                    with em_lock:
                        tx_fail_streak[0] += 1
            if skip_sn:
                raise RuntimeError("东财/腾讯/新浪历史接口均已触发熔断")
            try:
                frame = _history_sina(code, fetch_start, effective_end, name=name)
                with em_lock:
                    sn_fail_streak[0] = 0
                return frame
            except Exception:
                with em_lock:
                    sn_fail_streak[0] += 1
                raise

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
                        if frame["turn"].isna().any():
                            turn_patch_codes.append((code, name))
                    successful.add(code)
                    failures.pop(code, None)
                except Exception as exc:
                    failures[code] = str(exc)

                if len(batch_frames) >= 100:
                    db._cache_write(pd.concat(batch_frames, ignore_index=True))
                    batch_frames.clear()
                if progress is not None and (i % 10 == 0 or i == total_bf):
                    ratio = base_p + (0.95 - base_p) * (i / total_bf)
                    progress.progress(ratio, text=f"并发回补历史 {i}/{total_bf}（10 路连接池）")
        if batch_frames:
            db._cache_write(pd.concat(batch_frames, ignore_index=True))

    # 3. 补齐缺失换手率：若回补降级到腾讯 fqkline（无换手率）或库中最新日换手率为空，用腾讯批量快照极速补齐
    turn_patched = 0
    if turn_patch_codes:
        dedup_patch = list({c: n for c, n in turn_patch_codes}.items())
        snaps = _fetch_tencent_snapshots(dedup_patch, progress=progress, progress_range=(0.90, 0.96))
        exact_updates = []
        hist_updates = []
        for code, _ in dedup_patch:
            snap = snaps.get(code)
            if not snap:
                continue
            turn_v = snap["turn"]
            if np.isnan(turn_v) or turn_v <= 0 or snap["volume"] <= 0:
                continue
            if snap["date"] == effective_end:
                exact_updates.append((
                    turn_v, snap["amount"], snap["amount"], snap["pctChg"], code, effective_end,
                ))
            # shortcut: 历史缺失换手率按当日快照反推当前流通股本估算，若期间发生定增/解禁存在轻微偏差，仅补填 NULL 不覆盖真实值
            float_shares = snap["volume"] / (turn_v / 100.0)
            if float_shares > 0:
                hist_updates.append((float_shares, code))
        if exact_updates or hist_updates:
            with db.connect(timeout=30) as conn:
                if exact_updates:
                    conn.executemany(
                        "UPDATE daily SET turn = ?, amount = CASE WHEN ? > 0 THEN ? ELSE amount END, pctChg = ? "
                        "WHERE code = ? AND date = ?",
                        exact_updates,
                    )
                if hist_updates:
                    conn.executemany(
                        "UPDATE daily SET turn = ROUND(volume / ? * 100.0, 2) "
                        "WHERE code = ? AND turn IS NULL AND volume > 0",
                        hist_updates,
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO app_settings(key, value) VALUES ('cache_rev', ?)",
                    (str(int(time.time() * 1000)),),
                )
            db._invalidate_status_cache()
            turn_patched = max(len(exact_updates), len(hist_updates))
            used_snapshot = True
            for _, _, _, _, c, _ in exact_updates:
                changed.add(c)
            for _, c in hist_updates:
                changed.add(c)
        else:
            skipped += len(dedup_patch)

    # 4. 仅针对 HTTPS 均失败的个别残余标的启用 Baostock 兜底（先 3 秒探活避免卡死）
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
                    db._cache_write(pd.concat(batch, ignore_index=True))
                    batch.clear()
                if progress is not None and (i % 10 == 0 or i == tot_rem):
                    progress.progress(0.96 + 0.04 * (i / max(1, tot_rem)), text=f"Baostock 兜底 {i}/{tot_rem}")
            if batch:
                db._cache_write(pd.concat(batch, ignore_index=True))
        try:
            _query(fetch_baostock)
        except Exception:
            BAOSTOCK_AVAILABLE = False

    if successful and effective_end >= start:
        with db.connect(timeout=30) as conn:
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
        "snapshot_updated": snapshot_updated + turn_patched,
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
        cfg = db.get_auto_sync_config()
        if not cfg["enabled"]:
            return False, "已关闭"
        if now.weekday() >= 5:
            return False, "周末休市"
        today_str = now.strftime("%Y-%m-%d")
        if cfg["last_date"] == today_str:
            return False, "今日已自动更新"
        local = local_status if local_status is not None else db.cache_status()
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
                db._setting_set("auto_sync_last_date", today_str)
            db._setting_set("auto_sync_last_info", json.dumps(info, ensure_ascii=False))
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
            db._setting_set("auto_sync_last_info", json.dumps(info, ensure_ascii=False))
        finally:
            self._set_state(running=False)
            SYNC_LOCK.release()

    def snapshot(self, local_status=None):
        cfg = db.get_auto_sync_config()
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


_SINGLETON_LOCK = threading.Lock()
_SINGLETON_MGR = None


def get_sync_manager_singleton():
    """纯 Python 进程级单例（不依赖 Streamlit 运行时）。"""
    global _SINGLETON_MGR
    if _SINGLETON_MGR is None:
        with _SINGLETON_LOCK:
            if _SINGLETON_MGR is None:
                mgr = AutoSyncManager()
                mgr.start()
                _SINGLETON_MGR = mgr
    return _SINGLETON_MGR


@st.cache_resource
def get_auto_sync_manager():
    return get_sync_manager_singleton()


