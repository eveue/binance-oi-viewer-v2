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

# 内部限速：Coinalyze 限 40 次/分钟，我们留余量按 35 算
_RATE_LIMIT_PER_MIN = 35
_call_timestamps: list[float] = []   # 最近的请求时间戳


def _throttle():
    """阻塞直到能发起下一次请求（滑动窗口）。"""
    now = _time.time()
    # 清理 60 秒之前的
    cutoff = now - 60
    while _call_timestamps and _call_timestamps[0] < cutoff:
        _call_timestamps.pop(0)
    if len(_call_timestamps) >= _RATE_LIMIT_PER_MIN:
        # 等到最早那次调用满 60 秒
        wait = 60 - (now - _call_timestamps[0]) + 0.2
        if wait > 0:
            _time.sleep(wait)
        # 再清一次
        now = _time.time()
        cutoff = now - 60
        while _call_timestamps and _call_timestamps[0] < cutoff:
            _call_timestamps.pop(0)
    _call_timestamps.append(_time.time())


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
    """统一 GET：主动限速 + 429 重试 + 干净错误（不含 Key）。"""
    params = dict(params or {})
    params["api_key"] = _api_key()
    url = f"{API_BASE}{path}"
    last_exc = None
    for attempt in range(6):
        _throttle()                                 # 主动控速，避免触发 429
        try:
            resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            last_exc = e
            _time.sleep(2)
            continue
        if resp.status_code == 429:                 # 真触发了，强等
            ra = _safe_float(resp.headers.get("Retry-After")) or (5.0 * (attempt + 1))
            _time.sleep(min(max(ra, 8), 30))
            continue
        if resp.status_code >= 500:
            _time.sleep(2 + attempt * 2)
            continue
        if not resp.ok:
            # 抛干净错误，不暴露 URL 和 Key
            raise RuntimeError(f"API 错误 {resp.status_code}：{path}")
        return resp.json()
    if last_exc:
        raise RuntimeError(f"网络异常：{type(last_exc).__name__}")
    raise RuntimeError("请求频率受限，请稍后再查（建议等 1 分钟）")


def _safe_float(v):
    """任何值安全转 float，失败返回 None。"""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v):
    """任何值安全转 int（先经 float，容忍 '43.294' / 1.7e9 等），失败返回 None。"""
    f = _safe_float(v)
    if f is None:
        return None
    return int(f)


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
    for i in range(0, len(symbols), 8):
        batch = symbols[i:i + 8]
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
        t = _safe_int(h.get("t"))
        c = _safe_float(h.get("c"))
        if t is None or c is None:
            continue
        out.append({"t": t, "c": c})
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
    out = []
    for h in hist:
        t = _safe_int(h.get("t"))
        c = _safe_float(h.get("c"))
        if t is None or c is None:
            continue
        out.append({"t": t, "c": c})
    return out


# --------------------------------------------------------------------------
# 聚合：把某币种多个合约的历史按时间对齐相加
# --------------------------------------------------------------------------
def aggregate_oi_history_by_symbols(symbols: list[str], start_ts: int, end_ts: int,
                                    interval: Optional[str] = None):
    """
    按 symbol 列表聚合 OI 历史（按时间戳对齐求和）。

    Coinalyze 的限频按请求中 symbol 数量计费，且历史接口较严，
    因此采用小批次（每批 4 个）+ 批次间主动间隔，避免触发 429。
    """
    if interval is None:
        interval = pick_interval(end_ts - start_ts)

    bucket_sum: dict[int, float] = {}
    per_contract: dict[str, dict] = {}

    BATCH = 4                     # 每批合约数（保守，避开按-symbol计费的限频）
    GAP = 1.6                     # 批次间隔秒，把节奏控制在限频内

    batches = [symbols[i:i + BATCH] for i in range(0, len(symbols), BATCH)]
    for idx, batch in enumerate(batches):
        rows = _get("/open-interest-history", {
            "symbols": ",".join(batch),
            "interval": interval,
            "from": int(start_ts),
            "to": int(end_ts),
            "convert_to_usd": "true",
        })
        if isinstance(rows, list):
            for item in rows:
                sym = item.get("symbol", "")
                series = {}
                for h in item.get("history", []):
                    t = _safe_int(h.get("t"))
                    c = _safe_float(h.get("c"))
                    if t is None or c is None:
                        continue
                    series[t] = c
                    bucket_sum[t] = bucket_sum.get(t, 0.0) + c
                per_contract[sym] = series
        # 除最后一批外，批次间主动等待，平滑请求节奏
        if idx < len(batches) - 1:
            _time.sleep(GAP)

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
