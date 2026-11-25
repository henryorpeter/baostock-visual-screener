"""KLineRadar 核心模块 pytest 回归测试套件。

运行：pytest -v
"""
from datetime import datetime
from pathlib import Path
import shutil
import sqlite3
import sys
import uuid

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import charts
import db
import engine
import sync


@pytest.fixture
def isolated_db(monkeypatch):
    """为每个数据库/同步测试隔离独立的临时 SQLite 库，绝不污染本地行情缓存。"""
    tmp_dir = ROOT / ".pytest_tmp" / uuid.uuid4().hex
    tmp_dir.mkdir(parents=True, exist_ok=True)
    test_db = tmp_dir / "test_cache.sqlite3"
    monkeypatch.setattr(db, "CACHE_DB", test_db)
    monkeypatch.setattr(db, "_CACHE_INIT_PATH", None)
    db._invalidate_status_cache()
    db._init_cache()
    try:
        yield test_db
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _make_ohlcv(n=140, seed=42, trend=0.002, start="2026-01-02", code="sh.600000"):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start=start, periods=n)
    rets = rng.normal(trend, 0.018, size=n)
    close = 10.0 * np.cumprod(1.0 + rets)
    open_ = close * (1.0 + rng.normal(0.0, 0.006, size=n))
    high = np.maximum(open_, close) * (1.0 + rng.uniform(0.001, 0.015, size=n))
    low = np.minimum(open_, close) * (1.0 - rng.uniform(0.001, 0.015, size=n))
    vol = rng.uniform(1e6, 5e6, size=n)
    amt = close * vol
    turn = rng.uniform(1.0, 6.0, size=n)
    pct = pd.Series(close).pct_change().mul(100.0).fillna(0.0).to_numpy()
    return pd.DataFrame({
        "date": dates,
        "code": code,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": vol,
        "amount": amt,
        "turn": turn,
        "pctChg": pct,
        "isST": "0",
    })


# ==================== 1. db.py tests ====================

def test_db_schema_indexes_and_settings(isolated_db):
    with sqlite3.connect(isolated_db) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        idxs = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"daily", "sync_meta", "stock_names", "app_settings", "watchlist"}.issubset(tables)
    assert "idx_daily_date" in idxs
    assert "idx_daily_code_date" not in idxs

    db.save_auto_sync_config(False, "16:00")
    cfg = db.get_auto_sync_config()
    assert cfg["enabled"] is False
    assert cfg["sync_time"] == "16:00"


def test_db_cache_read_write_status_and_watchlist(isolated_db):
    df = _make_ohlcv(n=30, code="sh.600519")
    db._cache_write(df)
    read_back = db._cache_read("sh.600519", "2026-01-01", "2026-12-31")
    assert len(read_back) == 30

    st1 = db.cache_status(force=True)
    assert st1["stocks"] == 1 and st1["rows"] == 30
    st2 = db.cache_status()
    assert st2 == st1

    rows, newest, rev = db._cache_version()
    assert rows == 30 and newest != "" and rev == "0"

    # Watchlist CRUD
    db.watchlist_add("sh.600519", "贵州茅台")
    wl = db.watchlist_load()
    assert len(wl) == 1 and wl[0][0] == "sh.600519"
    db.watchlist_remove("sh.600519")
    assert db.watchlist_load() == []

    # Universe save / load
    uni = pd.DataFrame([{"code": "sh.600519", "code_name": "贵州茅台"}])
    db.cache_universe_save(uni)
    assert db.cache_universe_load() == [("sh.600519", "贵州茅台")]


# ==================== 2. engine.py tests ====================

def test_numpy_indicators_match_pandas_exactly():
    df = _make_ohlcv(n=150, seed=7)
    pd_feat = engine.indicators(df)
    np_feat = engine._fast_indicators_np(
        df["open"].to_numpy(),
        df["high"].to_numpy(),
        df["low"].to_numpy(),
        df["close"].to_numpy(),
        df["volume"].to_numpy(),
        df["amount"].to_numpy(),
        df["pctChg"].to_numpy(),
    )
    assert np_feat is not None
    # feat order: o, h, l, c, v, amt, pct, ma5, ma10, ma20, ma25, ma60, rsi, macd, sig, k, d, vol_ma5, vol_ma20
    _, _, _, _, _, _, _, ma5, ma10, ma20, ma25, ma60, rsi, macd, sig, k, d, v5, v20 = np_feat
    assert len(ma5) == len(pd_feat)
    for arr, col in (
        (ma5, "ma5"), (ma10, "ma10"), (ma20, "ma20"), (ma25, "ma25"),
        (rsi, "rsi"), (macd, "macd"), (sig, "signal"), (k, "k"), (d, "d"),
        (v5, "vol_ma5"), (v20, "vol_ma20"),
    ):
        np.testing.assert_allclose(arr, pd_feat[col].to_numpy(), rtol=1e-9, atol=1e-9, equal_nan=True)
    np.testing.assert_allclose(ma60, pd_feat["ma60"].to_numpy(), rtol=1e-9, atol=1e-9, equal_nan=True)


def test_board_thresholds_and_export_formatters():
    assert engine._limit_pct_threshold("sh.600519") == 9.5
    assert engine._limit_pct_threshold("sz.000001") == 9.5
    assert engine._limit_pct_threshold("sz.300750") == 19.5
    assert engine._limit_pct_threshold("sh.688981") == 19.5
    assert engine._limit_pct_threshold("bj.430047") == 29.5
    assert engine._is_gem_or_star("sz.300750") is True
    assert engine._is_gem_or_star("sh.600519") is False

    assert engine._format_tdx_ebk(["sh.600467", "sz.002771", "sh.600467"]) == b"1600467\r\n0002771\r\n"
    assert engine._format_plain_codes(["sh.600467", "sz.002771", "sh.600467"]) == "600467\n002771"


def test_screen_filters_and_golden_triangle_distinctness():
    # 构造一只确定触发“金叉不绿 + 阻力位突破 + 海龟突破”的主板股、一只 ST 股和一只创业板股
    base = _make_ohlcv(n=130, seed=11, trend=0.001)
    # 最后一根 K 线强放量阳线突破前 20 日高点且成交额 > 2 亿
    base.iloc[-1, base.columns.get_loc("open")] = base["close"].iloc[-2] * 1.01
    base.iloc[-1, base.columns.get_loc("close")] = base["high"].iloc[-21:-1].max() * 1.04
    base.iloc[-1, base.columns.get_loc("high")] = base["close"].iloc[-1] * 1.01
    base.iloc[-1, base.columns.get_loc("low")] = base["open"].iloc[-1] * 0.99
    base.iloc[-1, base.columns.get_loc("volume")] = base["volume"].iloc[-21:-1].mean() * 3.0
    base.iloc[-1, base.columns.get_loc("amount")] = 2.5e8
    base.iloc[-1, base.columns.get_loc("turn")] = 5.0
    base.iloc[-1, base.columns.get_loc("pctChg")] = 6.5

    frames = {
        "sh.600100": base.copy(),
        "sh.600101": base.copy(),
        "sz.300100": base.copy(),
    }
    codes = [
        ("sh.600100", "正常测试股"),
        ("sh.600101", "*ST风险股"),
        ("sz.300100", "创业测试股"),
    ]
    res, stats = engine.screen(
        codes, frames, ["海龟突破策略", "阻力位突破策略"],
        min_change=0.0, min_turn=0.0, max_turn=20.0,
        on_progress=lambda *a, **k: None,
        exclude_st=True, exclude_gem_star=True, min_amount=1.0,
    )
    assert stats["filtered"] == 2
    assert set(res["代码"]) == {"sh.600100"}
    assert set(res["策略"]) == {"海龟突破策略", "阻力位突破策略"}
    assert "成交额(亿)" in res.columns


def test_compute_forward_returns_with_resonance(isolated_db):
    df_up = _make_ohlcv(n=25, seed=1, trend=0.015, code="sh.600100")
    df_up["close"] = np.linspace(10.0, 14.0, 25)
    df_dn = _make_ohlcv(n=25, seed=2, trend=-0.01, code="sh.600101")
    df_dn["close"] = np.linspace(10.0, 8.0, 25)
    db._cache_write(pd.concat([df_up, df_dn], ignore_index=True))

    sig_date = df_up["date"].iloc[5].strftime("%Y-%m-%d")
    res_df = pd.DataFrame([
        {"策略": "海龟突破策略", "代码": "sh.600100", "名称": "A", "收盘": float(df_up["close"].iloc[5])},
        {"策略": "阻力位突破策略", "代码": "sh.600100", "名称": "A", "收盘": float(df_up["close"].iloc[5])},
        {"策略": "海龟突破策略", "代码": "sh.600101", "名称": "B", "收盘": float(df_dn["close"].iloc[5])},
    ])
    fwd = engine.compute_forward_returns(res_df, sig_date)
    assert not fwd.empty
    assert "★ 多策略共振 (≥2)" in fwd["策略"].tolist()
    res_row = fwd[fwd["策略"] == "★ 多策略共振 (≥2)"].iloc[0]
    assert res_row["样本数"] == 1 and res_row["5日胜率%"] == 100.0 and res_row["5日均涨幅%"] > 0


# ==================== 3. charts.py tests ====================

def test_charts_resample_panes_and_formatters():
    df = _make_ohlcv(n=120, seed=5)
    for iv in ("日线", "周线", "月线"):
        panes, kdf = charts.chart_panes(df, "测试 · sh.600000", iv, show_ma=True, show_macd=True)
        assert len(panes) == 3 and not kdf.empty

    # 全套 6 主图(MA/EMA/BOLL/BBI/SAR/KC) + 8 副图(VOL/MACD/KDJ/RSI/WR/CCI/OBV/ATR) 叠加验证
    full_panes, kdf_all = charts.chart_panes(
        df, "测试 · sh.600000", "日线",
        overlays=list(charts.MAIN_OVERLAYS),
        sub_panes=list(charts.SUB_INDICATORS),
    )
    assert len(full_panes) == 9
    # 主图包含：1 蜡烛 + 4 MA + 3 EMA + 3 BOLL + 1 BBI + 1 SAR + 3 KC = 16 条序列
    assert len(full_panes[0]["series"]) == 16
    for col in (
        "ema12", "ema60", "boll_up", "boll_dn", "bbi", "sar", "kc_up", "kc_dn",
        "kdj_k", "kdj_d", "kdj_j", "rsi6", "rsi12", "rsi24",
        "wr10", "wr6", "cci", "obv", "obv_ma30", "atr14",
    ):
        assert col in kdf_all.columns and kdf_all[col].dropna().size > 50

    assert charts._fmt_amount(2.5e8) == "2.50 亿"
    assert charts._fmt_amount(3.2e4) == "3 万"
    assert charts._fmt_volume(1.5e6) == "1.50 M"
    assert "fecaca" in charts._style_pct(10.0)
    assert "0ecb81" in charts._style_pct(-2.5)


# ==================== 4. sync.py unit tests ====================

def test_sync_recent_trade_days_and_auto_sync_schedule(isolated_db, monkeypatch):
    df = _make_ohlcv(n=10, start="2026-11-10", code="sh.600000")
    db._cache_write(df)
    last_two = sorted(df["date"].dt.strftime("%Y-%m-%d").tolist())[-2:]

    # 模拟 ifzq 不可用且 qt 返回本地最新日，验证在 15:00 前会自动排除当日盘中 Bar
    class _BrokenSession:
        def get(self, *a, **k):
            raise RuntimeError("mock network down")

    monkeypatch.setattr(sync, "_http_session", lambda: _BrokenSession())
    monkeypatch.setattr(sync, "_probe_snapshot_date", lambda: last_two[-1])
    monkeypatch.setattr(sync, "BAOSTOCK_AVAILABLE", False)

    eff_after, prev_after = sync._recent_trade_days(
        last_two[-1], now=datetime.strptime(last_two[-1] + " 15:30", "%Y-%m-%d %H:%M")
    )
    assert eff_after == last_two[-1] and prev_after == last_two[-2]

    eff_intraday, _ = sync._recent_trade_days(
        last_two[-1], now=datetime.strptime(last_two[-1] + " 11:00", "%Y-%m-%d %H:%M")
    )
    assert eff_intraday == last_two[-2]

    mgr = sync.AutoSyncManager()
    # 周末休市
    ok, reason = mgr.should_run_today(now=datetime(2026, 11, 21, 16, 0), local_status={"newest": "2026-11-20"})
    assert ok is False and "周末" in reason
    # 盘中未到触发时间
    db.save_auto_sync_config(True, "15:35")
    ok, reason = mgr.should_run_today(now=datetime(2026, 11, 25, 14, 0), local_status={"newest": "2026-11-24"})
    assert ok is False and "等待收盘后" in reason
    # 收盘后到达触发时间且本地尚未更新到今天
    ok, reason = mgr.should_run_today(now=datetime(2026, 11, 25, 15, 40), local_status={"newest": "2026-11-24"})
    assert ok is True


# ==================== 5. Web API (Starlette ASGI) tests ====================

def _asgi_call(app, method, path, query_string="", json_body=None):
    """零外部依赖（无需 httpx）的轻量 Starlette ASGI 直接调用器。"""
    import asyncio
    import json

    raw_body = json.dumps(json_body).encode("utf-8") if json_body is not None else b""
    headers = [(b"host", b"testserver")]
    if json_body is not None:
        headers.append((b"content-type", b"application/json"))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": query_string.encode("utf-8"),
        "headers": headers,
        "client": ("127.0.0.1", 0),
        "server": ("testserver", 8506),
    }
    sent = {"done": False}
    done_evt = asyncio.Event()
    status_code = 500
    resp_headers = {}
    chunks = []

    async def receive():
        if not sent["done"]:
            sent["done"] = True
            return {"type": "http.request", "body": raw_body, "more_body": False}
        await done_evt.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        nonlocal status_code
        if message["type"] == "http.response.start":
            status_code = message["status"]
            for k, v in message.get("headers", []):
                resp_headers[k.decode("latin1").lower()] = v.decode("latin1")
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                done_evt.set()

    async def run():
        await app(scope, receive, send)

    asyncio.run(run())
    body_bytes = b"".join(chunks)
    return status_code, resp_headers, body_bytes


def test_web_server_api_end_to_end(isolated_db):
    import json
    import web_server

    df = _make_ohlcv(n=130, seed=17, trend=0.002, code="sh.600519")
    df.iloc[-1, df.columns.get_loc("open")] = df["close"].iloc[-2] * 1.01
    df.iloc[-1, df.columns.get_loc("close")] = df["high"].iloc[-21:-1].max() * 1.04
    df.iloc[-1, df.columns.get_loc("high")] = df["close"].iloc[-1] * 1.01
    df.iloc[-1, df.columns.get_loc("low")] = df["open"].iloc[-1] * 0.99
    df.iloc[-1, df.columns.get_loc("volume")] = df["volume"].iloc[-21:-1].mean() * 3.0
    df.iloc[-1, df.columns.get_loc("amount")] = 3.2e8
    df.iloc[-1, df.columns.get_loc("turn")] = 4.5
    df.iloc[-1, df.columns.get_loc("pctChg")] = 6.2
    db._cache_write(df)
    db.cache_universe_save(pd.DataFrame([{"code": "sh.600519", "code_name": "贵州茅台"}]))
    web_server._get_universe_cached(force=True)

    # 1. Index HTML & bootstrap
    st, _, html_bytes = _asgi_call(web_server.web_app, "GET", "/")
    assert st == 200 and b"KLineRadar" in html_bytes

    st, _, raw = _asgi_call(web_server.web_app, "GET", "/api/bootstrap")
    assert st == 200
    boot = json.loads(raw)
    assert boot["status"]["stocks"] == 1
    assert len(boot["strategies"]) == 20
    assert "BOLL" in boot["main_overlays"] and "KDJ" in boot["sub_indicators"]

    # 2. Search
    st, _, raw = _asgi_call(web_server.web_app, "GET", "/api/search", "q=600519")
    assert st == 200 and json.loads(raw)["items"][0]["code"] == "sh.600519"

    # 3. Screen (also verifies dict-based column mapping for pctChg/turn/amount_yi)
    last_day = df["date"].iloc[-1].strftime("%Y-%m-%d")
    first_day = df["date"].iloc[0].strftime("%Y-%m-%d")
    st, _, raw = _asgi_call(
        web_server.web_app,
        "POST",
        "/api/screen",
        json_body={
            "strategies": ["海龟突破策略", "阻力位突破策略"],
            "start": first_day,
            "end": last_day,
            "exclude_st": True,
        },
    )
    assert st == 200
    scr = json.loads(raw)
    assert len(scr["aggregated"]) == 1
    sig = scr["signals"][0]
    assert sig["code"] == "sh.600519"
    assert sig["pctChg"] == 6.2 and sig["turn"] == 4.5 and sig["amount_yi"] == 3.2

    # 4. K-line with all 14 indicators
    st, _, raw = _asgi_call(web_server.web_app, "GET", "/api/kline/sh.600519", f"interval=日线&start={first_day}&end={last_day}")
    assert st == 200
    kl = json.loads(raw)
    assert kl["code"] == "sh.600519" and len(kl["bars"]) == 130
    last_bar = kl["bars"][-1]
    for col in ("ma5", "ema12", "boll_up", "bbi", "sar", "kc_up", "dif", "kdj_k", "rsi6", "wr10", "cci", "obv", "atr14"):
        assert last_bar[col] is not None

    # 5. Watchlist CRUD & Export
    st, _, raw = _asgi_call(web_server.web_app, "POST", "/api/watchlist", json_body={"code": "sh.600519", "name": "贵州茅台"})
    assert st == 200 and len(json.loads(raw)["items"]) == 1
    st, _, raw = _asgi_call(web_server.web_app, "DELETE", "/api/watchlist/sh.600519")
    assert st == 200 and json.loads(raw)["items"] == []

    st, hdrs, ebk = _asgi_call(web_server.web_app, "POST", "/api/export", json_body={"format": "ebk", "codes": ["sh.600519"]})
    assert st == 200 and ebk == b"1600519\r\n" and ".ebk" in hdrs.get("content-disposition", "")


# ==================== 6. Legacy AppTest E2E smoke tests ====================

@pytest.mark.parametrize("entry_file", ["app.py", "baostock_screener.py"])
def test_streamlit_entrypoints_boot_without_exceptions(entry_file):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / entry_file), default_timeout=25)
    at.run()
    assert not at.exception, f"{entry_file} raised: {[str(e.value) for e in at.exception]}"
