"""
Coinalyze API 封装
==================

为「丢一个币 → 看它在所有交易所的合约 OI（含历史、可聚合）」提供数据。

数据来源：Coinalyze 免费 API（https://api.coinalyze.net/v1）
- 它已聚合 28 个交易所（含 Hyperliquid / Aster / dYdX 等 DEX）的衍生品数据
- 提供当前 OI 与 OI 历史，省去自己对接各所原生 API

关键限制（来自官方文档）：
- 限频 40 次/分钟/Key
- 日内粒度（1m~12h）只保留最近 1500~2000 个点，旧的每天删
  → 看一个月历史请用 1hour 及以上粒度
- /open-interest 与历史接口 symbols 每次最多 20 个
- 历史返回按时间升序

symbol 命名规则（实测）：
- 格式 = {symbol_on_exchange}_{类型}.{交易所代号}，如 BTCUSDT_PERP.A
  （注意：部分所简写成 BTCUSDT.6 这种，无 _PERP）
- exchange 字段是单字符代号，需用 EXCHANGE_CODES 映射成交易所名
- margined: "STABLE"=U本位(USDT等), "COIN"=币本位(USD)
- is_perpetual: 是否永续
"""

from __future__ import annotations

import os
import time as _time
from dataclasses import dataclass, field
from typing import Optional

import requests

API_BASE = "https://api.coinalyze.net/v1"
HTTP_TIMEOUT = 20

# 交易所代号 → 名称（实测 /exchanges 返回）
EXCHANGE_CODES = {
    "P": "Poloniex", "V": "Vertex", "D": "Bitforex", "K": "Kraken",
    "U": "Bithumb", "B": "Bitstamp", "H": "Hyperliquid", "L": "BitFlyer",
    "M": "BtcMarkets", "I": "Bit2c", "E": "MercadoBitcoin",
    "N": "Independent Reserve", "G": "Gemini", "Y": "Gate.io",
    "2": "Deribit", "3": "OKX", "C": "Coinbase", "F": "Bitfinex",
    "J": "Luno", "0": "BitMEX", "7": "Phemex", "W": "WOO X",
    "4": "Huobi", "8": "dYdX", "6": "Bybit", "A": "Binance",
    "T": "Lighter", "S": "Aster",
}

# 哪些算 DEX（用于前端分类标注）
DEX_NAMES = {"Hyperliquid", "Aster", "dYdX", "Vertex", "Lighter"}


def _api_key() -> str:
    key = os.environ.get("COINALYZE_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "未配置 COINALYZE_API_KEY 环境变量。"
            "请在 Render → Environment 添加 COINALYZE_API_KEY = 你的Key。"
        )
    return key


def _get(path: str, params: dict) -> list | dict:
    """统一 GET，带 Key、限频重试。"""
    params = dict(params or {})
    params["api_key"] = _api_key()
    url = f"{API_BASE}{path}"
    for attempt in range(3):
        resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
        if resp.status_code == 429:               # 触发限频，等一下再试
            wait = int(resp.headers.get("Retry-After", "2"))
            _time.sleep(min(wait, 5))
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()
    return resp.json()


@dataclass
class Contract:
    """一个合约（某币种在某所的某种合约）。"""
    symbol: str                 # Coinalyze 内部 symbol，如 BTCUSDT_PERP.A
    exchange_code: str
    exchange_name: str
    symbol_on_exchange: str
    base_asset: str
    quote_asset: str
    is_perpetual: bool
    margined: str               # STABLE / COIN
    is_dex: bool = False

    @property
    def margin_label(self) -> str:
        # STABLE → 用 quote_asset 表示（USDT/USDC…）；COIN → 币本位(USD)
        return self.quote_asset if self.margined == "STABLE" else f"{self.quote_asset}(币本位)"

    @property
    def kind_label(self) -> str:
        return "Perp" if self.is_perpetual else "Futures"

    @property
    def display(self) -> str:
        # 形如：BTC / USDT Perp - Binance
        return f"{self.base_asset} / {self.quote_asset} {self.kind_label} - {self.exchange_name}"


# --------------------------------------------------------------------------
# 合约清单
# --------------------------------------------------------------------------
_markets_cache: dict = {}


def load_future_markets(force: bool = False) -> list[Contract]:
    """拉取全部期货合约清单（量大，建议上层缓存）。"""
    if _markets_cache.get("data") and not force:
        return _markets_cache["data"]

    raw = _get("/future-markets", {})
    out: list[Contract] = []
    for r in raw:
        code = r.get("exchange", "")
        name = EXCHANGE_CODES.get(code, code)
        out.append(Contract(
            symbol=r.get("symbol", ""),
            exchange_code=code,
            exchange_name=name,
            symbol_on_exchange=r.get("symbol_on_exchange", ""),
            base_asset=(r.get("base_asset") or "").upper(),
            quote_asset=(r.get("quote_asset") or "").upper(),
            is_perpetual=bool(r.get("is_perpetual")),
            margined=r.get("margined", ""),
            is_dex=(name in DEX_NAMES),
        ))
    _markets_cache["data"] = out
    return out


def list_base_assets() -> list[str]:
    """所有可选 base_asset（去重排序），给前端下拉用。"""
    markets = load_future_markets()
    bases = sorted({c.base_asset for c in markets if c.base_asset})
    return bases


def contracts_for_base(base_asset: str, perpetual_only: bool = True) -> list[Contract]:
    """
    某个币种（如 BTC）在所有所的合约。
    默认只取永续。按交易所名排序。
    """
    base = base_asset.upper()
    markets = load_future_markets()
    res = [c for c in markets if c.base_asset == base]
    if perpetual_only:
        res = [c for c in res if c.is_perpetual]
    res.sort(key=lambda c: (c.exchange_name, c.quote_asset))
    return res


# --------------------------------------------------------------------------
# 当前 OI
# --------------------------------------------------------------------------
def current_open_interest(symbols: list[str], convert_to_usd: bool = True) -> dict[str, float]:
    """
    批量取当前 OI。symbols 每批最多 20 个，自动分批。
    返回 {symbol: value}。convert_to_usd=True 时 value 为美元名义价值。
    """
    result: dict[str, float] = {}
    for i in range(0, len(symbols), 20):
        batch = symbols[i:i + 20]
        rows = _get("/open-interest", {
            "symbols": ",".join(batch),
            "convert_to_usd": "true" if convert_to_usd else "false",
        })
        for r in rows:
            result[r.get("symbol")] = float(r.get("value") or 0)
    return result


# --------------------------------------------------------------------------
# OI 历史
# --------------------------------------------------------------------------
# 粒度选择：一个月历史用 1hour（约 720 点，在 1500 上限内且不会被删）
def pick_interval(span_seconds: float) -> str:
    hours = span_seconds / 3600
    if hours <= 24:
        return "5min"
    if hours <= 24 * 4:
        return "15min"
    if hours <= 24 * 10:
        return "30min"
    if hours <= 24 * 90:
        return "1hour"
    if hours <= 24 * 365:
        return "4hour"
    return "daily"


def open_interest_history(symbol: str, start_ts: int, end_ts: int,
                          interval: Optional[str] = None,
                          convert_to_usd: bool = True):
    """
    单个合约的 OI 历史。
    start_ts/end_ts 为 UNIX 秒。返回 list[dict]，每项含 t(秒) 和 c(收盘OI)。
    Coinalyze 历史接口字段：t(time), o/h/l/c(OHLC of OI)。我们取 c。
    """
    if interval is None:
        interval = pick_interval(end_ts - start_ts)
    rows = _get("/open-interest-history", {
        "symbols": symbol,
        "interval": interval,
        "from": int(start_ts),
        "to": int(end_ts),
        "convert_to_usd": "true" if convert_to_usd else "false",
    })
    # 返回结构：[{"symbol":..., "history":[{"t":.., "o":.., "h":.., "l":.., "c":..}, ...]}]
    if not rows:
        return []
    hist = rows[0].get("history", []) if isinstance(rows, list) else []
    out = []
    for h in hist:
        out.append({"t": int(h.get("t", 0)), "c": float(h.get("c") or 0)})
    return out


# --------------------------------------------------------------------------
# 价格（OHLCV）历史
# --------------------------------------------------------------------------
def price_history(symbol: str, start_ts: int, end_ts: int,
                  interval: Optional[str] = None):
    """
    某合约的价格 K 线（收盘价）。返回 list[{"t":秒, "c":收盘价}]。
    用于价格视图 / 与 OI 叠加。
    """
    if interval is None:
        interval = pick_interval(end_ts - start_ts)
    rows = _get("/ohlcv-history", {
        "symbols": symbol,
        "interval": interval,
        "from": int(start_ts),
        "to": int(end_ts),
    })
    if not rows:
        return []
    hist = rows[0].get("history", []) if isinstance(rows, list) else []
    return [{"t": int(h.get("t", 0)), "c": float(h.get("c") or 0)} for h in hist]


# --------------------------------------------------------------------------
# 聚合：把某币种多个合约的历史按时间对齐相加
# --------------------------------------------------------------------------
def aggregate_oi_history_by_symbols(symbols: list[str], start_ts: int, end_ts: int,
                                    interval: Optional[str] = None):
    """按 symbol 字符串列表聚合（供上层缓存调用，避免传对象）。"""
    if interval is None:
        interval = pick_interval(end_ts - start_ts)
    bucket_sum: dict[int, float] = {}
    per_contract: dict[str, dict] = {}
    for sym in symbols:
        hist = open_interest_history(sym, start_ts, end_ts, interval, convert_to_usd=True)
        series = {}
        for point in hist:
            t, v = point["t"], point["c"]
            series[t] = v
            bucket_sum[t] = bucket_sum.get(t, 0.0) + v
        per_contract[sym] = series
    timestamps = sorted(bucket_sum.keys())
    totals = [bucket_sum[t] for t in timestamps]
    return timestamps, totals, per_contract


def aggregate_oi_history(contracts: list[Contract], start_ts: int, end_ts: int,
                         interval: Optional[str] = None):
    """
    把多个合约的 OI 历史聚合成一条总曲线（按时间戳对齐求和）。
    返回 (timestamps[], total_values[], per_contract{symbol: {t: c}})。
    注意：合约越多，API 调用越多（每个合约 1 次），注意限频 40/min。
    """
    if interval is None:
        interval = pick_interval(end_ts - start_ts)

    per_contract: dict[str, dict] = {}
    bucket_sum: dict[int, float] = {}

    for c in contracts:
        hist = open_interest_history(c.symbol, start_ts, end_ts, interval, convert_to_usd=True)
        series = {}
        for point in hist:
            t, v = point["t"], point["c"]
            series[t] = v
            bucket_sum[t] = bucket_sum.get(t, 0.0) + v
        per_contract[c.symbol] = series

    timestamps = sorted(bucket_sum.keys())
    totals = [bucket_sum[t] for t in timestamps]
    return timestamps, totals, per_contract
