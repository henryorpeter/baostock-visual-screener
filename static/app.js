/* KLineRadar Native Exchange Terminal Frontend
 * Uses bundled TradingView Lightweight Charts v5.0.7 + Starlette JSON API.
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const state = {
    theme: localStorage.getItem("kr_theme") || "dark",
    view: "terminal",
    bootstrap: null,

    // Screener parameters
    selectedStrategies: new Set(),
    activeGroup: "趋势突破",
    excludeSt: true,
    excludeGem: false,
    startDate: "",
    endDate: "",
    maxTurn: 100,
    maxStocks: 6000,

    // Scan results & right dock
    signals: [],
    aggregated: [],
    hitStrategies: [],
    forwardReturns: [],
    scanStats: null,
    scanRange: null,
    watchlist: [],
    watchlistCodes: new Set(),
    dockTab: "signals", // "signals" | "watchlist"
    dockMode: "agg",    // "agg" | "all"
    dockStrategy: "",
    dockQuery: "",
    dockSort: "resonance",
    visibleRows: [],

    // Active stock & chart
    currentCode: null,
    currentName: "",
    interval: "日线",
    overlays: new Set(["MA"]),
    subs: new Set(["VOL", "MACD"]),
    klineData: null,
    barByTime: new Map(),
    prevCloseByTime: new Map(),
    chart: null,
    chartResizeObs: null,
    paneSubMap: [],
    klineReqSeq: 0,

    // Sync polling
    syncTimer: null,
    searchTimer: null,
    searchSeq: 0,
    resizeRaf: 0,
  };

  // ==================== UTILITIES ====================
  const fmtNum = (v, d = 2) => (v === null || v === undefined || Number.isNaN(Number(v)) ? "--" : Number(v).toFixed(d));
  const fmtPct = (v) => {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "--";
    const n = Number(v);
    return `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;
  };
  const fmtVol = (v) => {
    if (v === null || v === undefined) return "--";
    const a = Math.abs(v);
    if (a >= 1e8) return (v / 1e8).toFixed(2) + "亿";
    if (a >= 1e4) return (v / 1e4).toFixed(1) + "万";
    return Math.round(v).toString();
  };
  const pctClass = (v) => (v === null || v === undefined ? "" : Number(v) >= 0 ? "up" : "down");

  function escHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function safeHexColor(val, fallback = "#848e9c") {
    return typeof val === "string" && /^#[0-9a-fA-F]{3,8}$/.test(val) ? val : fallback;
  }

  function showToast(msg, isError = false) {
    const host = $("toastHost");
    const el = document.createElement("div");
    el.className = "toast" + (isError ? " error" : "");
    el.textContent = msg;
    host.appendChild(el);
    setTimeout(() => el.remove(), 2800);
  }

  async function apiJson(url, opts = {}) {
    const res = await fetch(url, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.error || `请求失败 (${res.status})`);
    }
    return data;
  }

  function chartPalette() {
    const dark = state.theme !== "light";
    return dark
      ? {
          bg: "#131722",
          text: "#848e9c",
          grid: "rgba(255, 255, 255, 0.042)",
          border: "#222736",
          crosshair: "#5b6478",
          labelBg: "#252c3d",
          sep: "#222736",
          sepHover: "rgba(246, 70, 93, 0.35)",
          up: "#f6465d",
          down: "#0ecb81",
          upAlpha: "rgba(246, 70, 93, 0.45)",
          downAlpha: "rgba(14, 203, 129, 0.45)",
        }
      : {
          bg: "#ffffff",
          text: "#64748b",
          grid: "rgba(15, 23, 42, 0.055)",
          border: "#e2e8f0",
          crosshair: "#94a3b8",
          labelBg: "#334155",
          sep: "#e2e8f0",
          sepHover: "rgba(225, 29, 72, 0.3)",
          up: "#e11d48",
          down: "#059669",
          upAlpha: "rgba(225, 29, 72, 0.40)",
          downAlpha: "rgba(5, 150, 105, 0.40)",
        };
  }

  function applyTheme(theme) {
    state.theme = theme === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", state.theme);
    localStorage.setItem("kr_theme", state.theme);
    if (state.chart) {
      const p = chartPalette();
      state.chart.applyOptions({
        layout: {
          background: { color: p.bg },
          textColor: p.text,
          panes: { enableResize: true, separatorColor: p.sep, separatorHoverColor: p.sepHover },
        },
        grid: { vertLines: { color: p.grid }, horzLines: { color: p.grid } },
        rightPriceScale: { borderColor: p.border },
        timeScale: { borderColor: p.border },
        crosshair: {
          vertLine: { color: p.crosshair, labelBackgroundColor: p.labelBg },
          horzLine: { color: p.crosshair, labelBackgroundColor: p.labelBg },
        },
      });
      // Re-render candlesticks/histograms cleanly with theme-adjusted up/down colors
      if (state.klineData) renderChart(state.klineData);
    }
  }

  // ==================== SPARKLINE SVG ====================
  function renderSparkline(vals, pctChg) {
    if (!vals || vals.length < 2) {
      return `<svg width="56" height="20" viewBox="0 0 56 20"></svg>`;
    }
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const span = max - min || 1;
    const pts = vals
      .map((v, i) => {
        const x = (i / (vals.length - 1)) * 54 + 1;
        const y = 18 - ((v - min) / span) * 15;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");
    const color = pctChg !== null && pctChg !== undefined
      ? Number(pctChg) >= 0 ? "var(--up)" : "var(--down)"
      : vals[vals.length - 1] >= vals[0] ? "var(--up)" : "var(--down)";
    return `<svg width="56" height="20" viewBox="0 0 56 20" aria-hidden="true"><polyline fill="none" stroke="${color}" stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round" points="${pts}"/></svg>`;
  }

  // ==================== BOOTSTRAP ====================
  async function init() {
    applyTheme(state.theme);
    bindStaticEvents();

    try {
      const data = await apiJson("/api/bootstrap");
      state.bootstrap = data;
      state.startDate = data.default_start;
      state.endDate = data.today;
      $("inputStartDate").value = state.startDate;
      $("inputEndDate").value = state.endDate;

      // Default strategy selection: 趋势突破 group
      const firstGroup = Object.keys(data.strategy_groups)[0] || "";
      state.activeGroup = firstGroup;
      if (firstGroup && data.strategy_groups[firstGroup]) {
        state.selectedStrategies = new Set(data.strategy_groups[firstGroup].items);
      }

      setWatchlist(data.watchlist || []);
      renderGroupChips();
      renderStrategyPicker();
      renderIndicatorChips();
      renderStrategyMatrix();
      renderEmptyQuickButtons();
      renderCacheStatus(data.status, data.auto_sync, data.auto_sync_times);
      renderDockList();

      if (data.auto_sync && data.auto_sync.running) {
        startSyncPolling();
      }
    } catch (err) {
      showToast("初始化失败：" + err.message, true);
    }
  }

  // ==================== NAVIGATION & POPOVERS ====================
  function switchView(view) {
    state.view = view;
    document.querySelectorAll(".nav-tab").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === view);
    });
    document.querySelectorAll(".view").forEach((el) => {
      el.classList.toggle("active", el.id === `view-${view}`);
    });
    if (view === "cache") refreshSyncStatus();
    if (view === "terminal" && state.chart && state.klineData) {
      requestAnimationFrame(() => resizeChart());
    }
  }

  function closeAllPopovers() {
    clearTimeout(state.searchTimer);
    state.searchSeq++;
    $("stratPickerPopover").classList.add("hidden");
    $("moreFilterPopover").classList.add("hidden");
    $("exportDropdown").classList.add("hidden");
    $("globalSearchResults").classList.add("hidden");
  }

  function bindStaticEvents() {
    $("brandLogo").addEventListener("click", () => switchView("terminal"));
    $("navStatusBtn").addEventListener("click", () => switchView("cache"));
    $("themeToggleBtn").addEventListener("click", () => {
      applyTheme(state.theme === "dark" ? "light" : "dark");
    });

    $("navTabs").addEventListener("click", (e) => {
      const btn = e.target.closest(".nav-tab");
      if (btn) switchView(btn.dataset.view);
    });

    // Global Search
    const searchInput = $("globalSearchInput");
    searchInput.addEventListener("input", () => {
      clearTimeout(state.searchTimer);
      const q = searchInput.value.trim();
      state.searchTimer = setTimeout(() => fetchGlobalSearch(q), 140);
    });
    searchInput.addEventListener("focus", () => {
      fetchGlobalSearch(searchInput.value.trim());
    });
    searchInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        const first = $("globalSearchResults").querySelector(".search-item");
        if (first) first.click();
      } else if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        closeAllPopovers();
        searchInput.blur();
      }
    });

    // Popover triggers
    $("stratPickerBtn").addEventListener("click", (e) => {
      e.stopPropagation();
      const pop = $("stratPickerPopover");
      const open = pop.classList.contains("hidden");
      closeAllPopovers();
      if (open) pop.classList.remove("hidden");
    });
    $("moreFilterBtn").addEventListener("click", (e) => {
      e.stopPropagation();
      const pop = $("moreFilterPopover");
      const open = pop.classList.contains("hidden");
      closeAllPopovers();
      if (open) pop.classList.remove("hidden");
    });
    $("exportMenuBtn").addEventListener("click", (e) => {
      e.stopPropagation();
      const pop = $("exportDropdown");
      const open = pop.classList.contains("hidden");
      closeAllPopovers();
      if (open) pop.classList.remove("hidden");
    });

    document.addEventListener("click", (e) => {
      if (!e.target.closest(".popover-anchor") && !e.target.closest("#globalSearchWrap")) {
        closeAllPopovers();
      }
    });

    // Strategy popover select/clear all
    $("stratSelectAllBtn").addEventListener("click", () => {
      state.selectedStrategies = new Set(Object.keys(state.bootstrap.strategies));
      syncStrategyUI();
    });
    $("stratClearAllBtn").addEventListener("click", () => {
      state.selectedStrategies.clear();
      syncStrategyUI();
    });

    // Filter pills
    $("toggleExcludeSt").addEventListener("click", () => {
      state.excludeSt = !state.excludeSt;
      $("toggleExcludeSt").classList.toggle("active", state.excludeSt);
    });
    $("toggleExcludeGem").addEventListener("click", () => {
      state.excludeGem = !state.excludeGem;
      $("toggleExcludeGem").classList.toggle("active", state.excludeGem);
    });

    // Date range presets
    document.querySelectorAll(".preset-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const days = Number(btn.dataset.days);
        const endStr = state.bootstrap?.today || new Date().toISOString().slice(0, 10);
        const dt = new Date(endStr + "T00:00:00");
        dt.setDate(dt.getDate() - days);
        const startStr = dt.toISOString().slice(0, 10);
        state.startDate = startStr;
        state.endDate = endStr;
        $("inputStartDate").value = startStr;
        $("inputEndDate").value = endStr;
        document.querySelectorAll(".preset-btn").forEach((b) => b.classList.toggle("active", b === btn));
      });
    });

    $("inputStartDate").addEventListener("change", (e) => { state.startDate = e.target.value; });
    $("inputEndDate").addEventListener("change", (e) => { state.endDate = e.target.value; });
    $("inputMaxTurn").addEventListener("change", (e) => { state.maxTurn = Number(e.target.value) || 100; });
    $("inputMaxStocks").addEventListener("change", (e) => { state.maxStocks = Number(e.target.value) || 6000; });

    // Run Screen buttons
    $("btnRunScreen").addEventListener("click", runScreen);
    $("btnEmptyRunScreen").addEventListener("click", runScreen);
    $("btnMatrixRun").addEventListener("click", () => {
      switchView("terminal");
      runScreen();
    });

    // Intervals
    $("intervalGroup").addEventListener("click", (e) => {
      const btn = e.target.closest(".tb-btn");
      if (!btn || btn.dataset.interval === state.interval) return;
      state.interval = btn.dataset.interval;
      $("intervalGroup").querySelectorAll(".tb-btn").forEach((b) => {
        b.classList.toggle("active", b === btn);
      });
      if (state.currentCode) loadKline(state.currentCode, state.currentName);
    });

    $("btnResetZoom").addEventListener("click", () => {
      if (state.chart) state.chart.timeScale().fitContent();
    });

    // Watchlist star in ticker strip
    $("btnToggleWatchlist").addEventListener("click", toggleCurrentWatchlist);

    // Step Prev / Next stock
    $("btnPrevStock").addEventListener("click", () => stepStock(-1));
    $("btnNextStock").addEventListener("click", () => stepStock(1));

    // Forward returns drawer toggle
    $("fwdToggleBtn").addEventListener("click", () => {
      const body = $("fwdBody");
      const hidden = body.classList.toggle("hidden");
      $("fwdToggleCaret").textContent = hidden ? "▸" : "▾";
      requestAnimationFrame(() => resizeChart());
    });

    // Right Dock tabs & filters
    $("dockTabSignals").addEventListener("click", () => {
      state.dockTab = "signals";
      updateDockTabsUI();
      renderDockList();
    });
    $("dockTabWatchlist").addEventListener("click", () => {
      state.dockTab = "watchlist";
      updateDockTabsUI();
      renderDockList();
    });

    $("modeAggBtn").addEventListener("click", () => {
      state.dockMode = "agg";
      $("modeAggBtn").classList.add("active");
      $("modeAllBtn").classList.remove("active");
      renderDockList();
    });
    $("modeAllBtn").addEventListener("click", () => {
      state.dockMode = "all";
      $("modeAllBtn").classList.add("active");
      $("modeAggBtn").classList.remove("active");
      renderDockList();
    });
    $("dockSortSelect").addEventListener("change", (e) => {
      state.dockSort = e.target.value;
      renderDockList();
    });
    $("dockStrategyFilter").addEventListener("change", (e) => {
      state.dockStrategy = e.target.value;
      renderDockList();
    });
    $("dockSearchInput").addEventListener("input", (e) => {
      state.dockQuery = e.target.value.trim().toLowerCase();
      renderDockList();
    });

    // Export dropdown actions
    $("exportDropdown").addEventListener("click", (e) => {
      const btn = e.target.closest(".menu-item");
      if (!btn) return;
      closeAllPopovers();
      handleExport(btn.dataset.export);
    });

    // Sync triggers
    $("navSyncBtn").addEventListener("click", triggerSync);
    $("btnManualSync").addEventListener("click", triggerSync);
    $("btnSaveSyncConfig").addEventListener("click", saveSyncConfig);

    // Keyboard shortcuts: / search, Esc close popovers, Up/Down step stock
    document.addEventListener("keydown", (e) => {
      const tag = document.activeElement?.tagName;
      const inInput = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
      if (e.key === "/" && !inInput) {
        e.preventDefault();
        $("globalSearchInput").focus();
        $("globalSearchInput").select();
      } else if (e.key === "Escape") {
        closeAllPopovers();
      } else if (!inInput && state.view === "terminal" && state.visibleRows.length > 0) {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          stepStock(1);
        } else if (e.key === "ArrowUp") {
          e.preventDefault();
          stepStock(-1);
        }
      }
    });
  }

  // ==================== STRATEGY UI ====================
  function detectMatchingGroup() {
    const groups = state.bootstrap?.strategy_groups || {};
    for (const [gName, meta] of Object.entries(groups)) {
      if (meta.items.length === state.selectedStrategies.size &&
          meta.items.every((s) => state.selectedStrategies.has(s))) {
        return gName;
      }
    }
    return null;
  }

  function renderGroupChips() {
    const host = $("groupChips");
    const groups = state.bootstrap.strategy_groups;
    host.innerHTML = Object.keys(groups)
      .map((g) => `<button class="group-chip${g === state.activeGroup ? " active" : ""}" data-group="${escHtml(g)}" type="button">${escHtml(g)}</button>`)
      .join("");
    host.querySelectorAll(".group-chip").forEach((btn) => {
      btn.addEventListener("click", () => {
        const g = btn.dataset.group;
        state.selectedStrategies = new Set(groups[g].items);
        state.activeGroup = g;
        syncStrategyUI();
      });
    });
    updateStrategyCountUI();
  }

  function renderStrategyPicker() {
    const body = $("stratPickerBody");
    const groups = state.bootstrap.strategy_groups;
    const descs = state.bootstrap.strategies;

    body.innerHTML = Object.entries(groups)
      .map(([gName, meta]) => {
        const itemsHtml = meta.items
          .map((s) => {
            const checked = state.selectedStrategies.has(s) ? "checked" : "";
            return `
              <label class="strat-check">
                <input type="checkbox" value="${escHtml(s)}" ${checked} />
                <span>${escHtml(s)}<small>${escHtml(descs[s] || "")}</small></span>
              </label>`;
          })
          .join("");
        return `
          <div class="strat-pop-group">
            <div class="strat-pop-ghead">
              <span class="strat-pop-gtitle" style="color:${safeHexColor(meta.color)}">${escHtml(gName)}</span>
              <button class="link-btn" data-toggle-group="${escHtml(gName)}" type="button">仅选本组</button>
            </div>
            ${itemsHtml}
          </div>`;
      })
      .join("");

    body.addEventListener("change", (e) => {
      const cb = e.target;
      if (cb.type !== "checkbox") return;
      if (cb.checked) state.selectedStrategies.add(cb.value);
      else state.selectedStrategies.delete(cb.value);
      syncStrategyUI();
    });

    body.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-toggle-group]");
      if (!btn) return;
      const g = btn.dataset.toggleGroup;
      state.selectedStrategies = new Set(groups[g].items);
      syncStrategyUI();
    });
  }

  function renderStrategyMatrix() {
    const grid = $("strategyMatrixGrid");
    const groups = state.bootstrap.strategy_groups;
    const descs = state.bootstrap.strategies;

    grid.innerHTML = Object.entries(groups)
      .map(([gName, meta]) => {
        const rows = meta.items
          .map((s) => {
            const checked = state.selectedStrategies.has(s) ? "checked" : "";
            return `
              <div class="matrix-item" data-strat="${escHtml(s)}">
                <div class="matrix-item-left">
                  <input type="checkbox" value="${escHtml(s)}" ${checked} />
                  <div>
                    <div class="matrix-sname">${escHtml(s)}</div>
                    <div class="matrix-sdesc">${escHtml(descs[s] || "")}</div>
                  </div>
                </div>
                <button class="btn-mini" data-run-one="${escHtml(s)}" type="button">单选扫描</button>
              </div>`;
          })
          .join("");

        return `
          <div class="matrix-group">
            <div class="matrix-ghead">
              <div class="matrix-gtitle">
                <span class="matrix-dot" style="background:${safeHexColor(meta.color)}"></span>
                <span>${escHtml(gName)}</span>
                <span class="dock-badge">${meta.items.length}</span>
              </div>
              <button class="btn-mini" data-run-group="${escHtml(gName)}" type="button">应用并选股</button>
            </div>
            <div class="matrix-gdesc">${escHtml(meta.desc || "")}</div>
            ${rows}
          </div>`;
      })
      .join("");

    grid.addEventListener("click", (e) => {
      const runGroupBtn = e.target.closest("[data-run-group]");
      if (runGroupBtn) {
        const g = runGroupBtn.dataset.runGroup;
        state.selectedStrategies = new Set(groups[g].items);
        syncStrategyUI();
        switchView("terminal");
        runScreen();
        return;
      }
      const runOneBtn = e.target.closest("[data-run-one]");
      if (runOneBtn) {
        state.selectedStrategies = new Set([runOneBtn.dataset.runOne]);
        syncStrategyUI();
        switchView("terminal");
        runScreen();
        return;
      }
      const row = e.target.closest(".matrix-item");
      if (row) {
        const cb = row.querySelector("input[type=checkbox]");
        if (e.target !== cb) cb.checked = !cb.checked;
        if (cb.checked) state.selectedStrategies.add(cb.value);
        else state.selectedStrategies.delete(cb.value);
        syncStrategyUI();
      }
    });
  }

  function syncStrategyUI() {
    state.activeGroup = detectMatchingGroup();
    $("groupChips").querySelectorAll(".group-chip").forEach((b) => {
      b.classList.toggle("active", b.dataset.group === state.activeGroup);
    });
    $("stratPickerBody").querySelectorAll("input[type=checkbox]").forEach((cb) => {
      cb.checked = state.selectedStrategies.has(cb.value);
    });
    $("strategyMatrixGrid").querySelectorAll("input[type=checkbox]").forEach((cb) => {
      cb.checked = state.selectedStrategies.has(cb.value);
    });
    updateStrategyCountUI();
  }

  function updateStrategyCountUI() {
    const n = state.selectedStrategies.size;
    $("stratSelectedCount").textContent = String(n);
    const st = state.bootstrap?.status;
    const stockTxt = st?.stocks ? `${st.stocks.toLocaleString()} 只 A 股` : "本地行情";
    const groupLabel = state.activeGroup ? `「${state.activeGroup}」${n} 套策略` : `已选 ${n} 套策略`;
    $("emptySubtitle").innerHTML =
      `已就绪 ${escHtml(stockTxt)}，当前勾选 ${escHtml(groupLabel)} · 点击下方按钮立即扫描，或按 <kbd>/</kbd> 搜索任意个股直接看盘`;
  }

  function renderEmptyQuickButtons() {
    const groups = state.bootstrap.strategy_groups;
    const host = $("emptyQuickGroups");
    host.innerHTML = Object.keys(groups)
      .map((g) => `<button class="quick-pill" data-quick-group="${escHtml(g)}" type="button">${escHtml(g)}</button>`)
      .join("");
    host.querySelectorAll(".quick-pill").forEach((btn) => {
      btn.addEventListener("click", () => {
        const g = btn.dataset.quickGroup;
        state.selectedStrategies = new Set(groups[g].items);
        syncStrategyUI();
        runScreen();
      });
    });
  }

  // ==================== GLOBAL SEARCH ====================
  async function fetchGlobalSearch(q) {
    const seq = ++state.searchSeq;
    try {
      const data = await apiJson(`/api/search?q=${encodeURIComponent(q)}`);
      if (seq !== state.searchSeq || document.activeElement !== $("globalSearchInput")) return;
      const box = $("globalSearchResults");
      const items = data.items || [];
      if (!items.length) {
        box.innerHTML = `<div class="dock-empty" style="padding:14px">未找到匹配股票</div>`;
      } else {
        box.innerHTML = items
          .map(
            (it) =>
              `<button class="search-item" data-code="${escHtml(it.code)}" data-name="${escHtml(it.name)}" type="button">
                <span>${escHtml(it.name)}</span>
                <span class="search-item-code mono">${escHtml(it.code)}</span>
              </button>`
          )
          .join("");
        box.querySelectorAll(".search-item").forEach((btn) => {
          btn.addEventListener("click", () => {
            closeAllPopovers();
            $("globalSearchInput").value = "";
            switchView("terminal");
            openStock(btn.dataset.code, btn.dataset.name);
          });
        });
      }
      box.classList.remove("hidden");
    } catch (_) {
      // ignore transient search errors
    }
  }

  // ==================== SCREENING ====================
  function updateScanProgress(pct, detail, scanned, total, hits) {
    const p = Math.max(0, Math.min(100, Math.round(pct)));
    $("screenPct").textContent = `${p}%`;
    $("screenBarFill").style.width = `${p}%`;
    $("scanToplineFill").style.width = `${p}%`;
    if (detail) $("screenDetail").textContent = detail;
    if (total > 0) {
      $("screenLiveStats").textContent = `已扫描 ${scanned.toLocaleString()} / ${total.toLocaleString()} 只 · 命中 ${hits} 条信号`;
    } else {
      $("screenLiveStats").textContent = "正在读取本地行情数据，首次加载约需 3~5 秒…";
    }
  }

  async function runScreen() {
    const strategies = Array.from(state.selectedStrategies);
    if (!strategies.length) {
      showToast("请至少勾选 1 套选股策略", true);
      return;
    }

    const btn = $("btnRunScreen");
    const emptyBtn = $("btnEmptyRunScreen");
    btn.disabled = true;
    emptyBtn.disabled = true;
    $("screenSpinner").classList.remove("hidden");
    $("screenBtnText").textContent = "扫描中...";
    $("scanTopline").classList.remove("hidden");
    $("screenOverlay").classList.remove("hidden");
    updateScanProgress(4, "正在连接选股引擎…", 0, 0, 0);

    const t0 = performance.now();
    $("screenElapsed").textContent = "0.0s";
    const elapsedTimer = setInterval(() => {
      $("screenElapsed").textContent = `${((performance.now() - t0) / 1000).toFixed(1)}s`;
    }, 100);

    try {
      const payload = {
        strategies,
        start: state.startDate,
        end: state.endDate,
        min_change: Number($("inputMinChange").value) || 0,
        min_amount: Number($("inputMinAmount").value) || 0,
        max_turn: Number($("inputMaxTurn").value) || 100,
        max_stocks: Number($("inputMaxStocks").value) || 6000,
        exclude_st: state.excludeSt,
        exclude_gem_star: state.excludeGem,
      };
      const res = await fetch("/api/screen/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      let data = null;
      if (res.status === 404) {
        // 兼容尚未重启旧进程的情况：自动降级到 /api/screen 并维持平滑进度显示
        updateScanProgress(35, "正在扫描全市场日线行情（重启 start.bat 后支持实时逐股进度）…", 0, 0, 0);
        data = await apiJson("/api/screen", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
      } else if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.error || `选股请求失败 (${res.status})`);
      } else if (res.body && res.body.getReader) {
        const reader = res.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buf = "";
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const lines = buf.split("\n");
          buf = lines.pop() || "";
          for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed) continue;
            const msg = JSON.parse(trimmed);
            if (msg.type === "progress") {
              updateScanProgress(msg.pct, msg.detail, msg.scanned || 0, msg.total || 0, msg.hits || 0);
            } else if (msg.type === "error") {
              throw new Error(msg.error || "选股失败");
            } else if (msg.type === "result") {
              data = msg.data;
            }
          }
        }
        if (buf.trim()) {
          const msg = JSON.parse(buf.trim());
          if (msg.type === "error") throw new Error(msg.error || "选股失败");
          if (msg.type === "result") data = msg.data;
        }
      } else {
        // Fallback for environments without ReadableStream
        data = await apiJson("/api/screen", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
      }

      if (!data) throw new Error("未收到选股结果");
      updateScanProgress(100, "扫描完成，正在渲染榜单与 K 线…", data.stats?.ready || 0, data.stats?.ready || 0, data.stats?.hit_count || 0);

      state.signals = data.signals || [];
      state.aggregated = data.aggregated || [];
      state.hitStrategies = data.hit_strategies || [];
      state.forwardReturns = data.forward_returns || [];
      state.scanStats = data.stats || null;
      state.scanRange = data.range || null;
      state.dockTab = "signals";
      state.dockStrategy = "";
      updateDockTabsUI();
      populateStrategyFilter();
      renderDockSummary();
      renderForwardReturns();
      renderDockList();

      if (state.visibleRows.length > 0) {
        const first = state.visibleRows[0];
        await openStock(first.code, first.name);
        showToast(`扫描完成：命中 ${state.aggregated.length} 只股票 (${state.scanStats.elapsed}s)`);
      } else {
        showToast(`本次扫描未命中信号，可适当放宽过滤条件或切换策略`);
      }
    } catch (err) {
      showToast(err.message, true);
    } finally {
      clearInterval(elapsedTimer);
      btn.disabled = false;
      emptyBtn.disabled = false;
      $("screenSpinner").classList.add("hidden");
      $("screenBtnText").textContent = "开始选股";
      $("screenOverlay").classList.add("hidden");
      $("scanTopline").classList.add("hidden");
    }
  }

  function populateStrategyFilter() {
    const sel = $("dockStrategyFilter");
    if (state.hitStrategies.length <= 1) {
      sel.classList.add("hidden");
      sel.innerHTML = "";
      return;
    }
    sel.classList.remove("hidden");
    sel.innerHTML =
      `<option value="">全部命中策略 (${state.hitStrategies.length})</option>` +
      state.hitStrategies.map((s) => `<option value="${escHtml(s)}">${escHtml(s)}</option>`).join("");
  }

  function renderDockSummary() {
    const el = $("dockSummary");
    const s = state.scanStats;
    if (!s) {
      el.classList.add("hidden");
      return;
    }
    el.classList.remove("hidden");
    const avgTxt = s.avg_chg !== null ? fmtPct(s.avg_chg) : "--";
    el.innerHTML = `
      <span>命中 <strong>${s.unique_count}</strong> 股 / ${s.hit_count} 信号 · 涨停 ${s.limit_up}</span>
      <span>均涨 <strong class="${pctClass(s.avg_chg)}">${avgTxt}</strong> · ${s.elapsed}s</span>`;
  }

  function renderForwardReturns() {
    const drawer = $("fwdDrawer");
    const tbody = $("fwdTableBody");
    if (!state.forwardReturns.length) {
      drawer.classList.add("hidden");
      return;
    }
    drawer.classList.remove("hidden");
    tbody.innerHTML = state.forwardReturns
      .map((r) => {
        const isRes = String(r.strategy).startsWith("★");
        return `
          <tr class="${isRes ? "resonance-row" : ""}">
            <td>${escHtml(r.strategy)}</td>
            <td class="num">${r.samples}</td>
            <td class="num">${fmtNum(r.win5, 1)}%</td>
            <td class="num ${pctClass(r.avg5)}">${fmtPct(r.avg5)}</td>
            <td class="num">${fmtNum(r.win10, 1)}%</td>
            <td class="num ${pctClass(r.avg10)}">${fmtPct(r.avg10)}</td>
          </tr>`;
      })
      .join("");
  }

  // ==================== RIGHT DOCK LIST ====================
  function updateDockTabsUI() {
    $("dockTabSignals").classList.toggle("active", state.dockTab === "signals");
    $("dockTabWatchlist").classList.toggle("active", state.dockTab === "watchlist");
    $("badgeSignals").textContent = String(state.aggregated.length);
    $("badgeWatchlist").textContent = String(state.watchlist.length);

    const isSig = state.dockTab === "signals";
    $("dockFilterBar").classList.toggle("hidden", !isSig);
    $("dockSummary").classList.toggle("hidden", !isSig || !state.scanStats);
    if (!isSig) {
      $("dockStrategyFilter").classList.add("hidden");
    } else if (state.hitStrategies.length > 1) {
      $("dockStrategyFilter").classList.remove("hidden");
    }
  }

  function computeVisibleRows() {
    const q = state.dockQuery;
    if (state.dockTab === "watchlist") {
      let rows = state.watchlist.slice();
      if (q) {
        rows = rows.filter((r) => r.code.toLowerCase().includes(q) || r.name.toLowerCase().includes(q));
      }
      return rows;
    }

    let rows = state.dockMode === "agg" ? state.aggregated.slice() : state.signals.slice();
    if (state.dockStrategy) {
      rows = rows.filter((r) =>
        state.dockMode === "agg" ? (r.strategies || []).includes(state.dockStrategy) : r.strategy === state.dockStrategy
      );
    }
    if (q) {
      rows = rows.filter((r) => r.code.toLowerCase().includes(q) || r.name.toLowerCase().includes(q));
    }

    const numOr = (v, fb) => (v === null || v === undefined ? fb : Number(v));
    if (state.dockSort === "pct_desc") {
      rows.sort((a, b) => numOr(b.pctChg, -999) - numOr(a.pctChg, -999));
    } else if (state.dockSort === "pct_asc") {
      rows.sort((a, b) => numOr(a.pctChg, 999) - numOr(b.pctChg, 999));
    } else if (state.dockSort === "turn_desc") {
      rows.sort((a, b) => numOr(b.turn, -1) - numOr(a.turn, -1));
    } else if (state.dockSort === "amount_desc") {
      rows.sort((a, b) => numOr(b.amount_yi, -1) - numOr(a.amount_yi, -1));
    } else {
      rows.sort((a, b) => {
        const cDiff = (b.count || 1) - (a.count || 1);
        return cDiff !== 0 ? cDiff : numOr(b.pctChg, -999) - numOr(a.pctChg, -999);
      });
    }
    return rows;
  }

  function renderDockList() {
    const listEl = $("dockList");
    state.visibleRows = computeVisibleRows();

    if (!state.visibleRows.length) {
      if (state.dockTab === "watchlist") {
        listEl.innerHTML = `<div class="dock-empty">自选池为空<br/>打开任意股票 K 线后点击左上角 ☆ 收藏</div>`;
      } else if (!state.scanStats) {
        listEl.innerHTML = `<div class="dock-empty">暂无选股结果<br/>选择策略后点击上方「开始选股」</div>`;
      } else {
        listEl.innerHTML = `<div class="dock-empty">当前筛选条件下无匹配信号</div>`;
      }
      return;
    }

    const isWl = state.dockTab === "watchlist";
    listEl.innerHTML = state.visibleRows
      .map((r, idx) => {
        const active = r.code === state.currentCode ? " active" : "";
        const badge = !isWl && state.dockMode === "agg" && r.count > 1
          ? `<span class="sr-res-badge">×${r.count}</span>`
          : "";
        const subText = isWl
          ? `${escHtml(r.code)} ${r.date ? "· " + escHtml(r.date.slice(5)) : ""}`
          : state.dockMode === "agg"
            ? `${escHtml(r.code)} · ${escHtml((r.strategies || [])[0] || "")}`
            : `${escHtml(r.code)} · ${escHtml(r.strategy || "")}`;
        const sparkHtml = isWl
          ? `<button class="row-wl-del" data-del-wl="${escHtml(r.code)}" type="button" title="移出自选池">×</button>`
          : renderSparkline(r.spark, r.pctChg);
        const turnTxt = r.turn !== null && r.turn !== undefined ? `${fmtNum(r.turn, 1)}%` : "--";
        const amtTxt = r.amount_yi !== null && r.amount_yi !== undefined ? `${fmtNum(r.amount_yi, 1)}亿` : "--";

        return `
          <div class="stock-row${active}" data-idx="${idx}" data-code="${escHtml(r.code)}" data-name="${escHtml(r.name)}">
            <div class="sr-main">
              <div class="sr-top">
                <span class="sr-name">${escHtml(r.name)}</span>
                ${badge}
              </div>
              <div class="sr-sub mono">${subText}</div>
            </div>
            <div class="sr-spark">${sparkHtml}</div>
            <div class="sr-num">
              <div class="sr-price mono ${pctClass(r.pctChg)}">${fmtNum(r.close, 2)}</div>
              <div class="sr-pct mono ${pctClass(r.pctChg)}">${fmtPct(r.pctChg)}</div>
            </div>
            <div class="sr-num">
              <div class="sr-meta-top mono">${turnTxt}</div>
              <div class="sr-meta-bot mono">${amtTxt}</div>
            </div>
          </div>`;
      })
      .join("");

    listEl.onclick = (e) => {
      const delBtn = e.target.closest("[data-del-wl]");
      if (delBtn) {
        e.stopPropagation();
        removeWatchlist(delBtn.dataset.delWl);
        return;
      }
      const row = e.target.closest(".stock-row");
      if (row) {
        openStock(row.dataset.code, row.dataset.name);
      }
    };
  }

  function stepStock(delta) {
    if (!state.visibleRows.length) return;
    const idx = state.visibleRows.findIndex((r) => r.code === state.currentCode);
    let nextIdx = idx < 0 ? 0 : idx + delta;
    if (nextIdx < 0) nextIdx = state.visibleRows.length - 1;
    if (nextIdx >= state.visibleRows.length) nextIdx = 0;
    const target = state.visibleRows[nextIdx];
    openStock(target.code, target.name);
    const rowEl = $("dockList").querySelector(`.stock-row[data-idx="${nextIdx}"]`);
    if (rowEl) rowEl.scrollIntoView({ block: "nearest" });
  }

  // ==================== WATCHLIST ====================
  function setWatchlist(items) {
    state.watchlist = items || [];
    state.watchlistCodes = new Set(state.watchlist.map((x) => x.code));
    updateDockTabsUI();
    updateWatchlistStarUI();
    renderEmptyWatchlistRow();
  }

  function renderEmptyWatchlistRow() {
    const row = $("emptyWatchlistRow");
    const host = $("emptyWatchlistChips");
    if (!state.watchlist.length) {
      row.classList.add("hidden");
      return;
    }
    row.classList.remove("hidden");
    host.innerHTML = state.watchlist
      .slice(0, 6)
      .map(
        (w) =>
          `<button class="quick-pill" data-open-code="${escHtml(w.code)}" data-open-name="${escHtml(w.name)}" type="button">${escHtml(w.name)}</button>`
      )
      .join("");
    host.querySelectorAll(".quick-pill").forEach((btn) => {
      btn.addEventListener("click", () => openStock(btn.dataset.openCode, btn.dataset.openName));
    });
  }

  function updateWatchlistStarUI() {
    const btn = $("btnToggleWatchlist");
    const starred = state.currentCode && state.watchlistCodes.has(state.currentCode);
    btn.classList.toggle("starred", Boolean(starred));
    btn.textContent = starred ? "★" : "☆";
  }

  async function toggleCurrentWatchlist() {
    if (!state.currentCode) return;
    if (state.watchlistCodes.has(state.currentCode)) {
      await removeWatchlist(state.currentCode);
    } else {
      try {
        const res = await apiJson("/api/watchlist", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ code: state.currentCode, name: state.currentName }),
        });
        setWatchlist(res.items);
        if (state.dockTab === "watchlist") renderDockList();
        showToast(`已将 ${state.currentName} 加入自选池`);
      } catch (err) {
        showToast(err.message, true);
      }
    }
  }

  async function removeWatchlist(code) {
    try {
      const res = await apiJson(`/api/watchlist/${encodeURIComponent(code)}`, { method: "DELETE" });
      setWatchlist(res.items);
      if (state.dockTab === "watchlist") renderDockList();
      showToast("已移出自选池");
    } catch (err) {
      showToast(err.message, true);
    }
  }

  // ==================== INDICATORS TOOLBAR ====================
  function renderIndicatorChips() {
    const overlays = state.bootstrap.main_overlays || ["MA", "EMA", "BOLL", "BBI", "SAR", "KC"];
    const subs = state.bootstrap.sub_indicators || ["VOL", "MACD", "KDJ", "RSI", "WR", "CCI", "OBV", "ATR"];

    const ovHost = $("overlayChips");
    ovHost.innerHTML = overlays
      .map((k) => `<button class="ind-chip${state.overlays.has(k) ? " active" : ""}" data-ov="${escHtml(k)}" type="button">${escHtml(k)}</button>`)
      .join("");
    ovHost.addEventListener("click", (e) => {
      const btn = e.target.closest(".ind-chip");
      if (!btn) return;
      const k = btn.dataset.ov;
      if (state.overlays.has(k)) state.overlays.delete(k);
      else state.overlays.add(k);
      btn.classList.toggle("active", state.overlays.has(k));
      if (state.klineData) renderChart(state.klineData);
    });

    const subHost = $("subChips");
    subHost.innerHTML = subs
      .map((k) => `<button class="ind-chip${state.subs.has(k) ? " active" : ""}" data-sub="${escHtml(k)}" type="button">${escHtml(k)}</button>`)
      .join("");
    subHost.addEventListener("click", (e) => {
      const btn = e.target.closest(".ind-chip");
      if (!btn) return;
      const k = btn.dataset.sub;
      if (state.subs.has(k)) state.subs.delete(k);
      else state.subs.add(k);
      btn.classList.toggle("active", state.subs.has(k));
      if (state.klineData) renderChart(state.klineData);
    });
  }

  // ==================== OPEN STOCK & K-LINE CHART ====================
  async function openStock(code, name) {
    state.currentCode = code;
    state.currentName = name || code;
    $("emptyStage").classList.add("hidden");
    $("chartStage").classList.remove("hidden");

    // Highlight active row without re-rendering whole dock DOM
    $("dockList").querySelectorAll(".stock-row").forEach((el) => {
      el.classList.toggle("active", el.dataset.code === code);
    });
    updateWatchlistStarUI();
    await loadKline(code, name);
  }

  async function loadKline(code, name) {
    const seq = ++state.klineReqSeq;
    $("chartLoading").classList.remove("hidden");

    const start = state.scanRange?.start || state.startDate;
    const end = state.scanRange?.end || state.endDate;
    const qs = new URLSearchParams({ interval: state.interval, start, end });

    try {
      const data = await apiJson(`/api/kline/${encodeURIComponent(code)}?${qs.toString()}`);
      if (seq !== state.klineReqSeq) return; // stale request
      state.klineData = data;
      state.currentName = data.name || name || code;
      renderTickerStrip(data);
      renderChart(data);
    } catch (err) {
      if (seq === state.klineReqSeq) showToast(err.message, true);
    } finally {
      if (seq === state.klineReqSeq) $("chartLoading").classList.add("hidden");
    }
  }

  function renderTickerStrip(data) {
    const t = data.ticker || {};
    $("tickerName").textContent = data.name || data.code;
    $("tickerCode").textContent = data.code;

    const pEl = $("tickerPrice");
    pEl.textContent = fmtNum(t.close, 2);
    pEl.className = "ticker-price mono " + pctClass(t.pctChg);

    const cEl = $("tickerChange");
    cEl.textContent = fmtPct(t.pctChg);
    cEl.className = "ticker-pct mono " + pctClass(t.pctChg);

    updateWatchlistStarUI();

    const turnStr = t.turn !== null && t.turn !== undefined ? `${fmtNum(t.turn, 2)}%` : "--";
    const rsiStr = t.rsi6 !== null && t.rsi6 !== undefined ? fmtNum(t.rsi6, 1) : "--";
    const metrics = [
      ["开", fmtNum(t.open, 2)],
      ["高", fmtNum(t.high, 2)],
      ["低", fmtNum(t.low, 2)],
      ["换手", turnStr],
      ["成交额", t.amount_str || "--"],
      ["RSI(6)", rsiStr],
      ["MACD", t.macd_trend || "--"],
    ];
    $("tickerStats").innerHTML = metrics
      .map(
        ([k, v]) => `<div class="tm-item"><span class="tm-label">${k}</span><span class="tm-val mono">${escHtml(v)}</span></div>`
      )
      .join("");

    // Compact single-line hit strategy pill (full list on hover tooltip) so ‹ › nav is never clipped
    const aggHit = state.aggregated.find((a) => a.code === data.code);
    const tagsHost = $("tickerTags");
    if (aggHit && aggHit.strategies?.length) {
      const first = aggHit.strategies[0];
      const extra = aggHit.strategies.length > 1 ? ` +${aggHit.strategies.length - 1}` : "";
      const tip = aggHit.strategies.join(" / ");
      tagsHost.innerHTML = `<span class="hit-tag" title="${escHtml(tip)}">${escHtml(first)}${extra}</span>`;
    } else {
      tagsHost.innerHTML = "";
    }
  }

  function lineData(bars, key) {
    const out = [];
    for (const b of bars) {
      const v = b[key];
      if (v !== null && v !== undefined) out.push({ time: b.time, value: v });
    }
    return out;
  }

  function renderChart(data) {
    const mount = $("chartMount");
    const scrollWrap = $("chartScrollWrap");
    if (state.chartResizeObs) {
      state.chartResizeObs.disconnect();
      state.chartResizeObs = null;
    }
    if (state.chart) {
      state.chart.remove();
      state.chart = null;
    }
    mount.innerHTML = "";
    $("subpaneLegends").innerHTML = "";

    const bars = data.bars || [];
    const metaEl = $("chartRangeMeta");
    if (metaEl) {
      if (bars.length > 0) {
        const d0 = bars[0].time.slice(0, 7);
        const d1 = bars[bars.length - 1].time.slice(0, 7);
        metaEl.textContent = `${bars.length} 根 · ${d0}~${d1}`;
      } else {
        metaEl.textContent = "";
      }
    }
    if (!bars.length || typeof LightweightCharts === "undefined") return;

    state.barByTime.clear();
    state.prevCloseByTime.clear();
    bars.forEach((b, i) => {
      state.barByTime.set(b.time, b);
      state.prevCloseByTime.set(b.time, i > 0 ? bars[i - 1].close : b.open);
    });

    const p = chartPalette();
    const activeSubs = (state.bootstrap?.sub_indicators || []).filter((k) => state.subs.has(k));
    const subCount = activeSubs.length;

    const wrapH = Math.max(280, scrollWrap.clientHeight);
    const minNeededH = 260 + subCount * 90;
    const totalH = Math.max(wrapH, minNeededH);
    mount.style.height = totalH + "px";

    const chart = LightweightCharts.createChart(mount, {
      autoSize: true,
      layout: {
        background: { color: p.bg },
        textColor: p.text,
        fontSize: 11,
        fontFamily: "'Inter', -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif",
        attributionLogo: false,
        panes: {
          enableResize: true,
          separatorColor: p.sep,
          separatorHoverColor: p.sepHover,
        },
      },
      grid: {
        vertLines: { color: p.grid },
        horzLines: { color: p.grid },
      },
      crosshair: {
        mode: LightweightCharts.CrosshairMode.Normal,
        vertLine: { color: p.crosshair, width: 1, style: LightweightCharts.LineStyle.Dashed, labelBackgroundColor: p.labelBg },
        horzLine: { color: p.crosshair, width: 1, style: LightweightCharts.LineStyle.Dashed, labelBackgroundColor: p.labelBg },
      },
      rightPriceScale: {
        borderColor: p.border,
        scaleMargins: { top: 0.09, bottom: 0.11 },
      },
      timeScale: {
        borderColor: p.border,
        timeVisible: false,
        fixLeftEdge: true,
        fixRightEdge: true,
        rightOffset: 2,
        barSpacing: bars.length > 150 ? 6 : 9,
      },
      localization: {
        locale: "zh-CN",
      },
    });
    state.chart = chart;

    const addLine = (paneIdx, seriesData, color, opts = {}) => {
      if (!seriesData.length) return null;
      const s = chart.addSeries(
        LightweightCharts.LineSeries,
        {
          color,
          lineWidth: opts.width ?? 1.3,
          lineStyle: opts.style ?? LightweightCharts.LineStyle.Solid,
          lineVisible: opts.lineVisible ?? true,
          pointMarkersVisible: opts.pointMarkersVisible ?? false,
          pointMarkersRadius: opts.pointMarkersRadius ?? 2,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
          priceFormat: opts.priceFormat,
        },
        paneIdx
      );
      s.setData(seriesData);
      return s;
    };

    const addRefLines = (series, levels) => {
      if (!series) return;
      for (const lv of levels) {
        series.createPriceLine({
          price: lv,
          color: p.border,
          lineWidth: 1,
          lineStyle: LightweightCharts.LineStyle.Dashed,
          axisLabelVisible: false,
        });
      }
    };

    // ===== PANE 0: CANDLESTICK + MAIN OVERLAYS =====
    const candle = chart.addSeries(
      LightweightCharts.CandlestickSeries,
      {
        upColor: p.up,
        downColor: p.down,
        borderUpColor: p.up,
        borderDownColor: p.down,
        wickUpColor: p.up,
        wickDownColor: p.down,
        priceFormat: { type: "price", precision: 2, minMove: 0.01 },
      },
      0
    );
    candle.setData(
      bars.map((b) => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close }))
    );

    if (state.overlays.has("MA")) {
      addLine(0, lineData(bars, "ma5"), "#f0b90b");
      addLine(0, lineData(bars, "ma10"), "#38bdf8");
      addLine(0, lineData(bars, "ma20"), "#e879f9");
      addLine(0, lineData(bars, "ma30"), "#94a3b8", { width: 1 });
    }
    if (state.overlays.has("EMA")) {
      addLine(0, lineData(bars, "ema12"), "#fb923c");
      addLine(0, lineData(bars, "ema26"), "#60a5fa");
      addLine(0, lineData(bars, "ema60"), "#94a3b8", { width: 1, style: LightweightCharts.LineStyle.Dashed });
    }
    if (state.overlays.has("BOLL")) {
      addLine(0, lineData(bars, "boll_up"), "#f59e0b", { width: 1.2 });
      addLine(0, lineData(bars, "boll_mid"), "#94a3b8", { width: 1, style: LightweightCharts.LineStyle.Dashed });
      addLine(0, lineData(bars, "boll_dn"), "#38bdf8", { width: 1.2 });
    }
    if (state.overlays.has("BBI")) {
      addLine(0, lineData(bars, "bbi"), "#22d3ee", { width: 1.6 });
    }
    if (state.overlays.has("SAR")) {
      addLine(0, lineData(bars, "sar"), "#f472b6", {
        lineVisible: false,
        pointMarkersVisible: true,
        pointMarkersRadius: 2,
      });
    }
    if (state.overlays.has("KC")) {
      addLine(0, lineData(bars, "kc_up"), "#a78bfa", { width: 1.1 });
      addLine(0, lineData(bars, "kc_mid"), "#8b5cf6", { width: 1, style: LightweightCharts.LineStyle.Dashed });
      addLine(0, lineData(bars, "kc_dn"), "#a78bfa", { width: 1.1 });
    }

    // ===== PANES 1..N: ACTIVE SUB-INDICATORS =====
    const paneSubMap = []; // [{ paneIdx, key }]
    activeSubs.forEach((subKey, i) => {
      const paneIdx = i + 1;
      paneSubMap.push({ paneIdx, key: subKey });

      if (subKey === "VOL") {
        const volHist = chart.addSeries(
          LightweightCharts.HistogramSeries,
          { priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false },
          paneIdx
        );
        volHist.setData(
          bars.map((b) => ({
            time: b.time,
            value: b.volume || 0,
            color: b.close >= b.open ? p.upAlpha : p.downAlpha,
          }))
        );
        addLine(paneIdx, lineData(bars, "vol_ma5"), "#f0b90b", { width: 1, priceFormat: { type: "volume" } });
        addLine(paneIdx, lineData(bars, "vol_ma10"), "#38bdf8", { width: 1, priceFormat: { type: "volume" } });
      } else if (subKey === "MACD") {
        const macdHist = chart.addSeries(
          LightweightCharts.HistogramSeries,
          { priceFormat: { type: "price", precision: 3, minMove: 0.001 }, priceLineVisible: false, lastValueVisible: false },
          paneIdx
        );
        macdHist.setData(
          bars
            .filter((b) => b.macd_hist !== null && b.macd_hist !== undefined)
            .map((b) => ({
              time: b.time,
              value: b.macd_hist,
              color: b.macd_hist >= 0 ? p.up : p.down,
            }))
        );
        addLine(paneIdx, lineData(bars, "dif"), "#f0b90b", { width: 1.2, priceFormat: { type: "price", precision: 3, minMove: 0.001 } });
        addLine(paneIdx, lineData(bars, "dea"), "#38bdf8", { width: 1.2, priceFormat: { type: "price", precision: 3, minMove: 0.001 } });
      } else if (subKey === "KDJ") {
        const sk = addLine(paneIdx, lineData(bars, "kdj_k"), "#f0b90b", { width: 1.2 });
        addLine(paneIdx, lineData(bars, "kdj_d"), "#38bdf8", { width: 1.2 });
        addLine(paneIdx, lineData(bars, "kdj_j"), "#e879f9", { width: 1.2 });
        addRefLines(sk, [20, 80]);
      } else if (subKey === "RSI") {
        const sr = addLine(paneIdx, lineData(bars, "rsi6"), "#f0b90b", { width: 1.2 });
        addLine(paneIdx, lineData(bars, "rsi12"), "#38bdf8", { width: 1.2 });
        addLine(paneIdx, lineData(bars, "rsi24"), "#e879f9", { width: 1.1 });
        addRefLines(sr, [30, 70]);
      } else if (subKey === "WR") {
        const sw = addLine(paneIdx, lineData(bars, "wr10"), "#f0b90b", { width: 1.2 });
        addLine(paneIdx, lineData(bars, "wr6"), "#38bdf8", { width: 1.2 });
        addRefLines(sw, [20, 80]);
      } else if (subKey === "CCI") {
        const sc = addLine(paneIdx, lineData(bars, "cci"), "#22d3ee", { width: 1.3 });
        addRefLines(sc, [-100, 100]);
      } else if (subKey === "OBV") {
        addLine(paneIdx, lineData(bars, "obv"), "#38bdf8", { width: 1.3, priceFormat: { type: "volume" } });
        addLine(paneIdx, lineData(bars, "obv_ma30"), "#f0b90b", { width: 1.1, priceFormat: { type: "volume" } });
      } else if (subKey === "ATR") {
        addLine(paneIdx, lineData(bars, "atr14"), "#fb923c", { width: 1.3, priceFormat: { type: "price", precision: 3, minMove: 0.001 } });
      }
    });

    // Allocate crisp proportional heights to panes
    state.paneSubMap = paneSubMap;
    applyPaneHeights(totalH);

    // Default visible window: 日线/周线默认展示近 110 根（左拖或滚轮可看全部多年历史），月线直接展示近 120 月/全景
    const defaultSpan = state.interval === "月线" ? 140 : 110;
    if (bars.length > defaultSpan + 5) {
      chart.timeScale().setVisibleLogicalRange({ from: bars.length - defaultSpan, to: bars.length + 4 });
    } else {
      chart.timeScale().fitContent();
    }

    // Create subpane floating legends & update for latest bar
    const latestBar = bars[bars.length - 1];
    updateLegends(latestBar, paneSubMap);

    chart.subscribeCrosshairMove((param) => {
      const b = param && param.time ? state.barByTime.get(param.time) : null;
      updateLegends(b || latestBar, paneSubMap);
    });

    state.chartResizeObs = new ResizeObserver(() => {
      cancelAnimationFrame(state.resizeRaf);
      state.resizeRaf = requestAnimationFrame(() => resizeChart());
    });
    state.chartResizeObs.observe(scrollWrap);
    mount.onpointerup = () => requestAnimationFrame(() => positionSubpaneLegends(state.paneSubMap));
    requestAnimationFrame(() => positionSubpaneLegends(state.paneSubMap));
    setTimeout(() => positionSubpaneLegends(state.paneSubMap), 80);
  }

  function applyPaneHeights(totalH) {
    if (!state.chart) return;
    const panes = state.chart.panes();
    if (panes.length > 1) {
      const subEach = Math.max(78, Math.min(108, Math.floor((totalH * 0.40) / (panes.length - 1))));
      for (let i = 1; i < panes.length; i++) {
        panes[i].setHeight(subEach);
      }
    }
  }

  function resizeChart() {
    if (!state.chart) return;
    const scrollWrap = $("chartScrollWrap");
    const mount = $("chartMount");
    const activeSubs = (state.bootstrap?.sub_indicators || []).filter((k) => state.subs.has(k));
    const h = Math.max(scrollWrap.clientHeight, 260 + activeSubs.length * 90);
    if (mount.style.height !== h + "px") {
      mount.style.height = h + "px";
    }
    applyPaneHeights(h);
    requestAnimationFrame(() => positionSubpaneLegends(state.paneSubMap));
    setTimeout(() => positionSubpaneLegends(state.paneSubMap), 80);
  }

  function updateLegends(bar, paneSubMap) {
    if (!bar) return;
    const prev = state.prevCloseByTime.get(bar.time) || bar.open;
    const chg = prev ? ((bar.close / prev - 1) * 100) : 0;
    const cls = chg >= 0 ? "up" : "down";

    const parts = [
      `<span class="lg-item"><strong>${escHtml(bar.time)}</strong></span>`,
      `<span class="lg-item"><span class="lg-k">开</span><span class="${cls}">${fmtNum(bar.open, 2)}</span></span>`,
      `<span class="lg-item"><span class="lg-k">高</span><span class="${cls}">${fmtNum(bar.high, 2)}</span></span>`,
      `<span class="lg-item"><span class="lg-k">低</span><span class="${cls}">${fmtNum(bar.low, 2)}</span></span>`,
      `<span class="lg-item"><span class="lg-k">收</span><span class="${cls}">${fmtNum(bar.close, 2)} (${fmtPct(chg)})</span></span>`,
    ];

    if (state.overlays.has("MA")) {
      parts.push(
        `<span class="lg-item" style="color:#f0b90b">MA5 ${fmtNum(bar.ma5, 2)}</span>`,
        `<span class="lg-item" style="color:#38bdf8">MA10 ${fmtNum(bar.ma10, 2)}</span>`,
        `<span class="lg-item" style="color:#e879f9">MA20 ${fmtNum(bar.ma20, 2)}</span>`
      );
    }
    if (state.overlays.has("EMA")) {
      parts.push(
        `<span class="lg-item" style="color:#fb923c">EMA12 ${fmtNum(bar.ema12, 2)}</span>`,
        `<span class="lg-item" style="color:#60a5fa">EMA26 ${fmtNum(bar.ema26, 2)}</span>`
      );
    }
    if (state.overlays.has("BOLL")) {
      parts.push(
        `<span class="lg-item" style="color:#f59e0b">UP ${fmtNum(bar.boll_up, 2)}</span>`,
        `<span class="lg-item" style="color:#94a3b8">MID ${fmtNum(bar.boll_mid, 2)}</span>`,
        `<span class="lg-item" style="color:#38bdf8">DN ${fmtNum(bar.boll_dn, 2)}</span>`
      );
    }
    if (state.overlays.has("BBI")) {
      parts.push(`<span class="lg-item" style="color:#22d3ee">BBI ${fmtNum(bar.bbi, 2)}</span>`);
    }
    if (state.overlays.has("SAR")) {
      parts.push(`<span class="lg-item" style="color:#f472b6">SAR ${fmtNum(bar.sar, 2)}</span>`);
    }
    if (state.overlays.has("KC")) {
      parts.push(
        `<span class="lg-item" style="color:#a78bfa">KC ${fmtNum(bar.kc_up, 2)}/${fmtNum(bar.kc_dn, 2)}</span>`
      );
    }
    $("crosshairLegend").innerHTML = parts.join("");

    // Sub-pane floating legends
    const host = $("subpaneLegends");
    host.innerHTML = paneSubMap
      .map(({ paneIdx, key }) => `<div class="pane-legend-badge" data-pane="${paneIdx}">${formatSubLegend(key, bar)}</div>`)
      .join("");
    positionSubpaneLegends(paneSubMap);
  }

  function formatSubLegend(key, b) {
    switch (key) {
      case "VOL":
        return `<span>VOL ${fmtVol(b.volume)}</span><span style="color:#f0b90b">MA5 ${fmtVol(b.vol_ma5)}</span><span style="color:#38bdf8">MA10 ${fmtVol(b.vol_ma10)}</span>`;
      case "MACD":
        return `<span>MACD(12,26,9)</span><span style="color:#f0b90b">DIF ${fmtNum(b.dif, 3)}</span><span style="color:#38bdf8">DEA ${fmtNum(b.dea, 3)}</span><span class="${pctClass(b.macd_hist)}">BAR ${fmtNum(b.macd_hist, 3)}</span>`;
      case "KDJ":
        return `<span>KDJ(9,3,3)</span><span style="color:#f0b90b">K ${fmtNum(b.kdj_k, 1)}</span><span style="color:#38bdf8">D ${fmtNum(b.kdj_d, 1)}</span><span style="color:#e879f9">J ${fmtNum(b.kdj_j, 1)}</span>`;
      case "RSI":
        return `<span>RSI</span><span style="color:#f0b90b">6:${fmtNum(b.rsi6, 1)}</span><span style="color:#38bdf8">12:${fmtNum(b.rsi12, 1)}</span><span style="color:#e879f9">24:${fmtNum(b.rsi24, 1)}</span>`;
      case "WR":
        return `<span>WR</span><span style="color:#f0b90b">10:${fmtNum(b.wr10, 1)}</span><span style="color:#38bdf8">6:${fmtNum(b.wr6, 1)}</span>`;
      case "CCI":
        return `<span style="color:#22d3ee">CCI(14) ${fmtNum(b.cci, 1)}</span>`;
      case "OBV":
        return `<span style="color:#38bdf8">OBV ${fmtVol(b.obv)}</span><span style="color:#f0b90b">MA30 ${fmtVol(b.obv_ma30)}</span>`;
      case "ATR":
        return `<span style="color:#fb923c">ATR(14) ${fmtNum(b.atr14, 3)}</span>`;
      default:
        return escHtml(key);
    }
  }

  function positionSubpaneLegends(paneSubMap) {
    if (!state.chart || !paneSubMap?.length) return;
    const mount = $("chartMount");
    const mountRect = mount.getBoundingClientRect();
    const panes = state.chart.panes();
    // Measure actual rendered pane <tr> boxes (>35px tall, excluding 1px separators & ~26px time axis)
    const domPaneRows = Array.from(mount.querySelectorAll("tr")).filter(
      (tr) => tr.getBoundingClientRect().height > 35
    );
    if (domPaneRows.length >= panes.length) {
      for (const { paneIdx } of paneSubMap) {
        const badge = $("subpaneLegends").querySelector(`.pane-legend-badge[data-pane="${paneIdx}"]`);
        const row = domPaneRows[paneIdx];
        if (badge && row) {
          badge.style.top = Math.round(row.getBoundingClientRect().top - mountRect.top + 4) + "px";
        }
      }
      return;
    }
    let offsetY = 0;
    for (let i = 0; i < panes.length; i++) {
      const badge = $("subpaneLegends").querySelector(`.pane-legend-badge[data-pane="${i}"]`);
      if (badge) {
        badge.style.top = offsetY + 5 + "px";
      }
      offsetY += panes[i].getHeight() + 1;
    }
  }

  // ==================== EXPORT ====================
  async function handleExport(fmt) {
    const sourceRows = state.dockTab === "watchlist" ? state.watchlist : state.visibleRows;
    if (!sourceRows.length) {
      showToast("当前列表为空，没有可导出的股票", true);
      return;
    }
    const codes = Array.from(new Set(sourceRows.map((r) => r.code)));
    if (fmt === "copy") {
      const plain = codes.map((c) => c.split(".").pop()).join("\n");
      try {
        await navigator.clipboard.writeText(plain);
        showToast(`已复制 ${codes.length} 只股票 6 位代码`);
      } catch (_) {
        showToast("复制失败，请使用纯代码 .txt 导出", true);
      }
      return;
    }

    try {
      const res = await fetch("/api/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          format: fmt,
          codes,
          signals: state.signals.length ? state.signals : sourceRows,
        }),
      });
      if (!res.ok) throw new Error("导出失败");
      const blob = await res.blob();
      const disp = res.headers.get("Content-Disposition") || "";
      const m = disp.match(/filename="([^"]+)"/);
      const fname = m ? m[1] : `klineradar.${fmt}`;
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = fname;
      a.click();
      URL.revokeObjectURL(a.href);
      showToast(`已导出 ${fname}`);
    } catch (err) {
      showToast(err.message, true);
    }
  }

  // ==================== SYNC & CACHE VIEW ====================
  function formatLastSyncSummary(autoSync) {
    if (autoSync.error) return `上次同步异常：${autoSync.error}`;
    const info = autoSync.last_info;
    if (info && typeof info === "object" && info.time) {
      if (info.status === "error" && info.error) {
        return `${info.time} 同步失败：${info.error}`;
      }
      const upd = info.updated ?? 0;
      const skip = info.skipped ?? 0;
      const fail = info.failed ?? 0;
      return `最近同步 ${info.time}（更新 ${upd} / 跳过 ${skip}${fail ? ` / 失败 ${fail}` : ""}）`;
    }
    return autoSync.text || "空闲";
  }

  function renderCacheStatus(status, autoSync, timesList) {
    if (status) {
      const shortDate = status.newest ? status.newest.slice(5) : "--";
      $("navStatusText").textContent = `${(status.stocks || 0).toLocaleString()} 只 · ${shortDate}`;
      $("kpiStocks").textContent = (status.stocks || 0).toLocaleString();
      $("kpiRows").textContent = (status.rows || 0).toLocaleString();
      $("kpiNewest").textContent = status.newest || "--";
      $("kpiSize").textContent = status.size_mb !== undefined ? `${Number(status.size_mb).toFixed(1)} MB` : "--";
      $("cacheDbPath").textContent = status.db_name ? `本地库文件：${status.db_name}` : "";
    }

    if (timesList && $("autoSyncTime").options.length === 0) {
      $("autoSyncTime").innerHTML = timesList
        .map((t) => `<option value="${escHtml(t)}">${escHtml(t)}</option>`)
        .join("");
    }

    if (autoSync) {
      $("autoSyncEnabled").checked = Boolean(autoSync.enabled);
      if (autoSync.sync_time) $("autoSyncTime").value = autoSync.sync_time;

      const rawRatio = autoSync.ratio ?? autoSync.progress ?? 0;
      const pct = Math.max(0, Math.min(100, Math.round(rawRatio * 100)));
      $("syncProgressBar").style.width = `${pct}%`;
      $("syncProgressText").textContent = `${pct}%`;
      $("syncPhaseText").textContent = autoSync.running
        ? (autoSync.text || "正在同步...")
        : formatLastSyncSummary(autoSync);
      $("autoSyncStatusText").textContent = autoSync.today_status || "就绪";

      const navBtn = $("navSyncBtn");
      navBtn.classList.toggle("syncing", Boolean(autoSync.running));
      $("navSyncLabel").textContent = autoSync.running ? `同步中 ${pct}%` : "同步行情";
      $("btnManualSync").disabled = Boolean(autoSync.running);
      $("btnManualSync").textContent = autoSync.running ? `同步中 (${pct}%)` : "立即增量同步";
    }
  }

  async function triggerSync() {
    try {
      const res = await apiJson("/api/sync/start", { method: "POST" });
      if (res.started) showToast("已启动全市场增量同步");
      else showToast("同步任务已在后台运行中");
      renderCacheStatus(null, res.state);
      startSyncPolling();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function refreshSyncStatus() {
    try {
      const data = await apiJson("/api/sync/status");
      renderCacheStatus(data.status, data.auto_sync);
      return data;
    } catch (_) {
      return null;
    }
  }

  function startSyncPolling() {
    if (state.syncTimer) return;
    state.syncTimer = setInterval(async () => {
      const data = await refreshSyncStatus();
      if (data && data.auto_sync && !data.auto_sync.running) {
        clearInterval(state.syncTimer);
        state.syncTimer = null;
        showToast(formatLastSyncSummary(data.auto_sync), Boolean(data.auto_sync.error));
      }
    }, 1500);
  }

  async function saveSyncConfig() {
    try {
      const res = await apiJson("/api/sync/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          enabled: $("autoSyncEnabled").checked,
          sync_time: $("autoSyncTime").value,
        }),
      });
      renderCacheStatus(null, res.auto_sync);
      showToast("自动同步配置已保存");
    } catch (err) {
      showToast(err.message, true);
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
