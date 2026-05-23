"""
Binance OI Viewer — 币安 USDT 永续合约持仓量历史查询工具
含：币安完整历史 + 价格叠加视图 + 全所(CEX/DEX)当前 OI 对比
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from binance_oi import (
    fetch_full_range,
    fetch_usdt_perpetual_symbols,
    fetch_price_klines,
)
from exchanges import fetch_other_exchanges_oi, OIResult

st.set_page_config(
    page_title="Binance OI Viewer",
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
      .subtitle { color: #6b7280; font-size: 0.9rem; margin-bottom: 2rem; }
      .metric-card { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 1rem 1.2rem; }
      .metric-label { color: #64748b; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.06em; }
      .metric-value { font-size: 1.4rem; font-weight: 600; color: #0f172a; margin-top: 0.2rem; }
      .footer-note { color: #94a3b8; font-size: 0.8rem; margin-top: 3rem; padding-top: 1.5rem; border-top: 1px solid #e2e8f0; }
      .sec-header { font-size: 1.15rem; font-weight: 600; color: #0f172a; margin: 0.5rem 0 0.2rem; }
      .sec-sub { color: #6b7280; font-size: 0.82rem; margin-bottom: 0.8rem; }
      .tag-cex { background:#eff6ff; color:#1d4ed8; border-radius:4px; padding:1px 7px; font-size:0.72rem; font-weight:600;}
      .tag-dex { background:#f0fdf4; color:#15803d; border-radius:4px; padding:1px 7px; font-size:0.72rem; font-weight:600;}
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="main-header">Binance OI Viewer</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="subtitle">币安 USDT 永续合约 · 任意历史时段持仓量精确查询 · 多所当前 OI 对比</div>',
    unsafe_allow_html=True,
)


@st.cache_data(ttl=3600)
def get_symbol_list():
    return fetch_usdt_perpetual_symbols()


@st.cache_data(ttl=600, show_spinner=False)
def get_oi_data(symbol, start_iso, end_iso):
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    return fetch_full_range(symbol, start, end)


@st.cache_data(ttl=600, show_spinner=False)
def get_price_data(symbol, start_iso, end_iso):
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    return fetch_price_klines(symbol, start, end)


@st.cache_data(ttl=60, show_spinner=False)
def get_other_oi(symbol):
    # 当前 OI 变化快，缓存 60 秒；所有用户共用，避免重复轰炸各所 API
    return fetch_other_exchanges_oi(symbol)


with st.sidebar:
    st.markdown("### 查询条件")
    try:
        symbols = get_symbol_list()
    except Exception as e:
        st.error(f"加载交易对清单失败：{e}")
        st.stop()

    default_index = symbols.index("BTCUSDT") if "BTCUSDT" in symbols else 0
    symbol = st.selectbox("交易对", symbols, index=default_index)

    st.markdown("##### 时间范围（UTC）")
    today = datetime.now(timezone.utc).date()
    default_start = today - timedelta(days=30)

    col_start, col_end = st.columns(2)
    with col_start:
        start_date = st.date_input("起始日期", value=default_start,
                                    min_value=date(2019, 9, 8), max_value=today)
        start_time = st.time_input("起始时间", value=time(0, 0))
    with col_end:
        end_date = st.date_input("结束日期", value=today,
                                  min_value=date(2019, 9, 8), max_value=today)
        end_time = st.time_input("结束时间", value=time(23, 59))

    start_dt = datetime.combine(start_date, start_time, tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, end_time, tzinfo=timezone.utc)

    if end_dt <= start_dt:
        st.warning("结束时间必须晚于起始时间")
        st.stop()

    span_days = (end_dt - start_dt).days
    st.caption(f"查询跨度：{span_days} 天")

    show_price = st.checkbox("在曲线图叠加价格", value=True)

    run = st.button("查询", type="primary", use_container_width=True)


if not run:
    st.info(
        "在左侧选择交易对和时间范围，然后点击「查询」。\n\n"
        "**本工具能做什么**：\n"
        "- 币安永续：5 分钟粒度、任意历史时段的 OI 曲线（突破网页 1 万条限制）\n"
        "- OI 曲线可叠加价格，一眼看出量价背离\n"
        "- 全所对比：丢一个币，看它在 Binance / Bybit / OKX / Gate / Hyperliquid / Aster 的当前 OI 分布\n\n"
        "**数据说明**：\n"
        "- 币安历史来自官方公开归档（data.binance.vision），当天数据来自实时 API\n"
        "- 其他所提供「当前 OI」快照（公开 API 不含一个月历史归档）\n"
        "- OI = Open Interest 未平仓合约总量；金额为美元名义价值"
    )
    st.stop()


# ==========================================================================
# 1) 币安历史 OI（核心）
# ==========================================================================
with st.spinner(f"正在加载 {symbol} 在 {start_dt:%Y-%m-%d %H:%M} 至 {end_dt:%Y-%m-%d %H:%M} UTC 期间的数据..."):
    try:
        df = get_oi_data(symbol, start_dt.isoformat(), end_dt.isoformat())
    except Exception as e:
        st.error(f"数据加载失败：{e}")
        st.stop()

if df.empty:
    st.warning(f"该时段无 {symbol} 的 OI 数据。可能该交易对尚未上线，或币安归档暂无该日数据。")
    st.stop()

# 价格数据（叠加用），失败不阻断
price_df = pd.DataFrame(columns=["create_time", "close"])
try:
    price_df = get_price_data(symbol, start_dt.isoformat(), end_dt.isoformat())
except Exception:
    price_df = pd.DataFrame(columns=["create_time", "close"])

latest = df.iloc[-1]
peak_idx = df["sum_open_interest_value"].idxmax()
trough_idx = df["sum_open_interest_value"].idxmin()

# 顶部卡片：OI 期末 / 当前价格 / OI 区间变化 / 价格区间变化
first_val = df.iloc[0]["sum_open_interest_value"]
last_val = df.iloc[-1]["sum_open_interest_value"]
oi_pct = (last_val - first_val) / first_val * 100 if first_val else 0

cur_price = None
price_pct = None
if not price_df.empty:
    cur_price = price_df.iloc[-1]["close"]
    p0 = price_df.iloc[0]["close"]
    price_pct = (cur_price - p0) / p0 * 100 if p0 else None


def color_for(pct):
    if pct is None:
        return "#0f172a"
    return "#16a34a" if pct >= 0 else "#dc2626"


c1, c2, c3, c4 = st.columns(4)
with c1:
    st.markdown(
        f'<div class="metric-card"><div class="metric-label">期末 OI</div>'
        f'<div class="metric-value">${latest["sum_open_interest_value"]/1e6:,.1f}M</div></div>',
        unsafe_allow_html=True,
    )
with c2:
    price_txt = f"${cur_price:,.4g}" if cur_price is not None else "—"
    st.markdown(
        f'<div class="metric-card"><div class="metric-label">当前价格</div>'
        f'<div class="metric-value">{price_txt}</div></div>',
        unsafe_allow_html=True,
    )
with c3:
    st.markdown(
        f'<div class="metric-card"><div class="metric-label">区间 OI 变化</div>'
        f'<div class="metric-value" style="color:{color_for(oi_pct)}">{oi_pct:+.1f}%</div></div>',
        unsafe_allow_html=True,
    )
with c4:
    ptxt = f"{price_pct:+.1f}%" if price_pct is not None else "—"
    st.markdown(
        f'<div class="metric-card"><div class="metric-label">区间价格变化</div>'
        f'<div class="metric-value" style="color:{color_for(price_pct)}">{ptxt}</div></div>',
        unsafe_allow_html=True,
    )

st.markdown("")

tab_chart, tab_table, tab_lookup = st.tabs(["📈 曲线图", "📋 数据表", "🔍 精确时刻查询"])

with tab_chart:
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["create_time"], y=df["sum_open_interest_value"] / 1e6,
        mode="lines", name="OI (USD, M)", line=dict(color="#2563eb", width=2),
        yaxis="y1",
        hovertemplate="<b>%{x|%Y-%m-%d %H:%M} UTC</b><br>OI: $%{y:,.1f}M<extra></extra>",
    ))

    layout_kwargs = dict(
        title=f"{symbol} 持仓量历史 ({start_dt:%Y-%m-%d} ~ {end_dt:%Y-%m-%d} UTC)",
        xaxis_title="时间 (UTC)",
        height=520, hovermode="x unified",
        plot_bgcolor="white", paper_bgcolor="white",
        xaxis=dict(gridcolor="#f1f5f9", rangeslider=dict(visible=True, thickness=0.06),
                   rangeselector=dict(buttons=[
                       dict(count=1, label="1d", step="day", stepmode="backward"),
                       dict(count=7, label="1w", step="day", stepmode="backward"),
                       dict(count=1, label="1m", step="month", stepmode="backward"),
                       dict(count=3, label="3m", step="month", stepmode="backward"),
                       dict(step="all", label="全部"),
                   ])),
        yaxis=dict(title="未平仓合约名义价值 (Million USD)", gridcolor="#f1f5f9",
                   titlefont=dict(color="#2563eb"), tickfont=dict(color="#2563eb")),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    )

    if show_price and not price_df.empty:
        fig.add_trace(go.Scatter(
            x=price_df["create_time"], y=price_df["close"],
            mode="lines", name="价格 (USD)", line=dict(color="#d97706", width=1.6, dash="dot"),
            yaxis="y2",
            hovertemplate="<b>%{x|%Y-%m-%d %H:%M} UTC</b><br>价格: $%{y:,.4g}<extra></extra>",
        ))
        layout_kwargs["yaxis2"] = dict(
            title="价格 (USD)", overlaying="y", side="right",
            showgrid=False, titlefont=dict(color="#d97706"), tickfont=dict(color="#d97706"),
        )

    fig.update_layout(**layout_kwargs)
    st.plotly_chart(fig, use_container_width=True)

    if show_price and not price_df.empty:
        st.caption(
            "蓝线 OI（左轴）/ 橙色虚线 价格（右轴）。"
            "价格涨而 OI 跌 = 多头获利了结推动、趋势偏弱；价格涨且 OI 涨 = 新资金进场、趋势更实。"
        )

    with st.expander("查看持仓张数（base asset）"):
        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(
            x=df["create_time"], y=df["sum_open_interest"],
            mode="lines", name="OI (张数)", line=dict(color="#10b981", width=2),
            hovertemplate="<b>%{x|%Y-%m-%d %H:%M} UTC</b><br>OI 张数: %{y:,.2f}<extra></extra>",
        ))
        base_asset = symbol.replace("USDT", "")
        fig2.update_layout(
            xaxis_title="时间 (UTC)", yaxis_title=f"未平仓张数 ({base_asset})",
            height=400, plot_bgcolor="white", paper_bgcolor="white",
            xaxis=dict(gridcolor="#f1f5f9"), yaxis=dict(gridcolor="#f1f5f9"),
        )
        st.plotly_chart(fig2, use_container_width=True)


with tab_table:
    st.markdown(f"**共 {len(df):,} 条记录**（5 分钟粒度）")
    display_df = df.copy()
    display_df["create_time"] = display_df["create_time"].dt.strftime("%Y-%m-%d %H:%M:%S")
    display_df["sum_open_interest_value"] = display_df["sum_open_interest_value"].round(2)
    display_df["sum_open_interest"] = display_df["sum_open_interest"].round(4)
    display_df.columns = ["时间 (UTC)", "交易对", "OI 张数", "OI (USD)"]
    st.dataframe(display_df, use_container_width=True, height=500, hide_index=True)

    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button(label="📥 下载 CSV", data=csv,
        file_name=f"{symbol}_oi_{start_dt:%Y%m%d_%H%M}_{end_dt:%Y%m%d_%H%M}.csv",
        mime="text/csv")


with tab_lookup:
    st.markdown("查询任意精确时刻的 OI 值（返回最接近的 5 分钟快照）")
    col_a, col_b = st.columns([3, 2])
    with col_a:
        lookup_date = st.date_input("日期", value=df["create_time"].dt.date.iloc[-1],
            min_value=df["create_time"].dt.date.min(),
            max_value=df["create_time"].dt.date.max(), key="lookup_date")
    with col_b:
        lookup_time = st.time_input("时间 (UTC)", value=time(12, 0), key="lookup_time")

    lookup_dt = datetime.combine(lookup_date, lookup_time, tzinfo=timezone.utc)
    df_sorted = df.copy()
    df_sorted["diff"] = (df_sorted["create_time"] - lookup_dt).abs()
    closest = df_sorted.nsmallest(1, "diff").iloc[0]
    diff_minutes = closest["diff"].total_seconds() / 60

    st.markdown("---")
    rc1, rc2 = st.columns(2)
    with rc1:
        st.markdown(f'<div class="metric-card"><div class="metric-label">最近快照时间 (UTC)</div><div class="metric-value" style="font-size:1.1rem">{closest["create_time"]:%Y-%m-%d %H:%M:%S}</div></div>', unsafe_allow_html=True)
        st.caption(f"距查询时间 {diff_minutes:.1f} 分钟")
    with rc2:
        st.markdown(f'<div class="metric-card"><div class="metric-label">OI 值</div><div class="metric-value">${closest["sum_open_interest_value"]/1e6:,.1f}M</div></div>', unsafe_allow_html=True)
        st.caption(f"= ${closest['sum_open_interest_value']:,.2f} = {closest['sum_open_interest']:,.4f} {symbol.replace('USDT', '')}")


# ==========================================================================
# 2) 全所当前 OI 对比（CEX + DEX）
# ==========================================================================
st.markdown("---")
base_asset = symbol.replace("USDT", "")
st.markdown(f'<div class="sec-header">全所当前 OI 对比 · {base_asset}</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="sec-sub">丢一个币，看它此刻在各 CEX / DEX 上的未平仓合约分布（实时快照，缓存 60 秒）</div>',
    unsafe_allow_html=True,
)

with st.spinner("正在并发拉取各交易所当前 OI..."):
    # 币安当前 OI 直接取本次历史数据的最后一个点
    binance_now = OIResult(
        exchange="Binance", type="CEX",
        oi_value_usd=float(latest["sum_open_interest_value"]),
        oi_base_qty=float(latest["sum_open_interest"]),
        ok=True,
    )
    try:
        others = get_other_oi(symbol)
    except Exception as e:
        others = []
        st.warning(f"部分交易所拉取异常：{e}")

all_results = [binance_now] + list(others)
ok_results = [r for r in all_results if r.ok and r.oi_value_usd]
total_oi = sum(r.oi_value_usd for r in ok_results) if ok_results else 0.0

# 汇总卡片
m1, m2, m3 = st.columns(3)
with m1:
    st.markdown(
        f'<div class="metric-card"><div class="metric-label">全所合计 OI</div>'
        f'<div class="metric-value">${total_oi/1e6:,.1f}M</div></div>', unsafe_allow_html=True)
with m2:
    st.markdown(
        f'<div class="metric-card"><div class="metric-label">有数据的所</div>'
        f'<div class="metric-value">{len(ok_results)} / {len(all_results)}</div></div>', unsafe_allow_html=True)
with m3:
    if ok_results:
        top = max(ok_results, key=lambda r: r.oi_value_usd)
        share = top.oi_value_usd / total_oi * 100 if total_oi else 0
        st.markdown(
            f'<div class="metric-card"><div class="metric-label">OI 最大所</div>'
            f'<div class="metric-value" style="font-size:1.1rem">{top.exchange} · {share:.0f}%</div></div>',
            unsafe_allow_html=True)

st.markdown("")

col_tbl, col_pie = st.columns([3, 2])

with col_tbl:
    rows = []
    for r in all_results:
        if r.ok and r.oi_value_usd:
            share = r.oi_value_usd / total_oi * 100 if total_oi else 0
            rows.append({
                "交易所": r.exchange,
                "类型": r.type,
                "当前 OI": f"${r.oi_value_usd/1e6:,.1f}M",
                "占比": f"{share:.1f}%",
                f"张数 ({base_asset})": f"{r.oi_base_qty:,.0f}" if r.oi_base_qty else "—",
                "_v": r.oi_value_usd,
            })
        else:
            rows.append({
                "交易所": r.exchange, "类型": r.type,
                "当前 OI": "—", "占比": "—",
                f"张数 ({base_asset})": "—", "_v": -1,
            })
    table_df = pd.DataFrame(rows).sort_values("_v", ascending=False).drop(columns=["_v"])
    st.dataframe(table_df, use_container_width=True, hide_index=True)

    # 列出失败的所，方便排查
    failed = [r for r in all_results if not (r.ok and r.oi_value_usd)]
    if failed:
        notes = "；".join(f"{r.exchange}（{r.note or '无数据'}）" for r in failed)
        st.caption(f"未取到：{notes}")

with col_pie:
    if ok_results:
        pie = go.Figure(data=[go.Pie(
            labels=[r.exchange for r in ok_results],
            values=[r.oi_value_usd for r in ok_results],
            hole=0.5,
            textinfo="label+percent",
            marker=dict(colors=["#2563eb", "#0ea5e9", "#6366f1", "#14b8a6", "#10b981", "#f59e0b"]),
        )])
        pie.update_layout(height=320, margin=dict(t=10, b=10, l=10, r=10),
                          showlegend=False, paper_bgcolor="white")
        st.plotly_chart(pie, use_container_width=True)

st.markdown(
    '<div class="footer-note">'
    '数据源：币安 data.binance.vision（历史归档）+ fapi.binance.com（实时与价格）'
    '；Bybit / OKX / Gate / Hyperliquid / Aster 官方公开 API（当前 OI 快照）。'
    '所有时间均为 UTC。'
    '</div>',
    unsafe_allow_html=True,
)
