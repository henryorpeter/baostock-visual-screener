"""纯 NumPy 向量化技术指标、20 套选股策略引擎、历史胜率速验与外部软件导出工具。"""
from collections import OrderedDict
from datetime import date
import threading
import time
import requests

import numpy as np
import pandas as pd
import streamlit as st

import db

_ARRAY_CACHE_LOCK = threading.Lock()
_ARRAY_CACHE = OrderedDict()


def load_market_arrays(start, end, db_rows, db_newest, db_rev="0"):
    """纯 Python 线程安全 LRU 缓存版全市场 NumPy 数组加载（不依赖 Streamlit 运行时）。"""
    key = (start, end, db_rows, db_newest, db_rev)
    with _ARRAY_CACHE_LOCK:
        cached = _ARRAY_CACHE.get(key)
        if cached is not None:
            _ARRAY_CACHE.move_to_end(key)
            return cached
    data = _load_market_arrays_cached.__wrapped__(start, end, db_rows, db_newest, db_rev)
    with _ARRAY_CACHE_LOCK:
        _ARRAY_CACHE[key] = data
        _ARRAY_CACHE.move_to_end(key)
        while len(_ARRAY_CACHE) > 3:
            _ARRAY_CACHE.popitem(last=False)
    return data


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



@st.cache_resource(max_entries=3, show_spinner=False)
def _load_market_arrays_cached(start, end, db_rows, db_newest, db_rev="0"):
    """一次性从 SQLite 载入全市场行情并按股票拆分为只读 NumPy 数组字典（带版本感知内存缓存）。"""
    db._init_cache()
    with db.connect(timeout=30) as conn:
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
    db._init_cache()
    with db.connect(timeout=30) as conn:
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

    def _make_row(label, c_list):
        r5 = [ret5_map[c] for c in c_list if c in ret5_map]
        r10 = [ret10_map[c] for c in c_list if c in ret10_map]
        return {
            "策略": label,
            "样本数": len(c_list),
            "5日胜率%": round(sum(1 for v in r5 if v > 0) / len(r5) * 100.0, 1) if r5 else float("nan"),
            "5日均涨幅%": round(float(np.mean(r5)), 2) if r5 else float("nan"),
            "10日胜率%": round(sum(1 for v in r10 if v > 0) / len(r10) * 100.0, 1) if r10 else float("nan"),
            "10日均涨幅%": round(float(np.mean(r10)), 2) if r10 else float("nan"),
        }

    summary = []
    for strat, grp in result_df.groupby("策略", sort=False):
        summary.append(_make_row(strat, grp["代码"].unique().tolist()))

    # 额外统计多策略共振（≥2 / ≥3）标的的表现，方便直接对比共振是否提升胜率
    hit_counts = result_df.groupby("代码")["策略"].nunique()
    for min_hits, label in ((2, "★ 多策略共振 (≥2)"), (3, "★ 强共振 (≥3)")):
        res_codes = hit_counts[hit_counts >= min_hits].index.tolist()
        if res_codes:
            summary.append(_make_row(label, res_codes))
    return pd.DataFrame(summary)


def _format_tdx_ebk(codes):
    """导出通达信自定义板块 .ebk 格式：沪市 1+6位，深市 0+6位。"""
    lines = []
    for code in dict.fromkeys(codes):
        if "." in code:
            mkt, sym = code.split(".", 1)
            prefix = "1" if mkt == "sh" else "0"
            lines.append(f"{prefix}{sym}")
    return ("\r\n".join(lines) + "\r\n").encode("ascii", errors="ignore")


def _format_plain_codes(codes):
    """导出纯 6 位股票代码（适配同花顺 / 东方财富自选股批量粘贴导入）。"""
    syms = [code.split(".", 1)[1] if "." in code else code for code in dict.fromkeys(codes)]
    return "\n".join(syms)


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


