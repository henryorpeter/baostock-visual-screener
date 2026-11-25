"""KLineRadar · A股技术形态选股终端（Streamlit 主界面入口）。

运行：streamlit run app.py  或  streamlit run baostock_screener.py
"""
from datetime import date, datetime, timedelta
from html import escape
import json
import time

import pandas as pd
import streamlit as st
import streamlit_antd_components as sac
from lightweight_charts_v5 import lightweight_charts_v5_component

import charts
import db
import engine
import sync


def _apply_strategy_preset(names):
    st.session_state["pending_strategies"] = list(names)


def _apply_date_window(days):
    st.session_state["pending_window_days"] = days


def _trigger_scan():
    st.session_state["trigger_scan"] = True



def _run_update_action(start, end):
    if not sync.SYNC_LOCK.acquire(blocking=False):
        st.info("后台正在执行自动行情更新，请稍候完成后再试。")
        return
    end_str = end.strftime("%Y-%m-%d")
    tracker = charts.FloatingProgress("正在更新全市场行情缓存", ("校验交易日与股票池", "批量截面/并发拉取", "写入本地库"))
    try:
        tracker.update(0.05, stage=1, detail="正在加载股票池并核对最近已收盘交易日…")
        try:
            all_codes, list_date, list_source = sync._get_universe_for_sync(end_str)
        except Exception as exc:
            tracker.close()
            st.error(f"获取股票列表失败：{exc}")
            return
        if not all_codes:
            tracker.close()
            st.error("所选日期及之前 10 天内都没有可用的股票列表，请稍后重试。")
            return
        tracker.update(0.15, stage=2, detail=f"已就绪 {len(all_codes):,} 只股票（来源：{list_source}），正在检查本地缓存区间…")
        update_stats = sync.update_cache(all_codes, start.strftime("%Y-%m-%d"), end_str, tracker)
        tracker.update(1.0, stage=3, detail="全市场行情同步完成！")
        time.sleep(0.2)
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
    tracker = charts.FloatingProgress(
        f"正在执行本地策略选股（{len(strategies)} 套策略）",
        ("读取 SQLite 缓存", "技术指标与策略匹配", "生成信号与走势"),
    )
    started = time.monotonic()
    try:
        tracker.update(0.02, stage=1, detail="正在加载本地股票池…")
        universe = db.cache_universe_load()
        if not universe:
            tracker.close()
            st.error("本地还没有股票列表，请先点击「更新全市场行情缓存」。")
            return
        limit = min(int(max_stocks), len(universe))
        stride = max(1, len(universe) // limit)
        selected = universe[::stride][:limit]
        # 为保证 MA60/MACD/RSI/RPS 等指标充分预热且数据精准，后台自动向前多读取前置历史行情
        lookback_start = min(start, end - timedelta(days=210))
        db_rows, db_newest, db_rev = db._cache_version()
        tracker.update(0.08, stage=1, detail=f"正在从本地缓存加载全市场行情（共 {db_rows:,} 条记录）…")
        frames = engine._load_market_arrays_cached(
            lookback_start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), db_rows, db_newest, db_rev
        )
        if not frames:
            tracker.close()
            st.error("本地没有所选日期范围的行情数据，请先点击「更新全市场行情缓存」。")
            return
        tracker.update(0.22, stage=2, detail=f"已就绪 {len(frames):,} 只标的行情，开始极速策略扫描…")
        result, scan_stats = engine.screen(
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




def main():
    # ========================== Page Setup ==========================
    st.set_page_config(
        page_title="KLineRadar · A股技术形态选股终端",
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    charts._inject_custom_css()

    today = date.today()
    if "selected_strategies" not in st.session_state:
        st.session_state["selected_strategies"] = [next(iter(engine.STRATEGIES))]
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
        p1.button("趋势突破", use_container_width=True, on_click=_apply_strategy_preset, args=(engine.STRATEGY_GROUPS["趋势突破"]["items"],))
        p2.button("涨停战法", use_container_width=True, on_click=_apply_strategy_preset, args=(engine.STRATEGY_GROUPS["涨停战法"]["items"],))
        p3, p4 = st.columns(2)
        p3.button("均线共振", use_container_width=True, on_click=_apply_strategy_preset, args=(engine.STRATEGY_GROUPS["均线共振"]["items"],))
        p4.button("形态反转", use_container_width=True, on_click=_apply_strategy_preset, args=(engine.STRATEGY_GROUPS["形态反转"]["items"],))

        strategies = st.multiselect(
            "已选策略（支持多选）",
            options=list(engine.STRATEGIES.keys()),
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

        local_status = db.cache_status()
        sync_mgr = sync.get_auto_sync_manager()
        auto_cfg = db.get_auto_sync_config()
        auto_toggle = st.toggle(
            f"收盘后自动更新 ({auto_cfg['sync_time']})",
            value=auto_cfg["enabled"],
            help="每个交易日（周一至周五）收盘后自动在后台增量更新一次全市场日线，可在「数据源与缓存中心」自定义时间。",
            key="sb_auto_sync_toggle",
        )
        if auto_toggle != auto_cfg["enabled"]:
            db.save_auto_sync_config(auto_toggle, auto_cfg["sync_time"])
            st.rerun()

        auto_state = sync_mgr.snapshot(local_status=local_status)
        st.caption(f"自动更新状态：{auto_state['today_status']} · 日常选股全程离线读取 SQLite。")


    # ========================== Main Header & Navigation ==========================
    charts._render_hero(local_status, strategies, auto_sync=auto_state)

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
        local_status = db.cache_status(force=True)
        auto_state = sync_mgr.snapshot(local_status=local_status)

    should_scan = run_btn or st.session_state.pop("trigger_scan", False)
    if should_scan and valid_params:
        _run_screen_action(
            start, end, strategies, min_change, min_turn, max_turn, max_stocks,
            exclude_st=exclude_st, exclude_gem_star=exclude_gem_star, min_amount=min_amount,
        )

    result = st.session_state.get("result")
    hit_badge = sac.Tag(f"{len(result)}", color="red", bordered=False) if result is not None and not result.empty else None
    watchlist_rows = db.watchlist_load()
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
        charts._render_kpi_cards(local_status, result, st.session_state.get("scan_count"))

        if strategies:
            tag_items = [
                sac.Tag(s, color=engine.STRATEGY_GROUPS[engine.STRATEGY_TO_GROUP[s]]["tag_color"], bordered=False)
                for s in strategies
            ]
            with st.expander(f"当前启用策略逻辑（已选 {len(strategies)} 项）", expanded=False):
                sac.tags(tag_items, align="start", size="sm")
                for s in strategies:
                    st.markdown(f"- **{s}**：{engine.STRATEGIES[s]}")

        if result is None:
            pills_html = "".join(
                f'<span class="strat-badge" style="color:{engine.STRATEGY_GROUPS[engine.STRATEGY_TO_GROUP[s]]["color"]};'
                f'background:{engine.STRATEGY_GROUPS[engine.STRATEGY_TO_GROUP[s]]["bg"]};'
                f'border:1px solid {engine.STRATEGY_GROUPS[engine.STRATEGY_TO_GROUP[s]]["color"]}44;">{escape(s)}</span>'
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
            bar_c1, bar_c2, bar_c3, bar_c4 = st.columns([2.0, 1.1, 1.0, 0.9], vertical_alignment="bottom")
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
                export_codes = result["代码"].unique().tolist()
                with st.popover("导出 / 导入", use_container_width=True, icon=":material/download:"):
                    st.download_button(
                        "导出完整信号表 (.csv)",
                        data=result.to_csv(index=False).encode("utf-8-sig"),
                        file_name=f"klineradar_screen_{date.today():%Y%m%d}.csv",
                        mime="text/csv",
                        use_container_width=True,
                    )
                    st.download_button(
                        "导出通达信板块 (.ebk)",
                        data=engine._format_tdx_ebk(export_codes),
                        file_name=f"klineradar_{date.today():%Y%m%d}.ebk",
                        mime="application/octet-stream",
                        use_container_width=True,
                    )
                    plain_txt = engine._format_plain_codes(export_codes)
                    st.download_button(
                        "导出纯 6 位代码 (.txt)",
                        data=plain_txt.encode("utf-8"),
                        file_name=f"klineradar_codes_{date.today():%Y%m%d}.txt",
                        mime="text/plain",
                        use_container_width=True,
                    )
                    st.caption("纯 6 位代码（点击右上角一键复制，可直接粘贴进同花顺/东财自选股）：")
                    st.code(plain_txt, language="text")

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

                styled_df = table_df.style.map(charts._style_pct, subset=["涨跌幅%"])
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
                    fwd_df = engine.compute_forward_returns(filtered, scan_end_str)
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
                            db.watchlist_remove(cur_code)
                            st.toast(f"已移出自选：{name_by_code[cur_code]} ({cur_code})")
                        else:
                            db.watchlist_add(cur_code, name_by_code[cur_code])
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
                # 周线 / 月线直接读取本地全历史日线，避免 MA20/30 与 MACD 指标因区间过短留空
                if interval in ("周线", "月线"):
                    chart_fetch_start_str = "1990-01-01"
                    chart_display_start = None
                else:
                    chart_fetch_start_str = (start_date - timedelta(days=150)).strftime("%Y-%m-%d")
                    chart_display_start = start_date
                frame = db._cache_read(choice, chart_fetch_start_str, end_date.strftime("%Y-%m-%d"))

                if not frame.empty:
                    latest = frame.iloc[-1]
                    prior = frame.iloc[-2].close if len(frame) > 1 else latest.close
                    change = (latest.close / prior - 1) * 100 if prior else 0.0
                    amp = ((latest.high - latest.low) / prior * 100) if prior else 0.0
                    feat = engine.indicators(frame)
                    rsi_val = f"{feat.iloc[-1].rsi:.1f}" if not feat.empty else "—"
                    macd_val = feat.iloc[-1].macd - feat.iloc[-1].signal if not feat.empty else float("nan")
                    macd_text = ("多头金叉区" if macd_val > 0 else "空头整理区") if pd.notna(macd_val) else "—"
                    turn_text = f"{latest.turn:.2f}%" if pd.notna(latest.turn) else "—"

                    color_cls = "is-up" if change > 0 else ("is-down" if change < 0 else "is-flat")
                    chg_cls = "chg-up" if change > 0 else ("chg-down" if change < 0 else "chg-flat")

                    hit_list = result[result["代码"] == choice]["策略"].unique().tolist()
                    hit_pills = "".join(
                        f'<span class="strat-badge" style="color:{engine.STRATEGY_GROUPS[engine.STRATEGY_TO_GROUP[s]]["color"]};'
                        f'background:{engine.STRATEGY_GROUPS[engine.STRATEGY_TO_GROUP[s]]["bg"]};'
                        f'border:1px solid {engine.STRATEGY_GROUPS[engine.STRATEGY_TO_GROUP[s]]["color"]}44;">{escape(s)}</span>'
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
                            <div class="t-stat"><div class="t-stat-k">成交额</div><div class="t-stat-v">{charts._fmt_amount(latest.amount)}</div></div>
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

                    panes, kdf = charts.chart_panes(
                        frame, f"{name_by_code[choice]} · {choice}", interval,
                        show_ma=show_ma, show_macd=show_macd, display_start=chart_display_start,
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
            <div class="sec-title"><span class="sec-dot"></span>全市场快速查股 & 本地自选观察池</div>
            <div class="sec-meta">无需选股也可直接输入代码/名称查看任意 A 股 K 线，并一键加入自选持久化跟踪</div>
        </div>
        """, unsafe_allow_html=True)

        all_universe = db.cache_universe_load()
        all_name_map = dict(all_universe)
        all_codes_list = [c for c, _ in all_universe]

        if all_codes_list:
            q1, q2 = st.columns([3.4, 1.0], vertical_alignment="bottom")
            with q1:
                quick_pick = st.selectbox(
                    "快速查股（输入代码或名称搜索全市场 A 股）",
                    options=[""] + all_codes_list,
                    format_func=lambda c: "🔍 输入代码或股票名称快速看图（如 600519 / 茅台）…" if not c else f"{all_name_map[c]}  ({c})",
                    key="quick_lookup_select",
                )
            with q2:
                if quick_pick:
                    q_in_wl = quick_pick in watchlist_codes
                    if st.button("★ 已在自选" if q_in_wl else "☆ 加入自选", use_container_width=True, key=f"ql_wl_btn_{quick_pick}"):
                        if q_in_wl:
                            db.watchlist_remove(quick_pick)
                            st.toast(f"已移出自选：{all_name_map[quick_pick]} ({quick_pick})")
                        else:
                            db.watchlist_add(quick_pick, all_name_map[quick_pick])
                            st.toast(f"已加入自选：{all_name_map[quick_pick]} ({quick_pick})", icon="⭐")
                        st.rerun()
            if quick_pick and quick_pick != st.session_state.get("last_quick_pick"):
                st.session_state["last_quick_pick"] = quick_pick
                st.session_state["wl_active_stock"] = quick_pick

        if not watchlist_rows:
            sac.alert(
                label="自选观察池为空",
                description="可在上方搜索框直接查股并点击「☆ 加入自选」，或在「策略选股工作台」选中任意个股加入自选。",
                color="info",
                icon=True,
            )
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
                wl_df.style.map(charts._style_pct, subset=["涨跌幅%"]),
                use_container_width=True,
                hide_index=True,
                height=260,
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
                clicked_wl = wl_df.iloc[wl_sel[0]]["代码"]
                if clicked_wl != st.session_state.get("last_wl_clicked"):
                    st.session_state["last_wl_clicked"] = clicked_wl
                    st.session_state["wl_active_stock"] = clicked_wl

        # 统一渲染当前选中个股（来自快速搜股或自选表）的深度行情与多周期 K 线
        wl_name_map = {**all_name_map, **{r[0]: r[1] for r in watchlist_rows}}
        active_wl = st.session_state.get("wl_active_stock")
        if active_wl not in wl_name_map:
            active_wl = watchlist_rows[0][0] if watchlist_rows else ""
        if active_wl:
            wc1, wc2, wc3 = st.columns([2.2, 0.9, 1.9], vertical_alignment="bottom")
            with wc1:
                st.markdown(f"**当前看盘：{escape(wl_name_map[active_wl])}（`{escape(active_wl)}`）**")
            with wc2:
                if active_wl in watchlist_codes:
                    if st.button("移出自选", use_container_width=True, key=f"wl_remove_{active_wl}"):
                        db.watchlist_remove(active_wl)
                        st.toast(f"已移出自选：{wl_name_map[active_wl]} ({active_wl})")
                        st.rerun()
                else:
                    if st.button("☆ 加自选", use_container_width=True, key=f"wl_add_{active_wl}"):
                        db.watchlist_add(active_wl, wl_name_map[active_wl])
                        st.toast(f"已加入自选：{wl_name_map[active_wl]} ({active_wl})", icon="⭐")
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
            if wl_interval in ("周线", "月线"):
                wl_fetch_start, wl_disp_start = "1990-01-01", None
            else:
                wl_fetch_start, wl_disp_start = (start - timedelta(days=150)).strftime("%Y-%m-%d"), start
            wl_frame = db._cache_read(active_wl, wl_fetch_start, end.strftime("%Y-%m-%d"))
            if not wl_frame.empty:
                wl_latest = wl_frame.iloc[-1]
                wl_prior = wl_frame.iloc[-2].close if len(wl_frame) > 1 else wl_latest.close
                wl_chg = (wl_latest.close / wl_prior - 1) * 100 if wl_prior else 0.0
                wl_amp = ((wl_latest.high - wl_latest.low) / wl_prior * 100) if wl_prior else 0.0
                wl_feat = engine.indicators(wl_frame)
                wl_rsi = f"{wl_feat.iloc[-1].rsi:.1f}" if not wl_feat.empty else "—"
                wl_macd = wl_feat.iloc[-1].macd - wl_feat.iloc[-1].signal if not wl_feat.empty else float("nan")
                wl_macd_txt = ("多头金叉区" if wl_macd > 0 else "空头整理区") if pd.notna(wl_macd) else "—"
                wl_turn_txt = f"{wl_latest.turn:.2f}%" if pd.notna(wl_latest.turn) else "—"
                wl_c_cls = "is-up" if wl_chg > 0 else ("is-down" if wl_chg < 0 else "is-flat")
                wl_chg_cls = "chg-up" if wl_chg > 0 else ("chg-down" if wl_chg < 0 else "chg-flat")
                st.markdown(f"""
                <div class="ticker-card">
                    <div class="ticker-top">
                        <div class="ticker-name-wrap">
                            <span class="ticker-name">{escape(wl_name_map[active_wl])}</span>
                            <span class="ticker-code">{escape(active_wl)}</span>
                        </div>
                        <div class="ticker-price-wrap">
                            <span class="ticker-price {wl_c_cls}">¥{wl_latest.close:.2f}</span>
                            <span class="ticker-chg {wl_chg_cls}">{wl_chg:+.2f}%</span>
                            <span class="mono" style="font-size:0.78rem;color:#64748b;">{pd.Timestamp(wl_latest.date):%Y-%m-%d}</span>
                        </div>
                    </div>
                    <div class="ticker-stats">
                        <div class="t-stat"><div class="t-stat-k">今开</div><div class="t-stat-v">{wl_latest.open:.2f}</div></div>
                        <div class="t-stat"><div class="t-stat-k">最高</div><div class="t-stat-v is-up">{wl_latest.high:.2f}</div></div>
                        <div class="t-stat"><div class="t-stat-k">最低</div><div class="t-stat-v is-down">{wl_latest.low:.2f}</div></div>
                        <div class="t-stat"><div class="t-stat-k">振幅</div><div class="t-stat-v">{wl_amp:.2f}%</div></div>
                        <div class="t-stat"><div class="t-stat-k">换手率</div><div class="t-stat-v">{wl_turn_txt}</div></div>
                        <div class="t-stat"><div class="t-stat-k">成交额</div><div class="t-stat-v">{charts._fmt_amount(wl_latest.amount)}</div></div>
                        <div class="t-stat"><div class="t-stat-k">RSI (14)</div><div class="t-stat-v">{wl_rsi}</div></div>
                        <div class="t-stat"><div class="t-stat-k">MACD 趋势</div><div class="t-stat-v">{wl_macd_txt}</div></div>
                    </div>
                </div>
                """, unsafe_allow_html=True)
                wl_panes, _ = charts.chart_panes(wl_frame, f"{wl_name_map[active_wl]} · {active_wl}", wl_interval, show_ma=True, show_macd=True, display_start=wl_disp_start)
                lightweight_charts_v5_component(
                    name=f"wl {wl_name_map[active_wl]} {active_wl}",
                    charts=wl_panes,
                    height=440 if mobile_compact else 640,
                    zoom_level=65 if mobile_compact else 85,
                    key=f"wl_chart_{active_wl}_{wl_interval}_{mobile_compact}",
                )
            else:
                st.info(f"本地暂无 {wl_name_map[active_wl]}（{active_wl}）在该区间的日线缓存。")


    # ========================== Tab 3: 策略百科矩阵 ==========================
    elif active_tab == "策略百科矩阵":
        st.markdown("""
        <div class="sec-head">
            <div class="sec-title"><span class="sec-dot"></span>20 套 A 股技术形态策略矩阵</div>
            <div class="sec-meta">所有策略均基于本地日线 OHLCV 与量价/均线/MACD/KDJ/RSI 因子实时计算</div>
        </div>
        """, unsafe_allow_html=True)

        group_choice = sac.chip(
            items=["全部流派 (20)"] + [f"{g} ({len(meta['items'])})" for g, meta in engine.STRATEGY_GROUPS.items()],
            index=0,
            size="sm",
            radius="md",
            variant="light",
            color="red",
            key="wiki_group_chip",
        )

        selected_group_name = None if not group_choice or group_choice.startswith("全部") else group_choice.split(" ")[0]
        groups_to_show = (
            [(selected_group_name, engine.STRATEGY_GROUPS[selected_group_name])]
            if selected_group_name
            else list(engine.STRATEGY_GROUPS.items())
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
                        <div class="strat-desc">{escape(engine.STRATEGIES[s_name])}</div>
                        {active_html}
                    </div>
                    """, unsafe_allow_html=True)


    # ========================== Tab 3: 数据源与缓存中心 ==========================
    elif active_tab == "数据源与缓存中心":
        charts._render_kpi_cards(local_status, result, st.session_state.get("scan_count"))

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
            time_idx = db.AUTO_SYNC_TIMES.index(auto_state["sync_time"]) if auto_state["sync_time"] in db.AUTO_SYNC_TIMES else 2
            tab_time = st.selectbox("每日收盘后触发时间", options=db.AUTO_SYNC_TIMES, index=time_idx, key="tab_auto_sync_time")
        if tab_enabled != auto_state["enabled"] or tab_time != auto_state["sync_time"]:
            db.save_auto_sync_config(tab_enabled, tab_time)
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
                {"项目": "SQLite 路径", "数值": str(db.CACHE_DB)},
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


if __name__ == "__main__":
    main()
