import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import requests

# Page Config
st.set_page_config(page_title="Kishore Crypto & Fib Dashboard", layout="wide")

st.title("🚀 Smart Crypto & Fibonacci Confluence Dashboard")
st.markdown("---")

# Sidebar Controls
st.sidebar.header("Dashboard Settings")
symbol = st.sidebar.selectbox("Select Asset", ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"], index=0)
interval = st.sidebar.selectbox("Select Timeframe", ["1h", "4h", "1d"], index=1)
limit = st.sidebar.slider("Candles Limit", min_value=50, max_value=500, value=100, step=50)

# Fetch Data from Binance Public API
@st.cache_data(ttl=60)
def fetch_binance_data(symbol, interval, limit):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    response = requests.get(url)
    data = response.json()
    
    df = pd.DataFrame(data, columns=[
        'timestamp', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_asset_volume', 'number_of_trades',
        'taker_buy_base', 'taker_buy_quote', 'ignore'
    ])
    
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df['open'] = df['open'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    df['close'] = df['close'].astype(float)
    df['volume'] = df['volume'].astype(float)
    
    return df

df = fetch_binance_data(symbol, interval, limit)

if df.empty:
    st.error("Failed to fetch data from Binance. Please try again.")
else:
    # Current Price Metrics
    current_price = df['close'].iloc[-1]
    price_change = current_price - df['close'].iloc[-2]
    pct_change = (price_change / df['close'].iloc[-2]) * 100
    
    col1, col2, col3 = st.columns(3)
    col1.metric(label=f"{symbol} Current Price", value=f"${current_price:,.2f}", delta=f"{pct_change:.2f}%")
    col2.metric(label="Period High", value=f"${df['high'].max():,.2f}")
    col3.metric(label="Period Low", value=f"${df['low'].min():,.2f}")

    # Fibonacci Calculation based on Period High & Low
    max_high = df['high'].max()
    min_low = df['low'].min()
    diff = max_high - min_low

    # Check Trend Direction (Upward or Downward Swing)
    # Finding index of max and min to see if it's an uptrend or downtrend swing
    max_idx = df['high'].idxmax()
    min_idx = df['low'].idxmin()

    if max_idx > min_idx:
        # Uptrend swing: Retracement from Low to High
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
        # Downtrend swing: Retracement from High to Low
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
    
    # Plotly Candlestick Chart
    fig = go.Figure(data=[go.Candlestick(
        x=df['timestamp'],
        open=df['open'],
        high=df['high'],
        low=df['low'],
        close=df['close'],
        name="Candles"
    )])

    # Add Fibonacci Horizontal Lines
    colors = {
        "0.0%": "gray", "23.6%": "blue", "38.2%": "green", 
        "50.0%": "orange", "61.8%": "red", "78.6%": "purple", "100.0%": "gray"
    }

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

    # Fibonacci Data Table & Confluence Analysis
    col_a, col_b = st.columns(2)
    
    with col_a:
        st.markdown("### 📐 Fibonacci Retracement Table")
        fib_df = pd.DataFrame(list(fib_levels.items()), columns=["Level", "Price ($)"])
        st.dataframe(fib_df, use_container_width=True)

    with col_b:
        st.markdown("### 🔍 Market Status & Confluence Zone")
        
        # Check proximity to Golden Zone (61.8%)
        golden_zone_price = fib_levels["61.8% (Golden Zone)"]
        distance_pct = abs(current_price - golden_zone_price) / current_price * 100
        
        if distance_pct <= 0.5:
            st.success(f"🎯 **ALERT:** Price is currently inside the **Golden Pocket (61.8%) Confluence Zone** (${golden_zone_price:,.2f})! Watch for price action rejection.")
        elif current_price < golden_zone_price:
            st.info(f"📉 Price is trading below the Golden Pocket level. Key support below.")
        else:
            st.info(f"📈 Price is trading above the Golden Pocket level. Monitoring pullbacks.")
            
        st.write(f"- **Current Distance from 0.618 Fib:** `{distance_pct:.2f}%`")
        st.write(f"- **Suggested Risk Management:** Always use a strict Stop Loss below the 78.6% or 100% swing level.")
