import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import yfinance as yf

# Page Config
st.set_page_config(page_title="Kishore Crypto & Fib Dashboard", layout="wide")

st.title("🚀 Smart Crypto & Fibonacci Confluence Dashboard")
st.markdown("---")

# Sidebar Controls
st.sidebar.header("Dashboard Settings")
symbol_map = {
    "BTCUSDT": "BTC-USD",
    "ETHUSDT": "ETH-USD",
    "SOLUSDT": "SOL-USD",
    "XRPUSDT": "XRP-USD"
}
selected_symbol = st.sidebar.selectbox("Select Asset", list(symbol_map.keys()), index=0)
ticker = symbol_map[selected_symbol]

yf_interval = st.sidebar.selectbox("Select Interval", ["60m", "1d"], index=0)
period = st.sidebar.selectbox("Select Period", ["1mo", "3mo", "6mo", "1y"], index=0)

# Fetch Data from Yahoo Finance
@st.cache_data(ttl=60)
def fetch_yahoo_data(ticker, period, interval):
    try:
        df = yf.download(ticker, period=period, interval=interval, progress=False)
        if df.empty:
            return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.reset_index()
        df.columns = [str(col).lower() for col in df.columns]
        if 'datetime' in df.columns:
            df['timestamp'] = df['datetime']
        elif 'date' in df.columns:
            df['timestamp'] = df['date']
        return df
    except Exception as e:
        return pd.DataFrame()

df = fetch_yahoo_data(ticker, period, yf_interval)

if df.empty or 'close' not in df.columns:
    st.error("Failed to fetch data from Yahoo Finance. Please try another timeframe or asset.")
else:
    df['open'] = df['open'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    df['close'] = df['close'].astype(float)
    
    current_price = df['close'].iloc[-1]
    price_change = current_price - df['close'].iloc[-2]
    pct_change = (price_change / df['close'].iloc[-2]) * 100
    
    col1, col2, col3 = st.columns(3)
    col1.metric(label=f"{selected_symbol} Current Price", value=f"${current_price:,.2f}", delta=f"{pct_change:.2f}%")
    col2.metric(label="Period High", value=f"${df['high'].max():,.2f}")
    col3.metric(label="Period Low", value=f"${df['low'].min():,.2f}")

    max_high = df['high'].max()
    min_low = df['low'].min()
    diff = max_high - min_low

    max_idx = df['high'].idxmax()
    min_idx = df['low'].idxmin()

    if max_idx > min_idx:
        fib_levels = {
            "0.0% (High)": max_high,
            "23.6%": max_high - (diff * 0.236),
            "38.2%": max_high - (diff * 0.382),
            "50.0% (Mid)": max_high - (diff * 0.5),
            "61.8% (Golden Zone)": max_high - (diff * 0.618),
            "78.6%": max_high - (diff * 0.786),
            "100.0% (Low)": min_low
        }
    else:
        fib_levels = {
            "0.0% (Low)": min_low,
            "23.6%": min_low + (diff * 0.236),
            "38.2%": min_low + (diff * 0.382),
            "50.0% (Mid)": min_low + (diff * 0.5),
            "61.8% (Golden Zone)": min_low + (diff * 0.618),
            "78.6%": min_low + (diff * 0.786),
            "100.0% (High)": max_high
        }

    st.markdown("### 📊 Candlestick Chart with Auto Fibonacci Levels")
    
    fig = go.Figure(data=[go.Candlestick(
        x=df['timestamp'],
        open=df['open'],
        high=df['high'],
        low=df['low'],
        close=df['close'],
        name="Candles"
    )])

    for level_name, price_val in fib_levels.items():
        line_color = "gold" if "61.8" in level_name else "rgba(255,255,255,0.4)"
        fig.add_hline(
            y=price_val, 
            line_dash="dash", 
            line_color=line_color,
            annotation_text=f"{level_name}: ${price_val:,.2f}", 
            annotation_position="bottom right"
        )

    fig.update_layout(
        template="plotly_dark",
        xaxis_rangeslider_visible=False,
        height=600,
        margin=dict(l=20, r=20, t=40, b=20)
    )

    st.plotly_chart(fig, use_container_width=True)

    col_a, col_b = st.columns(2)
    
    with col_a:
        st.markdown("### 📐 Fibonacci Retracement Table")
        fib_df = pd.DataFrame(list(fib_levels.items()), columns=["Level", "Price ($)"])
        st.dataframe(fib_df, use_container_width=True)

    with col_b:
        st.markdown("### 🔍 Market Status & Confluence Zone")
        golden_zone_price = fib_levels["61.8% (Golden Zone)"]
        distance_pct = abs(current_price - golden_zone_price) / current_price * 100
        
        if distance_pct <= 0.5:
            st.success(f"🎯 **ALERT:** Price is currently inside the **Golden Pocket (61.8%) Confluence Zone** (${golden_zone_price:,.2f})!")
        elif current_price < golden_zone_price:
            st.info(f"📉 Price is trading below the Golden Pocket level.")
        else:
            st.info(f"📈 Price is trading above the Golden Pocket level.")
            
        st.write(f"- **Current Distance from 0.618 Fib:** `{distance_pct:.2f}%`")
        st.write(f"- **Suggested Risk Management:** Always use a strict Stop Loss.")
