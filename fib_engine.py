"""
Kishore Crypto Fib Dashboard - scan engine
==========================================

* Universe : every TRADING Binance Spot ``*USDT`` pair (stablecoins removed) - same list as the Trendline scanner
* Timeframes: 1m ... 1w (see TIMEFRAMES)
* Data     : Binance public market-data REST (no API key). Works through a list of
             fallback hosts, because api.binance.com is geo-blocked on some clouds.
* Logic    : ATR zig-zag swing leg -> Fibonacci retracement -> confluence score
             (fib zone + Supertrend (same + higher TF) + EMA99/EMA200 + anchored VWAP +
              rejection / sweep-reclaim candle + volume + multi-TF fib cluster)

Nothing here imports Streamlit, so it can be tested on its own.
"""
from __future__ import annotations

import os
import random
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w"]
TF_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800,
    "12h": 43200, "1d": 86400, "3d": 259200, "1w": 604800,
}
# higher timeframe used for the trend-alignment check
HTF_MAP = {
    "1m": "15m", "3m": "15m", "5m": "1h", "15m": "1h", "30m": "4h", "1h": "4h",
    "2h": "1d", "4h": "1d", "6h": "1d", "8h": "1d", "12h": "3d", "1d": "1w", "3d": "1w",
}

MIN_24H_VOLUME_USDT = 0            # 0 = scan every TRADING USDT pair (same as the Trendline scanner)
KLINE_LIMIT = 500                  # candles per request (EMA200 needs warm-up)
ZIGZAG_MIN_ATR = 2.5               # price must reverse this many ATRs to confirm a swing
MIN_CANDLES = 60                   # need at least this many closed candles

ST_PERIOD, ST_MULT = 10, 3.0       # Supertrend(10,3) - same settings as the wick-touch scanner
ST_PTS = 10                        # points for / against a Supertrend direction
TREND_CAP = 25                     # EMA-wrong-side + Supertrend + HTF Supertrend adjustments are capped to +/- this
GOLD_LO, GOLD_HI = 0.618, 0.65     # golden pocket
OTE_LO, OTE_HI = 0.5, 0.786        # optimal trade entry band
FIB_LEVELS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.65, 0.705, 0.786, 0.886, 1.0]

WORKERS = 10
BATCH = 240
UNIVERSE_TTL = 30 * 60             # refresh pair list every 30 min
PRICE_TTL = 10                     # refresh live prices every 10 s

# Binance hosts, tried in order. data-api.binance.vision is the public market-data
# mirror and normally works from cloud servers where api.binance.com returns 451.
DEFAULT_BASES = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api4.binance.com",
    "https://api-gcp.binance.com",
]
DEFAULT_BUDGET = 4800              # request weight per minute (Binance limit is 6000)
BASE_BUDGET: dict[str, int] = {}

STABLE_BASES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "AEUR", "EUR", "GBP", "TRY",
    "USDE", "USD1", "XUSD", "BFUSD", "RLUSD", "USDS", "PYUSD", "EURI", "UST",
}


# --------------------------------------------------------------------------- #
# Binance client (rate limited, with host fallback)
# --------------------------------------------------------------------------- #
class RateLimited(Exception):
    pass


class WeightLimiter:
    """Sliding 60 s window on request weight."""

    def __init__(self, budget: int = DEFAULT_BUDGET):
        self.budget = budget
        self.lock = threading.Lock()
        self.events: deque[tuple[float, int]] = deque()
        self.used = 0
        self.pause_until = 0.0

    def acquire(self, weight: int) -> None:
        while True:
            with self.lock:
                now = time.time()
                if now < self.pause_until:
                    wait = self.pause_until - now
                else:
                    while self.events and now - self.events[0][0] >= 60:
                        self.used -= self.events.popleft()[1]
                    if self.used + weight <= self.budget or not self.events:
                        self.events.append((now, weight))
                        self.used += weight
                        return
                    wait = 60 - (now - self.events[0][0]) + 0.05
            time.sleep(max(0.05, min(wait, 5.0)))

    def penalize(self, seconds: float) -> None:
        with self.lock:
            self.pause_until = max(self.pause_until, time.time() + seconds)


class BinanceClient:
    def __init__(self, bases: list[str] | None = None):
        extra = [b.strip().rstrip("/") for b in os.environ.get("BINANCE_BASE_URLS", "").split(",") if b.strip()]
        self.bases = extra + [b for b in (bases or DEFAULT_BASES) if b not in extra]
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (KishoreFibDashboard)"})
        self.limiter = WeightLimiter()
        self.pref = 0
        self.active = ""
        self.last_error = ""

    def get(self, path: str, params: dict | None = None, weight: int = 2, timeout: int = 15):
        errors: list[str] = []
        n = len(self.bases)
        start = self.pref
        for k in range(n):
            idx = (start + k) % n
            base = self.bases[idx]
            self.limiter.budget = BASE_BUDGET.get(base, DEFAULT_BUDGET)
            self.limiter.acquire(weight)
            try:
                r = self.session.get(base + path, params=params, timeout=timeout)
            except requests.RequestException as e:
                errors.append(f"{base}: {type(e).__name__}")
                continue
            used = r.headers.get("x-mbx-used-weight-1m")
            if used and used.isdigit() and int(used) >= self.limiter.budget + 600:
                self.limiter.penalize(15)
            if r.status_code == 200:
                self.pref = idx
                self.active = base
                try:
                    return r.json()
                except ValueError:
                    errors.append(f"{base}: bad JSON")
                    continue
            if r.status_code in (418, 429):
                wait = int(r.headers.get("Retry-After", 30 if r.status_code == 429 else 120))
                self.limiter.penalize(wait)
                raise RateLimited(f"{base}: HTTP {r.status_code}, waiting {wait}s")
            if r.status_code == 400:
                raise ValueError(f"{base}: HTTP 400 {r.text[:120]}")
            errors.append(f"{base}: HTTP {r.status_code}")
        self.last_error = "; ".join(errors)
        raise RuntimeError(self.last_error)

    def klines(self, symbol: str, interval: str, limit: int = KLINE_LIMIT):
        return self.get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit}, weight=2)


# --------------------------------------------------------------------------- #
# Indicators and swing logic
# --------------------------------------------------------------------------- #
def _ema(x: np.ndarray, span: int) -> np.ndarray:
    return pd.Series(x).ewm(span=span, adjust=False).mean().to_numpy()


def _atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:
    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    return pd.Series(tr).ewm(alpha=1.0 / n, adjust=False).mean().to_numpy()


def supertrend(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = ST_PERIOD, mult: float = ST_MULT):
    """Classic Supertrend (Wilder ATR on hl2). Returns (line, direction); direction +1 = bullish, -1 = bearish."""
    n = len(c)
    atr = _atr(h, l, c, period)
    hl2 = (h + l) / 2.0
    up, dn = hl2 + mult * atr, hl2 - mult * atr
    fu, fl = up.copy(), dn.copy()
    direction = np.ones(n, dtype=int)
    for i in range(1, n):
        fu[i] = up[i] if (up[i] < fu[i - 1] or c[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = dn[i] if (dn[i] > fl[i - 1] or c[i - 1] < fl[i - 1]) else fl[i - 1]
        if direction[i - 1] == -1:
            direction[i] = 1 if c[i] > fu[i] else -1
        else:
            direction[i] = -1 if c[i] < fl[i] else 1
    line = np.where(direction == 1, fl, fu)
    return line, direction


def trend_state(klines, now_ms: int | None = None) -> dict | None:
    """Supertrend direction on the last CLOSED candle (does not repaint intrabar)."""
    if not klines:
        return None
    now_ms = now_ms or int(time.time() * 1000)
    a = np.array([[k[2], k[3], k[4], k[6]] for k in klines], dtype=float)
    a = a[a[:, 3] < now_ms]
    if len(a) < 30:
        return None
    line, d = supertrend(a[:, 0], a[:, 1], a[:, 2])
    return {"dir": int(d[-1]), "line": float(line[-1])}


def zigzag(h: np.ndarray, l: np.ndarray, atr: np.ndarray, min_atr: float = ZIGZAG_MIN_ATR):
    """Confirmed swing pivots [(idx, 'H'|'L', price)].

    A swing high/low is confirmed only after price reverses ``min_atr`` ATRs away from it,
    so tiny noise never becomes a leg. The still-unconfirmed current extreme is not returned.
    """
    piv: list[tuple[int, str, float]] = []
    trend = 0  # 0 undecided, 1 tracking a high, -1 tracking a low
    hi_i = lo_i = 0
    for i in range(1, len(h)):
        thr = min_atr * atr[i]
        if trend == 0:
            if h[i] > h[hi_i]:
                hi_i = i
            if l[i] < l[lo_i]:
                lo_i = i
            if h[hi_i] - l[i] >= thr and hi_i != i:
                piv.append((hi_i, "H", float(h[hi_i])))
                trend, lo_i = -1, i
            elif h[i] - l[lo_i] >= thr and lo_i != i:
                piv.append((lo_i, "L", float(l[lo_i])))
                trend, hi_i = 1, i
        elif trend == 1:
            if h[i] > h[hi_i]:
                hi_i = i
            elif h[hi_i] - l[i] >= thr:
                piv.append((hi_i, "H", float(h[hi_i])))
                trend, lo_i = -1, i
        else:
            if l[i] < l[lo_i]:
                lo_i = i
            elif h[i] - l[lo_i] >= thr:
                piv.append((lo_i, "L", float(l[lo_i])))
                trend, hi_i = 1, i
    return piv


def fib_price(info: dict, ratio: float) -> float:
    """Price of a retracement ratio for the leg stored in ``info``."""
    return info["B"] - ratio * info["L"] if info["bias"] == "BUY" else info["B"] + ratio * info["L"]


def analyze(symbol: str, tf: str, klines, now_ms: int | None = None) -> dict | None:
    """Detect the latest swing leg on one symbol/timeframe and return its static facts."""
    if not klines:
        return None
    now_ms = now_ms or int(time.time() * 1000)
    a = np.array([[k[0], k[1], k[2], k[3], k[4], k[5], k[6]] for k in klines], dtype=float)
    closed = a[a[:, 6] < now_ms]
    if len(closed) < MIN_CANDLES:
        return None
    o, h, l, c, v = closed[:, 1], closed[:, 2], closed[:, 3], closed[:, 4], closed[:, 5]
    hh_all, ll_all = a[:, 2], a[:, 3]
    live_close = float(a[-1, 4])

    atr = _atr(h, l, c)
    zz = zigzag(h, l, atr)
    if len(zz) < 2:
        return None
    (a_idx, _, a_price), (b_idx, b_type, b_price) = zz[-2], zz[-1]
    bias = "BUY" if b_type == "H" else "SELL"

    # extend the leg end with candles after the last confirmed pivot (incl. live candle)
    tail_start = b_idx + 1
    if bias == "BUY":
        if tail_start < len(hh_all) and hh_all[tail_start:].max() > b_price:
            b_idx = tail_start + int(np.argmax(hh_all[tail_start:]))
            b_price = float(hh_all[b_idx])
        if a_idx + 1 < len(ll_all) and ll_all[a_idx + 1:].min() < a_price:
            return None  # leg start already broken
    else:
        if tail_start < len(ll_all) and ll_all[tail_start:].min() < b_price:
            b_idx = tail_start + int(np.argmin(ll_all[tail_start:]))
            b_price = float(ll_all[b_idx])
        if a_idx + 1 < len(hh_all) and hh_all[a_idx + 1:].max() > a_price:
            return None
    leg = abs(b_price - a_price)
    if leg <= 0:
        return None

    info = {
        "symbol": symbol, "tf": tf, "bias": bias,
        "A": float(a_price), "B": float(b_price), "L": float(leg),
        "A_time": int(a[a_idx, 0]), "B_time": int(a[b_idx, 0]),
        "atr": float(atr[-1]),
        "ema99": float(_ema(c, 99)[-1]) if len(c) >= 99 else float("nan"),
        "ema200": float(_ema(c, 200)[-1]) if len(c) >= 200 else float("nan"),
        "last_price": live_close,
        "updated": time.time(),
    }

    # anchored VWAP from the leg start, up to the last closed candle
    seg = slice(a_idx, len(c))
    tp = (h[seg] + l[seg] + c[seg]) / 3.0
    vs = v[seg].sum()
    info["avwap"] = float((tp * v[seg]).sum() / vs) if vs > 0 else float("nan")

    # rejection / sweep + reclaim in the last two closed candles
    rej = 0
    for k in (-1, -2):
        i = len(c) + k
        rng = h[i] - l[i]
        if rng <= 0:
            continue
        if bias == "BUY":
            wick = min(o[i], c[i]) - l[i]
            ok = wick / rng >= 0.5 and c[i] >= l[i] + 0.6 * rng and l[i] <= fib_price(info, 0.5)
            swept = ok and i >= 5 and l[i] < l[i - 5:i].min()
        else:
            wick = h[i] - max(o[i], c[i])
            ok = wick / rng >= 0.5 and c[i] <= h[i] - 0.6 * rng and h[i] >= fib_price(info, 0.5)
            swept = ok and i >= 5 and h[i] > h[i - 5:i].max()
        if ok:
            rej = max(rej, 2 if swept else 1)
    info["rej"] = rej

    base_vol = v[-22:-2].mean() if len(v) >= 22 else float("nan")
    ratio = float(max(v[-1], v[-2]) / base_vol) if base_vol and base_vol > 0 else 0.0
    info["vol_ratio"] = ratio
    info["vol_spike"] = ratio >= 1.5
    return info


# --------------------------------------------------------------------------- #
# Live evaluation (cheap - runs on every refresh with the latest price)
# --------------------------------------------------------------------------- #
def evaluate(info: dict, price: float | None) -> dict | None:
    if price is None or not np.isfinite(price) or info["L"] <= 0:
        return None
    B, L = info["B"], info["L"]
    if info["bias"] == "BUY":
        if price >= B:
            return None
        retr = (B - price) / L
    else:
        if price <= B:
            return None
        retr = (price - B) / L
    if retr > 1.0:
        return None

    in_gold = GOLD_LO - 0.01 <= retr <= GOLD_HI + 0.01
    in_ote = OTE_LO - 0.005 <= retr <= OTE_HI + 0.005
    near382 = abs(retr - 0.382) <= 0.02
    if in_gold:
        zone, zs = "Golden Pocket", 40
    elif in_ote:
        zone, zs = "OTE 0.5-0.786", 28
    elif near382:
        zone, zs = "0.382", 15
    else:
        zone, zs = None, 0

    out = {"retr": retr, "zone": zone, "in_ote": in_gold or in_ote, "score": zs, "tags": [], "ema_pen": 0}
    if zone is None:
        return out

    atr = info["atr"]
    tol = 0.5 * atr if np.isfinite(atr) and atr > 0 else 0.002 * price
    score, tags = zs, out["tags"]
    buy = info["bias"] == "BUY"
    pen = [0]

    def level_score(level: float, support_pts: int, wrong_side_pts: int, name: str) -> int:
        """A BUY needs the level UNDER price (support); a SELL needs it OVER price (resistance).
        Level on the correct side and within tolerance -> bonus. Level on the wrong side
        (price already under EMA for a BUY / over EMA for a SELL) -> counter-trend, penalty."""
        if not np.isfinite(level):
            return 0
        on_support_side = level <= price if buy else level >= price
        if on_support_side:
            if abs(price - level) <= tol:
                tags.append(name)
                return support_pts
            return 0
        if wrong_side_pts:
            tags.append(f"{'Under' if buy else 'Over'} {name} ✗")
            pen[0] -= wrong_side_pts
        return 0

    score += level_score(info["ema99"], 10, 10, "EMA99")
    score += level_score(info["ema200"], 10, 10, "EMA200")
    score += level_score(info["avwap"], 8, 0, "AVWAP")
    if info["rej"] == 2:
        score += 15
        tags.append("Sweep+Reclaim")
    elif info["rej"] == 1:
        score += 10
        tags.append("Rejection")
    if info["vol_spike"]:
        score += 6
        tags.append(f"Vol x{info['vol_ratio']:.1f}")
    out["score"] = score
    out["ema_pen"] = pen[0]
    return out


def trade_plan(info: dict, price: float) -> dict | None:
    atr = info["atr"] if np.isfinite(info["atr"]) and info["atr"] > 0 else 0.002 * price
    buy = info["bias"] == "BUY"
    sl = fib_price(info, 0.886) + (-0.25 * atr if buy else 0.25 * atr)
    if (buy and sl >= price) or (not buy and sl <= price):
        sl = price - 0.5 * atr if buy else price + 0.5 * atr
    tp1 = info["B"]
    tp2 = info["B"] + (0.272 * info["L"] if buy else -0.272 * info["L"])
    risk = abs(price - sl)
    if risk <= 0:
        return None
    return {
        "entry": price, "sl": sl, "tp1": tp1, "tp2": tp2,
        "rr1": abs(tp1 - price) / risk, "rr2": abs(tp2 - price) / risk,
    }


def build_signals(results: dict, prices: dict, universe: set | None = None,
                  trend: dict | None = None) -> list[dict]:
    """Turn stored per-(symbol, tf) legs into scored signals using the live prices.

    ``trend`` maps (symbol, tf) -> {'dir': +1/-1, 'line': x} (Supertrend state).
    """
    trend = trend or {}
    by_symbol: dict[str, dict[str, dict]] = defaultdict(dict)
    for (sym, tf), info in results.items():
        if info and (universe is None or sym in universe):
            by_symbol[sym][tf] = info

    rows: list[dict] = []
    for sym, tfs in by_symbol.items():
        price = prices.get(sym)
        evals = {}
        for tf, info in tfs.items():
            p = price if price else info["last_price"]
            evals[tf] = (evaluate(info, p), p)

        for tf, (ev, p) in evals.items():
            if not ev or ev["zone"] is None:
                continue
            info = tfs[tf]
            score, tags = ev["score"], list(ev["tags"])

            # trend adjustment = EMA-wrong-side penalty + same-TF Supertrend + HTF Supertrend,
            # capped so one trend condition cannot be counted three times
            want = 1 if info["bias"] == "BUY" else -1
            adj = ev.get("ema_pen", 0)
            st_same = trend.get((sym, tf))
            if st_same:
                ok = st_same["dir"] == want
                adj += ST_PTS if ok else -ST_PTS
                tags.append("Supertrend ✓" if ok else "Supertrend ✗")
            htf = HTF_MAP.get(tf)
            st_htf = trend.get((sym, htf)) if htf else None
            if st_htf:
                ok = st_htf["dir"] == want
                adj += ST_PTS if ok else -ST_PTS
                tags.append(f"HTF {htf} Supertrend {'✓' if ok else '✗'}")
            score += max(-TREND_CAP, min(TREND_CAP, adj))

            cluster = [t for t, (e, _) in evals.items()
                       if t != tf and e and e["in_ote"] and tfs[t]["bias"] == info["bias"]]
            if cluster:
                score += min(6 * len(cluster), 18)
                top = sorted(cluster, key=lambda t: -TF_SECONDS[t])
                shown = "/".join(top[:4]) + (f" +{len(top) - 4}" if len(top) > 4 else "")
                tags.append(f"Fib cluster ×{len(cluster)} ({shown})")

            score = int(max(0, min(100, score)))
            plan = trade_plan(info, p)
            if not plan:
                continue
            rows.append({
                "symbol": sym, "tf": tf, "bias": info["bias"], "score": score,
                "retr": ev["retr"], "zone": ev["zone"], "price": p,
                "tags": tags, "cluster": len(cluster), **plan,
            })
    rows.sort(key=lambda r: (-r["score"], TF_SECONDS[r["tf"]]))
    return rows


# --------------------------------------------------------------------------- #
# Background scanner
# --------------------------------------------------------------------------- #
class Scanner:
    def __init__(self, client: BinanceClient | None = None):
        self.client = client or BinanceClient()
        self.lock = threading.RLock()
        self.results: dict[tuple[str, str], dict | None] = {}
        self.trend: dict[tuple[str, str], dict] = {}
        self.next_due: dict[tuple[str, str], float] = {}
        self.universe: list[str] = []
        self.volumes: dict[str, float] = {}
        self.prices: dict[str, float] = {}
        self.universe_ts = 0.0
        self.price_ts = 0.0
        self.skipped = 0
        self.status_filter = False
        self.stats = {"ok": 0, "err": 0, "last_error": "", "last_error_ts": 0.0, "started": time.time()}
        self.pool = ThreadPoolExecutor(max_workers=WORKERS)
        self._thread: threading.Thread | None = None

    # ---- public ---------------------------------------------------------- #
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="fib-scanner", daemon=True)
        self._thread.start()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "results": dict(self.results),
                "trend": dict(self.trend),
                "prices": dict(self.prices),
                "universe": list(self.universe),
                "volumes": dict(self.volumes),
                "stats": dict(self.stats),
                "expected": len(self.universe) * len(TIMEFRAMES),
                "skipped": self.skipped,
                "status_filter": self.status_filter,
                "active_base": self.client.active,
                "client_error": self.client.last_error,
            }

    # ---- internals ------------------------------------------------------- #
    def _note_error(self, msg: str) -> None:
        with self.lock:
            self.stats["err"] += 1
            self.stats["last_error"] = msg[:400]
            self.stats["last_error_ts"] = time.time()

    def _refresh_universe(self) -> None:
        data = self.client.get("/api/v3/ticker/24hr", weight=80, timeout=40)
        if not isinstance(data, list):
            raise RuntimeError(f"unexpected 24hr ticker response: {str(data)[:120]}")
        # only pairs whose exchange status is TRADING (halted / delisting pairs still
        # show up in the 24h ticker for a day, so the ticker alone is not enough)
        tradable = None
        try:
            ex = self.client.get("/api/v3/exchangeInfo", {"permissions": "SPOT"}, weight=20, timeout=60)
            tradable = {x["symbol"] for x in ex.get("symbols", [])
                        if x.get("status") == "TRADING" and x.get("isSpotTradingAllowed", True)}
            if not tradable:
                tradable = None
        except Exception as e:
            self._note_error(f"exchangeInfo: {type(e).__name__}: {e}")
        syms, vols, px, skipped = [], {}, {}, 0
        for d in data:
            s = d.get("symbol", "")
            if not s.endswith("USDT") or s[:-4] in STABLE_BASES:
                continue
            qv = float(d.get("quoteVolume") or 0)
            if qv < MIN_24H_VOLUME_USDT or int(d.get("count") or 0) == 0:
                continue
            if tradable is not None and s not in tradable:
                skipped += 1
                continue
            syms.append(s)
            vols[s] = qv
            px[s] = float(d["lastPrice"])
        syms.sort(key=lambda s: -vols[s])
        with self.lock:
            self.universe = syms
            self.volumes = vols
            self.skipped = skipped
            self.status_filter = tradable is not None
            self.prices.update(px)
            keep = set(syms)
            for key in [k for k in self.results if k[0] not in keep]:
                self.results.pop(key, None)
                self.trend.pop(key, None)
                self.next_due.pop(key, None)
            self.universe_ts = time.time()
            self.price_ts = time.time()

    def _refresh_prices(self) -> None:
        data = self.client.get("/api/v3/ticker/price", weight=4)
        px = {d["symbol"]: float(d["price"]) for d in data if d["symbol"].endswith("USDT")}
        with self.lock:
            self.prices.update(px)
            self.price_ts = time.time()

    def _collect_due(self, now: float):
        with self.lock:
            due = []
            for rank, sym in enumerate(self.universe):
                for ti, tf in enumerate(TIMEFRAMES):
                    nd = self.next_due.get((sym, tf), 0.0)
                    if nd <= now:
                        due.append((nd, rank, ti, sym, tf))
        due.sort()
        return due

    def _task(self, sym: str, tf: str) -> bool:
        key = (sym, tf)
        try:
            kl = self.client.klines(sym, tf, KLINE_LIMIT)
            info = analyze(sym, tf, kl)
            tr = trend_state(kl)
            if kl:
                nd = (kl[-1][6] + 1) / 1000.0 + 2.0 + random.random() * 3.0
            else:
                nd = time.time() + 3600
            nd = max(nd, time.time() + 5)
            with self.lock:
                self.results[key] = info
                if tr:
                    self.trend[key] = tr
                self.next_due[key] = nd
                self.stats["ok"] += 1
            return True
        except ValueError as e:  # invalid / delisted symbol
            with self.lock:
                self.results[key] = None
                self.next_due[key] = time.time() + 3600
            self._note_error(f"{sym} {tf}: {e}")
            return False
        except Exception as e:  # network, rate limit, bug - retry later
            with self.lock:
                self.next_due[key] = time.time() + 30
            self._note_error(f"{sym} {tf}: {type(e).__name__}: {e}")
            return False

    def _loop(self) -> None:
        while True:
            try:
                now = time.time()
                if now - self.universe_ts > UNIVERSE_TTL or not self.universe:
                    try:
                        self._refresh_universe()
                    except Exception as e:
                        self._note_error(f"universe: {type(e).__name__}: {e}")
                        if not self.universe:
                            time.sleep(20)
                            continue
                        self.universe_ts = time.time() - UNIVERSE_TTL + 120  # retry in 2 min
                if time.time() - self.price_ts > PRICE_TTL:
                    try:
                        self._refresh_prices()
                    except Exception as e:
                        self._note_error(f"prices: {type(e).__name__}: {e}")
                        self.price_ts = time.time() - PRICE_TTL + 5
                due = self._collect_due(time.time())
                if not due:
                    time.sleep(1.0)
                    continue
                chunk = due[:BATCH]
                oks = list(self.pool.map(lambda t: self._task(t[3], t[4]), chunk))
                if not any(oks):
                    time.sleep(15)
            except Exception as e:  # never let the thread die
                self._note_error(f"loop: {type(e).__name__}: {e}")
                time.sleep(5)


# --------------------------------------------------------------------------- #
# Helpers for the UI
# --------------------------------------------------------------------------- #
def candles_df(klines, tz: str = "Asia/Kolkata") -> pd.DataFrame:
    df = pd.DataFrame(
        [[k[0], float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), k[6]] for k in klines],
        columns=["open_time", "open", "high", "low", "close", "volume", "close_time"],
    )
    df["time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True).dt.tz_convert(tz).dt.tz_localize(None)
    return df


def fmt_price(p: float) -> str:
    if p is None or not np.isfinite(p):
        return "-"
    a = abs(p)
    if a >= 1000:
        return f"{p:,.2f}"
    if a >= 1:
        return f"{p:.4f}"
    if a >= 0.01:
        return f"{p:.6f}"
    return f"{p:.8f}"
