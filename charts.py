"""K 线多周期重采样、全套 14 项技术指标计算与行情数值格式化工具。"""
import numpy as np
import pandas as pd


MAIN_OVERLAYS = ("MA", "EMA", "BOLL", "BBI", "SAR", "KC")
SUB_INDICATORS = ("VOL", "MACD", "KDJ", "RSI", "WR", "CCI", "OBV", "ATR")


def _sar_series(high, low, step=0.02, max_af=0.20):
    """标准 Wilder 抛物线转向指标 (Parabolic SAR)。"""
    h = high.to_numpy(dtype=np.float64)
    l = low.to_numpy(dtype=np.float64)
    n = len(h)
    if n < 2:
        return np.full(n, np.nan)
    sar = np.full(n, np.nan, dtype=np.float64)
    bull = h[1] >= h[0]
    af = step
    ep = h[0] if bull else l[0]
    sar[0] = l[0] if bull else h[0]
    for i in range(1, n):
        prev_sar = sar[i - 1]
        cur = prev_sar + af * (ep - prev_sar)
        if bull:
            cur = min(cur, l[i - 1], l[i - 2] if i >= 2 else l[i - 1])
            if l[i] < cur:
                bull = False
                cur = ep
                ep = l[i]
                af = step
            else:
                if h[i] > ep:
                    ep = h[i]
                    af = min(max_af, af + step)
        else:
            cur = max(cur, h[i - 1], h[i - 2] if i >= 2 else h[i - 1])
            if h[i] > cur:
                bull = True
                cur = ep
                ep = h[i]
                af = step
            else:
                if l[i] < ep:
                    ep = l[i]
                    af = min(max_af, af + step)
        sar[i] = cur
    return sar


def _kdj_series(high, low, close, n=9):
    """标准 A 股 / 交易所递推 KDJ (9, 3, 3)。"""
    low_n = low.rolling(n).min()
    high_n = high.rolling(n).max()
    rsv = (close - low_n) / (high_n - low_n).replace(0, float("nan")) * 100.0
    k_vals, d_vals, j_vals = [], [], []
    k, d_v = 50.0, 50.0
    for val in rsv:
        if pd.isna(val):
            k_vals.append(float("nan"))
            d_vals.append(float("nan"))
            j_vals.append(float("nan"))
        else:
            k = (2.0 / 3.0) * k + (1.0 / 3.0) * float(val)
            d_v = (2.0 / 3.0) * d_v + (1.0 / 3.0) * k
            j = 3.0 * k - 2.0 * d_v
            k_vals.append(k)
            d_vals.append(d_v)
            j_vals.append(j)
    return k_vals, d_vals, j_vals


def _rsi_series(close, window):
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_g = gain.ewm(alpha=1.0 / window, min_periods=window, adjust=False).mean()
    avg_l = loss.ewm(alpha=1.0 / window, min_periods=window, adjust=False).mean()
    rs = avg_g / avg_l.replace(0, float("nan"))
    rsi = 100.0 - 100.0 / (1.0 + rs)
    rsi = rsi.where(~((avg_l == 0) & (avg_g > 0)), 100.0)
    rsi = rsi.where(~((avg_l == 0) & (avg_g == 0)), 50.0)
    return rsi


def _resample_kline(frame, interval, already_resampled=False):
    d = frame.copy().sort_values("date").dropna(subset=["open", "high", "low", "close"])
    d["volume"] = pd.to_numeric(d.volume, errors="coerce").fillna(0)
    if already_resampled:
        period = None
    elif interval == "周线":
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
    # 主图均线 MA / EMA / BOLL / BBI / SAR / KC
    for window in (3, 5, 6, 10, 12, 20, 24, 30):
        d[f"ma{window}"] = d.close.rolling(window).mean()
    for span in (12, 20, 26, 60):
        d[f"ema{span}"] = d.close.ewm(span=span, adjust=False).mean()
    boll_mid = d["ma20"]
    boll_std = d.close.rolling(20).std(ddof=0)
    d["boll_mid"] = boll_mid
    d["boll_up"] = boll_mid + 2.0 * boll_std
    d["boll_dn"] = boll_mid - 2.0 * boll_std
    d["bbi"] = (d["ma3"] + d["ma6"] + d["ma12"] + d["ma24"]) / 4.0
    d["sar"] = _sar_series(d.high, d.low)
    prev_c = d.close.shift(1)
    tr = pd.concat([(d.high - d.low), (d.high - prev_c).abs(), (d.low - prev_c).abs()], axis=1).max(axis=1)
    d["atr10"] = tr.ewm(alpha=1.0 / 10.0, min_periods=10, adjust=False).mean()
    d["atr14"] = tr.ewm(alpha=1.0 / 14.0, min_periods=14, adjust=False).mean()
    d["kc_mid"] = d["ema20"]
    d["kc_up"] = d["ema20"] + 2.0 * d["atr10"]
    d["kc_dn"] = d["ema20"] - 2.0 * d["atr10"]
    # 成交量均量 & OBV
    d["vol_ma5"] = d.volume.rolling(5).mean()
    d["vol_ma10"] = d.volume.rolling(10).mean()
    d["obv"] = (np.sign(d.close.diff().fillna(0.0)) * d.volume).cumsum()
    d["obv_ma30"] = d["obv"].rolling(30).mean()
    # MACD (12, 26, 9)
    d["dif"] = d["ema12"] - d["ema26"]
    d["dea"] = d["dif"].ewm(span=9, adjust=False).mean()
    d["macd_hist"] = (d["dif"] - d["dea"]) * 2
    # KDJ (9, 3, 3)
    d["kdj_k"], d["kdj_d"], d["kdj_j"] = _kdj_series(d.high, d.low, d.close, n=9)
    # RSI (6, 12, 24)
    for w in (6, 12, 24):
        d[f"rsi{w}"] = _rsi_series(d.close, w)
    # WR (10, 6) 威廉指标（0~100 刻度，A股看盘惯例）
    for w in (10, 6):
        hh = d.high.rolling(w).max()
        ll = d.low.rolling(w).min()
        d[f"wr{w}"] = 100.0 * (hh - d.close) / (hh - ll).replace(0, float("nan"))
    # CCI (14) 顺势指标
    tp = (d.high + d.low + d.close) / 3.0
    tp_ma = tp.rolling(14).mean()
    tp_md = tp.rolling(14).apply(lambda arr: float(np.mean(np.abs(arr - np.mean(arr)))), raw=True)
    d["cci"] = (tp - tp_ma) / (0.015 * tp_md.replace(0, float("nan")))
    d["time"] = pd.to_datetime(d.date).dt.strftime("%Y-%m-%d")
    return d


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
