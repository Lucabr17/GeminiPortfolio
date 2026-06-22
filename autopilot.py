"""GVD AUTONOMOUS PORTFOLIO SYSTEM — v3.5  (single-file build)
=======================================================
Based on:
  @theaiportfolios  — PWER ranking, 15-stock portfolio, opportunity cost engine
  Jeremy Lefebvre   — GVD framework: Growth + Value + Dividend

This file is organized in two parts:
  PART 1 — pure strategy logic (constants, the three bucket scorers, the
    intrinsic-value engine, the position stop-rule engine, the regime/
    tailwind scorers, the opportunity-cost engine, the bucket balance
    check). Nothing in Part 1 touches the network, an API client, a
    file, or the system clock — every function takes data the caller
    already fetched and returns a decision. It was briefly split into
    its own gvd_strategy.py module while a backtest engine was being
    built (importing this file directly would otherwise try to
    initialize a live Gemini client at import time and crash a
    backtest), then merged back into this single file for simple
    one-file GitHub deployment. The section boundary is kept clearly
    marked below so it can be lifted out again in one piece if needed.
  PART 2 — the live system: data fetching (yfinance), the AI agents
    (Gemini), Telegram notifications, persistence, and execution. This
    is the part that actually runs when you execute this script.

v3.5 (this round):
  + CONSOLIDATED back into one file (see note above). No logic changed
    in this step — purely a reorganization for deployment simplicity.
  + FIXED (CRITICAL — caught live on 2026-06-19): dividend-yield
    normalizer blind spot. The old "if raw dividendYield > 1.0, treat as
    percentage-point" heuristic cannot distinguish a genuine 35% decimal
    yield from a 0.35%-in-percentage-point-form yield — both arrive as
    the raw value 0.35. That's exactly the range most large-cap,
    low-yield names sit in. It caused META (real yield ~0.35%) to be
    read as a 35% yield and bought into the Dividend bucket with a
    HIGH_YIELD tag. Any ticker with a sub-1% real yield was at risk, not
    just META. Fixed by deriving yield from dividendRate / price (an
    absolute $/share/year figure with no unit ambiguity) as the primary
    method; the old heuristic is now only a fallback for tickers where a
    vendor doesn't surface dividendRate at all.

v3.2-v3.4 fixes (condensed — see git history / prior changelogs for full detail):
  + Ticker prefix-collision bug: position/order matching used
    `key.startswith(ticker)` instead of an exact match, which misfired
    whenever one held ticker was a string-prefix of another (e.g. "T" vs
    "TALEN") — silently hid valid opportunity-cost swaps and could cause
    execute_agent_orders to sell or resize an unrelated position. Fixed
    everywhere to use `pos_key.split("_")[0] == ticker`.
  + PWER input dict-merge ({**all_cands, **existing_pos_data}) discarded
    candidate context (tags/score/sector) for any ticker that was both
    held and still screening as a buy candidate. Now merged per-ticker.
  + Removed dead CAP_G/CAP_V/CAP_D constants (defined, never read) and
    consolidated regime-dependent position caps into one lookup table
    (POS_CAP_PCT) instead of two diverging hardcoded copies.
  + Value-bucket TAKE_PROFIT depended on the held ticker surviving that
    cycle's top-10 Value-screener cutoff — a position that fell out of
    the top 10 silently lost its take-profit check. Now recalculated
    directly from fresh data every run, independent of screener ranking.
  + dividendYield-field format normalizer added (decimal vs
    percentage-point yfinance inconsistency) — later found to have its
    own blind spot, see v3.5 above.
  + Graham Number distortion guard for conglomerates/insurers with
    GAAP-EPS-distorting mark-to-market gains (e.g. Berkshire).
  + Candidate bucket-collision dedup, NAV recomputation after mandatory
    exits, sector concentration cap (35% NAV/sector), pipeline reordered
    so positions are monitored and mandatory exits executed before PWER
    scoring and the AI agents run.
"""

import os, sys, re, json, time, logging
import pandas as pd
import numpy as np
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
import yfinance as yf
from google import genai
from google.genai import types
import warnings


warnings.filterwarnings("ignore")


# ============================================================
# PART 1 — PURE STRATEGY LOGIC
# ============================================================
# No network calls, no API clients, no filesystem access, no system-
# clock reads anywhere below this line until PART 2. Every function
# takes data the caller already fetched and returns a decision. Kept
# in one clearly-bounded block so it can be lifted into its own module
# again later (e.g. for a backtest engine) without rewriting anything.

# ============================================================
# CONSTANTS — single source of truth for both live + backtest
# ============================================================
BUCKET_G = "GROWTH";   MAX_G = 0.50; TRAIL_G = 20
BUCKET_V = "VALUE";    MAX_V = 0.35; TRAIL_V = 15
BUCKET_D = "DIVIDEND"; MAX_D = 0.15; TRAIL_D = 10

TARGET_POSITIONS        = 15
MIN_HOLD_DAYS           = 30    # no discretionary sells before this age
EARNINGS_TRIM_PNL_MIN   = 35    # % gain required before pre-earnings trim fires
PWER_CHUNK              = 15    # max candidates per PWER API call (live only)
MAX_SECTOR_PCT          = 35.0  # max % of NAV in any single sector

GRAHAM_DIVERGENCE_RATIO = 3.0   # methods disagreeing by more than this -> distrust optimistic one
MOS_ANOMALY_THRESHOLD   = 85.0  # raw margin of safety above this = likely data artifact
MOS_SCORING_CAP         = 70.0  # cap used for scoring so legit deep-value isn't over-weighted

OPPORTUNITY_COST_PWER_GAP = 15  # min PWER edge required to flag a swap

# Per-position cap (% of NAV), keyed by macro regime — the only place
# this is defined (v3.2/v3.3 duplicated this; see autopilot.py changelog).
POS_CAP_PCT = {
    "BULL":    {BUCKET_G: 0.12, BUCKET_V: 0.10, BUCKET_D: 0.08},
    "CAUTION": {BUCKET_G: 0.08, BUCKET_V: 0.10, BUCKET_D: 0.08},
    "BEAR":    {BUCKET_G: 0.05, BUCKET_V: 0.07, BUCKET_D: 0.05},
}
CASH_FLOOR_PCT = {"BULL": 0.04, "CAUTION": 0.15, "BEAR": 0.40}
NEW_GROWTH_BUYS_ALLOWED = {"BULL": True, "CAUTION": True, "BEAR": False}

# Thematic tailwind clusters + the sectors each one covers. Shared by the
# live tailwind scanner and the backtest's point-in-time tailwind scorer.
TAILWIND_CLUSTERS = {
    "AI_SEMICONDUCTORS":  ["SOXX", "SMH"],
    "AI_SOFTWARE_CLOUD":  ["IGV",  "WCLD"],
    "ENERGY_POWER_INFRA": ["XLU",  "XLE"],
    "DEFENSE_AEROSPACE":  ["ITA",  "XAR"],
    "HEALTHCARE_BIOTECH": ["XBI",  "IBB"],
    "FINTECH_PAYMENTS":   ["FINX", "IPAY"],
    "CYBERSECURITY":      ["CIBR", "HACK"],
    "CONSUMER_RECOVERY":  ["XLY",  "XRT"],
}

TAILWIND_SECTOR_MAP = {
    "Technology":             ["AI_SEMICONDUCTORS", "AI_SOFTWARE_CLOUD", "CYBERSECURITY"],
    "Information Technology": ["AI_SEMICONDUCTORS", "AI_SOFTWARE_CLOUD", "CYBERSECURITY"],
    "Communication Services": ["AI_SOFTWARE_CLOUD"],
    "Energy":                 ["ENERGY_POWER_INFRA"],
    "Utilities":              ["ENERGY_POWER_INFRA"],
    "Industrials":            ["DEFENSE_AEROSPACE"],
    "Healthcare":              ["HEALTHCARE_BIOTECH"],
    "Health Care":             ["HEALTHCARE_BIOTECH"],
    "Financial Services":      ["FINTECH_PAYMENTS"],
    "Financials":              ["FINTECH_PAYMENTS"],
    "Consumer Cyclical":       ["CONSUMER_RECOVERY"],
    "Consumer Discretionary":  ["CONSUMER_RECOVERY"],
    "Consumer Defensive":      ["CONSUMER_RECOVERY"],
    "Consumer Staples":        ["CONSUMER_RECOVERY"],
}


# ============================================================
# SMALL PURE HELPERS
# ============================================================
def normalize_bucket(raw: str) -> str:
    r = (raw or "").upper().strip()
    if "GROWTH" in r or r == "G":  return BUCKET_G
    if "VALUE"  in r or r == "V":  return BUCKET_V
    if "DIVID"  in r or r == "D":  return BUCKET_D
    return BUCKET_G


_normalize_bucket = normalize_bucket  # alias used throughout this file


def normalize_dividend_yield(info: dict) -> float:
    """
    Returns a decimal dividend yield (0.0036 = 0.36%).

    PRIMARY METHOD: dividendRate / price. dividendRate is an absolute
    $/share/year figure with no unit ambiguity, so this sidesteps the
    yfinance dividendYield-field format problem entirely.

    FALLBACK: if dividendRate or price isn't available, fall back to the
    dividendYield field with a >1.0-means-percentage-point heuristic.
    That heuristic has a known blind spot: it can't tell a genuine 35%
    decimal yield apart from a 0.35%-in-percentage-point-form yield,
    because both arrive as the raw value 0.35. That's not a hypothetical
    edge case — it's exactly the range most large-cap, low-yield stocks
    (META, GOOG, AAPL, MSFT...) sit in, and it's what caused META's real
    ~0.35% yield to be read as 35% and wrongly qualify for the Dividend
    bucket with a HIGH_YIELD tag. Prefer the rate-based method whenever
    possible; this fallback only exists for tickers where the vendor
    doesn't surface dividendRate at all.
    """
    rate  = info.get("dividendRate")
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    if rate is not None and price:
        try:
            rate_f, price_f = float(rate), float(price)
            if price_f > 0:
                return rate_f / price_f
        except (TypeError, ValueError):
            pass

    raw = info.get("dividendYield")
    if raw is None:
        return 0.0
    try:
        raw = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return raw / 100.0 if raw > 1.0 else raw


def verify_pwer(s: dict) -> dict:
    """
    Recalculate PWER from its components and normalise probabilities to
    sum to 1. Used both to sanity-check live Gemini output and to finish
    off the backtest's deterministic PWER proxy, so both paths get the
    same numerical treatment.
    """
    bp = float(s.get("bull_prob", 0.25))
    ap = float(s.get("base_prob", 0.50))
    ep = float(s.get("bear_prob", 0.25))
    total = bp + ap + ep
    if total > 0:
        bp, ap, ep = bp / total, ap / total, ep / total
    s["bull_prob"] = round(bp, 4)
    s["base_prob"] = round(ap, 4)
    s["bear_prob"] = round(ep, 4)
    pwer = (float(s.get("bull_ret_pct", 0)) * bp +
            float(s.get("base_ret_pct", 0)) * ap +
            float(s.get("bear_ret_pct", 0)) * ep)
    s["pwer_pct"] = round(pwer, 2)
    return s


def _merge_candidates_dedup_core(g: dict, v: dict, d: dict) -> dict:
    """
    A stock can occasionally qualify for more than one bucket. Naive
    dict-merge ({**g, **v, **d}) lets the last bucket silently overwrite
    the earlier one. This keeps the higher-scoring bucket assignment per
    ticker. Returns (merged_dict, list_of_conflict_log_lines) so callers
    can log conflicts however they like (print, logging, etc.) without
    this pure function doing any I/O itself.
    """
    merged: dict = {}
    conflicts: list = []
    for bucket_cands in (g, v, d):
        for t, c in bucket_cands.items():
            if t not in merged or c.get("score", 0) > merged[t].get("score", 0):
                if t in merged:
                    conflicts.append(
                        f"[Dedup] {t} qualified for both {merged[t]['bucket']} "
                        f"and {c['bucket']} — keeping higher-scoring {c['bucket']}."
                    )
                merged[t] = c
    return merged, conflicts


def compute_sector_exposure(portfolio: dict, risk_matrix: dict, total_nav: float,
                             ticker_sector: dict) -> dict:
    """
    Returns {sector: pct_of_nav}. ticker_sector is a pre-fetched
    {ticker: sector} lookup the caller builds however suits its data
    source (live yfinance .info, or a point-in-time fundamentals table).
    """
    if total_nav <= 0:
        return {}
    exposure: dict = {}
    for pk, pos in portfolio.get("positions", {}).items():
        ticker = pk.split("_")[0]
        sector = ticker_sector.get(ticker, "Unknown")
        val = risk_matrix.get(pk, {}).get("current_value", pos.get("allocated_capital", 0))
        exposure[sector] = exposure.get(sector, 0) + val
    return {s: round(v / total_nav * 100, 1) for s, v in exposure.items()}


def atr_position_size(daily_ohlc: "pd.DataFrame", nav: float, max_pct: float,
                       risk_pct: float = 0.01) -> float:
    """
    ATR-based position size. Dollar risk = nav * risk_pct, stop = 2x
    ATR(14). Capped at nav * max_pct. daily_ohlc must have High/Low/Close
    columns, already sliced to data available as of the sizing date (no
    future bars), most-recent-last.
    """
    try:
        h = daily_ohlc
        if h is None or h.empty or len(h) < 14:
            raise ValueError("insufficient history")
        hi, lo, pc = h["High"], h["Low"], h["Close"].shift(1)
        tr = pd.concat([hi - lo, (hi - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
        atr = float(tr.iloc[-14:].mean())
        price = float(h["Close"].iloc[-1])
        shares = (nav * risk_pct) / (atr * 2)
        return min(shares * price, nav * max_pct)
    except Exception:
        return nav * max_pct * 0.50


# ============================================================
# MACRO REGIME (pure scorer — caller supplies the 5 raw signals)
# ============================================================
def score_regime(spy_drawdown_pct=None, vix=None, yield_spread=None,
                  credit_delta=None, breadth_pct=None) -> dict:
    """
    5-factor regime score. Any signal the caller couldn't compute (e.g.
    a vendor doesn't carry ^MMTH breadth historically) can be passed as
    None and is simply skipped rather than faked.
    """
    rs = 0
    signals: dict = {}

    if spy_drawdown_pct is not None:
        signals["spy_drawdown"] = round(spy_drawdown_pct, 2)
        rs += 30 if spy_drawdown_pct >= 10 else (15 if spy_drawdown_pct >= 5 else 0)

    if vix is not None:
        signals["vix"] = round(vix, 2)
        rs += 30 if vix >= 35 else (20 if vix >= 25 else (8 if vix >= 18 else 0))

    if yield_spread is not None:
        signals["yield_spread"] = round(yield_spread, 3)
        rs += 20 if yield_spread < -0.5 else (10 if yield_spread < 0 else 0)

    if credit_delta is not None:
        signals["credit_delta"] = round(credit_delta, 2)
        rs += 20 if credit_delta < -5 else (10 if credit_delta < -2 else 0)

    if breadth_pct is not None:
        signals["breadth_pct"] = round(breadth_pct, 1)
        rs += 20 if breadth_pct < 30 else (10 if breadth_pct < 50 else 0)

    if rs >= 60:
        regime = "BEAR"
    elif rs >= 30:
        regime = "CAUTION"
    else:
        regime = "BULL"

    return {
        "regime": regime, "risk_score": rs, "signals": signals,
        "cash_floor_pct": CASH_FLOOR_PCT[regime],
        "max_pos_pct": POS_CAP_PCT[regime],
        "new_growth_buys_allowed":   NEW_GROWTH_BUYS_ALLOWED[regime],
        "new_value_buys_allowed":    True,
        "new_dividend_buys_allowed": True,
    }


def score_tailwinds(cluster_6m_returns: dict, spy_6m_pct: float) -> dict:
    """
    cluster_6m_returns: {cluster_name: avg_6m_return_pct}, already
    computed by the caller from whatever price source it's using.
    Active tailwind = outperforms SPY by >=5%. Strong = >=15%.
    """
    active: dict = {}
    for cluster, avg in cluster_6m_returns.items():
        excess = avg - spy_6m_pct
        if excess >= 5:
            active[cluster] = {
                "6m_ret_pct":    round(avg, 1),
                "excess_vs_spy": round(excess, 1),
                "strength":      "STRONG" if excess >= 15 else "MODERATE",
            }
    return active


# ============================================================
# INTRINSIC VALUE ENGINE (with anomaly guard) — unchanged from v3.2/v3.3
# ============================================================
def calculate_intrinsic_value(info: dict) -> dict:
    """
    Method 1 — Graham Number:  sqrt(22.5 * EPS * BVPS)
    Method 2 — FCF Yield:      FCF/share / (discount - terminal_growth)

    Cross-check: GAAP EPS can be badly distorted for insurers and
    conglomerates with large equity holdings (mark-to-market gains flow
    through net income under ASU 2016-01). If the two methods diverge by
    more than GRAHAM_DIVERGENCE_RATIO, the conservative (lower) value is
    used instead of the average.

    Hard guard: any raw margin of safety above MOS_ANOMALY_THRESHOLD is
    flagged as a likely data artifact rather than a real opportunity.
    """
    price  = info.get("currentPrice") or info.get("regularMarketPrice") or 0
    eps    = info.get("trailingEps")   or 0
    bvps   = info.get("bookValue")     or 0
    fcf    = info.get("freeCashflow")  or 0
    shares = info.get("sharesOutstanding") or 1
    rev_g  = float(info.get("revenueGrowth") or 0)

    graham_val = None
    fcf_val    = None

    if eps > 0 and bvps > 0:
        graham_val = (22.5 * eps * bvps) ** 0.5

    if fcf > 0 and shares > 0:
        tg = min(max(rev_g, 0.0), 0.03)
        dr = 0.10
        if dr > tg:
            fcf_val = (fcf / shares) / (dr - tg)

    vals = [v for v in (graham_val, fcf_val) if v is not None]
    if not vals or price <= 0:
        return {"intrinsic_value": 0, "margin_of_safety_pct": 0,
                "raw_margin_of_safety_pct": 0,
                "undervalued": False, "data_anomaly": False,
                "method": "UNAVAILABLE"}

    if graham_val and fcf_val:
        hi, lo = max(graham_val, fcf_val), min(graham_val, fcf_val)
        if lo > 0 and hi / lo > GRAHAM_DIVERGENCE_RATIO:
            fv     = lo  # distrust the optimistic outlier
            method = "DIVERGENT_METHODS_CONSERVATIVE_VALUE_USED"
        else:
            fv     = float(np.mean(vals))
            method = "GRAHAM_FCF_AVERAGE"
    else:
        fv     = float(np.mean(vals))
        method = "SINGLE_METHOD"

    mos_raw    = ((fv - price) / fv) * 100
    is_anomaly = mos_raw > MOS_ANOMALY_THRESHOLD
    mos_scored = min(mos_raw, MOS_SCORING_CAP)

    return {
        "intrinsic_value":          round(fv, 2),
        "current_price":            round(price, 2),
        "margin_of_safety_pct":     round(mos_scored, 1),
        "raw_margin_of_safety_pct": round(mos_raw, 1),
        "undervalued":              (mos_scored >= 15) and not is_anomaly,
        "data_anomaly":             is_anomaly,
        "method":                   method,
    }


# ============================================================
# THREE-BUCKET CANDIDATE SCORERS (pure — caller supplies fetched data)
# ============================================================
def score_growth_candidate(ticker: str, info: dict, daily_close: "pd.Series",
                            active_tailwinds: dict, sector_map: dict,
                            spy_6m_pct: float, weekly_close: "pd.Series" = None) -> dict:
    """
    BUCKET G — quality growth aligned with a confirmed macro tailwind.
    daily_close: Close-price Series, already sliced to data available as
    of the evaluation date (most-recent-last), needs >=200 bars.
    weekly_close: optional weekly-resampled Close Series for the 200-week
    EMA support check; if omitted, that signal is simply skipped.
    Returns the candidate dict if score >= 45, else None.
    """
    if daily_close is None or len(daily_close) < 200:
        return None

    c = daily_close
    p = float(c.iloc[-1])
    roe = float(info.get("returnOnEquity")   or 0)
    opc = float(info.get("operatingCashflow") or 0)
    rg  = float(info.get("revenueGrowth")    or 0)

    if roe < 0.10 or opc <= 0 or rg < 0.10:
        return None

    sector = info.get("sector", "")
    active_clusters = set(active_tailwinds.keys())
    aligned = [cl for cl in sector_map.get(sector, []) if cl in active_clusters]
    if not aligned:
        return None

    ts     = max(active_tailwinds[cl]["excess_vs_spy"] for cl in aligned)
    sma200 = float(c.iloc[-200:].mean())
    rs6m   = ((p / float(c.iloc[-126]) - 1) * 100 if len(c) >= 126 else 0) - spy_6m_pct

    mktcap = float(info.get("marketCap")    or 0)
    fcf    = float(info.get("freeCashflow")  or 0)
    fcy    = (fcf / mktcap) if mktcap > 0 else 0
    peg    = float(info.get("pegRatio")      or 0)
    gm     = float(info.get("grossMargins")  or 0)

    ema200w = None
    if weekly_close is not None and len(weekly_close) >= 200:
        ema200w = float(weekly_close.dropna().ewm(span=200, adjust=False).mean().iloc[-1])

    sc = 0; tags = []

    if   ts >= 15: sc += 30; tags.append("STRONG_TAILWIND")
    elif ts >=  5: sc += 15; tags.append("MOD_TAILWIND")

    if ema200w and 0.98 <= p / ema200w <= 1.04:
        sc += 25; tags.append("200W_EMA_SUPPORT")
    elif p > sma200:
        sc += 10; tags.append("ABOVE_200D_SMA")

    if   rg >= 0.30: sc += 20; tags.append("HYPERGROWTH_30PCT")
    elif rg >= 0.20: sc += 12; tags.append("HIGH_GROWTH_20PCT")
    elif rg >= 0.10: sc +=  6

    if   roe >= 0.25: sc += 10; tags.append("HIGH_ROE")
    elif roe >= 0.15: sc +=  5
    if   fcy >= 0.03: sc +=  8; tags.append("FCF_POSITIVE")
    if   gm  >= 0.50: sc +=  7; tags.append("HIGH_MARGINS")

    if   rs6m >= 10: sc += 10; tags.append("STRONG_RS_6M")
    elif rs6m >=  0: sc +=  5

    if 0 < peg <= 1.5: sc += 5; tags.append("GOOD_PEG")

    if sc < 45:
        return None

    return {
        "bucket": BUCKET_G, "price": round(p, 2), "score": sc,
        "tags": " | ".join(tags), "sector": sector, "tailwinds": aligned,
        "tailwind_excess": round(ts, 1), "rev_growth_pct": round(rg * 100, 1),
        "roe_pct": round(roe * 100, 1), "fcf_yield_pct": round(fcy * 100, 2),
        "rs_6m": round(rs6m, 1), "ema200w": round(ema200w, 2) if ema200w else "N/A",
    }


def score_value_candidate(ticker: str, info: dict) -> dict:
    """
    BUCKET V — quality businesses trading below intrinsic value.
    Requires >=15% margin of safety (anomaly-checked). Min market cap $5B.
    Returns the candidate dict if score >= 35, else None.
    """
    p      = float(info.get("currentPrice") or info.get("regularMarketPrice") or 0)
    roe    = float(info.get("returnOnEquity")    or 0)
    opc    = float(info.get("operatingCashflow") or 0)
    mktcap = float(info.get("marketCap")         or 0)
    fcf    = float(info.get("freeCashflow")      or 0)
    de     = float(info.get("debtToEquity")      or 0)

    if p <= 0 or roe < 0.08 or opc <= 0 or de > 200:
        return None
    if mktcap < 5_000_000_000:
        return None

    iv = calculate_intrinsic_value(info)
    if iv.get("data_anomaly") or not iv["undervalued"]:
        return None

    dy  = normalize_dividend_yield(info)
    fcy = (fcf / mktcap) if mktcap > 0 else 0
    pb  = float(info.get("priceToBook") or 99)
    fpe = float(info.get("forwardPE")   or  0)

    sc = 0; tags = []
    mos = iv["margin_of_safety_pct"]

    if   mos >= 40: sc += 40; tags.append(f"DEEP_VALUE_MOS{int(mos)}PCT")
    elif mos >= 25: sc += 25; tags.append(f"GOOD_VALUE_MOS{int(mos)}PCT")
    elif mos >= 15: sc += 15; tags.append(f"VALUE_MOS{int(mos)}PCT")

    if   roe >= 0.20: sc += 15; tags.append("HIGH_ROE")
    elif roe >= 0.12: sc +=  8

    if   fcy >= 0.06: sc += 12; tags.append("HIGH_FCF_YIELD")
    elif fcy >= 0.04: sc +=  7

    if pb < 2.0 and roe > 0.12:
        sc += 10; tags.append("LOW_PB_HIGH_ROE")

    if   0 < fpe < 15: sc +=  8; tags.append("LOW_FWD_PE")
    elif 0 < fpe < 20: sc +=  4

    if dy >= 0.02: sc += 5; tags.append("DIV_BONUS")

    if sc < 35:
        return None

    return {
        "bucket": BUCKET_V, "price": round(p, 2), "score": sc, "tags": " | ".join(tags),
        "sector": info.get("sector", "Unknown"), "intrinsic_value": iv["intrinsic_value"],
        "margin_of_safety_pct": mos, "iv_method": iv["method"], "roe_pct": round(roe * 100, 1),
        "fcf_yield_pct": round(fcy * 100, 2), "pb_ratio": round(pb, 2),
        "forward_pe": round(fpe, 1) if fpe else "N/A", "div_yield_pct": round(dy * 100, 2),
    }


def score_dividend_candidate(ticker: str, info: dict, dividend_history: "pd.Series" = None) -> dict:
    """
    BUCKET D — income-generating quality dividend payers.
    Requirements: yield >=2%, payout <=65%, FCF covers dividend.
    dividend_history: optional Series of per-payment dividend amounts,
    DatetimeIndex, already sliced to data available as of the evaluation
    date. If omitted, the dividend-growth bonus is simply skipped.
    Returns the candidate dict if score >= 30, else None.
    """
    dy  = normalize_dividend_yield(info)
    pr  = float(info.get("payoutRatio")       or 1)
    fcf = float(info.get("freeCashflow")      or 0)
    dp  = float(info.get("dividendsPaid")     or 0)
    opc = float(info.get("operatingCashflow") or 0)
    roe = float(info.get("returnOnEquity")    or 0)

    if dy < 0.02 or pr > 0.65 or pr <= 0 or opc <= 0:
        return None

    cov = fcf / abs(dp) if dp != 0 else 0
    if cov < 1.0 and dp != 0:
        return None

    sc = 0; tags = []

    if   dy >= 0.050: sc += 25; tags.append(f"HIGH_YIELD_{round(dy*100,1)}PCT")
    elif dy >= 0.035: sc += 15; tags.append(f"SOLID_YIELD_{round(dy*100,1)}PCT")
    elif dy >= 0.020: sc +=  8

    if   pr < 0.35: sc += 15; tags.append("CONSERVATIVE_PAYOUT")
    elif pr < 0.50: sc +=  8

    if   cov >= 2.0: sc += 15; tags.append("STRONG_FCF_COV")
    elif cov >= 1.5: sc +=  8

    if   roe >= 0.20: sc += 10; tags.append("HIGH_ROE")
    elif roe >= 0.12: sc +=  5

    if dividend_history is not None and len(dividend_history) > 0:
        dh = dividend_history
        yrs = (dh.index[-1] - dh.index[0]).days / 365
        if yrs >= 5:
            f4 = dh.iloc[:4].mean()
            l4 = dh.iloc[-4:].mean()
            dg = (l4 / f4 - 1) / yrs if f4 > 0 else 0
            if   dg >= 0.05: sc += 15; tags.append("GROWING_DIV_5PCT_PA")
            elif dg >= 0.00: sc +=  5

    if sc < 30:
        return None

    p = float(info.get("currentPrice") or info.get("regularMarketPrice") or 0)
    return {
        "bucket": BUCKET_D, "price": round(p, 2), "score": sc, "tags": " | ".join(tags),
        "sector": info.get("sector", "Unknown"), "div_yield_pct": round(dy * 100, 2),
        "payout_ratio_pct": round(pr * 100, 1), "fcf_coverage": round(cov, 2),
        "roe_pct": round(roe * 100, 1),
    }


# ============================================================
# POSITION STOP-RULE ENGINE (pure — caller supplies live/PIT inputs)
# ============================================================
def evaluate_position_stop_rule(bucket: str, current_price: float, drawdown_from_peak_pct: float,
                                 pnl_pct: float, age_days: int, info: dict,
                                 dividend_cut_detected: bool = False,
                                 days_to_earnings: "int | None" = None) -> tuple:
    """
    Hard stop rules — evaluated in pure Python, never delegated to an AI
    agent. Returns (status_string, is_mandatory_exit: bool).

    BUCKET G: trailing stop -20% from peak -> MANDATORY_SELL.
              revenue growth <= 0          -> MANDATORY_SELL (thesis broken).
              -13% from peak               -> TRIM_50 warning.
    BUCKET V: price reaches intrinsic value -> TAKE_PROFIT (mandatory).
              trailing stop -15% from peak  -> MANDATORY_SELL.
              -10% from peak                -> TRIM_50 warning.
    BUCKET D: dividend cut detected         -> MANDATORY_SELL.
              payout ratio > 85%            -> REVIEW warning.
              trailing stop -10% from peak  -> MANDATORY_SELL.
    ALL:      pre-earnings AND PnL >= 35%   -> TRIM_25_PRE_EARNINGS
              (profit protection only — never fires when flat or at a loss).
              age < MIN_HOLD_DAYS           -> adds a HOLD note to status
              (does NOT suppress mandatory exits, only discretionary sells).
    """
    dd  = drawdown_from_peak_pct
    pnl = pnl_pct
    cp  = current_price
    rg  = float(info.get("revenueGrowth", 0) or 0)
    pr  = float(info.get("payoutRatio",   0) or 0)

    status  = "HEALTHY"
    is_mand = False

    if bucket == BUCKET_G:
        if dd >= TRAIL_G:
            status  = f"MANDATORY_SELL: G trailing stop (-{round(dd,1)}% from peak)"
            is_mand = True
        elif rg <= 0:
            status  = "MANDATORY_SELL: G revenue growth turned negative"
            is_mand = True
        elif dd >= TRAIL_G * 0.65:
            status = f"TRIM_50: G at -{round(dd,1)}% — approaching stop"

    elif bucket == BUCKET_V:
        iv_data = calculate_intrinsic_value(info)
        iv = 0 if iv_data.get("data_anomaly") else iv_data.get("intrinsic_value", 0)
        if iv > 0 and cp >= iv * 0.95:
            status  = f"TAKE_PROFIT: V price ${cp} reached intrinsic value ${iv}"
            is_mand = True
        elif dd >= TRAIL_V:
            status  = f"MANDATORY_SELL: V trailing stop (-{round(dd,1)}% from peak)"
            is_mand = True
        elif dd >= TRAIL_V * 0.65:
            status = f"TRIM_50: V at -{round(dd,1)}% — approaching stop"

    elif bucket == BUCKET_D:
        if dividend_cut_detected:
            status  = "MANDATORY_SELL: D dividend cut — income thesis broken"
            is_mand = True
        if not is_mand:
            if pr > 0.85:
                status = "REVIEW: D payout ratio >85% — dividend at risk"
            elif dd >= TRAIL_D:
                status  = f"MANDATORY_SELL: D trailing stop (-{round(dd,1)}% from peak)"
                is_mand = True

    if not is_mand and pnl >= EARNINGS_TRIM_PNL_MIN and days_to_earnings is not None:
        if 0 <= days_to_earnings <= 5:
            status = (f"TRIM_25_PRE_EARNINGS: up {round(pnl,1)}% "
                      f"— trim 25% to protect gains before earnings")

    if not is_mand and age_days < MIN_HOLD_DAYS:
        status = status + f" [HOLD: {age_days}/{MIN_HOLD_DAYS}d min]"

    return status, is_mand


# ============================================================
# BUCKET BALANCE CHECK (already pure — unchanged from v3.2/v3.3)
# ============================================================
def check_portfolio_balance(risk_matrix: dict, total_nav: float) -> tuple:
    """
    Compares current bucket weights against targets. Flags buckets that
    have drifted >20% above their target. Returns (actual_pcts, warnings).
    """
    if not risk_matrix or total_nav <= 0:
        return {}, []

    vals = {BUCKET_G: 0.0, BUCKET_V: 0.0, BUCKET_D: 0.0}
    for data in risk_matrix.values():
        b = data.get("bucket", BUCKET_G)
        vals[b] = vals.get(b, 0.0) + data.get("current_value", 0.0)

    targets = {BUCKET_G: MAX_G, BUCKET_V: MAX_V, BUCKET_D: MAX_D}
    actual  = {b: round(v / total_nav * 100, 1) for b, v in vals.items()}
    warnings_list: list = []

    for b, pct in actual.items():
        target_pct = targets[b] * 100
        if pct > target_pct * 1.20:
            warnings_list.append(f"{b} OVERWEIGHT: {pct}% actual vs {target_pct}% target")

    return actual, warnings_list


# ============================================================
# OPPORTUNITY COST ENGINE (already pure — fixed ticker-matching bug)
# ============================================================
def run_opportunity_cost_engine(portfolio: dict, pwer_scores: dict) -> list:
    """
    If an existing position's PWER is more than OPPORTUNITY_COST_PWER_GAP
    points below a replacement candidate's PWER, flag it as a swap.
    Matches tickers by exact value (pos_key.split("_")[0] == ticker), NOT
    by string prefix — v3.2 used `.startswith()`, which misfired whenever
    one ticker was a literal string-prefix of another (e.g. "T" vs
    "TALEN").
    """
    swaps: list = []
    if not pwer_scores or not portfolio.get("positions"):
        return swaps

    held_tickers = {pk.split("_")[0] for pk in portfolio["positions"]}

    for pk, pos in portfolio["positions"].items():
        ticker = pk.split("_")[0]
        ep = pwer_scores.get(ticker, {}).get("pwer_pct", None)
        if ep is None:
            continue

        better = [
            (t, s) for t, s in pwer_scores.items()
            if s.get("pwer_pct", 0) > ep + OPPORTUNITY_COST_PWER_GAP
            and t not in held_tickers
        ]
        better.sort(key=lambda x: x[1]["pwer_pct"], reverse=True)

        if better:
            bt, bs = better[0]
            swaps.append({
                "existing":           ticker,
                "existing_pk":        pk,
                "existing_pwer":      ep,
                "replacement":        bt,
                "replacement_pwer":   bs["pwer_pct"],
                "pwer_improvement":   round(bs["pwer_pct"] - ep, 1),
                "replacement_bucket": bs.get("bucket", BUCKET_G),
                "catalyst":           bs.get("key_catalyst", ""),
            })

    return swaps


# ============================================================
# PART 2 — LIVE SYSTEM
# ============================================================
# Data fetching (yfinance), the AI agents (Gemini), Telegram
# notifications, persistence, and execution. This is the part that
# actually runs when you execute this script.


# ============================================================
# UTILITY HELPERS
# ============================================================
def _strip_fences(text: str) -> str:
    """Remove markdown code fences without triple-backtick literals in source."""
    return re.sub(r"`{3}(?:json)?\s*", "", text).strip()


def _call_gemini(prompt: str, temperature: float = 0.10,
                 thinking_level: str = "high", retries: int = 3) -> str:
    """Call Gemini with exponential back-off on failure."""
    last_err = None
    for attempt in range(retries):
        try:
            r = ai_client.models.generate_content(
                model=GEMINI_MODEL, contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=temperature,
                    thinking_config=types.ThinkingConfig(
                        thinking_level=thinking_level)),
            )
            return r.text.strip()
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                wait = 2 ** attempt
                log_and_print(f"{YLW}[Gemini retry {attempt+1}] waiting {wait}s: {e}{RST}")
                time.sleep(wait)
    raise RuntimeError(f"Gemini failed after {retries} attempts: {last_err}")


def _atr_size(ticker: str, nav: float, max_pct: float,
              risk_pct: float = 0.01) -> float:
    """
    ATR-based position size. Thin live-data wrapper — the actual ATR
    math lives in atr_position_size() so the backtest uses the
    identical formula against point-in-time price history.
    """
    try:
        h = yf.Ticker(ticker).history(period="60d")
    except Exception:
        h = None
    return atr_position_size(h, nav, max_pct, risk_pct)


def merge_candidates_dedup(g: dict, v: dict, d: dict) -> dict:
    """
    Thin wrapper around _merge_candidates_dedup_core() that logs any
    bucket conflicts to agent.log the way v3.2/v3.3 did, and returns
    just the merged dict so the call site in main() doesn't need to
    change.
    """
    merged, conflicts = _merge_candidates_dedup_core(g, v, d)
    for line in conflicts:
        log_and_print(f"{YLW}{line}{RST}")
    return merged


def get_current_sector_exposure(portfolio: dict, risk_matrix: dict,
                                total_nav: float) -> dict:
    """Returns {sector: pct_of_nav} across all currently open positions."""
    ticker_sector: dict = {}
    for pk in portfolio.get("positions", {}):
        ticker = pk.split("_")[0]
        try:
            ticker_sector[ticker] = yf.Ticker(ticker).info.get("sector", "Unknown")
        except Exception:
            ticker_sector[ticker] = "Unknown"
    return compute_sector_exposure(portfolio, risk_matrix, total_nav, ticker_sector)


# ============================================================
# 1. INITIALIZATION
# ============================================================
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY",     "YOUR_GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "YOUR_TELEGRAM_CHAT_ID")
GEMINI_MODEL       = os.getenv("GEMINI_MODEL",        "gemini-3.1-pro-preview")

logging.basicConfig(filename="agent.log", level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")


def log_and_print(msg: str) -> None:
    print(msg)
    logging.info(re.sub(r"\033\[[0-9;]*m", "", msg))


try:
    ai_client = genai.Client(api_key=GEMINI_API_KEY)
except Exception as e:
    print(f"CRITICAL: Gemini init failed: {e}"); sys.exit(1)

GRN = "\033[1;32m"; RED = "\033[1;31m"; CYN = "\033[1;36m"
YLW = "\033[1;33m"; RST = "\033[0m"


# ============================================================
# 2. PORTFOLIO PERSISTENCE
# ============================================================
def load_portfolio() -> dict:
    try:
        with open("portfolio.json") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"cash_balance": 100_000.0, "positions": {}}


def save_portfolio(d: dict) -> None:
    with open("portfolio.json", "w") as f:
        json.dump(d, f, indent=4)


def send_notification(msg: str) -> None:
    if not TELEGRAM_BOT_TOKEN or "YOUR_" in TELEGRAM_BOT_TOKEN:
        return
    url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage"
    try:
        requests.post(url,
                      json={"chat_id": TELEGRAM_CHAT_ID,
                            "text": msg, "parse_mode": "Markdown"},
                      timeout=10)
    except Exception as e:
        log_and_print(f"Telegram: {e}")


# ============================================================
# 3. UNIVERSE  (S&P 500 + Nasdaq 100 + strategic picks)
# ============================================================
def get_macro_universe() -> list:
    tickers: set = set()
    log_and_print(f"{CYN}[Universe] Compiling universe...{RST}")
    hdrs = {"User-Agent": "Mozilla/5.0"}

    try:
        r = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers=hdrs, timeout=15)
        tickers.update(pd.read_html(r.text)[0]["Symbol"].tolist())
    except Exception as e:
        log_and_print(f"Warning S&P500: {e}")

    try:
        r = requests.get("https://en.wikipedia.org/wiki/Nasdaq-100",
                         headers=hdrs, timeout=15)
        tbls = pd.read_html(r.text, match="Ticker|Symbol")
        col  = "Ticker" if "Ticker" in tbls[0].columns else "Symbol"
        tickers.update(tbls[0][col].tolist())
    except Exception as e:
        log_and_print(f"Warning Nasdaq100: {e}")

    strategic = [
        # G: AI & Semiconductors
        "NVDA", "AMD", "AVGO", "QCOM", "MU", "ARM", "ALAB", "MRVL",
        # G: AI Software & Cloud
        "NOW", "CRM", "PLTR", "AXON", "APP", "ZETA", "MSFT", "SNOW",
        # G: Energy / Power infrastructure
        "VST", "CEG", "NRG", "TALEN",
        # G: Defense & Space
        "RKLB", "KTOS", "LMT", "RTX",
        # G: Fintech / Consumer
        "MELI", "NU", "HOOD", "COIN", "SQ",
        # V: Quality compounders at value
        "BRK-B", "JPM", "BAC", "WFC", "UNH", "LLY", "ABBV", "BMY",
        "GOOG", "META", "AMZN", "AAPL", "ADBE", "ORCL",
        # D: Dividend payers
        "JNJ", "PG", "KO", "VZ", "T", "MO", "XOM", "CVX",
        "NEE", "D", "O", "MAIN",
    ]
    tickers.update(strategic)
    cleaned = [str(t).replace(".", "-") for t in tickers if isinstance(t, str)]
    log_and_print(f"{CYN}[Universe] {len(cleaned)} tickers ready.{RST}")
    return list(set(cleaned))


# ============================================================
# 4. MACRO REGIME  (5-factor model)
# ============================================================
def check_market_regime() -> dict:
    log_and_print(f"{CYN}[Regime] Running 5-factor macro assessment...{RST}")

    def _f(ticker: str, period: str) -> pd.DataFrame:
        try:
            return yf.Ticker(ticker).history(period=period)
        except Exception:
            return pd.DataFrame()

    spy  = _f("^GSPC", "60d");  vix  = _f("^VIX", "30d")
    t10  = _f("^TNX",  "5d");   t5   = _f("^FVX", "5d")
    hyg  = _f("HYG",   "30d");  ief  = _f("IEF",  "30d")
    mmth = _f("^MMTH", "10d")

    spy_drawdown_pct = None
    if not spy.empty:
        spy_drawdown_pct = ((spy["High"].max() - spy["Close"].iloc[-1]) /
                             spy["High"].max()) * 100

    vix_level = float(vix["Close"].iloc[-1]) if not vix.empty else None

    yield_spread = None
    if not t10.empty and not t5.empty:
        yield_spread = float(t10["Close"].iloc[-1]) - float(t5["Close"].iloc[-1])

    credit_delta = None
    if not hyg.empty and not ief.empty:
        credit_delta = ((hyg["Close"].iloc[-1] / hyg["Close"].iloc[0] - 1) -
                         (ief["Close"].iloc[-1] / ief["Close"].iloc[0] - 1)) * 100

    breadth_pct = float(mmth["Close"].iloc[-1]) if not mmth.empty else None

    out = score_regime(spy_drawdown_pct=spy_drawdown_pct, vix=vix_level,
                            yield_spread=yield_spread, credit_delta=credit_delta,
                            breadth_pct=breadth_pct)
    log_and_print(f"{CYN}[Regime] {out['regime']} | Score {out['risk_score']}/120 "
                  f"| {out['signals']}{RST}")
    return out


# ============================================================
# 5. MACRO TAILWIND SCANNER
# ============================================================
def scan_macro_tailwinds() -> tuple:
    """
    Measures 6-month performance of 8 thematic ETF clusters vs SPY.
    Active tailwind  = outperforms SPY by >=5%.
    Strong tailwind  = outperforms SPY by >=15%.
    Only stocks aligned with active tailwinds enter Bucket G.
    """
    log_and_print(f"{CYN}[Tailwinds] Scanning 8 thematic clusters...{RST}")

    try:
        spy_h  = yf.Ticker("SPY").history(period="6mo")["Close"]
        spy_6m = (spy_h.iloc[-1] / spy_h.iloc[0] - 1) * 100
    except Exception:
        spy_6m = 0.0

    cluster_6m_returns: dict = {}
    for cluster, etfs in TAILWIND_CLUSTERS.items():
        rets = []
        for etf in etfs:
            try:
                h = yf.Ticker(etf).history(period="6mo")["Close"]
                if not h.empty:
                    rets.append((h.iloc[-1] / h.iloc[0] - 1) * 100)
            except Exception:
                pass
        if rets:
            cluster_6m_returns[cluster] = float(np.mean(rets))

    active = score_tailwinds(cluster_6m_returns, spy_6m)
    log_and_print(f"{CYN}[Tailwinds] Active ({len(active)}): {list(active.keys())}{RST}")
    return active, TAILWIND_SECTOR_MAP


# ============================================================
# 6. INTRINSIC VALUE ENGINE
# ============================================================
# calculate_intrinsic_value() is defined in Part 1 above.


# ============================================================
# 7. EARNINGS LANDMINE GUARD
# ============================================================
def get_earnings_landmines(tickers: list, days_ahead: int = 14) -> dict:
    """Block new buys within 14 days of an earnings event."""
    mines: dict = {}
    for ticker in tickers:
        try:
            cal = yf.Ticker(ticker).calendar
            if cal is None:
                continue
            if hasattr(cal, "index") and "Earnings Date" in cal.index:
                dt = cal.loc["Earnings Date"].iloc[0]
            elif isinstance(cal, dict) and "Earnings Date" in cal:
                raw = cal["Earnings Date"]
                dt  = raw[0] if isinstance(raw, list) else raw
            else:
                continue
            if pd.notna(dt):
                d = (pd.Timestamp(dt) - pd.Timestamp.now()).days
                if -5 <= d <= days_ahead:
                    mines[ticker] = {"date": str(pd.Timestamp(dt).date()), "days": d}
        except Exception:
            continue
    if mines:
        log_and_print(f"{YLW}[Earnings] {len(mines)} blocked: {list(mines.keys())}{RST}")
    return mines


# ============================================================
# 8. THREE-BUCKET SCREENERS
# ============================================================
def run_growth_screener(tickers: list, active_tailwinds: dict,
                        sector_map: dict) -> dict:
    """
    BUCKET G — Quality growth stocks aligned with confirmed macro tailwinds.
    Every qualifying stock must map to at least one active tailwind cluster.
    Minimum score to qualify: 45.
    """
    log_and_print(f"{CYN}[G] Growth screener...{RST}")
    candidates: dict = {}

    try:
        spy   = yf.Ticker("SPY").history(period="1y")["Close"]
        spy6m = (spy.iloc[-1] / spy.iloc[-126] - 1) * 100 if len(spy) >= 126 else 0.0
    except Exception:
        spy6m = 0.0

    for ticker in tickers:
        try:
            t    = yf.Ticker(ticker)
            hd   = t.history(period="1y")
            info = t.info
            if hd.empty or len(hd) < 200:
                continue

            hw = t.history(period="5y", interval="1wk")
            weekly_close = hw["Close"] if len(hw) >= 200 else None

            cand = score_growth_candidate(
                ticker, info, hd["Close"], active_tailwinds, sector_map,
                spy6m, weekly_close=weekly_close)
            if cand:
                candidates[ticker] = cand
            time.sleep(0.04)
        except Exception:
            continue

    log_and_print(f"{CYN}[G] {len(candidates)} qualified.{RST}")
    return dict(sorted(candidates.items(),
                       key=lambda x: x[1]["score"], reverse=True)[:12])


def run_value_screener(tickers: list) -> dict:
    """
    BUCKET V — Quality businesses trading below intrinsic value.
    Works in ALL market conditions; powerful in downturns.
    Requires >=15% margin of safety (anomaly-checked). Min market cap $5B.
    """
    log_and_print(f"{CYN}[V] Value screener...{RST}")
    candidates: dict = {}

    for ticker in tickers:
        try:
            t    = yf.Ticker(ticker)
            info = t.info
            hd   = t.history(period="1y")
            if hd.empty or len(hd) < 100:
                continue

            # Anomaly logging needs log_and_print, so it stays here even
            # though score_value_candidate independently re-checks
            # (and gates on) the same anomaly flag internally.
            iv_check = calculate_intrinsic_value(info)
            if iv_check.get("data_anomaly"):
                log_and_print(
                    f"{YLW}[V] {ticker} excluded — implausible margin of "
                    f"safety ({iv_check.get('raw_margin_of_safety_pct')}%), "
                    f"likely distorted EPS data.{RST}")
                continue

            dy_raw = info.get("dividendYield")
            if dy_raw is not None and float(dy_raw) > 1.0:
                dy_for_log = normalize_dividend_yield(info)
                log_and_print(
                    f"{YLW}[Normalize] {ticker} dividendYield {dy_raw} "
                    f"-> {round(dy_for_log*100,2)}%{RST}")

            cand = score_value_candidate(ticker, info)
            if cand:
                candidates[ticker] = cand
            time.sleep(0.04)
        except Exception:
            continue

    log_and_print(f"{CYN}[V] {len(candidates)} qualified.{RST}")
    return dict(sorted(candidates.items(),
                       key=lambda x: x[1]["score"], reverse=True)[:10])


def run_dividend_screener(tickers: list) -> dict:
    """
    BUCKET D — Income-generating quality dividend payers.
    Portfolio buffer: reduces volatility, provides income in flat/down markets.
    Requirements: yield >=2%, payout <=65%, FCF covers dividend.
    """
    log_and_print(f"{CYN}[D] Dividend screener...{RST}")
    candidates: dict = {}

    for ticker in tickers:
        try:
            t    = yf.Ticker(ticker)
            info = t.info

            dy_raw = info.get("dividendYield")
            if dy_raw is not None and float(dy_raw) > 1.0:
                dy_for_log = normalize_dividend_yield(info)
                log_and_print(
                    f"{YLW}[Normalize] {ticker} dividendYield {dy_raw} "
                    f"-> {round(dy_for_log*100,2)}%{RST}")

            try:
                dh = t.dividends
            except Exception:
                dh = None
            dividend_history = dh if dh is not None and len(dh) > 0 else None

            cand = score_dividend_candidate(ticker, info, dividend_history)
            if cand:
                candidates[ticker] = cand
            time.sleep(0.04)
        except Exception:
            continue

    log_and_print(f"{CYN}[D] {len(candidates)} qualified.{RST}")
    return dict(sorted(candidates.items(),
                       key=lambda x: x[1]["score"], reverse=True)[:6])


# ============================================================
# 9. PWER ENGINE  (chunked + Python-verified)
# ============================================================
_PWER_FORMAT = (
    '{"pwer_scores":['
    '{"ticker":"X","bucket":"GROWTH",'
    '"bull_ret_pct":40,"bull_prob":0.25,'
    '"base_ret_pct":15,"base_prob":0.50,'
    '"bear_ret_pct":-20,"bear_prob":0.25,'
    '"pwer_pct":11.25,'
    '"key_catalyst":"one sentence",'
    '"key_risk":"one sentence",'
    '"conviction":"HIGH|MEDIUM|LOW"}'
    "]}"
)


def calculate_pwer_batch(all_candidates: dict, regime: dict) -> dict:
    """
    Probability-Weighted Expected Return = sum(Return x Probability).
    Batches candidates in chunks of PWER_CHUNK to avoid token-limit failures.
    After the AI responds, Python verifies and recalculates every PWER figure.
    """
    if not all_candidates:
        return {}

    n = len(all_candidates)
    log_and_print(f"{YLW}[PWER] Scoring {n} candidates in chunks of {PWER_CHUNK}...{RST}")

    tickers     = list(all_candidates.keys())
    all_scores: dict = {}

    for i in range(0, len(tickers), PWER_CHUNK):
        chunk_t = tickers[i:i + PWER_CHUNK]
        chunk   = {t: all_candidates[t] for t in chunk_t}

        prompt = (
            "You are a top-tier equity research analyst.\n"
            "MACRO REGIME: " + regime.get("regime", "BULL") + "\n\n"
            "Assign realistic 12-month scenarios for each stock below.\n"
            "Use specific numbers. Probabilities MUST sum to 1.0 per stock.\n\n"
            "CANDIDATES:\n" + json.dumps(chunk, indent=2) + "\n\n"
            "Output ONLY valid JSON (no markdown fences):\n" +
            _PWER_FORMAT
        )

        try:
            raw  = _call_gemini(prompt, temperature=0.10, thinking_level="high")
            raw  = _strip_fences(raw)
            data = json.loads(raw)
            for s in data.get("pwer_scores", []):
                t = s.get("ticker", "")
                if t:
                    all_scores[t] = verify_pwer(s)
        except Exception as e:
            log_and_print(f"{RED}[PWER chunk {i//PWER_CHUNK+1} error] {e}{RST}")

        if i + PWER_CHUNK < len(tickers):
            time.sleep(1.5)

    log_and_print(f"{GRN}[PWER] Scored {len(all_scores)}/{n} candidates.{RST}")
    return all_scores


# ============================================================
# 10. POSITION MONITOR  (bucket-specific rules)
# ============================================================
def evaluate_open_positions(portfolio: dict) -> tuple:
    """
    Hard stop rules evaluated HERE in Python — mandatory exits never reach the AI.

    BUCKET G:
      - Trailing stop -20% from peak       -> MANDATORY_SELL
      - Revenue growth <= 0                -> MANDATORY_SELL (thesis broken)
      - At -13% from peak                  -> TRIM_50 warning

    BUCKET V:
      - Price reaches intrinsic value      -> TAKE_PROFIT (mandatory)
      - Trailing stop -15% from peak       -> MANDATORY_SELL
      - At -10% from peak                  -> TRIM_50 warning

    BUCKET D:
      - Dividend cut detected              -> MANDATORY_SELL
      - Payout ratio > 85%                 -> REVIEW warning
      - Trailing stop -10% from peak       -> MANDATORY_SELL

    ALL BUCKETS:
      - Pre-earnings AND PnL >= 35%        -> TRIM_25_PRE_EARNINGS
        (profit protection only — never fires when flat or at a loss)
      - Age < MIN_HOLD_DAYS                -> adds a HOLD note to status
        (does NOT suppress mandatory exits, only discretionary AI sells)
    """
    risk_matrix:     dict = {}
    mandatory_exits: list = []
    positions_value       = 0.0
    temp_prices:     dict = {}

    log_and_print(f"{YLW}[Monitor] Evaluating positions (bucket-specific rules)...{RST}")

    for pk, pos in portfolio.get("positions", {}).items():
        ticker = pk.split("_")[0]
        try:
            cp = float(yf.Ticker(ticker).history(period="5d")["Close"].iloc[-1])
        except Exception:
            cp = pos["entry_price"]
        temp_prices[pk]  = cp
        positions_value += pos["shares"] * cp

    total_nav = portfolio["cash_balance"] + positions_value

    for pk, pos in portfolio.get("positions", {}).items():
        ticker = pk.split("_")[0]
        bucket = _normalize_bucket(pos.get("strategy_bucket", BUCKET_G))
        cp     = temp_prices.get(pk, pos["entry_price"])
        ep     = pos["entry_price"]
        pnl    = ((cp - ep) / ep) * 100 if ep > 0 else 0
        wt     = (pos["shares"] * cp) / total_nav if total_nav > 0 else 0

        hwm = float(pos.get("high_watermark", ep))
        if cp > hwm:
            hwm = cp
        portfolio["positions"][pk]["high_watermark"] = hwm
        dd = ((hwm - cp) / hwm) * 100 if hwm > 0 else 0

        try:
            purchase_dt = datetime.strptime(
                pos.get("purchase_date", "2020-01-01"), "%Y-%m-%d"
            ).replace(tzinfo=ZoneInfo("America/New_York"))
            age_days = (datetime.now(ZoneInfo("America/New_York")) - purchase_dt).days
        except Exception:
            age_days = 999

        status   = "HEALTHY"
        is_mand  = False

        try:
            info = yf.Ticker(ticker).info

            dividend_cut_detected = False
            if bucket == BUCKET_D:
                try:
                    dh = yf.Ticker(ticker).dividends
                    if len(dh) >= 2 and dh.iloc[-1] < dh.iloc[-2] * 0.90:
                        dividend_cut_detected = True
                except Exception:
                    pass

            days_to_earn = None
            if pnl >= EARNINGS_TRIM_PNL_MIN:
                try:
                    cal = yf.Ticker(ticker).calendar
                    dt  = None
                    if cal is not None:
                        if hasattr(cal, "index") and "Earnings Date" in cal.index:
                            dt = cal.loc["Earnings Date"].iloc[0]
                        elif isinstance(cal, dict) and "Earnings Date" in cal:
                            raw_dt = cal["Earnings Date"]
                            dt = raw_dt[0] if isinstance(raw_dt, list) else raw_dt
                    if dt is not None and pd.notna(dt):
                        days_to_earn = (pd.Timestamp(dt) - pd.Timestamp.now()).days
                except Exception:
                    pass

            status, is_mand = evaluate_position_stop_rule(
                bucket=bucket, current_price=cp, drawdown_from_peak_pct=dd,
                pnl_pct=pnl, age_days=age_days, info=info,
                dividend_cut_detected=dividend_cut_detected,
                days_to_earnings=days_to_earn,
            )

        except Exception as e:
            log_and_print(f"Warning monitor {ticker}: {e}")
            status = "DATA_UNAVAILABLE"

        if is_mand:
            mandatory_exits.append({
                "pos_key": pk, "ticker": ticker, "bucket": bucket,
                "status": status, "current_price": cp,
            })

        risk_matrix[pk] = {
            "ticker":            ticker,
            "bucket":            bucket,
            "current_price":     round(cp, 2),
            "current_value":     round(pos["shares"] * cp, 2),
            "weight_pct":        round(wt * 100, 2),
            "pnl_pct":           round(pnl, 2),
            "dd_from_peak_pct":  round(dd, 2),
            "age_days":          age_days,
            "status":            status,
            "is_mandatory_exit": is_mand,
        }

    save_portfolio(portfolio)   # persist updated high-watermarks
    return risk_matrix, total_nav, mandatory_exits


# ============================================================
# 11. OPPORTUNITY COST ENGINE
# ============================================================
# run_opportunity_cost_engine() is defined in Part 1 above.


# ============================================================
# 12. BUCKET BALANCE CHECK
# ============================================================
# check_portfolio_balance() is defined in Part 1 above.


# ============================================================
# 13. MULTI-AGENT AI PIPELINE
# ============================================================
_MACRO_FORMAT = (
    '{"positioning_bias":"AGGRESSIVE|NEUTRAL|DEFENSIVE",'
    '"strongest_tailwinds":["AI_SEMICONDUCTORS"],'
    '"sectors_to_avoid":["Real Estate"],'
    '"value_buying_env":"YES|NO",'
    '"increase_dividend_alloc":"YES|NO"}'
)

_ORDER_FORMAT = (
    '{"orders":['
    '{"action":"BUY|SELL","ticker":"X",'
    '"strategy_bucket":"GROWTH|VALUE|DIVIDEND",'
    '"transaction_value_usd":5000,'
    '"analysis_display":"rationale — cite PWER score"}'
    "]}"
)


def query_macro_agent(regime: dict, risk_matrix: dict,
                      active_tailwinds: dict) -> tuple:
    """AGENT 1 — Macro brief and sector rotation directives."""
    prompt = (
        "You are the CIO of a top global macro hedge fund "
        "running a GVD portfolio (Growth, Value, Dividend).\n\n"
        "REGIME:\n" + json.dumps(regime, indent=2) + "\n\n"
        "ACTIVE TAILWINDS:\n" + json.dumps(active_tailwinds, indent=2) + "\n\n"
        "PORTFOLIO RISK MATRIX:\n" + json.dumps(risk_matrix, indent=2) + "\n\n"
        "Write a 3-paragraph macro brief covering: regime characterisation, "
        "sector rotation, and key tail risks for the next 30 days.\n\n"
        "Then output your directives under exactly this heading:\n"
        "JSON_DIRECTIVES:\n" + _MACRO_FORMAT
    )
    try:
        raw  = _call_gemini(prompt, temperature=0.10, thinking_level="high")
        body = raw.split("JSON_DIRECTIVES:")[0].strip() if "JSON_DIRECTIVES:" in raw else raw
        jstr = (_strip_fences(raw.split("JSON_DIRECTIVES:")[1])
                if "JSON_DIRECTIVES:" in raw else "{}")
        d = json.loads(jstr)
        log_and_print(f"{CYN}[Macro] Bias: {d.get('positioning_bias')}{RST}")
        send_notification("*MACRO BRIEF*\n\n" + body[:800])
        return d, body
    except Exception as e:
        log_and_print(f"{RED}[Macro Agent error] {e}{RST}")
        return {
            "positioning_bias": "NEUTRAL",
            "sectors_to_avoid": [],
            "value_buying_env": "YES",
            "increase_dividend_alloc": "NO",
        }, ""


def query_portfolio_constructor(
    g: dict, v: dict, d: dict,
    pwer: dict, macro: dict, regime: dict,
    portfolio: dict, nav: float,
    mines: dict, swaps: list,
    balance_warnings: list,
) -> list:
    """
    AGENT 2 — Selects new buys and opportunity-cost swaps.
    Cannot override mandatory exits (those already executed in Python).
    Every buy order must cite the PWER score in analysis_display.
    """
    avoid = set(macro.get("sectors_to_avoid", []))
    bias  = macro.get("positioning_bias", "NEUTRAL")

    def filt(cands):
        return {k: c for k, c in cands.items()
                if k not in mines and c.get("sector", "") not in avoid}

    sg = filt(g) if regime.get("new_growth_buys_allowed") else {}
    sv = filt(v)
    sd = filt(d)

    def add_pwer(cands):
        for t in cands:
            if t in pwer:
                cands[t]["pwer_pct"]   = pwer[t].get("pwer_pct", 0)
                cands[t]["catalyst"]   = pwer[t].get("key_catalyst", "")
                cands[t]["risk"]       = pwer[t].get("key_risk", "")
                cands[t]["conviction"] = pwer[t].get("conviction", "MEDIUM")
            atr_val = _atr_size(
                t, nav,
                regime["max_pos_pct"].get(_normalize_bucket(
                    cands[t].get("bucket", BUCKET_G)), 0.10)
            )
            cands[t]["atr_recommended_size_usd"] = round(atr_val, 2)
        return cands

    sg = add_pwer(sg); sv = add_pwer(sv); sd = add_pwer(sd)
    slots = max(0, TARGET_POSITIONS - len(portfolio.get("positions", {})))

    prompt = (
        "You are a Portfolio Constructor managing a GVD fund.\n"
        "Allocation targets: Growth <=50% NAV | Value <=35% | Dividend <=15%.\n"
        "Maximum 15 total positions. Every slot is earned by PWER ranking.\n\n"
        "MACRO BIAS: " + bias + " | REGIME: " + regime.get("regime","BULL") + "\n"
        "PORTFOLIO NAV: $" + str(round(nav, 2)) + "\n"
        "AVAILABLE CASH: $" + str(round(portfolio["cash_balance"], 2)) + "\n"
        "OPEN SLOTS: " + str(slots) + "\n"
        "EXISTING POSITIONS: " + json.dumps(list(portfolio.get("positions", {}).keys())) + "\n"
        "EARNINGS BLOCKED: " + json.dumps(list(mines.keys())) + "\n"
        "BUCKET BALANCE WARNINGS: " + json.dumps(balance_warnings) + "\n\n"
        "GROWTH CANDIDATES (use atr_recommended_size_usd for sizing):\n"
        + json.dumps(sg, indent=2) + "\n\n"
        "VALUE CANDIDATES (intrinsic value discounts):\n"
        + json.dumps(sv, indent=2) + "\n\n"
        "DIVIDEND CANDIDATES (portfolio stability):\n"
        + json.dumps(sd, indent=2) + "\n\n"
        "OPPORTUNITY COST SWAPS (>15 point PWER improvement available):\n"
        + json.dumps(swaps, indent=2) + "\n\n"
        "Rules:\n"
        "1. Select 2-4 buys across G/V/D. In BEAR regime: no G, lean V+D.\n"
        "2. For confirmed swaps: issue SELL for old + BUY for replacement.\n"
        "3. ALWAYS cite the PWER score in analysis_display.\n"
        "4. Use atr_recommended_size_usd as default position size.\n"
        "5. Do NOT suggest selling positions flagged as HOLD (min age).\n\n"
        "Output ONLY valid JSON (no markdown fences):\n" + _ORDER_FORMAT
    )
    try:
        raw  = _call_gemini(prompt, temperature=0.12, thinking_level="medium")
        raw  = _strip_fences(raw)
        data = json.loads(raw)
        return data.get("orders", [])
    except Exception as e:
        log_and_print(f"{RED}[Constructor error] {e}{RST}")
        return []


# ============================================================
# 14. EXECUTION ENGINE
# ============================================================
def execute_mandatory_exits(mandatory_exits: list, portfolio: dict) -> None:
    """Hard stop exits — Python-enforced, zero AI discretion."""
    for item in mandatory_exits:
        pk     = item["pos_key"]
        ticker = item["ticker"]
        bucket = item.get("bucket", "?")
        cp     = item["current_price"]
        status = item["status"]

        if pk not in portfolio["positions"]:
            continue

        pos = portfolio["positions"][pk]
        rec = pos["shares"] * cp
        portfolio["cash_balance"] += rec
        del portfolio["positions"][pk]

        msg = (f"MANDATORY EXIT [{bucket}]: {ticker}\n"
               f"Rule: {status}\n"
               f"Reclaimed: ${round(rec, 2)}")
        log_and_print(f"{RED}{msg}{RST}")
        send_notification(msg)

    save_portfolio(portfolio)


def execute_agent_orders(orders: list, portfolio: dict, all_candidates: dict,
                         risk_matrix: dict, total_nav: float,
                         regime: dict) -> None:
    """
    Execute agent BUY and SELL orders with ATR-aware position sizing,
    bucket caps, AND a sector concentration cap (35% NAV per sector).
    """
    if not orders:
        log_and_print(f"{YLW}[Exec] No agent orders.{RST}")
        return

    FLOOR = total_nav * regime.get("cash_floor_pct", 0.04)
    sector_exposure = get_current_sector_exposure(portfolio, risk_matrix, total_nav)

    for o in orders:
        action = o.get("action", "").upper()
        ticker = o.get("ticker", "").upper()
        bucket = _normalize_bucket(o.get("strategy_bucket", BUCKET_G))
        val    = float(o.get("transaction_value_usd", 0))
        rat    = o.get("analysis_display", "")

        if action == "SELL":
            keys = [pk for pk in portfolio["positions"] if pk.split("_")[0] == ticker]
            for pk in list(keys):
                pos = portfolio["positions"].get(pk)
                if not pos:
                    continue
                try:
                    purchase_dt = datetime.strptime(
                        pos.get("purchase_date", "2020-01-01"), "%Y-%m-%d"
                    ).replace(tzinfo=ZoneInfo("America/New_York"))
                    age_days = (datetime.now(ZoneInfo("America/New_York"))
                                - purchase_dt).days
                    if age_days < MIN_HOLD_DAYS:
                        log_and_print(
                            f"{YLW}[Exec] Skip sell {ticker}: "
                            f"only {age_days}d old (min {MIN_HOLD_DAYS}d){RST}")
                        continue
                except Exception:
                    pass

                cp  = risk_matrix.get(pk, {}).get("current_price", pos["entry_price"])
                rec = pos["shares"] * cp
                portfolio["cash_balance"] += rec
                del portfolio["positions"][pk]
                msg = (f"AGENT SELL [{bucket}]: {ticker} | "
                       f"${round(rec, 2)} reclaimed | {rat}")
                log_and_print(f"{RED}{msg}{RST}")
                send_notification(msg)

        elif action == "BUY":
            cand = all_candidates.get(ticker)
            if not cand:
                log_and_print(f"{YLW}[Exec] {ticker} not in screened candidates — skip.{RST}")
                continue

            avail = portfolio["cash_balance"] - FLOOR
            if avail <= 0:
                log_and_print(f"{YLW}[Exec] Cash floor reached — skip {ticker}.{RST}")
                continue
            val = min(val, avail)

            mp = total_nav * regime["max_pos_pct"].get(bucket, 0.10)
            exist_bucket = sum(p["allocated_capital"]
                               for pk, p in portfolio["positions"].items()
                               if pk.split("_")[0] == ticker)
            val = min(val, max(0.0, mp - exist_bucket))

            cand_sector        = cand.get("sector", "Unknown")
            current_sector_pct = sector_exposure.get(cand_sector, 0.0)
            room_pct           = max(0.0, MAX_SECTOR_PCT - current_sector_pct)
            val = min(val, total_nav * room_pct / 100.0)

            if val < 500:
                log_and_print(
                    f"{YLW}[Exec] {ticker} blocked — sector '{cand_sector}' "
                    f"at/near {MAX_SECTOR_PCT}% cap, or size too small.{RST}")
                continue

            cp = cand.get("price", 0)
            if cp <= 0:
                continue

            sh      = val / cp
            pos_key = f"{ticker}_{bucket}"
            portfolio["cash_balance"] -= val

            if pos_key in portfolio["positions"]:
                old = portfolio["positions"][pos_key]
                nc  = old["allocated_capital"] + val
                ns  = old["shares"] + sh
                portfolio["positions"][pos_key].update({
                    "shares":            ns,
                    "allocated_capital": nc,
                    "entry_price":       nc / ns,
                    "high_watermark":    max(float(old.get("high_watermark", 0)), cp),
                })
            else:
                portfolio["positions"][pos_key] = {
                    "entry_price":       cp,
                    "shares":            sh,
                    "allocated_capital": val,
                    "strategy_bucket":   bucket,
                    "purchase_date":     datetime.now(
                        ZoneInfo("America/New_York")).strftime("%Y-%m-%d"),
                    "buying_thesis":     rat,
                    "high_watermark":    cp,
                }

            sector_exposure[cand_sector] = current_sector_pct + (val / total_nav * 100)

            pwer_str = (f" | PWER:{cand.get('pwer_pct','?')}%"
                        if "pwer_pct" in cand else "")
            msg = (f"BUY [{bucket}]: {ticker} | "
                   f"${round(val,2)} @ ${round(cp,2)}{pwer_str}\n"
                   f"Tags: {cand.get('tags','')}\n{rat}")
            log_and_print(f"{GRN}{msg}{RST}")
            send_notification(msg)

    save_portfolio(portfolio)


# ============================================================
# 15. PERFORMANCE ATTRIBUTION
# ============================================================
def calculate_performance_metrics(portfolio: dict,
                                  benchmark: str = "SPY") -> dict:
    """Sharpe, Sortino, Alpha (Jensen), Beta, Max DD, Calmar — vs SPY."""
    if not portfolio.get("positions"):
        return {}
    try:
        start = min(
            pd.Timestamp(p.get("purchase_date", "2024-01-01"))
            for p in portfolio["positions"].values()
        )
        tickers = list(set(pk.split("_")[0] for pk in portfolio["positions"]))
        if not tickers:
            return {}

        px = yf.download(
            tickers + [benchmark], start=start,
            auto_adjust=True, progress=False
        )["Close"]

        if isinstance(px, pd.Series):
            px = px.to_frame(name=tickers[0])
        if benchmark not in px.columns:
            return {}

        bench   = px[benchmark].pct_change().dropna()
        b_total = (px[benchmark].iloc[-1] / px[benchmark].iloc[0] - 1) * 100

        tc = sum(p["allocated_capital"] for p in portfolio["positions"].values())
        if tc <= 0:
            return {}

        w: dict = {}
        for pk, p in portfolio["positions"].items():
            t = pk.split("_")[0]
            w[t] = w.get(t, 0) + p["allocated_capital"] / tc

        valid = [t for t in tickers if t in px.columns]
        pr    = pd.Series(0.0, index=px.index)
        for t in valid:
            if t in w:
                pr = pr + px[t].pct_change() * w[t]

        al = pd.concat([pr, bench], axis=1, join="inner").dropna()
        al.columns = ["p", "b"]
        if len(al) < 5:
            return {}

        RF  = 0.045;  ANN = 252
        mu  = al["p"].mean() * ANN
        sig = al["p"].std()  * ANN ** 0.5
        sh  = (mu - RF) / sig if sig > 0 else 0

        dn  = al["p"][al["p"] < RF / ANN]
        sdn = dn.std() * ANN ** 0.5 if len(dn) > 0 else sig
        so  = (mu - RF) / sdn if sdn > 0 else 0

        cov  = np.cov(al["p"], al["b"])[0, 1]
        beta = cov / al["b"].var() if al["b"].var() > 0 else 1.0
        mu_b = al["b"].mean() * ANN
        alph = mu - (RF + beta * (mu_b - RF))

        cum  = (1 + al["p"]).cumprod()
        mdd  = float(((cum - cum.cummax()) / cum.cummax()).min()) * 100
        tot  = ((1 + al["p"]).prod() - 1) * 100
        cal  = (mu - RF) / abs(mdd / 100) if mdd != 0 else 0

        bkt: dict = {}
        for pk, pos in portfolio["positions"].items():
            t = pk.split("_")[0]; b = pos.get("strategy_bucket", "?")
            if t in px.columns:
                r = (px[t].iloc[-1] / px[t].iloc[0] - 1) * 100
                bkt[b] = bkt.get(b, []) + [r]
        bkt_avg = {b: round(float(np.mean(v)), 1) for b, v in bkt.items()}

        metrics = {
            "total_return_pct":  round(tot, 2),
            "benchmark_pct":     round(b_total, 2),
            "alpha_ann_pct":     round(alph * 100, 2),
            "beta":              round(beta, 3),
            "sharpe":            round(sh, 3),
            "sortino":           round(so, 3),
            "max_dd_pct":        round(mdd, 2),
            "calmar":            round(cal, 3),
            "ann_vol_pct":       round(sig * 100, 2),
            "bucket_returns":    bkt_avg,
        }
        log_and_print(f"{GRN}[Perf] {metrics}{RST}")
        return metrics

    except Exception as e:
        log_and_print(f"Warning: Performance calc failed: {e}")
        return {}


# ============================================================
# 16. WEEKLY REPORT
# ============================================================
def check_and_send_weekly_summary(
    portfolio: dict, risk_matrix: dict, total_nav: float,
    perf: dict, bucket_pcts: dict, balance_warnings: list,
) -> None:
    is_fri = datetime.now(ZoneInfo("America/New_York")).weekday() == 4
    if not is_fri and "--weekly-summary" not in sys.argv:
        return

    cash     = portfolio.get("cash_balance", 0)
    cash_pct = cash / total_nav * 100 if total_nav > 0 else 0

    bkts: dict = {BUCKET_G: [], BUCKET_V: [], BUCKET_D: []}
    for pk, data in risk_matrix.items():
        b   = data.get("bucket", BUCKET_G)
        val = data.get("current_value", 0)
        pnl = data.get("pnl_pct", 0)
        if val > 0:
            bkts.get(b, []).append(
                f"  {pk.split('_')[0]}: ${round(val,2)} | PnL: {round(pnl,1)}%"
            )
    holdings = ""
    for bn, lines in bkts.items():
        alloc = bucket_pcts.get(bn, 0)
        if lines:
            holdings += f"\n[{bn} — {alloc}% NAV]\n" + "\n".join(lines) + "\n"
    if not holdings:
        holdings = "  100% Cash\n"

    ps = ""
    if perf:
        ps = (
            f"\nPERFORMANCE vs SPY\n"
            f"  Return: {perf.get('total_return_pct','N/A')}%  "
            f"SPY: {perf.get('benchmark_pct','N/A')}%\n"
            f"  Alpha:  {perf.get('alpha_ann_pct','N/A')}%  "
            f"Sharpe: {perf.get('sharpe','N/A')}\n"
            f"  MaxDD:  {perf.get('max_dd_pct','N/A')}%  "
            f"Sortino: {perf.get('sortino','N/A')}\n"
            f"  Bucket returns: {perf.get('bucket_returns',{})}\n"
        )

    bw = ("\nBALANCE WARNINGS\n" +
          "\n".join(f"  * {w}" for w in balance_warnings)
          if balance_warnings else "")

    msg = (
        "GVD WEEKLY REPORT — SUPER INVESTOR PORTFOLIO\n"
        "============================================\n"
        f"NAV:  ${round(total_nav,2)}\n"
        f"Cash: ${round(cash,2)} ({round(cash_pct,1)}%)\n"
        f"{holdings}{ps}{bw}\n"
        "============================================\n"
        "Next scan: Monday after close"
    )
    send_notification(msg)
    log_and_print(f"{GRN}[Report] Weekly GVD report delivered.{RST}")


# ============================================================
# 17. MAIN PIPELINE
# ============================================================
if __name__ == "__main__":
    log_and_print(f"{CYN}{'='*60}{RST}")
    log_and_print(f"{CYN}   GVD PORTFOLIO SYSTEM v3.3 — INITIALIZING         {RST}")
    log_and_print(f"{CYN}   Strategy: Growth + Value + Dividend | PWER        {RST}")
    log_and_print(f"{CYN}   Model: {GEMINI_MODEL}                             {RST}")
    log_and_print(f"{CYN}{'='*60}{RST}")

    portfolio = load_portfolio()

    # ── Phase 1: Market Intelligence ─────────────────────────────────
    universe                     = get_macro_universe()
    regime                       = check_market_regime()
    active_tailwinds, sector_map = scan_macro_tailwinds()

    # ── Phase 2: Three-Bucket Screening ──────────────────────────────
    log_and_print(f"{CYN}[Phase 2] Running three-bucket screeners...{RST}")
    g_cands = run_growth_screener(universe, active_tailwinds, sector_map)
    v_cands = run_value_screener(universe)
    d_cands = run_dividend_screener(universe)
    all_cands = merge_candidates_dedup(g_cands, v_cands, d_cands)
    mines     = get_earnings_landmines(list(all_cands.keys()))

    # ── Phase 3: Position Monitoring (BEFORE PWER / AI) ──────────────
    risk_matrix, current_nav, mandatory_exits = evaluate_open_positions(portfolio)

    # ── Phase 4: Mandatory Hard Stops (Python — zero AI discretion) ──
    if mandatory_exits:
        log_and_print(f"{RED}[Phase 4] Executing {len(mandatory_exits)} mandatory exits...{RST}")
        execute_mandatory_exits(mandatory_exits, portfolio)
        portfolio = load_portfolio()

        exited_keys = {item["pos_key"] for item in mandatory_exits}
        risk_matrix = {k: v for k, v in risk_matrix.items() if k not in exited_keys}
        current_nav = portfolio["cash_balance"] + sum(
            v["current_value"] for v in risk_matrix.values()
        )

    # ── Phase 5: Bucket Balance Check ─────────────────────────────────
    bucket_pcts, balance_warnings = check_portfolio_balance(risk_matrix, current_nav)
    for w in balance_warnings:
        log_and_print(f"{YLW}[Balance] {w}{RST}")

    # ── Phase 6: PWER Scoring (existing positions use LIVE prices) ───
    existing_pos_data = {}
    for pk, pos in portfolio.get("positions", {}).items():
        ticker = pk.split("_")[0]
        rm = risk_matrix.get(pk, {})
        existing_pos_data[ticker] = {
            "bucket":        pos.get("strategy_bucket", ""),
            "current_price": rm.get("current_price", pos.get("entry_price", 0)),
            "pnl_pct":       rm.get("pnl_pct", 0),
        }
    pwer_input = {
        t: {**all_cands.get(t, {}), **existing_pos_data.get(t, {})}
        for t in set(all_cands) | set(existing_pos_data)
    }
    pwer_scores = calculate_pwer_batch(pwer_input, regime)

    # ── Phase 7: Opportunity Cost Engine ──────────────────────────────
    swap_cands = run_opportunity_cost_engine(portfolio, pwer_scores)

    # ── Phase 8: Performance Attribution ──────────────────────────────
    performance = calculate_performance_metrics(portfolio)

    # ── Phase 9: AI Agents ─────────────────────────────────────────────
    macro_directives, _ = query_macro_agent(regime, risk_matrix, active_tailwinds)
    agent_orders         = query_portfolio_constructor(
        g_cands, v_cands, d_cands,
        pwer_scores, macro_directives, regime,
        portfolio, current_nav, mines, swap_cands, balance_warnings,
    )

    # ── Phase 10: Execute Agent Orders ────────────────────────────────
    execute_agent_orders(agent_orders, portfolio, all_cands,
                         risk_matrix, current_nav, regime)
    portfolio = load_portfolio()

    # ── Phase 11: Weekly Report ───────────────────────────────────────
    check_and_send_weekly_summary(
        portfolio, risk_matrix, current_nav,
        performance, bucket_pcts, balance_warnings,
    )

    log_and_print(f"{CYN}{'='*60}{RST}")
    log_and_print(f"{CYN}   GVD SYSTEM v3.3 CYCLE COMPLETE                   {RST}")
    log_and_print(f"{CYN}{'='*60}{RST}")
