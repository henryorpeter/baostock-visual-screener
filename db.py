"""SQLite 持久化存储层：日线缓存、股票池字典、自选观察池与应用配置。"""
from contextlib import contextmanager
from datetime import date, datetime
import json
from pathlib import Path
import sqlite3
import threading
import time

import pandas as pd


DAILY_FIELDS = "date,code,open,high,low,close,volume,amount,turn,pctChg,isST"
CACHE_DB = Path(__file__).with_name("stock_cache.sqlite3")
AUTO_SYNC_TIMES = ["15:15", "15:30", "15:35", "16:00", "17:00", "18:00", "20:00"]

_INIT_LOCK = threading.Lock()
_CACHE_INIT_PATH = None
_STATUS_LOCK = threading.Lock()
_STATUS_CACHE = {"ts": 0.0, "data": None}


@contextmanager
def connect(timeout=30):
    conn = sqlite3.connect(CACHE_DB, timeout=timeout)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _invalidate_status_cache():
    with _STATUS_LOCK:
        _STATUS_CACHE["ts"] = 0.0
        _STATUS_CACHE["data"] = None


def _init_cache():
    global _CACHE_INIT_PATH
    if _CACHE_INIT_PATH == CACHE_DB and CACHE_DB.exists():
        return
    with _INIT_LOCK:
        if _CACHE_INIT_PATH == CACHE_DB and CACHE_DB.exists():
            return
        with connect(timeout=30) as conn:
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
        _CACHE_INIT_PATH = CACHE_DB


def _setting_get(key, default=None):
    _init_cache()
    with connect(timeout=30) as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _setting_set(key, value):
    _init_cache()
    with connect(timeout=30) as conn:
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
    with connect(timeout=30) as conn:
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
    with connect(timeout=30) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO daily (date,code,open,high,low,close,volume,amount,turn,pctChg,isST) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)", data.itertuples(index=False, name=None),
        )
    _invalidate_status_cache()



def cache_status(force=False):
    now = time.monotonic()
    with _STATUS_LOCK:
        if not force and _STATUS_CACHE["data"] is not None and (now - _STATUS_CACHE["ts"]) < 10.0:
            return dict(_STATUS_CACHE["data"])
    _init_cache()
    with connect(timeout=30) as conn:
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
    with connect(timeout=30) as conn:
        return conn.execute("SELECT code, name, added_at FROM watchlist ORDER BY added_at DESC, code").fetchall()


def watchlist_add(code, name):
    _init_cache()
    with connect(timeout=30) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO watchlist(code, name, added_at) VALUES (?, ?, ?)",
            (code, name, datetime.now().strftime("%Y-%m-%d %H:%M")),
        )


def watchlist_remove(code):
    _init_cache()
    with connect(timeout=30) as conn:
        conn.execute("DELETE FROM watchlist WHERE code=?", (code,))


def cache_universe_save(stocks):
    _init_cache()
    with connect(timeout=30) as conn:
        conn.execute("DELETE FROM stock_names")
        conn.executemany("INSERT OR REPLACE INTO stock_names(code,name) VALUES (?,?)",
                         stocks[["code", "code_name"]].itertuples(index=False, name=None))
    _setting_set("stock_list_updated_date", date.today().strftime("%Y-%m-%d"))
    _invalidate_status_cache()


def cache_universe_load():
    _init_cache()
    with connect(timeout=30) as conn:
        return conn.execute("SELECT code,name FROM stock_names ORDER BY code").fetchall()




def _cache_version():
    _init_cache()
    with connect(timeout=30) as conn:
        rows, newest = conn.execute("SELECT COUNT(*), COALESCE(MAX(date), '') FROM daily").fetchone()
        rev_row = conn.execute("SELECT value FROM app_settings WHERE key='cache_rev'").fetchone()
    return int(rows or 0), str(newest or ""), (rev_row[0] if rev_row else "0")


