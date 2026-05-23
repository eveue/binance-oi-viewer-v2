"""
多交易所当前 OI 采集模块
==========================

为「丢一个币 → 看它在各 CEX/DEX 的当前 OI 分布」提供数据。

设计原则：
- 每个交易所一个独立函数，返回 (oi_value_usd, oi_base_qty) 或 None。
- 每家都包了异常处理：任意一家网络失败 / 字段变动 / 该所未上线此币，
  都只让这一家返回 None，绝不影响其他所和整个页面。
- 币安的当前 OI 不在这里（沿用 binance_oi.py），这里是「币安之外」的所。

注意：除币安外，其他所的公开 API 基本只提供「当前 OI」，
不提供一个月历史归档，所以本模块只取当前快照。
历史曲线仍以币安为主（见 binance_oi.py）。

各接口来源（公开文档）：
- Bybit  : GET /v5/market/tickers          (category=linear)
- OKX    : GET /api/v5/public/open-interest (instType=SWAP)
- Gate   : GET /api/v4/futures/usdt/contracts/{contract}
- Hyperliquid: POST /info  (type=metaAndAssetCtxs)
- Aster  : GET /fapi/v1/openInterest + /fapi/v1/ticker/price
           (Aster 的合约 API 风格与币安高度相似)
"""

from __future__ import annotations

import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional

import requests

HTTP_TIMEOUT = 12

# 每个所一个友好的展示名 + 是否 DEX，用于前端分类显示
EXCHANGE_META = {
    "Binance": {"type": "CEX"},
    "Bybit": {"type": "CEX"},
    "OKX": {"type": "CEX"},
    "Gate": {"type": "CEX"},
    "Hyperliquid": {"type": "DEX"},
    "Aster": {"type": "DEX"},
}


@dataclass
class OIResult:
    """单个交易所的 OI 查询结果。"""
    exchange: str
    type: str            # CEX / DEX
    oi_value_usd: Optional[float]   # 名义价值（美元）
    oi_base_qty: Optional[float]    # 持仓张数（base asset 数量），可能为 None
    ok: bool
    note: str = ""       # 失败原因或备注


def _norm_base(symbol: str) -> str:
    """从 BTCUSDT 提取 base：BTC。"""
    s = symbol.upper()
    for quote in ("USDT", "USDC", "USD"):
        if s.endswith(quote):
            return s[: -len(quote)]
    return s


# --------------------------------------------------------------------------
# Bybit
# --------------------------------------------------------------------------
def fetch_bybit_oi(symbol: str) -> OIResult:
    base = _norm_base(symbol)
    sym = f"{base}USDT"
    try:
        resp = requests.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "linear", "symbol": sym},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        lst = data.get("result", {}).get("list", [])
        if not lst:
            return OIResult("Bybit", "CEX", None, None, False, "未上线该交易对")
        row = lst[0]
        # openInterest = 张数(base)，openInterestValue = 名义价值(USD)
        qty = float(row.get("openInterest") or 0) or None
        val = row.get("openInterestValue")
        if val is not None:
            val = float(val)
        else:
            # 退路：用 last price * 张数 估算
            price = float(row.get("lastPrice") or 0)
            val = (qty or 0) * price if price else None
        return OIResult("Bybit", "CEX", val, qty, True)
    except Exception as e:  # noqa: BLE001
        return OIResult("Bybit", "CEX", None, None, False, f"请求失败: {e}")


# --------------------------------------------------------------------------
# OKX
# --------------------------------------------------------------------------
def fetch_okx_oi(symbol: str) -> OIResult:
    base = _norm_base(symbol)
    inst = f"{base}-USDT-SWAP"
    try:
        resp = requests.get(
            "https://www.okx.com/api/v5/public/open-interest",
            params={"instType": "SWAP", "instId": inst},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("data", [])
        if not rows:
            return OIResult("OKX", "CEX", None, None, False, "未上线该交易对")
        row = rows[0]
        # oiCcy = 以 base 计的张数；oiUsd = 名义价值(USD)（部分版本提供）
        qty = float(row.get("oiCcy") or 0) or None
        val = row.get("oiUsd")
        if val is not None and val != "":
            val = float(val)
        else:
            val = None
        return OIResult("OKX", "CEX", val, qty, True)
    except Exception as e:  # noqa: BLE001
        return OIResult("OKX", "CEX", None, None, False, f"请求失败: {e}")


# --------------------------------------------------------------------------
# Gate
# --------------------------------------------------------------------------
def fetch_gate_oi(symbol: str) -> OIResult:
    base = _norm_base(symbol)
    contract = f"{base}_USDT"
    try:
        resp = requests.get(
            f"https://api.gateio.ws/api/v4/futures/usdt/contracts/{contract}",
            timeout=HTTP_TIMEOUT,
            headers={"Accept": "application/json"},
        )
        if resp.status_code == 404:
            return OIResult("Gate", "CEX", None, None, False, "未上线该交易对")
        resp.raise_for_status()
        row = resp.json()
        # position_size = 总持仓张数；quanto_multiplier = 每张合约对应 base 数量
        # mark_price = 标记价；名义价值 = 张数 * 乘数 * 价格
        qty_contracts = float(row.get("position_size") or 0)
        mult = float(row.get("quanto_multiplier") or 0) or 1.0
        mark = float(row.get("mark_price") or 0)
        base_qty = qty_contracts * mult if mult else qty_contracts
        val = base_qty * mark if mark else None
        return OIResult("Gate", "CEX", val, base_qty or None, True)
    except Exception as e:  # noqa: BLE001
        return OIResult("Gate", "CEX", None, None, False, f"请求失败: {e}")


# --------------------------------------------------------------------------
# Hyperliquid (DEX)
# --------------------------------------------------------------------------
def fetch_hyperliquid_oi(symbol: str) -> OIResult:
    base = _norm_base(symbol)
    try:
        resp = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "metaAndAssetCtxs"},
            timeout=HTTP_TIMEOUT,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        # 返回 [meta, assetCtxs]；meta.universe 是币种顺序，assetCtxs 一一对应
        meta, ctxs = data[0], data[1]
        universe = meta.get("universe", [])
        idx = None
        for i, u in enumerate(universe):
            if u.get("name", "").upper() == base:
                idx = i
                break
        if idx is None or idx >= len(ctxs):
            return OIResult("Hyperliquid", "DEX", None, None, False, "未上线该币")
        ctx = ctxs[idx]
        # openInterest 以 base 计；markPx = 标记价
        qty = float(ctx.get("openInterest") or 0) or None
        mark = float(ctx.get("markPx") or 0)
        val = (qty or 0) * mark if (qty and mark) else None
        return OIResult("Hyperliquid", "DEX", val, qty, True)
    except Exception as e:  # noqa: BLE001
        return OIResult("Hyperliquid", "DEX", None, None, False, f"请求失败: {e}")


# --------------------------------------------------------------------------
# Aster (DEX, 合约 API 风格类似币安)
# --------------------------------------------------------------------------
def fetch_aster_oi(symbol: str) -> OIResult:
    base = _norm_base(symbol)
    sym = f"{base}USDT"
    try:
        oi_resp = requests.get(
            "https://fapi.asterdex.com/fapi/v1/openInterest",
            params={"symbol": sym},
            timeout=HTTP_TIMEOUT,
        )
        if oi_resp.status_code in (400, 404):
            return OIResult("Aster", "DEX", None, None, False, "未上线该交易对")
        oi_resp.raise_for_status()
        oi_json = oi_resp.json()
        qty = float(oi_json.get("openInterest") or 0) or None

        # 取价格换算名义价值
        val = None
        try:
            px_resp = requests.get(
                "https://fapi.asterdex.com/fapi/v1/ticker/price",
                params={"symbol": sym},
                timeout=HTTP_TIMEOUT,
            )
            px_resp.raise_for_status()
            price = float(px_resp.json().get("price") or 0)
            val = (qty or 0) * price if (qty and price) else None
        except Exception:  # noqa: BLE001
            val = None
        return OIResult("Aster", "DEX", val, qty, True)
    except Exception as e:  # noqa: BLE001
        return OIResult("Aster", "DEX", None, None, False, f"请求失败: {e}")


# --------------------------------------------------------------------------
# 聚合：并发拉取「币安之外」的所
# --------------------------------------------------------------------------
_FETCHERS = {
    "Bybit": fetch_bybit_oi,
    "OKX": fetch_okx_oi,
    "Gate": fetch_gate_oi,
    "Hyperliquid": fetch_hyperliquid_oi,
    "Aster": fetch_aster_oi,
}


def fetch_other_exchanges_oi(symbol: str) -> list[OIResult]:
    """
    并发拉取币安之外 5 个所的当前 OI。
    返回 OIResult 列表（含失败项，失败项 ok=False）。
    """
    results: list[OIResult] = []
    with ThreadPoolExecutor(max_workers=len(_FETCHERS)) as pool:
        futures = {pool.submit(fn, symbol): name for name, fn in _FETCHERS.items()}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:  # noqa: BLE001
                name = futures[fut]
                meta = EXCHANGE_META.get(name, {"type": "CEX"})
                results.append(OIResult(name, meta["type"], None, None, False, f"异常: {e}"))
    # 固定顺序：CEX 在前，DEX 在后，组内按名字
    order = ["Bybit", "OKX", "Gate", "Hyperliquid", "Aster"]
    results.sort(key=lambda r: order.index(r.exchange) if r.exchange in order else 99)
    return results
