"""
Kishore Crypto Fib Dashboard
============================
Binance Spot - every USDT pair - every timeframe (1m ... 1w) - fully automatic scan.

The scan runs in a background thread (fib_engine.Scanner); this file only shows the
results and a chart. No scan buttons, no sidebar controls: open the page and it works.
"""
import os
import time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from fib_engine import (
    FIB_LEVELS, GOLD_HI, GOLD_LO, TF_SECONDS, TIMEFRAMES,
    Scanner, analyze, build_signals, candles_df, evaluate, fib_price, fmt_price, trade_plan,
)

st.set_page_config(page_title="Kishore Crypto Fib Dashboard", page_icon="🚀", layout="wide")

# optional: extra Binance hosts / a proxy can be set in Streamlit secrets
try:
    if "BINANCE_BASE_URLS" in st.secrets:
        os.environ["BINANCE_BASE_URLS"] = str(st.secrets["BINANCE_BASE_URLS"])
except Exception:
    pass


@st.cache_resource(show_spinner=False)
def get_scanner() -> Scanner:
    sc = Scanner()
    sc.start()
    return sc


scanner = get_scanner()


@st.cache_data(ttl=20, show_spinner=False)
def load_klines(symbol: str, tf: str):
    return scanner.client.klines(symbol, tf, 500)


st.title("🚀 Kishore Crypto Fib Dashboard")
st.caption(
    "Binance Spot · all USDT pairs · all timeframes · automatic scan. "
    "Setup = price pulling back into a Fibonacci zone of the latest swing leg. "
    "Confirm your own entry; this is not financial advice."
)

with st.expander("⚙️ Filters", expanded=False):
    c1, c2, c3 = st.columns(3)
    c1.slider("Minimum score", 0, 100, 55, key="f_min_score")
    c2.selectbox("Bias", ["All", "BUY", "SELL"], key="f_bias")
    c3.checkbox("Best signal per coin only", value=False, key="f_best")
    st.multiselect("Timeframes", TIMEFRAMES, default=TIMEFRAMES, key="f_tfs")
    st.slider("Min 24h volume (million USDT, 0 = all coins)", 0.0, 100.0, 0.0, 0.5, key="f_min_vol")


# --------------------------------------------------------------------------- #
# Signals (auto-refreshing)
# --------------------------------------------------------------------------- #
@st.fragment(run_every=10)
def signals_panel():
    snap = scanner.snapshot()
    stats = snap["stats"]
    n_pairs = len(snap["universe"])
    done, exp = len(snap["results"]), snap["expected"]

    if n_pairs == 0:
        err = stats["last_error"] or snap["client_error"]
        if err and time.time() - stats["last_error_ts"] < 180:
            st.error(
                "Could not load the Binance pair list yet.\n\n"
                f"`{err}`\n\n"
                "HTTP 451 / 403 means Binance blocks this server's region. The scanner keeps "
                "retrying every 20 seconds on all backup hosts."
            )
        else:
            st.info("⏳ Connecting to Binance and loading all USDT pairs...")
        return

    host = (snap["active_base"] or "?").replace("https://", "")
    st.caption(f"✅ Data: {host} · {n_pairs} USDT pairs × {len(TIMEFRAMES)} timeframes · "
               f"scanned {done}/{exp} · auto-refresh every 10 s"
               + (f" · {snap['skipped']} non-trading pairs skipped" if snap.get("status_filter") else ""))
    if exp and done < 0.97 * exp:
        st.progress(min(done / exp, 1.0), text=f"First full scan in progress... {done}/{exp}")

    min_score = st.session_state.get("f_min_score", 55)
    bias_f = st.session_state.get("f_bias", "All")
    best_only = st.session_state.get("f_best", False)
    tf_f = st.session_state.get("f_tfs", TIMEFRAMES)
    min_vol = st.session_state.get("f_min_vol", 0.0) * 1e6
    vols = snap["volumes"]

    rows = build_signals(snap["results"], snap["prices"], set(snap["universe"]))
    rows = [r for r in rows
            if r["score"] >= min_score and r["tf"] in tf_f and (bias_f == "All" or r["bias"] == bias_f)
            and vols.get(r["symbol"], 0.0) >= min_vol]
    if best_only:
        seen, uniq = set(), []
        for r in rows:  # rows are already sorted by score
            if r["symbol"] not in seen:
                seen.add(r["symbol"])
                uniq.append(r)
        rows = uniq

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Signals", len(rows))
    m2.metric("🟢 BUY zones", sum(r["bias"] == "BUY" for r in rows))
    m3.metric("🔴 SELL zones", sum(r["bias"] == "SELL" for r in rows))
    m4.metric("Top score", f"{rows[0]['symbol']} {rows[0]['tf']} · {rows[0]['score']}" if rows else "-")

    if not rows:
        st.info("No setups match the filters right now. The list updates automatically.")
        return

    table = pd.DataFrame([{
        "Coin": r["symbol"],
        "TF": r["tf"],
        "Bias": "🟢 BUY" if r["bias"] == "BUY" else "🔴 SELL",
        "Score": r["score"],
        "Grade": "A" if r["score"] >= 75 else ("B" if r["score"] >= 60 else "C"),
        "Fib %": round(r["retr"] * 100, 1),
        "Zone": r["zone"],
        "Vol $M": round(vols.get(r["symbol"], 0.0) / 1e6, 2),
        "Price": fmt_price(r["price"]),
        "SL": fmt_price(r["sl"]),
        "TP1": fmt_price(r["tp1"]),
        "TP2": fmt_price(r["tp2"]),
        "RR1": round(r["rr1"], 2),
        "Confluence": ", ".join(r["tags"]),
    } for r in rows[:150]])

    st.dataframe(
        table, hide_index=True, width="stretch",
        column_config={
            "Score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%d"),
            "Fib %": st.column_config.NumberColumn("Fib %", format="%.1f"),
            "RR1": st.column_config.NumberColumn("RR1", format="%.2f"),
        },
    )
    st.caption(
        "Score = Fib zone (Golden Pocket 40 / OTE 28 / 0.382 15) + EMA99 + EMA200 (+10 each) + anchored VWAP (+8) "
        "+ Rejection (+10) or Sweep+Reclaim (+15) + volume spike (+6) + higher-TF direction (+10 / −10) "
        "+ multi-TF fib cluster (+6 per TF, max 18). SL = beyond 0.886, TP1 = swing extreme, TP2 = 1.272 extension."
    )


signals_panel()

st.markdown("---")
st.markdown("### 📊 Chart with auto Fibonacci")


# --------------------------------------------------------------------------- #
# Chart
# --------------------------------------------------------------------------- #
def make_chart(symbol: str, tf: str, kl) -> tuple[go.Figure, dict | None, pd.DataFrame]:
    df = candles_df(kl)
    info = analyze(symbol, tf, kl)
    df["ema99"] = df["close"].ewm(span=99, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    view = df.tail(200)

    fig = go.Figure(go.Candlestick(
        x=view["time"], open=view["open"], high=view["high"], low=view["low"], close=view["close"], name="Price",
    ))
    if len(df) >= 99:
        fig.add_trace(go.Scatter(x=view["time"], y=view["ema99"], mode="lines", name="EMA99",
                                 line=dict(color="#f0b90b", width=1.3)))
    if len(df) >= 200:
        fig.add_trace(go.Scatter(x=view["time"], y=view["ema200"], mode="lines", name="EMA200",
                                 line=dict(color="#a97bff", width=1.3)))

    if info:
        tz = "Asia/Kolkata"
        t_a = pd.to_datetime(info["A_time"], unit="ms", utc=True).tz_convert(tz).tz_localize(None)
        t_b = pd.to_datetime(info["B_time"], unit="ms", utc=True).tz_convert(tz).tz_localize(None)
        x0 = max(t_a, view["time"].iloc[0])
        x_end = view["time"].iloc[-1] + pd.to_timedelta(TF_SECONDS[tf], unit="s")

        fig.add_shape(type="rect", x0=x0, x1=x_end, y0=fib_price(info, GOLD_LO), y1=fib_price(info, GOLD_HI),
                      fillcolor="rgba(255,215,0,0.18)", line=dict(width=0), layer="below")
        for r in FIB_LEVELS:
            y = fib_price(info, r)
            gold = r in (0.618, 0.65)
            fig.add_shape(type="line", x0=x0, x1=x_end, y0=y, y1=y,
                          line=dict(color="gold" if gold else "rgba(255,255,255,0.35)", width=1, dash="dot"))
            fig.add_annotation(xref="paper", x=1.0, xanchor="left", y=y, yref="y", showarrow=False,
                               text=f"{r:g} · {fmt_price(y)}", font=dict(size=10, color="#cfcfcf"))
        fig.add_trace(go.Scatter(x=[t_a, t_b], y=[info["A"], info["B"]], mode="lines", name="Swing leg",
                                 line=dict(color="rgba(255,255,255,0.6)", width=1.5, dash="dash")))

        seg = df[df["open_time"] >= info["A_time"]].copy()
        if not seg.empty and seg["volume"].sum() > 0:
            tp = (seg["high"] + seg["low"] + seg["close"]) / 3.0
            seg["avwap"] = (tp * seg["volume"]).cumsum() / seg["volume"].cumsum()
            seg = seg[seg["time"] >= view["time"].iloc[0]]
            fig.add_trace(go.Scatter(x=seg["time"], y=seg["avwap"], mode="lines", name="Anchored VWAP",
                                     line=dict(color="#2ec4b6", width=1.5)))

    fig.update_layout(
        template="plotly_dark", height=580, xaxis_rangeslider_visible=False,
        margin=dict(l=10, r=110, t=30, b=10), uirevision=f"{symbol}-{tf}",
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0),
    )
    return fig, info, df


@st.fragment(run_every=60)
def chart_panel():
    snap = scanner.snapshot()
    symbols = snap["universe"] or ["BTCUSDT", "ETHUSDT"]
    c1, c2 = st.columns([2, 1])
    default_idx = symbols.index("BTCUSDT") if "BTCUSDT" in symbols else 0
    symbol = c1.selectbox("Coin", symbols, index=default_idx, key="chart_symbol")
    tf = c2.selectbox("Timeframe", TIMEFRAMES, index=TIMEFRAMES.index("1h"), key="chart_tf")

    try:
        kl = load_klines(symbol, tf)
    except Exception as e:
        st.error(f"Could not load candles for {symbol} {tf}: {e}")
        return
    if not kl or len(kl) < 30:
        st.warning("Not enough candle data for this coin/timeframe.")
        return

    fig, info, df = make_chart(symbol, tf, kl)
    st.plotly_chart(fig, width="stretch")

    price = snap["prices"].get(symbol) or float(df["close"].iloc[-1])
    if not info:
        st.info("No valid swing leg found right now (the last leg was broken or is too small).")
        return
    ev = evaluate(info, price)
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Leg bias", "🟢 BUY (up-leg pullback)" if info["bias"] == "BUY" else "🔴 SELL (down-leg bounce)")
    k2.metric("Price", fmt_price(price))
    if ev is None:
        k3.metric("Retracement", "-")
        k4.metric("Zone", "Outside leg")
        st.caption("Price is at a new extreme or has broken the start of the leg, so there is no active fib pullback.")
        return
    k3.metric("Retracement", f"{ev['retr'] * 100:.1f}%")
    k4.metric("Zone", ev["zone"] or "Not in a fib zone")
    if ev["zone"]:
        plan = trade_plan(info, price)
        if plan:
            st.write(
                f"**Entry** {fmt_price(plan['entry'])} · **SL** {fmt_price(plan['sl'])} · "
                f"**TP1** {fmt_price(plan['tp1'])} (RR {plan['rr1']:.2f}) · "
                f"**TP2** {fmt_price(plan['tp2'])} (RR {plan['rr2']:.2f})"
            )
        if ev["tags"]:
            st.caption("Confluence on this timeframe: " + ", ".join(ev["tags"]))


chart_panel()

st.caption("Scans every TRADING Binance Spot USDT pair (same coin list and data hosts as the Trendline scanner). Times shown in IST.")
