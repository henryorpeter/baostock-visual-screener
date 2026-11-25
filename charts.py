"""TradingView Lightweight Charts 多周期 K 线构建、终端暗黑主题 CSS、卡片与悬浮进度面板。"""
from html import escape
import time

import pandas as pd
import streamlit as st


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


