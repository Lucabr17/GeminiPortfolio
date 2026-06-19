"""
GVD AUTONOMOUS PORTFOLIO SYSTEM — v3.2
=======================================================
Based on:
  @theaiportfolios  — PWER ranking, 15-stock portfolio, opportunity cost engine
  Jeremy Lefebvre   — GVD framework: Growth + Value + Dividend

v3.2 fixes (this round):
  + FIXED: dividendYield bug — yfinance now sometimes returns this field
    already in percentage-point form (1.72 = 1.72%) instead of decimal
    (0.0172). Caused QCOM to score a fictitious "172% yield". Added a
    normalizer that detects and corrects this automatically, with logging.
  + FIXED: Graham Number distortion on conglomerates/insurers — Berkshire-
    type businesses have GAAP EPS distorted by mark-to-market gains on
    equity holdings, which blew up the Graham Number to ~95% "margin of
    safety". Added (a) a cross-check between Graham and FCF methods — if
    they diverge >3x, the conservative value is used instead of the
    average, and (b) a hard anomaly cap: any raw MOS above 85% is now
    flagged as a likely data artifact and excluded rather than auto-bought.
  + FIXED: candidate bucket collision — if a stock qualified for two
    buckets (e.g. Growth AND Dividend), the old {**g,**v,**d} merge
    silently let one bucket's data overwrite the other at execution time.
    Now de-duplicated by score, with logging when it happens.
  + FIXED: NAV recomputation after mandatory exits used `high_watermark`
    (the peak price ever seen) instead of current price — overstated NAV.
    Now recomputed from the risk matrix's real current values.
  + FIXED: existing-position data fed into PWER scoring used a placeholder
    0.0 PnL via an unnecessary walrus/tuple hack. Replaced with real
    current price + real PnL, sourced after position monitoring runs.
  + IMPROVED: pipeline reordered — positions are now monitored and
    mandatory exits executed BEFORE PWER scoring and the AI agents run,
    so the AI never wastes a call evaluating something already sold, and
    PWER scoring of existing holdings uses live prices, not entry prices.
  + IMPROVED: added a sector concentration cap (35% NAV per sector) that
    is enforced directly in the execution engine, not just inferred by
    the AI's discretion.
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
# UTILITY HELPERS
# ============================================================
def _strip_fences(text: str) -> str:
    """Remove markdown code fences without triple-backtick literals in source."""
    return re.sub(r"`{3}(?:json)?\s*", "", text).strip()


def _normalize_bucket(raw: str) -> str:
    r = raw.upper().strip()
    if "GROWTH" in r or r == "G":  return BUCKET_G
    if "VALUE"  in r or r == "V":  return BUCKET_V
    if "DIVID"  in r or r == "D":  return BUCKET_D
    return BUCKET_G


def _normalize_dividend_yield(raw) -> float:
    """
    yfinance has changed `dividendYield` format across versions:
    sometimes decimal (0.025 = 2.5%), sometimes already percentage-point
    (2.5 = 2.5%). No real sustainable dividend yield exceeds 100%, so any
    raw value above 1.0 is treated as already being in percentage-point
    form and divided by 100.
    """
    if raw is None:
        return 0.0
    try:
        raw = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return raw / 100.0 if raw > 1.0 else raw


def _verify_pwer(s: dict) -> dict:
    """Recalculate PWER from components; normalise probabilities to sum = 1."""
    bp = float(s.get("bull_prob", 0.25))
    ap = float(s.get("base_prob", 0.50))
    ep = float(s.get("bear_prob", 0.25))
    total = bp + ap + ep
    if total > 0:
        bp, ap, ep = bp/total, ap/total, ep/total
    s["bull_prob"] = round(bp, 4)
    s["base_prob"] = round(ap, 4)
    s["bear_prob"] = round(ep, 4)
    pwer = (float(s.get("bull_ret_pct", 0)) * bp +
            float(s.get("base_ret_pct", 0)) * ap +
            float(s.get("bear_ret_pct", 0)) * ep)
    s["pwer_pct"] = round(pwer, 2)
    return s


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
    ATR-based position size. Dollar risk = nav * risk_pct, stop = 2x ATR(14).
    Capped at nav * max_pct. High-volatility stocks automatically get
    smaller positions for the same dollar-risk budget.
    """
    try:
        h = yf.Ticker(ticker).history(period="60d")
        if h.empty or len(h) < 14:
            raise ValueError("insufficient history")
        hi, lo, pc = h["High"], h["Low"], h["Close"].shift(1)
        tr     = pd.concat([hi - lo,
                            (hi - pc).abs(),
                            (lo - pc).abs()], axis=1).max(axis=1)
        atr    = float(tr.iloc[-14:].mean())
        price  = float(h["Close"].iloc[-1])
        shares = (nav * risk_pct) / (atr * 2)
        return min(shares * price, nav * max_pct)
    except Exception:
        return nav * max_pct * 0.50


def merge_candidates_dedup(g: dict, v: dict, d: dict) -> dict:
    """
    A stock can occasionally qualify for more than one bucket (e.g. a
    quality grower that also pays a solid dividend). Naively merging
    dicts with {**g, **v, **d} lets the last bucket silently overwrite
    the earlier one — corrupting bucket-specific data (trailing stop %,
    max position size) used later at execution time.

    This keeps the higher-scoring bucket assignment per ticker and logs
    any conflict so it is visible in agent.log.
    """
    merged: dict = {}
    for bucket_cands in (g, v, d):
        for t, c in bucket_cands.items():
            if t not in merged or c.get("score", 0) > merged[t].get("score", 0):
                if t in merged:
                    log_and_print(
                        f"{YLW}[Dedup] {t} qualified for both "
                        f"{merged[t]['bucket']} and {c['bucket']} — "
                        f"keeping higher-scoring {c['bucket']}.{RST}"
                    )
                merged[t] = c
    return merged


def get_current_sector_exposure(portfolio: dict, risk_matrix: dict,
                                total_nav: float) -> dict:
    """Returns {sector: pct_of_nav} across all currently open positions."""
    if total_nav <= 0:
        return {}
    exposure: dict = {}
    for pk, pos in portfolio.get("positions", {}).items():
        ticker = pk.split("_")[0]
        try:
            sector = yf.Ticker(ticker).info.get("sector", "Unknown")
        except Exception:
            sector = "Unknown"
        val = risk_matrix.get(pk, {}).get("current_value",
                                          pos.get("allocated_capital", 0))
        exposure[sector] = exposure.get(sector, 0) + val
    return {s: round(v / total_nav * 100, 1) for s, v in exposure.items()}


# ============================================================
# CONSTANTS
# ============================================================
BUCKET_G = "GROWTH";   MAX_G = 0.50; CAP_G = 0.12; TRAIL_G = 20
BUCKET_V = "VALUE";    MAX_V = 0.35; CAP_V = 0.10; TRAIL_V = 15
BUCKET_D = "DIVIDEND"; MAX_D = 0.15; CAP_D = 0.08; TRAIL_D = 10

TARGET_POSITIONS        = 15
MIN_HOLD_DAYS           = 30    # no discretionary sells before this age
EARNINGS_TRIM_PNL_MIN   = 35    # % gain required before pre-earnings trim fires
PWER_CHUNK              = 15    # max candidates per PWER API call
MAX_SECTOR_PCT          = 35.0  # max % of NAV in any single sector

GRAHAM_DIVERGENCE_RATIO = 3.0   # methods disagreeing by more than this -> distrust optimistic one
MOS_ANOMALY_THRESHOLD   = 85.0  # raw margin of safety above this = likely data artifact
MOS_SCORING_CAP         = 70.0  # cap used for scoring so legit deep-value isn't over-weighted


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
    rs = 0; signals: dict = {}

    def _f(ticker: str, period: str) -> pd.DataFrame:
        try:
            return yf.Ticker(ticker).history(period=period)
        except Exception:
            return pd.DataFrame()

    spy  = _f("^GSPC", "60d");  vix  = _f("^VIX", "30d")
    t10  = _f("^TNX",  "5d");   t5   = _f("^FVX", "5d")
    hyg  = _f("HYG",   "30d");  ief  = _f("IEF",  "30d")
    mmth = _f("^MMTH", "10d")

    if not spy.empty:
        dd = ((spy["High"].max() - spy["Close"].iloc[-1]) /
              spy["High"].max()) * 100
        signals["spy_drawdown"] = round(dd, 2)
        rs += 30 if dd >= 10 else (15 if dd >= 5 else 0)

    if not vix.empty:
        v = float(vix["Close"].iloc[-1])
        signals["vix"] = round(v, 2)
        rs += 30 if v >= 35 else (20 if v >= 25 else (8 if v >= 18 else 0))

    if not t10.empty and not t5.empty:
        sp = float(t10["Close"].iloc[-1]) - float(t5["Close"].iloc[-1])
        signals["yield_spread"] = round(sp, 3)
        rs += 20 if sp < -0.5 else (10 if sp < 0 else 0)

    if not hyg.empty and not ief.empty:
        dl = ((hyg["Close"].iloc[-1] / hyg["Close"].iloc[0] - 1) -
              (ief["Close"].iloc[-1] / ief["Close"].iloc[0] - 1)) * 100
        signals["credit_delta"] = round(dl, 2)
        rs += 20 if dl < -5 else (10 if dl < -2 else 0)

    if not mmth.empty:
        br = float(mmth["Close"].iloc[-1])
        signals["breadth_pct"] = round(br, 1)
        rs += 20 if br < 30 else (10 if br < 50 else 0)

    if rs >= 60:
        regime, cf, gp, vp, dp, ng = "BEAR",    0.40, 0.05, 0.07, 0.05, False
    elif rs >= 30:
        regime, cf, gp, vp, dp, ng = "CAUTION", 0.15, 0.08, 0.10, 0.08, True
    else:
        regime, cf, gp, vp, dp, ng = "BULL",    0.04, 0.12, 0.10, 0.08, True

    out = {
        "regime": regime, "risk_score": rs, "signals": signals,
        "cash_floor_pct": cf,
        "max_pos_pct": {BUCKET_G: gp, BUCKET_V: vp, BUCKET_D: dp},
        "new_growth_buys_allowed":   ng,
        "new_value_buys_allowed":    True,
        "new_dividend_buys_allowed": True,
    }
    log_and_print(f"{CYN}[Regime] {regime} | Score {rs}/120 | {signals}{RST}")
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

    CLUSTERS = {
        "AI_SEMICONDUCTORS":  ["SOXX", "SMH"],
        "AI_SOFTWARE_CLOUD":  ["IGV",  "WCLD"],
        "ENERGY_POWER_INFRA": ["XLU",  "XLE"],
        "DEFENSE_AEROSPACE":  ["ITA",  "XAR"],
        "HEALTHCARE_BIOTECH": ["XBI",  "IBB"],
        "FINTECH_PAYMENTS":   ["FINX", "IPAY"],
        "CYBERSECURITY":      ["CIBR", "HACK"],
        "CONSUMER_RECOVERY":  ["XLY",  "XRT"],
    }

    SECTOR_MAP = {
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

    try:
        spy_h  = yf.Ticker("SPY").history(period="6mo")["Close"]
        spy_6m = (spy_h.iloc[-1] / spy_h.iloc[0] - 1) * 100
    except Exception:
        spy_6m = 0.0

    active: dict = {}
    for cluster, etfs in CLUSTERS.items():
        rets = []
        for etf in etfs:
            try:
                h = yf.Ticker(etf).history(period="6mo")["Close"]
                if not h.empty:
                    rets.append((h.iloc[-1] / h.iloc[0] - 1) * 100)
            except Exception:
                pass
        if rets:
            avg    = float(np.mean(rets))
            excess = avg - spy_6m
            if excess >= 5:
                active[cluster] = {
                    "6m_ret_pct":    round(avg, 1),
                    "excess_vs_spy": round(excess, 1),
                    "strength":      "STRONG" if excess >= 15 else "MODERATE",
                }

    log_and_print(f"{CYN}[Tailwinds] Active ({len(active)}): {list(active.keys())}{RST}")
    return active, SECTOR_MAP


# ============================================================
# 6. INTRINSIC VALUE ENGINE  (with anomaly guard)
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
    active_clusters  = set(active_tailwinds.keys())

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

            c   = hd["Close"]
            p   = float(c.iloc[-1])
            roe = float(info.get("returnOnEquity")   or 0)
            opc = float(info.get("operatingCashflow") or 0)
            rg  = float(info.get("revenueGrowth")    or 0)

            if roe < 0.10 or opc <= 0 or rg < 0.10:
                continue

            sector  = info.get("sector", "")
            aligned = [cl for cl in sector_map.get(sector, [])
                       if cl in active_clusters]
            if not aligned:
                continue

            ts     = max(active_tailwinds[cl]["excess_vs_spy"] for cl in aligned)
            sma200 = float(c.iloc[-200:].mean())
            rs6m   = ((p / float(c.iloc[-126]) - 1) * 100
                      if len(c) >= 126 else 0) - spy6m

            mktcap = float(info.get("marketCap")    or 0)
            fcf    = float(info.get("freeCashflow")  or 0)
            fcy    = (fcf / mktcap) if mktcap > 0 else 0
            peg    = float(info.get("pegRatio")      or 0)
            gm     = float(info.get("grossMargins")  or 0)

            hw = t.history(period="5y", interval="1wk")
            ema200w = None
            if len(hw) >= 200:
                ema200w = float(
                    hw["Close"].dropna().ewm(span=200, adjust=False)
                    .mean().iloc[-1])

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

            if sc >= 45:
                candidates[ticker] = {
                    "bucket":          BUCKET_G,
                    "price":           round(p, 2),
                    "score":           sc,
                    "tags":            " | ".join(tags),
                    "sector":          sector,
                    "tailwinds":       aligned,
                    "tailwind_excess": round(ts, 1),
                    "rev_growth_pct":  round(rg * 100, 1),
                    "roe_pct":         round(roe * 100, 1),
                    "fcf_yield_pct":   round(fcy * 100, 2),
                    "rs_6m":           round(rs6m, 1),
                    "ema200w":         round(ema200w, 2) if ema200w else "N/A",
                }
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

            p      = float(info.get("currentPrice") or
                           info.get("regularMarketPrice") or 0)
            roe    = float(info.get("returnOnEquity")    or 0)
            opc    = float(info.get("operatingCashflow") or 0)
            mktcap = float(info.get("marketCap")         or 0)
            fcf    = float(info.get("freeCashflow")      or 0)
            de     = float(info.get("debtToEquity")      or 0)

            if p <= 0 or roe < 0.08 or opc <= 0 or de > 200:
                continue
            if mktcap < 5_000_000_000:
                continue

            iv = calculate_intrinsic_value(info)
            if iv.get("data_anomaly"):
                log_and_print(
                    f"{YLW}[V] {ticker} excluded — implausible margin of "
                    f"safety ({iv.get('raw_margin_of_safety_pct')}%), "
                    f"likely distorted EPS data.{RST}")
                continue
            if not iv["undervalued"]:
                continue

            dy_raw = info.get("dividendYield")
            dy     = _normalize_dividend_yield(dy_raw)
            if dy_raw is not None and float(dy_raw) > 1.0:
                log_and_print(
                    f"{YLW}[Normalize] {ticker} dividendYield {dy_raw} "
                    f"-> {round(dy*100,2)}%{RST}")

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

            if sc >= 35:
                candidates[ticker] = {
                    "bucket":                   BUCKET_V,
                    "price":                    round(p, 2),
                    "score":                    sc,
                    "tags":                     " | ".join(tags),
                    "sector":                   info.get("sector", "Unknown"),
                    "intrinsic_value":          iv["intrinsic_value"],
                    "margin_of_safety_pct":     mos,
                    "iv_method":                iv["method"],
                    "roe_pct":                  round(roe * 100, 1),
                    "fcf_yield_pct":            round(fcy * 100, 2),
                    "pb_ratio":                 round(pb, 2),
                    "forward_pe":               round(fpe, 1) if fpe else "N/A",
                    "div_yield_pct":            round(dy * 100, 2),
                }
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
            dy     = _normalize_dividend_yield(dy_raw)
            if dy_raw is not None and float(dy_raw) > 1.0:
                log_and_print(
                    f"{YLW}[Normalize] {ticker} dividendYield {dy_raw} "
                    f"-> {round(dy*100,2)}%{RST}")

            pr  = float(info.get("payoutRatio")       or 1)
            fcf = float(info.get("freeCashflow")      or 0)
            dp  = float(info.get("dividendsPaid")     or 0)
            opc = float(info.get("operatingCashflow") or 0)
            roe = float(info.get("returnOnEquity")    or 0)

            if dy < 0.02 or pr > 0.65 or pr <= 0 or opc <= 0:
                continue

            cov = fcf / abs(dp) if dp != 0 else 0
            if cov < 1.0 and dp != 0:
                continue

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

            try:
                dh = t.dividends
                if len(dh) > 0:
                    yrs = (dh.index[-1] - dh.index[0]).days / 365
                    if yrs >= 5:
                        f4 = dh.iloc[:4].mean()
                        l4 = dh.iloc[-4:].mean()
                        dg = (l4 / f4 - 1) / yrs if f4 > 0 else 0
                        if   dg >= 0.05: sc += 15; tags.append("GROWING_DIV_5PCT_PA")
                        elif dg >= 0.00: sc +=  5
            except Exception:
                pass

            if sc >= 30:
                p = float(info.get("currentPrice") or
                          info.get("regularMarketPrice") or 0)
                candidates[ticker] = {
                    "bucket":           BUCKET_D,
                    "price":            round(p, 2),
                    "score":            sc,
                    "tags":             " | ".join(tags),
                    "sector":           info.get("sector", "Unknown"),
                    "div_yield_pct":    round(dy * 100, 2),
                    "payout_ratio_pct": round(pr * 100, 1),
                    "fcf_coverage":     round(cov, 2),
                    "roe_pct":          round(roe * 100, 1),
                }
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
                    all_scores[t] = _verify_pwer(s)
        except Exception as e:
            log_and_print(f"{RED}[PWER chunk {i//PWER_CHUNK+1} error] {e}{RST}")

        if i + PWER_CHUNK < len(tickers):
            time.sleep(1.5)

    log_and_print(f"{GRN}[PWER] Scored {len(all_scores)}/{n} candidates.{RST}")
    return all_scores


# ============================================================
# 10. POSITION MONITOR  (bucket-specific rules)
# ============================================================
def evaluate_open_positions(portfolio: dict, v_candidates: dict) -> tuple:
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
            rg   = float(info.get("revenueGrowth", 0) or 0)
            pr   = float(info.get("payoutRatio",   0) or 0)

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
                iv = v_candidates.get(ticker, {}).get("intrinsic_value", 0)
                if iv > 0 and cp >= iv * 0.95:
                    status  = f"TAKE_PROFIT: V price ${cp} reached intrinsic value ${iv}"
                    is_mand = True
                elif dd >= TRAIL_V:
                    status  = f"MANDATORY_SELL: V trailing stop (-{round(dd,1)}% from peak)"
                    is_mand = True
                elif dd >= TRAIL_V * 0.65:
                    status = f"TRIM_50: V at -{round(dd,1)}% — approaching stop"

            elif bucket == BUCKET_D:
                try:
                    dh = yf.Ticker(ticker).dividends
                    if len(dh) >= 2 and dh.iloc[-1] < dh.iloc[-2] * 0.90:
                        status  = "MANDATORY_SELL: D dividend cut — income thesis broken"
                        is_mand = True
                except Exception:
                    pass

                if not is_mand:
                    if pr > 0.85:
                        status = "REVIEW: D payout ratio >85% — dividend at risk"
                    elif dd >= TRAIL_D:
                        status  = f"MANDATORY_SELL: D trailing stop (-{round(dd,1)}% from peak)"
                        is_mand = True

            # Pre-earnings profit protection — ONLY fires when up >=35%.
            if not is_mand and pnl >= EARNINGS_TRIM_PNL_MIN:
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
                        if 0 <= days_to_earn <= 5:
                            status = (f"TRIM_25_PRE_EARNINGS: up {round(pnl,1)}% "
                                      f"— trim 25% to protect gains before earnings")
                except Exception:
                    pass

            if not is_mand and age_days < MIN_HOLD_DAYS:
                status = status + f" [HOLD: {age_days}/{MIN_HOLD_DAYS}d min]"

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
def run_opportunity_cost_engine(portfolio: dict, pwer_scores: dict) -> list:
    """
    If an existing position's PWER is more than 15 points below a
    replacement candidate's PWER, flag it as a swap opportunity.
    E.g. TMO at +5.3% PWER vs NVDA at +37.7% PWER -> swap.
    """
    swaps: list = []
    if not pwer_scores or not portfolio.get("positions"):
        return swaps

    for pk, pos in portfolio["positions"].items():
        ticker = pk.split("_")[0]
        ep = pwer_scores.get(ticker, {}).get("pwer_pct", None)
        if ep is None:
            continue

        better = [
            (t, s) for t, s in pwer_scores.items()
            if s.get("pwer_pct", 0) > ep + 15
            and not any(k.startswith(t) for k in portfolio["positions"])
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

    if swaps:
        log_and_print(f"{YLW}[OpCost] {len(swaps)} swap candidates.{RST}")
    return swaps


# ============================================================
# 12. BUCKET BALANCE CHECK
# ============================================================
def check_portfolio_balance(risk_matrix: dict, total_nav: float) -> tuple:
    """
    Compares current bucket weights against targets.
    Flags buckets that have drifted >20% above their target.
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
            warnings_list.append(
                f"{b} OVERWEIGHT: {pct}% actual vs {target_pct}% target"
            )

    return actual, warnings_list


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
            keys = [pk for pk in portfolio["positions"] if pk.startswith(ticker)]
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
                               if pk.startswith(ticker))
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
    log_and_print(f"{CYN}   GVD PORTFOLIO SYSTEM v3.2 — INITIALIZING         {RST}")
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
    risk_matrix, current_nav, mandatory_exits = evaluate_open_positions(
        portfolio, v_cands)

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
    pwer_input  = {**all_cands, **existing_pos_data}
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
    log_and_print(f"{CYN}   GVD SYSTEM v3.2 CYCLE COMPLETE                   {RST}")
    log_and_print(f"{CYN}{'='*60}{RST}")
