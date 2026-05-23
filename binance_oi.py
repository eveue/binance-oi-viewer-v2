"""币安 USDT 永续合约 OI 数据采集模块"""

from __future__ import annotations

import io
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import requests

VISION_BASE = "https://data.binance.vision/data/futures/um/daily/metrics"
FAPI_BASE = "https://fapi.binance.com"
EXCHANGE_INFO_URL = f"{FAPI_BASE}/fapi/v1/exchangeInfo"
OI_HIST_URL = f"{FAPI_BASE}/futures/data/openInterestHist"
KLINES_URL = f"{FAPI_BASE}/fapi/v1/klines"

HTTP_TIMEOUT = 30
MAX_WORKERS = 8

METRICS_COLUMNS = [
    "create_time", "symbol", "sum_open_interest", "sum_open_interest_value",
    "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
    "count_long_short_ratio", "sum_taker_long_short_vol_ratio",
]


def fetch_usdt_perpetual_symbols() -> list:
    resp = requests.get(EXCHANGE_INFO_URL, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    symbols = []
    for s in data.get("symbols", []):
        if (s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT"
                and s.get("status") == "TRADING"):
            symbols.append(s["symbol"])
    return sorted(symbols)


def _build_vision_url(symbol: str, day: date) -> str:
    fname = f"{symbol}-metrics-{day.isoformat()}.zip"
    return f"{VISION_BASE}/{symbol}/{fname}"


def _fetch_one_day(symbol: str, day: date) -> Optional[pd.DataFrame]:
    url = _build_vision_url(symbol, day)
    try:
        resp = requests.get(url, timeout=HTTP_TIMEOUT)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            csv_name = zf.namelist()[0]
            with zf.open(csv_name) as f:
                raw = f.read().decode("utf-8")
        first_line = raw.splitlines()[0]
        has_header = "create_time" in first_line
        df = pd.read_csv(
            io.StringIO(raw),
            header=0 if has_header else None,
            names=None if has_header else METRICS_COLUMNS,
        )
    except Exception:
        return None

    if "create_time" not in df.columns:
        return None

    df["create_time"] = pd.to_datetime(df["create_time"], utc=True, errors="coerce")
    df["sum_open_interest"] = pd.to_numeric(df["sum_open_interest"], errors="coerce")
    df["sum_open_interest_value"] = pd.to_numeric(df["sum_open_interest_value"], errors="coerce")
    df = df.dropna(subset=["create_time", "sum_open_interest_value"])
    df = df[["create_time", "symbol", "sum_open_interest", "sum_open_interest_value"]]
    return df


def fetch_history_range(symbol, start, end, progress_callback=None):
    if end < start:
        return pd.DataFrame(columns=["create_time", "symbol", "sum_open_interest", "sum_open_interest_value"])

    days = []
    cursor = start
    while cursor <= end:
        days.append(cursor)
        cursor += timedelta(days=1)

    frames = []
    completed = 0
    total = len(days)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_one_day, symbol, d): d for d in days}
        for fut in as_completed(futures):
            df = fut.result()
            if df is not None and not df.empty:
                frames.append(df)
            completed += 1
            if progress_callback:
                progress_callback(completed, total)

    if not frames:
        return pd.DataFrame(columns=["create_time", "symbol", "sum_open_interest", "sum_open_interest_value"])

    out = pd.concat(frames, ignore_index=True)
    out = out.sort_values("create_time").reset_index(drop=True)
    return out


def fetch_recent_oi(symbol, period="5m", limit=500):
    params = {"symbol": symbol, "period": period, "limit": limit}
    resp = requests.get(OI_HIST_URL, params=params, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        return pd.DataFrame(columns=["create_time", "symbol", "sum_open_interest", "sum_open_interest_value"])

    df = pd.DataFrame(rows)
    df["create_time"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df["sum_open_interest"] = pd.to_numeric(df["sumOpenInterest"], errors="coerce")
    df["sum_open_interest_value"] = pd.to_numeric(df["sumOpenInterestValue"], errors="coerce")
    return df[["create_time", "symbol", "sum_open_interest", "sum_open_interest_value"]].sort_values("create_time").reset_index(drop=True)


def fetch_full_range(symbol, start, end, progress_callback=None):
    today_utc = datetime.now(timezone.utc).date()
    yesterday_utc = today_utc - timedelta(days=1)

    start_date = start.date()
    end_date = end.date()

    archive_end = min(end_date, yesterday_utc)
    if start_date <= archive_end:
        hist = fetch_history_range(symbol, start_date, archive_end, progress_callback)
    else:
        hist = pd.DataFrame(columns=["create_time", "symbol", "sum_open_interest", "sum_open_interest_value"])

    recent = pd.DataFrame(columns=hist.columns)
    if end_date >= yesterday_utc:
        try:
            recent = fetch_recent_oi(symbol, period="5m", limit=500)
        except Exception:
            pass

    combined = pd.concat([hist, recent], ignore_index=True)
    if combined.empty:
        return combined

    combined = combined.drop_duplicates(subset=["create_time"], keep="last")
    combined = combined.sort_values("create_time").reset_index(drop=True)

    start_utc = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
    end_utc = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
    mask = (combined["create_time"] >= start_utc) & (combined["create_time"] <= end_utc)
    return combined.loc[mask].reset_index(drop=True)


# ==========================================================================
# 新增：价格 K 线（给「价格视图」用）
# ==========================================================================
# 币安永续合约 K 线接口免费、覆盖长历史，单次最多 1500 根，
# 跨度长时自动分页循环抓取。返回列：create_time / close（收盘价 USD）。

_INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "1d": 86_400_000,
}


def _pick_price_interval(span_seconds: float) -> str:
    """根据查询跨度自动选合适的 K 线粒度，避免点数过多。"""
    hours = span_seconds / 3600
    if hours <= 24:
        return "5m"
    if hours <= 24 * 7:
        return "15m"
    if hours <= 24 * 35:
        return "1h"
    if hours <= 24 * 120:
        return "4h"
    return "1d"


def fetch_price_klines(symbol, start, end, interval: Optional[str] = None):
    """
    拉取币安永续合约收盘价序列，用于价格视图 / OI 叠加。

    返回 DataFrame，列：create_time(UTC), close(float)。失败返回空表。
    """
    start_utc = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
    end_utc = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
    start_ms = int(start_utc.timestamp() * 1000)
    end_ms = int(end_utc.timestamp() * 1000)

    if interval is None:
        interval = _pick_price_interval((end_utc - start_utc).total_seconds())
    step = _INTERVAL_MS.get(interval, 3_600_000)

    rows = []
    cursor = start_ms
    # 安全上限，避免极端跨度下死循环
    for _ in range(200):
        if cursor > end_ms:
            break
        try:
            resp = requests.get(
                KLINES_URL,
                params={
                    "symbol": symbol,
                    "interval": interval,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": 1500,
                },
                timeout=HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            batch = resp.json()
        except Exception:
            break
        if not batch:
            break
        rows.extend(batch)
        last_open = batch[-1][0]
        nxt = last_open + step
        if nxt <= cursor:        # 防止不前进
            break
        cursor = nxt
        if len(batch) < 1500:    # 已到末尾
            break

    if not rows:
        return pd.DataFrame(columns=["create_time", "close"])

    df = pd.DataFrame(rows)
    df = df[[0, 4]]
    df.columns = ["open_time", "close"]
    df["create_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"])
    df = df[(df["create_time"] >= start_utc) & (df["create_time"] <= end_utc)]
    df = df.drop_duplicates(subset=["create_time"]).sort_values("create_time")
    return df[["create_time", "close"]].reset_index(drop=True)
