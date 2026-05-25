"""
Crypto OI Aggregator — 全交易所永续合约持仓量聚合查询
========================================================
数据源：Coinalyze API（聚合 28 个交易所，含 Hyperliquid / Aster 等 DEX）

功能：
- 选择币种 + 时间范围 → 全交易所聚合 OI 历史曲线
- 各交易所/各合约明细（BTC / USDT Perp - Binance 格式），可勾选计入聚合
- OI 曲线叠加价格，识别量价背离
- OI 数值显示精度 0.1M
"""

from __future__ import annotations

import time
from datetime import date, datetime, time as dtime, timedelta, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import coinalyze as cz

st.set_page_config(
    page_title="OI Aggregator",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
      html, body, [class*="css"] {
        font-family: -apple-system, "SF Pro Display", "Segoe UI", "PingFang SC",
                     "Microsoft YaHei", sans-serif;
      }
      .main-header { font-size: 1.85rem; font-weight: 600; letter-spacing: -0.02em; margin-bottom: 0.2rem; }
      .subtitle { color: #6b7280; font-size: 0.9rem; margin-bottom: 1.6rem; }
      .metric-card { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 1rem 1.2rem; }
      .metric-label { color: #64748b; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.06em; }
      .metric-value { font-size: 1.4rem; font-weight: 600; color: #0f172a; margin-top: 0.2rem; }
      .sec-header { font-size: 1.12rem; font-weight: 600; color: #0f172a; margin: 1.4rem 0 0.2rem; }
      .sec-sub { color: #6b7280; font-size: 0.82rem; margin-bottom: 0.6rem; }
      .footer-note { color: #94a3b8; font-size: 0.8rem; margin-top: 2.5rem; padding-top: 1.2rem; border-top: 1px solid #e2e8f0; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="main-header">OI Aggregator</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="subtitle">全交易所永续合约持仓量聚合 · 历史曲线 · 各所明细 · 价格叠加</div>',
    unsafe_allow_html=True,
)


def fmt_usd_m(v: float) -> str:
    """统一显示到 0.1M。十亿级用 B。"""
    if v is None:
        return "—"
    if abs(v) >= 1e9:
        return f"${v/1e9:,.2f}B"
    return f"${v/1e6:,.1f}M"


@st.cache_data(ttl=3600, show_spinner=False)
def get_base_assets():
    return cz.list_base_assets()


@st.cache_data(ttl=3600, show_spinner=False)
def get_contracts(base):
    cs = cz.contracts_for_base(base, perpetual_only=True)
    # 转成可缓存的纯数据（dataclass 可缓存，但转 dict 更稳）
    return [
        {
            "symbol": c.symbol, "exchange_name": c.exchange_name,
            "base_asset": c.base_asset, "quote_asset": c.quote_asset,
            "is_dex": c.is_dex, "display": c.display,
        }
        for c in cs
    ]


@st.cache_data(ttl=300, show_spinner=False)
def get_current_oi(symbols):
    return cz.current_open_interest(symbols, convert_to_usd=True)


@st.cache_data(ttl=300, show_spinner=False)
def get_agg_history(symbols, start_ts, end_ts):
    return cz.aggregate_oi_history_by_symbols(list(symbols), start_ts, end_ts)


@st.cache_data(ttl=300, show_spinner=False)
def get_price_history(symbol, start_ts, end_ts):
    return cz.price_history(symbol, start_ts, end_ts)


# ---------------------------- 侧边栏 ----------------------------
with st.sidebar:
    st.markdown("### 查询条件")
    try:
        bases = get_base_assets()
    except Exception as e:
        st.error(f"加载币种清单失败：{e}")
        st.stop()

    default_idx = bases.index("BTC") if "BTC" in bases else 0
    base = st.selectbox("币种", bases, index=default_idx)

    st.markdown("##### 时间范围（UTC）")
    today = datetime.now(timezone.utc).date()
    default_start = today - timedelta(days=30)
    c1, c2 = st.columns(2)
    with c1:
        sd = st.date_input("起始日期", value=default_start, max_value=today)
    with c2:
        ed = st.date_input("结束日期", value=today, max_value=today)
    start_dt = datetime.combine(sd, dtime(0, 0), tzinfo=timezone.utc)
    end_dt = datetime.combine(ed, dtime(23, 59), tzinfo=timezone.utc)
    if end_dt <= start_dt:
        st.warning("结束时间必须晚于起始时间")
        st.stop()

    span_days = (end_dt - start_dt).days
    st.caption(f"查询跨度：{span_days} 天")
    show_price = st.checkbox("叠加价格", value=True)
    run = st.button("查询", type="primary", use_container_width=True)


if not run:
    st.info(
        "选择币种与时间范围后点击「查询」。\n\n"
        "- **聚合 OI**：该币种在所有交易所永续合约的未平仓合约名义价值（美元）之和\n"
        "- 数据由 Coinalyze 聚合，覆盖 Binance / Bybit / OKX / Gate / Hyperliquid / Aster 等 28 个交易所\n"
        "- 一个月以上区间采用 1 小时粒度（日内分钟级数据交易所仅短期保留）\n"
        "- OI 数值精度 0.1M"
    )
    st.stop()


# ---------------------------- 数据加载 ----------------------------
contracts = get_contracts(base)
if not contracts:
    st.warning(f"未找到 {base} 的永续合约。")
    st.stop()

start_ts = int(start_dt.timestamp())
end_ts = int(end_dt.timestamp())
all_symbols = [c["symbol"] for c in contracts]

with st.spinner(f"正在聚合 {base} 在 {len(contracts)} 个合约上的持仓量历史..."):
    try:
        cur_oi = get_current_oi(all_symbols)
    except Exception as e:
        st.error(f"当前 OI 加载失败：{e}")
        st.stop()

# 按当前 OI 排序合约（大的在前），默认全选计入聚合
for c in contracts:
    c["cur"] = cur_oi.get(c["symbol"], 0.0) or 0.0
contracts.sort(key=lambda x: x["cur"], reverse=True)

total_now = sum(c["cur"] for c in contracts)

# ---------------------------- 顶部汇总卡片（全部标注：聚合/全所）----------------------------
top = contracts[0] if contracts else None
c1, c2, c3, c4 = st.columns(4)
with c1:
    st.markdown(
        f'<div class="metric-card"><div class="metric-label">聚合 OI（全所合计·当前）</div>'
        f'<div class="metric-value">{fmt_usd_m(total_now)}</div></div>', unsafe_allow_html=True)
with c2:
    st.markdown(
        f'<div class="metric-card"><div class="metric-label">合约数（全所）</div>'
        f'<div class="metric-value">{len([c for c in contracts if c["cur"]>0])}</div></div>',
        unsafe_allow_html=True)
with c3:
    if top and total_now:
        share = top["cur"] / total_now * 100
        st.markdown(
            f'<div class="metric-card"><div class="metric-label">OI 最大交易所</div>'
            f'<div class="metric-value" style="font-size:1.05rem">{top["exchange_name"]} · {share:.0f}%</div></div>',
            unsafe_allow_html=True)
with c4:
    # 当前价格（取 OI 最大那个合约的价格）
    cur_price = None
    if top:
        try:
            ph = get_price_history(top["symbol"], end_ts - 7200, end_ts)
            if ph:
                cur_price = ph[-1]["c"]
        except Exception:
            cur_price = None
    ptxt = f"${cur_price:,.4g}" if cur_price else "—"
    st.markdown(
        f'<div class="metric-card"><div class="metric-label">当前价格</div>'
        f'<div class="metric-value">{ptxt}</div></div>', unsafe_allow_html=True)


# ---------------------------- 各所明细表 + 勾选 ----------------------------
st.markdown('<div class="sec-header">各交易所合约明细</div>', unsafe_allow_html=True)
st.markdown('<div class="sec-sub">勾选要计入聚合曲线的合约；按当前 OI 降序</div>', unsafe_allow_html=True)

# 默认勾选 OI>0 的合约
selected_symbols = []
detail_rows = []
for c in contracts:
    share = c["cur"] / total_now * 100 if total_now else 0
    detail_rows.append({
        "合约": c["display"],
        "类型": "DEX" if c["is_dex"] else "CEX",
        "当前 OI": fmt_usd_m(c["cur"]) if c["cur"] else "—",
        "占比": f"{share:.1f}%" if c["cur"] else "—",
    })
    if c["cur"] > 0:
        selected_symbols.append(c["symbol"])

st.dataframe(pd.DataFrame(detail_rows), use_container_width=True, hide_index=True, height=min(400, 60 + 35*len(detail_rows)))


# ---------------------------- 聚合历史曲线（主图）----------------------------
st.markdown('<div class="sec-header">聚合 OI 历史曲线</div>', unsafe_allow_html=True)
st.markdown(
    f'<div class="sec-sub">{base} 全交易所永续合约 OI 之和（美元名义价值）· '
    f'{start_dt:%Y-%m-%d} ~ {end_dt:%Y-%m-%d} UTC</div>', unsafe_allow_html=True)

with st.spinner("正在拉取各合约历史并聚合（合约较多时需要一点时间）..."):
    try:
        ts, totals, per = get_agg_history(tuple(selected_symbols), start_ts, end_ts)
    except Exception as e:
        st.error(f"聚合历史加载失败：{e}")
        st.stop()

if not ts:
    st.warning("该时段无聚合 OI 数据。")
    st.stop()

agg_df = pd.DataFrame({
    "time": pd.to_datetime(ts, unit="s", utc=True),
    "oi": totals,
})

# 价格（取 OI 最大合约）
price_df = pd.DataFrame(columns=["time", "close"])
if show_price and top:
    try:
        ph = get_price_history(top["symbol"], start_ts, end_ts)
        if ph:
            price_df = pd.DataFrame({
                "time": pd.to_datetime([p["t"] for p in ph], unit="s", utc=True),
                "close": [p["c"] for p in ph],
            })
    except Exception:
        price_df = pd.DataFrame(columns=["time", "close"])

# 区间变化卡片
first_oi, last_oi = agg_df["oi"].iloc[0], agg_df["oi"].iloc[-1]
oi_chg = (last_oi - first_oi) / first_oi * 100 if first_oi else 0
m1, m2 = st.columns(2)
with m1:
    st.markdown(
        f'<div class="metric-card"><div class="metric-label">区间末聚合 OI</div>'
        f'<div class="metric-value">{fmt_usd_m(last_oi)}</div></div>', unsafe_allow_html=True)
with m2:
    col = "#16a34a" if oi_chg >= 0 else "#dc2626"
    st.markdown(
        f'<div class="metric-card"><div class="metric-label">区间聚合 OI 变化</div>'
        f'<div class="metric-value" style="color:{col}">{oi_chg:+.1f}%</div></div>', unsafe_allow_html=True)

fig = go.Figure()
fig.add_trace(go.Scatter(
    x=agg_df["time"], y=agg_df["oi"] / 1e6, yaxis="y1",
    mode="lines", name="聚合 OI", line=dict(color="#2563eb", width=2),
    hovertemplate="<b>%{x|%Y-%m-%d %H:%M} UTC</b><br>聚合 OI: $%{y:,.1f}M<extra></extra>",
))
layout = dict(
    height=520, hovermode="x unified", plot_bgcolor="white", paper_bgcolor="white",
    xaxis=dict(title="时间 (UTC)", gridcolor="#f1f5f9",
               rangeslider=dict(visible=True, thickness=0.06)),
    yaxis=dict(title=dict(text="聚合 OI (Million USD)", font=dict(color="#2563eb")),
               gridcolor="#f1f5f9", tickfont=dict(color="#2563eb")),
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
)
if show_price and not price_df.empty:
    fig.add_trace(go.Scatter(
        x=price_df["time"], y=price_df["close"], yaxis="y2",
        mode="lines", name=f"价格 ({top['exchange_name']})",
        line=dict(color="#d97706", width=1.6, dash="dot"),
        hovertemplate="<b>%{x|%Y-%m-%d %H:%M} UTC</b><br>价格: $%{y:,.4g}<extra></extra>",
    ))
    layout["yaxis2"] = dict(title=dict(text="价格 (USD)", font=dict(color="#d97706")),
                            overlaying="y", side="right", showgrid=False,
                            tickfont=dict(color="#d97706"))
fig.update_layout(**layout)
st.plotly_chart(fig, use_container_width=True)

if show_price and not price_df.empty:
    st.caption("蓝线＝聚合 OI（左轴）/ 橙色虚线＝价格（右轴）。价格涨而 OI 跌通常意味上涨乏力；价格涨且 OI 涨意味新资金进场。")

st.markdown(
    '<div class="footer-note">'
    '数据源：Coinalyze API（聚合 28 个交易所衍生品数据，含 Hyperliquid / Aster 等 DEX）。'
    '聚合 OI 为所选合约的美元名义价值之和。一个月以上区间采用 1 小时粒度。所有时间为 UTC。'
    '</div>', unsafe_allow_html=True)
