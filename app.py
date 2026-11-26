"""KLineRadar · A股技术形态选股与看盘终端（OKX / TradingView 极简扁平暗黑风格）。

运行：streamlit run app.py  或  streamlit run baostock_screener.py
"""
from datetime import date, datetime, timedelta
from html import escape
import json
import time

import pandas as pd
import streamlit as st
from lightweight_charts_v5 import lightweight_charts_v5_component

import charts
import db
import engine
import sync


def _apply_strategy_preset(names):
    st.session_state["pending_strategies"] = list(names)


def _apply_date_window(days):
    st.session_state["pending_window_days"] = days


def _run_update_action(start, end):
    if not sync.SYNC_LOCK.acquire(blocking=False):
        st.info("后台正在执行自动行情更新，请稍候。")
        return
    end_str = end.strftime("%Y-%m-%d")
    tracker = charts.FloatingProgress("更新全市场行情缓存", ("校验交易日", "拉取行情", "写入本地库"))
    try:
        tracker.update(0.05, stage=1, detail="加载股票池与交易日…")
        try:
            all_codes, list_date, list_source = sync._get_universe_for_sync(end_str)
        except Exception as exc:
            tracker.close()
            st.error(f"获取股票列表失败：{exc}")
            return
        if not all_codes:
            tracker.close()
            st.error("所选日期附近无可用股票列表，请稍后重试。")
            return
        tracker.update(0.15, stage=2, detail=f"就绪 {len(all_codes):,} 只（{list_source}），检查缺口…")
        update_stats = sync.update_cache(all_codes, start.strftime("%Y-%m-%d"), end_str, tracker)
        tracker.update(1.0, stage=3, detail="同步完成")
        time.sleep(0.15)
    finally:
        tracker.close()
        sync.SYNC_LOCK.release()

    eff_end = update_stats.get("effective_end", end_str)
    today_str = date.today().strftime("%Y-%m-%d")
    if eff_end >= today_str:
        db._setting_set("auto_sync_last_date", today_str)
        db._setting_set("auto_sync_last_info", json.dumps({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "date": eff_end,
            "trigger": "手动更新",
            "updated": update_stats.get("updated", 0),
            "skipped": update_stats.get("skipped", 0),
            "failed": update_stats.get("failed", 0),
            "snapshot_updated": update_stats.get("snapshot_updated", 0),
            "ex_div": update_stats.get("ex_div", 0),
            "source": update_stats.get("source", "批量增量日更"),
            "status": "warning" if update_stats.get("failed") else "success",
        }, ensure_ascii=False))

    st.session_state["update_stats"] = update_stats
    for key in ("result", "scan_count", "scan_stats", "history_source", "sparks"):
        st.session_state.pop(key, None)

    summary = f"更新 {update_stats['updated']} 只，跳过 {update_stats['skipped']} 只"
    if update_stats["failed"]:
        st.warning(f"缓存更新完成（{summary}），{update_stats['failed']} 只失败。")
    else:
        st.toast(f"行情已更新：{summary}")


def _run_screen_action(
    start, end, strategies, min_change, min_turn, max_turn, max_stocks,
    exclude_st=True, exclude_gem_star=False, min_amount=0.0, silent=False,
):
    tracker = None if silent else charts.FloatingProgress(
        f"扫描全市场（{len(strategies)} 套策略）",
        ("读取缓存", "计算指标", "汇总信号"),
    )
    started = time.monotonic()
    try:
        if tracker:
            tracker.update(0.05, stage=1, detail="读取本地股票池…")
        universe = db.cache_universe_load()
        if not universe:
            if not silent:
                st.error("本地暂无股票列表，请先点击左侧「同步行情」。")
            return
        limit = min(int(max_stocks), len(universe))
        stride = max(1, len(universe) // limit)
        selected = universe[::stride][:limit]
        lookback_start = min(start, end - timedelta(days=210))
        db_rows, db_newest, db_rev = db._cache_version()
        if tracker:
            tracker.update(0.12, stage=1, detail=f"加载本地行情（{db_rows:,} 条）…")
        frames = engine._load_market_arrays_cached(
            lookback_start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), db_rows, db_newest, db_rev
        )
        if not frames:
            if not silent:
                st.error("本地无该区间行情，请先点击左侧「同步行情」。")
            return
        result, scan_stats = engine.screen(
            selected, frames, strategies, min_change, min_turn, max_turn,
            on_progress=(tracker.update if tracker else (lambda *a, **k: None)),
            window_start=start,
            exclude_st=exclude_st, exclude_gem_star=exclude_gem_star, min_amount=min_amount,
        )
        sparks = scan_stats.pop("sparks", {})
        if tracker:
            tracker.update(1.0, stage=3, detail=f"完成，命中 {len(result):,} 条", hits=len(result), scanned=len(selected), total=len(selected))
            time.sleep(0.1)
    finally:
        if tracker:
            tracker.close()

    elapsed = time.monotonic() - started
    st.session_state["scan_count"] = (scan_stats["ready"], len(selected), len(universe))
    st.session_state["scan_elapsed"] = elapsed
    st.session_state["result"] = result
    st.session_state["sparks"] = sparks
    st.session_state["range"] = (start, end)
    st.session_state["strategies"] = list(strategies)
    st.session_state["history_source"] = "本地 SQLite"
    st.session_state["scan_stats"] = scan_stats
    if not result.empty:
        st.session_state["active_stock"] = result.iloc[0]["代码"]
        if not silent:
            st.toast(f"命中 {len(result):,} 条信号（{elapsed:.2f}s）")
    elif not silent:
        st.toast(f"扫描完成（{elapsed:.2f}s），暂无命中标的")


def _render_stock_terminal(
    code, name, start_date, end_date, interval, mobile_compact,
    hit_list=None, chart_key_prefix="chart",
):
    """OKX 扁平 Ticker 栏 + 主图(MA/EMA/BOLL) & 副图(VOL/MACD/KDJ/RSI) 完整 K 线终端。"""
    if interval in ("周线", "月线"):
        fetch_start_str, display_start = "1990-01-01", None
    else:
        fetch_start_str, display_start = (start_date - timedelta(days=150)).strftime("%Y-%m-%d"), start_date
    frame = db._cache_read(code, fetch_start_str, end_date.strftime("%Y-%m-%d"))
    if frame.empty:
        st.info(f"暂无 {name}（{code}）在该区间的行情缓存。")
        return

    latest = frame.iloc[-1]
    prior = frame.iloc[-2].close if len(frame) > 1 else latest.close
    change = (latest.close / prior - 1) * 100 if prior else 0.0
    amp = ((latest.high - latest.low) / prior * 100) if prior else 0.0
    feat = engine.indicators(frame)
    rsi_val = f"{feat.iloc[-1].rsi:.1f}" if not feat.empty else "—"
    macd_val = feat.iloc[-1].macd - feat.iloc[-1].signal if not feat.empty else float("nan")
    macd_text = ("金叉区" if macd_val > 0 else "空头区") if pd.notna(macd_val) else "—"
    turn_text = f"{latest.turn:.2f}%" if pd.notna(latest.turn) else "—"
    c_cls = "is-up" if change > 0 else ("is-down" if change < 0 else "is-flat")

    tags_html = ""
    if hit_list:
        tags_html = '<div class="okx-tags">' + "".join(f'<span class="okx-tag">{escape(s)}</span>' for s in hit_list) + '</div>'

    st.markdown(
        f'<div class="okx-ticker">'
        f'<div class="okx-pair"><span class="okx-name">{escape(name)}</span><span class="okx-code">{escape(code)}</span></div>'
        f'<div class="okx-px-wrap"><span class="okx-px {c_cls}">{latest.close:.2f}</span><span class="okx-chg {c_cls}">{change:+.2f}%</span></div>'
        f'<div class="okx-kv-row">'
        f'<div class="okx-kv"><span class="okx-k">今开</span><span class="okx-v">{latest.open:.2f}</span></div>'
        f'<div class="okx-kv"><span class="okx-k">最高</span><span class="okx-v is-up">{latest.high:.2f}</span></div>'
        f'<div class="okx-kv"><span class="okx-k">最低</span><span class="okx-v is-down">{latest.low:.2f}</span></div>'
        f'<div class="okx-kv"><span class="okx-k">振幅</span><span class="okx-v">{amp:.2f}%</span></div>'
        f'<div class="okx-kv"><span class="okx-k">换手</span><span class="okx-v">{turn_text}</span></div>'
        f'<div class="okx-kv"><span class="okx-k">成交额</span><span class="okx-v">{charts._fmt_amount(latest.amount)}</span></div>'
        f'<div class="okx-kv"><span class="okx-k">RSI</span><span class="okx-v">{rsi_val}</span></div>'
        f'<div class="okx-kv"><span class="okx-k">MACD</span><span class="okx-v">{macd_text}</span></div>'
        f'</div>'
        f'{tags_html}'
        f'</div>',
        unsafe_allow_html=True,
    )

    # OKX/交易所风格指标工具行：主图叠加（MA/EMA/BOLL/BBI/SAR/KC）+ 副图指标（VOL/MACD/KDJ/RSI/WR/CCI/OBV/ATR）
    ic1, ic2 = st.columns([1.05, 1.45], vertical_alignment="center")
    with ic1:
        overlays = st.pills(
            "主图指标",
            options=list(charts.MAIN_OVERLAYS),
            default=["MA"],
            selection_mode="multi",
            label_visibility="collapsed",
            key=f"{chart_key_prefix}_overlays",
        ) or []
    with ic2:
        sub_panes = st.pills(
            "副图指标",
            options=list(charts.SUB_INDICATORS),
            default=["VOL", "MACD"],
            selection_mode="multi",
            label_visibility="collapsed",
            key=f"{chart_key_prefix}_subs",
        ) or []

    panes, kdf = charts.chart_panes(
        frame, f"{name} {code}", interval,
        display_start=display_start,
        overlays=overlays, sub_panes=sub_panes,
    )
    last_k = kdf.iloc[-1]

    def _fv(col, fmt=".2f"):
        v = last_k.get(col)
        return format(float(v), fmt) if pd.notna(v) else "—"

    parts = []
    if "MA" in overlays:
        for w, col in ((5, "#f0b90b"), (10, "#e879f9"), (20, "#38bdf8"), (30, "#fb923c")):
            parts.append(f'<span style="color:{col};">MA{w} {_fv(f"ma{w}")}</span>')
    if "EMA" in overlays:
        for s, col in ((12, "#22d3ee"), (26, "#a78bfa"), (60, "#f59e0b")):
            parts.append(f'<span style="color:{col};">EMA{s} {_fv(f"ema{s}")}</span>')
    if "BOLL" in overlays:
        parts.append(f'<span style="color:#f59e0b;">BOLL UP {_fv("boll_up")}</span>')
        parts.append(f'<span style="color:#d4d8e2;">MID {_fv("boll_mid")}</span>')
        parts.append(f'<span style="color:#a855f7;">DN {_fv("boll_dn")}</span>')
    if "BBI" in overlays:
        parts.append(f'<span style="color:#2dd4bf;">BBI {_fv("bbi")}</span>')
    if "SAR" in overlays:
        parts.append(f'<span style="color:#fbbf24;">SAR {_fv("sar")}</span>')
    if "KC" in overlays:
        parts.append(f'<span style="color:#38bdf8;">KC UP {_fv("kc_up")} / DN {_fv("kc_dn")}</span>')
    if "MACD" in sub_panes:
        parts.append(f'<span style="color:#f0b90b;">DIF {_fv("dif", ".3f")}</span>')
        parts.append(f'<span style="color:#e879f9;">DEA {_fv("dea", ".3f")}</span>')
        parts.append(f'<span>MACD {_fv("macd_hist", "+.3f")}</span>')
    if "KDJ" in sub_panes:
        parts.append(f'<span style="color:#f0b90b;">K {_fv("kdj_k", ".1f")}</span>')
        parts.append(f'<span style="color:#38bdf8;">D {_fv("kdj_d", ".1f")}</span>')
        parts.append(f'<span style="color:#e879f9;">J {_fv("kdj_j", ".1f")}</span>')
    if "RSI" in sub_panes:
        parts.append(f'<span style="color:#f0b90b;">RSI6 {_fv("rsi6", ".1f")}</span>')
        parts.append(f'<span style="color:#38bdf8;">RSI12 {_fv("rsi12", ".1f")}</span>')
        parts.append(f'<span style="color:#e879f9;">RSI24 {_fv("rsi24", ".1f")}</span>')
    if "WR" in sub_panes:
        parts.append(f'<span style="color:#f0b90b;">WR10 {_fv("wr10", ".1f")}</span>')
        parts.append(f'<span style="color:#38bdf8;">WR6 {_fv("wr6", ".1f")}</span>')
    if "CCI" in sub_panes:
        parts.append(f'<span style="color:#38bdf8;">CCI {_fv("cci", ".1f")}</span>')
    if "OBV" in sub_panes:
        parts.append(f'<span style="color:#f0b90b;">OBV {charts._fmt_volume(last_k.get("obv"))}</span>')
    if "ATR" in sub_panes:
        parts.append(f'<span style="color:#fb923c;">ATR14 {_fv("atr14", ".3f")}</span>')
    if parts:
        st.markdown(f'<div class="ma-bar">{"".join(parts)}</div>', unsafe_allow_html=True)

    chart_height = charts.calc_chart_height(sub_panes, mobile_compact=mobile_compact)
    ind_sig = "_".join(sorted(overlays)) + "__" + "_".join(sub_panes)
    lightweight_charts_v5_component(
        name=f"{name} {code}",
        charts=panes,
        height=chart_height,
        zoom_level=65 if mobile_compact else 84,
        key=f"{chart_key_prefix}_{code}_{interval}_{ind_sig}_{mobile_compact}",
    )


def main():
    st.set_page_config(
        page_title="KLineRadar · A股选股与看盘终端",
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    charts._inject_custom_css()

    today = date.today()
    if "selected_strategies" not in st.session_state:
        st.session_state["selected_strategies"] = list(engine.STRATEGY_GROUPS["趋势突破"]["items"])
    if "end_date" not in st.session_state:
        st.session_state["end_date"] = today
    if "start_date" not in st.session_state:
        st.session_state["start_date"] = today - timedelta(days=180)

    if "pending_strategies" in st.session_state:
        st.session_state["selected_strategies"] = st.session_state.pop("pending_strategies")
    if "pending_window_days" in st.session_state:
        days = st.session_state.pop("pending_window_days")
        st.session_state["start_date"] = st.session_state["end_date"] - timedelta(days=days)

    # ========================== Sidebar ==========================
    with st.sidebar:
        st.markdown('<div class="sb-title">选股参数<span>KLineRadar</span></div>', unsafe_allow_html=True)

        b1, b2 = st.columns([1.25, 1.0])
        with b1:
            run_btn = st.button("开始选股", type="primary", width="stretch")
        with b2:
            update_btn = st.button("同步行情", width="stretch")

        st.markdown('<div class="sb-label">策略流派</div>', unsafe_allow_html=True)
        p1, p2 = st.columns(2)
        p1.button("趋势突破", width="stretch", on_click=_apply_strategy_preset, args=(engine.STRATEGY_GROUPS["趋势突破"]["items"],))
        p2.button("涨停战法", width="stretch", on_click=_apply_strategy_preset, args=(engine.STRATEGY_GROUPS["涨停战法"]["items"],))
        p3, p4 = st.columns(2)
        p3.button("均线共振", width="stretch", on_click=_apply_strategy_preset, args=(engine.STRATEGY_GROUPS["均线共振"]["items"],))
        p4.button("形态反转", width="stretch", on_click=_apply_strategy_preset, args=(engine.STRATEGY_GROUPS["形态反转"]["items"],))

        strategies = st.multiselect(
            "策略",
            options=list(engine.STRATEGIES.keys()),
            key="selected_strategies",
            placeholder="选择策略…",
            label_visibility="collapsed",
        )

        st.markdown('<div class="sb-label">条件过滤</div>', unsafe_allow_html=True)
        f1, f2 = st.columns(2)
        with f1:
            exclude_st = st.checkbox("排除 ST", value=True)
        with f2:
            exclude_gem_star = st.checkbox("排除双创", value=False)
        q1, q2 = st.columns(2)
        with q1:
            min_change = st.number_input("最低涨幅%", -10.0, 20.0, 0.0, 0.5, format="%.1f")
        with q2:
            min_amount = st.number_input("最低额(亿)", 0.0, 100.0, 0.0, 0.5, format="%.1f")

        st.markdown('<div class="sb-label">历史区间</div>', unsafe_allow_html=True)
        w1, w2, w3 = st.columns(3)
        w1.button("90天", width="stretch", on_click=_apply_date_window, args=(90,))
        w2.button("180天", width="stretch", on_click=_apply_date_window, args=(180,))
        w3.button("1年", width="stretch", on_click=_apply_date_window, args=(365,))

        with st.expander("高级设置", expanded=False):
            c_start, c_end = st.columns(2)
            with c_start:
                start = st.date_input("开始", key="start_date")
            with c_end:
                end = st.date_input("结束", key="end_date")
            min_turn = 0.0
            max_turn = st.slider("最高换手率 (%)", 0.0, 100.0, 100.0, step=0.5)
            max_stocks = st.number_input("最多扫描数", 10, 6000, 6000, 200)
            mobile_compact = st.toggle("手机紧凑布局", value=False)

            local_status = db.cache_status()
            sync_mgr = sync.get_auto_sync_manager()
            auto_cfg = db.get_auto_sync_config()
            auto_toggle = st.toggle(
                f"收盘自动同步 ({auto_cfg['sync_time']})",
                value=auto_cfg["enabled"],
                key="sb_auto_sync_toggle",
            )
            if auto_toggle != auto_cfg["enabled"]:
                db.save_auto_sync_config(auto_toggle, auto_cfg["sync_time"])
                st.rerun()

        auto_state = sync_mgr.snapshot(local_status=local_status)
        st.caption(f"{auto_state['today_status']} · 本地 SQLite")

    # ========================== Actions & Auto First-Load Scan ==========================
    valid_params = True
    if start >= end:
        st.error("开始日期必须早于结束日期，请在左侧「高级设置」调整。")
        valid_params = False
    elif not strategies:
        st.warning("请在左侧选择至少 1 个策略。")
        valid_params = False

    if update_btn and valid_params:
        _run_update_action(start, end)
        local_status = db.cache_status(force=True)
        auto_state = sync_mgr.snapshot(local_status=local_status)

    # 不在首屏自动阻塞扫描或提前渲染 K 线，确保打开页面秒开；点击「开始选股」后再执行扫描
    should_scan = run_btn or st.session_state.pop("trigger_scan", False)
    if should_scan and valid_params:
        _run_screen_action(
            start, end, strategies, min_change, min_turn, max_turn, max_stocks,
            exclude_st=exclude_st, exclude_gem_star=exclude_gem_star, min_amount=min_amount,
        )

    result = st.session_state.get("result")
    watchlist_rows = db.watchlist_load()
    watchlist_codes = {r[0] for r in watchlist_rows}

    # ========================== Top Flat Header & Lazy Native Nav ==========================
    charts._render_hero(local_status, strategies, auto_sync=auto_state, result=result)

    if auto_state["running"]:
        b1, b2 = st.columns([6, 1], vertical_alignment="center")
        with b1:
            st.progress(min(1.0, max(0.02, auto_state["ratio"])), text=f"{auto_state['text']}（{auto_state['elapsed']:.0f}s）")
        with b2:
            if st.button("刷新", width="stretch", key="refresh_bg_sync_btn"):
                st.rerun()

    hit_txt = f" ({len(result)})" if result is not None and not result.empty else ""
    wl_txt = f" ({len(watchlist_rows)})" if watchlist_rows else ""
    nav_labels = {
        "trade": f"行情终端{hit_txt}",
        "watch": f"自选快查{wl_txt}",
        "wiki": "策略矩阵 (20)",
        "data": "数据缓存",
    }
    active_nav = st.segmented_control(
        "导航",
        options=list(nav_labels.keys()),
        format_func=lambda k: nav_labels[k],
        default="trade",
        label_visibility="collapsed",
        key="main_nav_seg",
    ) or "trade"

    # ========================== Tab 1: OKX / TradingView 主图在左 · 信号榜在右 ==========================
    if active_nav == "trade":
        if result is None:
            strat_tags = "".join(f'<span class="wait-tag">{escape(s)}</span>' for s in strategies)
            st.markdown(
                f'<div class="wait-panel">'
                f'<div class="wait-main">'
                f'<svg width="34" height="34" viewBox="0 0 24 24" fill="none">'
                f'<path d="M6 19V11M6 7V5M12 19V15M12 9V5M18 19V13M18 7V5" stroke="#555c69" stroke-width="1.5" stroke-linecap="round"/>'
                f'<rect x="4" y="7" width="4" height="4" rx="0.8" stroke="#f6465d" stroke-width="1.4"/>'
                f'<rect x="10" y="9" width="4" height="6" rx="0.8" stroke="#0ecb81" stroke-width="1.4"/>'
                f'<rect x="16" y="7" width="4" height="6" rx="0.8" stroke="#f6465d" stroke-width="1.4"/>'
                f'</svg>'
                f'<div class="wait-title">选择策略与过滤条件后，点击开始选股</div>'
                f'<div class="wait-sub">本地已就绪 <strong style="color:#e6e8ec;">{local_status["stocks"]:,}</strong> 只 A 股日线'
                f'（最新 {escape(str(local_status["newest"] or "暂无"))} · 区间 {start:%Y-%m-%d} ~ {end:%Y-%m-%d}）。'
                f'选股完成后将自动呈现右侧信号榜与左侧 TradingView 多指标 K 线图。</div>'
                f'<div class="wait-tags">{strat_tags}</div>'
                f'</div>'
                f'<div class="wait-steps">'
                f'<div class="wait-step"><div class="wait-step-num">01 / 配置参数</div><div class="wait-step-txt">在左侧勾选策略流派，并按需设置排除 ST、最低涨幅与成交额</div></div>'
                f'<div class="wait-step"><div class="wait-step-num">02 / 极速扫描</div><div class="wait-step-txt">点击「开始选股」，纯 NumPy 引擎离线全市场扫描（无需等待网络）</div></div>'
                f'<div class="wait-step"><div class="wait-step-num">03 / 分屏复盘</div><div class="wait-step-txt">点击右侧信号行即时切换左侧 K 线，支持 BOLL/EMA/KDJ/RSI 等 14 项指标</div></div>'
                f'</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
            _, c_btn, _ = st.columns([2.2, 1.2, 2.2])
            with c_btn:
                if st.button("开始选股", type="primary", width="stretch", disabled=not valid_params, key="quick_start_scan_btn"):
                    st.session_state["trigger_scan"] = True
                    st.rerun()
        elif result.empty:
            st.info("当前过滤条件下暂无命中标的，可在左侧放宽最低涨幅或成交额条件后重新选股。")
        else:
            hit_strategies = list(result["策略"].unique())
            if len(hit_strategies) > 1:
                pill_opts = ["全部"] + hit_strategies
                if st.session_state.get("result_strategy_pill") not in pill_opts:
                    st.session_state["result_strategy_pill"] = "全部"
                filter_strategy = st.pills(
                    "策略过滤",
                    options=pill_opts,
                    selection_mode="single",
                    label_visibility="collapsed",
                    key="result_strategy_pill",
                ) or "全部"
            else:
                st.session_state.pop("result_strategy_pill", None)
                filter_strategy = "全部"

            # OKX / TradingView 经典格局：左 62% 主图终端 · 右 38% 高密度行情信号列表
            if mobile_compact:
                chart_col = st.container()
                list_col = st.container()
            else:
                chart_col, list_col = st.columns([1.70, 1.08], gap="small")

            # 先计算右侧列表过滤结果与选中标的
            with list_col:
                c_mode, c_exp = st.columns([1.45, 0.85], vertical_alignment="center")
                with c_mode:
                    view_mode = st.segmented_control(
                        "模式",
                        options=["共振", "全部"],
                        default="共振",
                        label_visibility="collapsed",
                        key="signal_view_mode",
                    ) or "共振"
                with c_exp:
                    export_codes = result["代码"].unique().tolist()
                    with st.popover("导出", width="stretch"):
                        st.download_button(
                            "信号表 (.csv)",
                            data=result.to_csv(index=False).encode("utf-8-sig"),
                            file_name=f"klineradar_{date.today():%Y%m%d}.csv",
                            mime="text/csv",
                            width="stretch",
                        )
                        st.download_button(
                            "通达信板块 (.ebk)",
                            data=engine._format_tdx_ebk(export_codes),
                            file_name=f"klineradar_{date.today():%Y%m%d}.ebk",
                            mime="application/octet-stream",
                            width="stretch",
                        )
                        plain_txt = engine._format_plain_codes(export_codes)
                        st.download_button(
                            "纯 6 位代码 (.txt)",
                            data=plain_txt.encode("utf-8"),
                            file_name=f"klineradar_codes_{date.today():%Y%m%d}.txt",
                            mime="text/plain",
                            width="stretch",
                        )
                        st.code(plain_txt, language="text")

                keyword = st.text_input("搜索", placeholder="搜索代码或名称…", label_visibility="collapsed")

                filtered = result.copy()
                if filter_strategy != "全部":
                    filtered = filtered[filtered["策略"] == filter_strategy]
                if keyword.strip():
                    kw = keyword.strip().lower()
                    filtered = filtered[
                        filtered["代码"].str.lower().str.contains(kw, na=False)
                        | filtered["名称"].str.lower().str.contains(kw, na=False)
                    ]
                filtered = filtered.reset_index(drop=True)

                if filtered.empty:
                    st.info("无匹配标的")
                    display_df = pd.DataFrame()
                    table_event = None
                else:
                    sparks = st.session_state.get("sparks", {})
                    if view_mode == "共振":
                        agg_rows = []
                        for code, grp in filtered.groupby("代码", sort=False):
                            first = grp.iloc[0]
                            strats = grp["策略"].unique().tolist()
                            agg_rows.append({
                                "名称": first["名称"],
                                "代码": code,
                                "现价": first["收盘"],
                                "涨跌幅%": first["涨跌幅%"],
                                "共振": len(strats),
                                "换手%": first["换手率%"],
                                "额(亿)": first["成交额(亿)"] if "成交额(亿)" in first else float("nan"),
                                "20日": sparks.get(code, []),
                                "策略": " · ".join(strats),
                            })
                        display_df = pd.DataFrame(agg_rows).sort_values(
                            ["共振", "涨跌幅%"], ascending=[False, False]
                        ).reset_index(drop=True)
                        col_cfg = {
                            "名称": st.column_config.TextColumn("名称", width=76),
                            "代码": st.column_config.TextColumn("代码", width=82),
                            "现价": st.column_config.NumberColumn("现价", format="%.2f", width=62),
                            "涨跌幅%": st.column_config.NumberColumn("涨跌", format="%+.2f%%", width=72),
                            "共振": st.column_config.NumberColumn("共振", format="%d", width=46),
                            "换手%": st.column_config.NumberColumn("换手", format="%.1f%%", width=62),
                            "额(亿)": st.column_config.NumberColumn("额", format="%.1f", width=56),
                            "20日": st.column_config.LineChartColumn("走势", width=78),
                            "策略": st.column_config.TextColumn("策略组合", width="medium"),
                        }
                        show_cols = ["名称", "现价", "涨跌幅%", "共振", "换手%", "额(亿)", "20日", "代码", "策略"]
                    else:
                        display_df = filtered.rename(
                            columns={"收盘": "现价", "换手率%": "换手%", "成交额(亿)": "额(亿)"}
                        ).copy()
                        display_df["20日"] = display_df["代码"].map(lambda c: sparks.get(c, []))
                        col_cfg = {
                            "名称": st.column_config.TextColumn("名称", width="small"),
                            "代码": st.column_config.TextColumn("代码", width="small"),
                            "现价": st.column_config.NumberColumn("现价", format="%.2f", width="small"),
                            "涨跌幅%": st.column_config.NumberColumn("涨跌", format="%+.2f%%", width="small"),
                            "策略": st.column_config.TextColumn("策略", width="medium"),
                            "换手%": st.column_config.NumberColumn("换手", format="%.1f%%", width="small"),
                            "额(亿)": st.column_config.NumberColumn("额", format="%.1f", width="small"),
                            "20日": st.column_config.LineChartColumn("走势", width="small"),
                        }
                        show_cols = ["名称", "现价", "涨跌幅%", "策略", "换手%", "额(亿)", "20日", "代码"]

                    if mobile_compact:
                        show_cols = show_cols[:4]

                    table_event = st.dataframe(
                        display_df[show_cols].style.map(charts._style_pct, subset=["涨跌幅%"]),
                        width="stretch",
                        hide_index=True,
                        height=300 if mobile_compact else 612,
                        on_select="rerun",
                        selection_mode="single-row",
                        key=f"signal_table_{view_mode}_{mobile_compact}",
                        column_config=col_cfg,
                    )

            with chart_col:
                if not display_df.empty:
                    stocks_df = display_df[["代码", "名称"]].drop_duplicates("代码")
                    name_by_code = dict(stocks_df.itertuples(index=False, name=None))
                    stock_codes = list(name_by_code.keys())

                    selected_rows = table_event.selection.rows if table_event and hasattr(table_event, "selection") else []
                    if selected_rows and selected_rows[0] < len(display_df):
                        clicked_code = display_df.iloc[selected_rows[0]]["代码"]
                        if clicked_code != st.session_state.get("last_clicked_code"):
                            st.session_state["last_clicked_code"] = clicked_code
                            st.session_state["active_stock"] = clicked_code

                    if st.session_state.get("active_stock") not in stock_codes:
                        st.session_state["active_stock"] = stock_codes[0]

                    current_idx = stock_codes.index(st.session_state["active_stock"])

                    # 全部使用统一原生组件的一行控制条（零截断、零高度错位）
                    c1, c2, c3, c4, c5 = st.columns([2.25, 0.48, 0.48, 0.90, 2.05], vertical_alignment="center")
                    with c1:
                        choice = st.selectbox(
                            "标的",
                            options=stock_codes,
                            index=current_idx,
                            format_func=lambda code: f"{name_by_code[code]} {code}",
                            label_visibility="collapsed",
                        )
                        if choice != st.session_state["active_stock"]:
                            st.session_state["active_stock"] = choice
                            current_idx = stock_codes.index(choice)
                    with c2:
                        if st.button("", icon=":material/chevron_left:", width="stretch", disabled=current_idx <= 0, help="上一只"):
                            st.session_state["active_stock"] = stock_codes[current_idx - 1]
                            st.rerun()
                    with c3:
                        if st.button("", icon=":material/chevron_right:", width="stretch", disabled=current_idx >= len(stock_codes) - 1, help="下一只"):
                            st.session_state["active_stock"] = stock_codes[current_idx + 1]
                            st.rerun()
                    with c4:
                        cur_code = st.session_state["active_stock"]
                        in_wl = cur_code in watchlist_codes
                        if st.button("已自选" if in_wl else "+ 自选", width="stretch", key=f"wl_toggle_{cur_code}"):
                            if in_wl:
                                db.watchlist_remove(cur_code)
                            else:
                                db.watchlist_add(cur_code, name_by_code[cur_code])
                            st.rerun()
                    with c5:
                        interval = st.segmented_control(
                            "周期",
                            options=["日线", "周线", "月线"],
                            default="日线",
                            label_visibility="collapsed",
                            key="kline_interval_seg",
                        ) or "日线"

                    choice = st.session_state["active_stock"]
                    start_date, end_date = st.session_state["range"]
                    hit_list = result[result["代码"] == choice]["策略"].unique().tolist()
                    _render_stock_terminal(
                        choice, name_by_code[choice], start_date, end_date, interval,
                        mobile_compact=mobile_compact, hit_list=hit_list,
                        chart_key_prefix="chart",
                    )

            # 历史胜率速验（仅结束日期早于最新交易日时显示）
            if not filtered.empty:
                scan_end_dt = st.session_state.get("range", (start, end))[1]
                scan_end_str = scan_end_dt.strftime("%Y-%m-%d")
                newest_db_str = str(local_status.get("newest") or "")
                if newest_db_str and scan_end_str < newest_db_str:
                    fwd_df = engine.compute_forward_returns(filtered, scan_end_str)
                    with st.expander(f"历史胜率速验（信号日 {scan_end_str} 后 5 / 10 日表现）", expanded=not fwd_df.empty):
                        if fwd_df.empty:
                            st.caption("信号日后交易日不足 5 天。")
                        else:
                            st.dataframe(
                                fwd_df,
                                width="stretch",
                                hide_index=True,
                                column_config={
                                    "5日胜率%": st.column_config.NumberColumn("5日胜率", format="%.1f%%"),
                                    "5日均涨幅%": st.column_config.NumberColumn("5日均涨幅", format="%+.2f%%"),
                                    "10日胜率%": st.column_config.NumberColumn("10日胜率", format="%.1f%%"),
                                    "10日均涨幅%": st.column_config.NumberColumn("10日均涨幅", format="%+.2f%%"),
                                },
                            )

    # ========================== Tab 2: 自选快查（左主图 · 右自选） ==========================
    elif active_nav == "watch":
        all_universe = db.cache_universe_load()
        all_name_map = dict(all_universe)
        all_codes_list = [c for c, _ in all_universe]

        if all_codes_list:
            q1, q2 = st.columns([4.2, 0.8], vertical_alignment="center")
            with q1:
                quick_pick = st.selectbox(
                    "快查",
                    options=[""] + all_codes_list,
                    format_func=lambda c: "输入代码或名称查看全市场任意 A 股 K 线（如 600519 / 茅台）" if not c else f"{all_name_map[c]} ({c})",
                    label_visibility="collapsed",
                    key="quick_lookup_select",
                )
            with q2:
                if quick_pick:
                    q_in_wl = quick_pick in watchlist_codes
                    if st.button("移出自选" if q_in_wl else "+ 加自选", width="stretch", key=f"ql_wl_btn_{quick_pick}"):
                        if q_in_wl:
                            db.watchlist_remove(quick_pick)
                        else:
                            db.watchlist_add(quick_pick, all_name_map[quick_pick])
                        st.rerun()
            if quick_pick and quick_pick != st.session_state.get("last_quick_pick"):
                st.session_state["last_quick_pick"] = quick_pick
                st.session_state["wl_active_stock"] = quick_pick

        if mobile_compact:
            wl_chart_col = st.container()
            wl_list_col = st.container()
        else:
            wl_chart_col, wl_list_col = st.columns([1.85, 1.0], gap="small")

        with wl_list_col:
            if not watchlist_rows:
                st.info("自选池为空。可在上方搜索框查股添加，或在行情终端中点「+ 自选」。")
            else:
                wl_codes_list = [r[0] for r in watchlist_rows]
                db._init_cache()
                with db.connect(timeout=30) as conn:
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
                for code, name, _added_at in watchlist_rows:
                    _dt, cl, pct, trn, amt = latest_map.get(code, (None, None, None, None, None))
                    wl_data.append({
                        "名称": name,
                        "代码": code,
                        "现价": float(cl) if cl is not None else float("nan"),
                        "涨跌幅%": float(pct) if pct is not None else float("nan"),
                        "换手%": float(trn) if trn is not None else float("nan"),
                        "额(亿)": round(float(amt) / 1e8, 2) if amt is not None else float("nan"),
                    })
                wl_df = pd.DataFrame(wl_data)
                wl_event = st.dataframe(
                    wl_df.style.map(charts._style_pct, subset=["涨跌幅%"]),
                    width="stretch",
                    hide_index=True,
                    height=260 if mobile_compact else 612,
                    on_select="rerun",
                    selection_mode="single-row",
                    key="watchlist_table",
                    column_config={
                        "现价": st.column_config.NumberColumn("现价", format="%.2f"),
                        "涨跌幅%": st.column_config.NumberColumn("涨跌", format="%+.2f%%"),
                        "换手%": st.column_config.NumberColumn("换手", format="%.1f%%"),
                        "额(亿)": st.column_config.NumberColumn("额", format="%.1f"),
                    },
                )
                wl_sel = wl_event.selection.rows if wl_event and hasattr(wl_event, "selection") else []
                if wl_sel and wl_sel[0] < len(wl_df):
                    clicked_wl = wl_df.iloc[wl_sel[0]]["代码"]
                    if clicked_wl != st.session_state.get("last_wl_clicked"):
                        st.session_state["last_wl_clicked"] = clicked_wl
                        st.session_state["wl_active_stock"] = clicked_wl

        with wl_chart_col:
            wl_name_map = {**all_name_map, **{r[0]: r[1] for r in watchlist_rows}}
            active_wl = st.session_state.get("wl_active_stock")
            if active_wl not in wl_name_map:
                active_wl = watchlist_rows[0][0] if watchlist_rows else (all_codes_list[0] if all_codes_list else "")
            if active_wl:
                wc1, wc2, wc3 = st.columns([2.2, 0.9, 1.5], vertical_alignment="center")
                with wc1:
                    st.markdown(f"**{escape(wl_name_map[active_wl])}** `{escape(active_wl)}`")
                with wc2:
                    if active_wl in watchlist_codes:
                        if st.button("移出自选", width="stretch", key=f"wl_remove_{active_wl}"):
                            db.watchlist_remove(active_wl)
                            st.rerun()
                    else:
                        if st.button("+ 自选", width="stretch", key=f"wl_add_{active_wl}"):
                            db.watchlist_add(active_wl, wl_name_map[active_wl])
                            st.rerun()
                with wc3:
                    wl_interval = st.segmented_control(
                        "自选周期",
                        options=["日线", "周线", "月线"],
                        default="日线",
                        label_visibility="collapsed",
                        key="wl_interval_seg",
                    ) or "日线"
                _render_stock_terminal(
                    active_wl, wl_name_map[active_wl], start, end, wl_interval,
                    mobile_compact=mobile_compact,
                    chart_key_prefix="wl_chart",
                )

    # ========================== Tab 3: 策略矩阵 ==========================
    elif active_nav == "wiki":
        group_opts = ["全部"] + list(engine.STRATEGY_GROUPS.keys())
        group_choice = st.pills("流派", options=group_opts, default="全部", label_visibility="collapsed", key="wiki_group_pill") or "全部"
        groups_to_show = (
            [(group_choice, engine.STRATEGY_GROUPS[group_choice])]
            if group_choice != "全部"
            else list(engine.STRATEGY_GROUPS.items())
        )
        for g_name, meta in groups_to_show:
            gh1, gh2 = st.columns([4.5, 1.0], vertical_alignment="center")
            with gh1:
                st.markdown(f"**{escape(g_name)}** · {escape(meta['desc'])}")
            with gh2:
                st.button(f"应用 {g_name}", key=f"apply_grp_{g_name}", width="stretch", on_click=_apply_strategy_preset, args=(meta["items"],))

            cols = st.columns(4)
            for idx, s_name in enumerate(meta["items"]):
                is_on = s_name in strategies
                mark = '<span style="color:#f6465d;font-size:0.7rem;">已选</span>' if is_on else ""
                with cols[idx % 4]:
                    st.markdown(
                        f'<div class="strat-row {"on" if is_on else ""}">'
                        f'<div class="strat-row-h"><span class="strat-row-name">{escape(s_name)}</span>{mark}</div>'
                        f'<div class="strat-row-desc">{escape(engine.STRATEGIES[s_name])}</div>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

    # ========================== Tab 4: 数据缓存 ==========================
    elif active_nav == "data":
        charts._render_kpi_cards(local_status, result, st.session_state.get("scan_count"))

        ac1, ac2, ac3, ac4 = st.columns([1.2, 1.2, 2.0, 1.2], vertical_alignment="bottom")
        with ac1:
            tab_enabled = st.toggle("收盘后自动同步", value=auto_state["enabled"], key="tab_auto_sync_toggle")
        with ac2:
            time_idx = db.AUTO_SYNC_TIMES.index(auto_state["sync_time"]) if auto_state["sync_time"] in db.AUTO_SYNC_TIMES else 2
            tab_time = st.selectbox("触发时间", options=db.AUTO_SYNC_TIMES, index=time_idx, key="tab_auto_sync_time")
        if tab_enabled != auto_state["enabled"] or tab_time != auto_state["sync_time"]:
            db.save_auto_sync_config(tab_enabled, tab_time)
            st.rerun()
        with ac3:
            st.caption(f"今日状态：{auto_state['today_status']}（上次自动更新：{auto_state['last_date'] or '暂无'}）")
        with ac4:
            if st.button("后台更新一次", width="stretch", disabled=auto_state["running"]):
                if sync_mgr.trigger_now():
                    st.toast("已启动后台更新")
                    st.rerun()

        c_left, c_right = st.columns([1.3, 1.0])
        with c_left:
            info_df = pd.DataFrame([
                {"项目": "SQLite 路径", "数值": str(db.CACHE_DB)},
                {"项目": "文件体积", "数值": f"{local_status['size_mb']:.2f} MB"},
                {"项目": "已缓存标的", "数值": f"{local_status['stocks']:,} 只"},
                {"项目": "日线总行数", "数值": f"{local_status['rows']:,} 条"},
                {"项目": "起止日期", "数值": f"{local_status['earliest'] or '暂无'} ~ {local_status['newest'] or '暂无'}"},
            ])
            st.dataframe(info_df, width="stretch", hide_index=True)

        with c_right:
            last_auto = auto_state.get("last_info") or {}
            u = st.session_state.get("update_stats")
            if u:
                st.caption(f"本次同步（{u.get('source', '自动')}）：更新 {u.get('updated', 0)} · 跳过 {u.get('skipped', 0)} · 失败 {u.get('failed', 0)}")
                if u.get("failures"):
                    st.dataframe(pd.DataFrame(u["failures"], columns=["代码", "原因"]), width="stretch", hide_index=True, height=180)
            elif last_auto:
                st.caption(f"上次同步：{last_auto.get('trigger', '')} {last_auto.get('time', '')} · 更新 {last_auto.get('updated', 0)} · 失败 {last_auto.get('failed', 0)}")
            else:
                st.caption("现有 SQLite 缓存可直接离线选股。")


if __name__ == "__main__":
    main()
