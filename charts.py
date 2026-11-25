"""TradingView Lightweight Charts 多周期 K 线构建与 OKX/TradingView 极简扁平暗黑终端样式。"""
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
    up, down = "#f6465d", "#0ecb81"
    theme = {
        "layout": {
            "background": {"color": "#13161c"},
            "textColor": "#7e8694",
            "fontFamily": "'Inter', -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif",
            "fontSize": 11,
        },
        "grid": {
            "vertLines": {"color": "#1b1f28"},
            "horzLines": {"color": "#1b1f28"},
        },
        "rightPriceScale": {"borderColor": "#22262f"},
        "timeScale": {
            "borderColor": "#22262f",
            "timeVisible": False,
            "rightOffset": 5,
            "barSpacing": 8,
        },
        "crosshair": {
            "mode": "normal",
            "vertLine": {"color": "#4b5262", "style": 2, "width": 1},
            "horzLine": {"color": "#4b5262", "style": 2, "width": 1},
        },
    }
    candles = d[["time", "open", "high", "low", "close"]].to_dict("records")
    price_series = [{
        "type": "Candlestick",
        "data": candles,
        "options": {
            "upColor": up, "downColor": down,
            "borderUpColor": up, "borderDownColor": down,
            "wickUpColor": up, "wickDownColor": down,
        },
    }]
    if show_ma:
        for window, color in ((5, "#f0b90b"), (10, "#e879f9"), (20, "#38bdf8"), (30, "#fb923c")):
            line = d[["time", f"ma{window}"]].dropna().rename(columns={f"ma{window}": "value"}).to_dict("records")
            if line:
                price_series.append({
                    "type": "Line",
                    "data": line,
                    "options": {
                        "color": color,
                        "lineWidth": 1,
                        "title": "",
                        "priceLineVisible": False,
                        "lastValueVisible": False,
                        "crosshairMarkerVisible": False,
                    },
                })

    volume = [
        {
            "time": row.time,
            "value": float(row.volume),
            "color": "rgba(246, 70, 93, 0.48)" if row.close >= row.open else "rgba(14, 203, 129, 0.44)",
        }
        for row in d.itertuples(index=False)
    ]
    panes = [
        {"chart": theme, "height": 330, "title": f"{title} · {interval}", "series": price_series},
        {
            "chart": theme,
            "height": 84,
            "title": "VOL",
            "series": [{"type": "Histogram", "data": volume, "options": {"priceFormat": {"type": "volume"}}}],
        },
    ]
    if show_macd:
        macd_bars = [
            {
                "time": row.time,
                "value": float(row.macd_hist),
                "color": "rgba(246, 70, 93, 0.78)" if row.macd_hist >= 0 else "rgba(14, 203, 129, 0.74)",
            }
            for row in d.dropna(subset=["macd_hist"]).itertuples(index=False)
        ]
        dif_data = d[["time", "dif"]].dropna().rename(columns={"dif": "value"}).to_dict("records")
        dea_data = d[["time", "dea"]].dropna().rename(columns={"dea": "value"}).to_dict("records")
        panes.append({
            "chart": theme,
            "height": 102,
            "title": "MACD(12,26,9)",
            "series": [
                {"type": "Histogram", "data": macd_bars, "options": {"title": "", "priceLineVisible": False, "lastValueVisible": False}},
                {"type": "Line", "data": dif_data, "options": {"color": "#f0b90b", "lineWidth": 1, "title": "", "priceLineVisible": False, "lastValueVisible": False, "crosshairMarkerVisible": False}},
                {"type": "Line", "data": dea_data, "options": {"color": "#e879f9", "lineWidth": 1, "title": "", "priceLineVisible": False, "lastValueVisible": False, "crosshairMarkerVisible": False}},
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


def _style_pct(val):
    if pd.isna(val):
        return "color: #5e6673;"
    if val >= 9.5:
        return "color: #fecaca; background-color: rgba(246, 70, 93, 0.26); font-weight: 700;"
    if val > 0:
        return "color: #f6465d; font-weight: 600;"
    if val < 0:
        return "color: #0ecb81; font-weight: 600;"
    return "color: #7e8694;"


def _inject_custom_css():
    st.markdown("""
<style>
:root {
    --bg: #0d0f13;
    --panel: #13161c;
    --panel-2: #191d26;
    --hover: #1f2430;
    --line: #22262f;
    --line-2: #2f3542;
    --text: #e6e8ec;
    --muted: #7e8694;
    --dim: #555c69;
    --red: #f6465d;
    --red-hover: #ff5b70;
    --red-bg: rgba(246, 70, 93, 0.14);
    --green: #0ecb81;
    --green-bg: rgba(14, 203, 129, 0.14);
}

html, body, [class*="css"] {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
    -webkit-font-smoothing: antialiased;
}

.stApp {
    background-color: var(--bg);
    color: var(--text);
}

/* Force true full-width trading workspace & clean top offset */
.stMainBlockContainer,
[data-testid="stMainBlockContainer"],
.block-container {
    max-width: 100% !important;
    width: 100% !important;
    padding: 2.7rem 1.15rem 1.4rem 1.15rem !important;
}

[data-testid="stHeader"] {
    background: var(--bg);
    border-bottom: 1px solid var(--line);
    height: 2.4rem;
}

#MainMenu, footer, [data-testid="stDecoration"] { display: none !important; }

/* Sidebar: flat hairline rail */
[data-testid="stSidebar"] {
    background-color: #101318;
    border-right: 1px solid var(--line);
}
[data-testid="stSidebar"] > div:first-child {
    padding: 0.7rem 0.85rem 1rem 0.85rem;
}
.sb-title {
    font-size: 0.92rem;
    font-weight: 700;
    color: var(--text);
    letter-spacing: -0.01em;
    padding: 2px 0 10px 0;
    margin-bottom: 8px;
    border-bottom: 1px solid var(--line);
    display: flex;
    align-items: baseline;
    justify-content: space-between;
}
.sb-title span {
    font-size: 0.72rem;
    font-weight: 400;
    color: var(--muted);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.sb-label {
    font-size: 0.72rem;
    font-weight: 600;
    color: var(--muted);
    margin: 10px 0 4px 1px;
}

/* Native Streamlit controls — unified crisp flat hairline style */
.stButton > button {
    border-radius: 4px;
    font-size: 0.81rem;
    font-weight: 500;
    border: 1px solid var(--line-2);
    background-color: var(--panel);
    color: var(--text);
    padding: 0.34rem 0.38rem;
    box-shadow: none !important;
    transition: background-color 0.12s ease, border-color 0.12s ease;
}
.stButton > button:hover {
    background-color: var(--hover);
    border-color: #424a5c;
    color: #ffffff;
}
.stButton > button[kind="primary"] {
    background-color: var(--red);
    border-color: var(--red);
    color: #ffffff;
    font-weight: 600;
}
.stButton > button[kind="primary"]:hover {
    background-color: var(--red-hover);
    border-color: var(--red-hover);
    color: #ffffff;
}

/* Flat top header strip (NO box, NO pills, NO pulsing dot — pure OKX hairline) */
.term-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 8px 16px;
    padding: 0 2px 9px 2px;
    margin-bottom: 8px;
    border-bottom: 1px solid var(--line);
}
.term-brand {
    font-size: 0.98rem;
    font-weight: 700;
    color: var(--text);
    letter-spacing: -0.02em;
}
.term-brand small {
    font-size: 0.75rem;
    font-weight: 400;
    color: var(--muted);
    margin-left: 10px;
}
.term-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 14px;
    font-size: 0.75rem;
    color: var(--muted);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-variant-numeric: tabular-nums;
}
.term-meta b {
    color: var(--text);
    font-weight: 600;
}
.term-meta .m-up { color: var(--red); font-weight: 600; }
.term-meta .m-dn { color: var(--green); font-weight: 600; }

/* OKX Flat Ticker Bar (single clean horizontal strip, ZERO nested boxes) */
.okx-ticker {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 16px;
    padding: 9px 12px;
    margin-bottom: 6px;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 4px;
}
.okx-pair {
    display: flex;
    align-items: baseline;
    gap: 7px;
    padding-right: 12px;
    border-right: 1px solid var(--line);
}
.okx-name {
    font-size: 1.04rem;
    font-weight: 700;
    color: var(--text);
}
.okx-code {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.74rem;
    color: var(--muted);
}
.okx-px-wrap {
    display: flex;
    align-items: baseline;
    gap: 7px;
    padding-right: 12px;
    border-right: 1px solid var(--line);
}
.okx-px {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 1.26rem;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
}
.okx-chg {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.78rem;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
}
.is-up { color: var(--red); }
.is-down { color: var(--green); }
.is-flat { color: var(--muted); }
.okx-kv-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 13px;
    flex: 1;
}
.okx-kv {
    display: flex;
    flex-direction: column;
    line-height: 1.25;
}
.okx-k {
    font-size: 0.68rem;
    color: var(--muted);
}
.okx-v {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.79rem;
    font-weight: 600;
    color: var(--text);
    font-variant-numeric: tabular-nums;
}
.okx-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    width: 100%;
    padding-top: 7px;
    border-top: 1px solid var(--line);
    font-size: 0.71rem;
}
.okx-tag {
    padding: 1px 7px;
    border-radius: 3px;
    background: var(--red-bg);
    color: #fda4af;
    font-weight: 500;
}

/* Subtle inline chart indicator strip */
.ma-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 14px;
    padding: 4px 10px;
    margin-bottom: 4px;
    background: var(--panel);
    border: 1px solid var(--line);
    border-bottom: none;
    border-radius: 4px 4px 0 0;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.72rem;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
}

/* Native Tabs underline polish */
[data-testid="stTabs"] [role="tablist"] {
    gap: 20px;
    border-bottom: 1px solid var(--line);
    margin-bottom: 10px;
}
[data-testid="stTabs"] button[role="tab"] {
    padding: 6px 2px 9px 2px;
    font-size: 0.85rem;
    font-weight: 500;
    color: var(--muted);
}
[data-testid="stTabs"] button[role="tab"][aria-selected="true"] {
    color: var(--text);
    font-weight: 600;
}

/* Strategy Matrix & Data Center flat tables/cards */
.flat-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 1px;
    background: var(--line);
    border: 1px solid var(--line);
    border-radius: 4px;
    overflow: hidden;
    margin: 4px 0 14px 0;
}
.flat-cell {
    background: var(--panel);
    padding: 12px 14px;
}
.flat-k {
    font-size: 0.72rem;
    color: var(--muted);
    margin-bottom: 4px;
}
.flat-v {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 1.2rem;
    font-weight: 600;
    color: var(--text);
    font-variant-numeric: tabular-nums;
}
.flat-sub {
    font-size: 0.72rem;
    color: var(--dim);
    margin-top: 3px;
}
.strat-row {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 4px;
    padding: 11px 13px;
    margin-bottom: 8px;
    min-height: 82px;
}
.strat-row.on {
    border-color: rgba(246, 70, 93, 0.55);
}
.strat-row-h {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 5px;
}
.strat-row-name {
    font-size: 0.86rem;
    font-weight: 600;
    color: var(--text);
}
.strat-row-desc {
    font-size: 0.76rem;
    color: var(--muted);
    line-height: 1.48;
}

/* Compact Floating Progress Overlay */
.scan-float-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(13, 15, 19, 0.72);
    z-index: 99998;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
}
.scan-float-card {
    width: min(500px, 92vw);
    padding: 18px 20px;
    border-radius: 6px;
    background: var(--panel);
    border: 1px solid var(--line-2);
    color: var(--text);
}
.scan-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 10px;
}
.scan-title {
    font-size: 0.9rem;
    font-weight: 600;
}
.scan-pct {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 1.15rem;
    font-weight: 700;
    color: var(--red);
}
.scan-bar-track {
    width: 100%;
    height: 4px;
    background: var(--line);
    margin-bottom: 10px;
}
.scan-bar-fill {
    height: 100%;
    background: var(--red);
    transition: width 0.15s ease-out;
}
.scan-foot {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    font-size: 0.75rem;
    color: var(--muted);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}

[data-testid="stMultiSelect"] span[data-baseweb="tag"],
[data-testid="stSidebar"] span[data-baseweb="tag"] {
    background-color: #1f2430 !important;
    border: 1px solid #323949 !important;
    color: #d4d8e2 !important;
    border-radius: 4px !important;
}
[data-testid="stDataFrame"] {
    border: 1px solid var(--line);
    border-radius: 4px;
}
[data-testid="stExpander"] {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 4px;
}

@media (max-width: 960px) {
    .flat-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .okx-pair, .okx-px-wrap { border-right: none; padding-right: 0; }
}
@media (max-width: 640px) {
    .stMainBlockContainer,
    [data-testid="stMainBlockContainer"],
    .block-container {
        padding: 2.6rem 0.6rem 1.4rem 0.6rem !important;
    }
    .term-brand small { display: none; }
    .okx-ticker { padding: 8px 10px; gap: 8px 14px; }
    .stButton > button { min-height: 36px; }
}
</style>
""", unsafe_allow_html=True)


def _render_hero(local_status, selected_strategies, auto_sync=None, result=None):
    """极简无框顶部状态行（OKX 风格，零胶囊、零装饰图标）。"""
    newest = local_status["newest"] or "待同步"
    stocks_cnt = local_status["stocks"]
    parts = [
        f'<span>标的 <b>{stocks_cnt:,}</b></span>',
        f'<span>最新 <b>{escape(str(newest))}</b></span>',
        f'<span>策略 <b>{len(selected_strategies)}</b></span>',
    ]
    if result is not None and not result.empty:
        uniq = result["代码"].nunique()
        avg_chg = result["涨跌幅%"].mean()
        cls = "m-up" if avg_chg >= 0 else "m-dn"
        parts.append(f'<span>命中 <b>{len(result)}</b> ({uniq}只 · <span class="{cls}">{avg_chg:+.2f}%</span>)</span>')
    if auto_sync:
        if auto_sync["running"]:
            parts.append(f'<span class="m-up">后台同步 {int(round(auto_sync["ratio"] * 100))}%</span>')
        elif auto_sync["enabled"]:
            parts.append(f'<span>日更 {escape(auto_sync["sync_time"])}</span>')

    st.markdown(
        f'<div class="term-head">'
        f'<div class="term-brand">KLineRadar<small>A股本地选股与看盘终端</small></div>'
        f'<div class="term-meta">{"".join(parts)}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


def _render_kpi_cards(local_status, result, scan_count):
    hit_cnt = f"{len(result):,}" if result is not None else "—"
    earliest = local_status["earliest"] or "—"
    newest = local_status["newest"] or "—"
    st.markdown(
        f'<div class="flat-grid">'
        f'<div class="flat-cell"><div class="flat-k">本地股票数</div><div class="flat-v">{local_status["stocks"]:,}</div><div class="flat-sub">股票池 {local_status["named"]:,} 只</div></div>'
        f'<div class="flat-cell"><div class="flat-k">日线记录</div><div class="flat-v">{local_status["rows"]:,}</div><div class="flat-sub">{local_status["size_mb"]:.1f} MB</div></div>'
        f'<div class="flat-cell"><div class="flat-k">数据区间</div><div class="flat-v">{escape(str(newest))}</div><div class="flat-sub">起自 {escape(str(earliest))}</div></div>'
        f'<div class="flat-cell"><div class="flat-k">本次命中</div><div class="flat-v">{hit_cnt}</div><div class="flat-sub">离线 NumPy 引擎</div></div>'
        f'</div>',
        unsafe_allow_html=True,
    )


class FloatingProgress:
    """极简居中进度遮罩。"""

    def __init__(self, title, steps):
        self.title = title
        self.steps = steps
        self.started = time.monotonic()
        self.slot = st.empty()

    def update(self, ratio, stage=1, detail="", hits=None, scanned=None, total=None):
        pct = int(round(min(1.0, max(0.0, ratio)) * 100))
        elapsed = time.monotonic() - self.started
        right = f"{elapsed:.1f}s"
        if scanned is not None and total:
            right = f"{scanned:,}/{total:,} · {right}"
        if hits is not None:
            right = f"命中 {hits:,} · {right}"
        self.slot.markdown(
            f'<div class="scan-float-backdrop"><div class="scan-float-card">'
            f'<div class="scan-head"><span class="scan-title">{escape(self.title)}</span><span class="scan-pct">{pct}%</span></div>'
            f'<div class="scan-bar-track"><div class="scan-bar-fill" style="width:{max(3, pct)}%;"></div></div>'
            f'<div class="scan-foot"><span>{escape(detail)}</span><span>{escape(right)}</span></div>'
            f'</div></div>',
            unsafe_allow_html=True,
        )

    def progress(self, ratio, text=""):
        stage = 2 if ratio < 0.98 else 3
        self.update(0.15 + 0.85 * ratio, stage=stage, detail=text)

    def close(self):
        self.slot.empty()
