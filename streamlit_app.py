"""
GV Portfolio — Streamlit dashboard
==================================
Deploy this on Streamlit Community Cloud (share.streamlit.io) straight from
your GeminiPortfolio repo — same as your wheel-strategy tracker. Because the
app lives in the same (private) repo, it reads portfolio.json and
nav_history.json directly from the repo, and Streamlit auto-redeploys each
time your Actions commit new data. Live prices are fetched with yfinance on
every refresh.

Files this app expects in the repo root (all produced by your existing setup):
  portfolio.json      — written by autopilot.py
  nav_history.json    — written by record_nav.py (via nav-history.yml)   [optional]
"""

import os
import json
from datetime import datetime

import pandas as pd
import streamlit as st

# set_page_config must be the first Streamlit call
st.set_page_config(page_title="GV Portfolio", page_icon="📈",
                   layout="wide", initial_sidebar_state="collapsed")

# optional deps — degrade gracefully if a package is missing
try:
    import yfinance as yf
except Exception:
    yf = None
try:
    import plotly.graph_objects as go
    HAVE_PLOTLY = True
except Exception:
    HAVE_PLOTLY = False

# auto-refresh (smooth rerun if the helper is installed, else a plain reload)
REFRESH_SECONDS = 30
try:
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=REFRESH_SECONDS * 1000, key="auto")
except Exception:
    st.markdown(f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">',
                unsafe_allow_html=True)

PORTFOLIO_FILE   = os.environ.get("PORTFOLIO_FILE", "portfolio.json")
NAV_HISTORY_FILE = os.environ.get("NAV_HISTORY_FILE", "nav_history.json")
SLEEVE_DEFAULT   = "SPY"
GAIN, LOSS = "#2ED47A", "#FF5C6C"
STOCKS, SLEEVE, CASH = "#5B8DEF", "#8B5CF6", "#39445A"


def load_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


@st.cache_data(ttl=25, show_spinner=False)
def fetch_prices(symbols: tuple):
    """{sym: price or None}. Cached 25s so refreshes don't hammer yfinance."""
    prices = {}
    for s in symbols:
        px = None
        if yf is not None:
            try:
                t = yf.Ticker(s)
                try:
                    px = float(t.fast_info["last_price"])
                except Exception:
                    px = None
                if not px or px <= 0:
                    h = t.history(period="1d")
                    if h is not None and not h.empty:
                        px = float(h["Close"].iloc[-1])
            except Exception:
                px = None
        prices[s] = px if (px and px > 0) else None
    return prices


def compute(pf, hist, prices):
    positions = pf.get("positions", {}) or {}
    cash    = float(pf.get("cash_balance", 0) or 0)
    sleeve  = pf.get("sleeve") or {}
    slv_tkr = (sleeve.get("ticker") or SLEEVE_DEFAULT).upper()
    slv_sh  = float(sleeve.get("shares", 0) or 0)

    rows, pos_val, cost_tot, stale = [], 0.0, 0.0, False
    for pk, pos in positions.items():
        tkr    = (pos.get("ticker") or pk.rsplit("_", 1)[0]).upper()
        bucket = (pos.get("strategy_bucket")
                  or (pk.rsplit("_", 1)[1] if "_" in pk else "")).upper()
        sh    = float(pos.get("shares", 0) or 0)
        entry = float(pos.get("entry_price", 0) or 0)
        cost  = float(pos.get("allocated_capital", entry * sh) or 0)
        px = prices.get(tkr)
        if not px:
            px, stale = entry, True
        mv = sh * px
        pos_val += mv
        cost_tot += cost
        rows.append({"Ticker": tkr, "Bucket": bucket, "Shares": sh, "Avg entry": entry,
                     "Last": px, "Market value": mv, "Weight %": 0.0,
                     "P&L": mv - cost, "P&L %": ((px / entry - 1) * 100) if entry > 0 else 0.0})

    slv_px  = prices.get(slv_tkr) if slv_sh > 0 else None
    slv_val = (slv_sh * slv_px) if (slv_sh > 0 and slv_px) else 0.0
    nav = cash + slv_val + pos_val
    for r in rows:
        r["Weight %"] = (r["Market value"] / nav * 100) if nav > 0 else 0.0
    rows.sort(key=lambda r: r["Market value"], reverse=True)

    pnl = pos_val - cost_tot
    pnl_pct = (pnl / cost_tot * 100) if cost_tot > 0 else 0.0

    H = []
    if isinstance(hist, list):
        for h in hist:
            try:
                H.append({"date": str(h["date"]), "nav": float(h["nav"])})
            except Exception:
                pass
        H.sort(key=lambda x: x["date"])
    inc = H[0]["nav"] if H else None
    inc_date = H[0]["date"] if H else None
    tot_ret = ((nav / inc - 1) * 100) if (inc and inc > 0) else None

    today = datetime.now().strftime("%Y-%m-%d")
    prev = None
    for h in reversed(H):
        if h["date"] < today:
            prev = h["nav"]
            break
    if prev is None and len(H) >= 2 and H[-1]["date"] == today:
        prev = H[-2]["nav"]
    day = ((nav / prev - 1) * 100) if (prev and prev > 0) else None

    return dict(nav=nav, cash=cash, slv_tkr=slv_tkr, slv_val=slv_val, pos_val=pos_val,
                pnl=pnl, pnl_pct=pnl_pct, rows=rows, H=H, tot_ret=tot_ret,
                inc_date=inc_date, day=day, stale=stale, count=len(rows),
                alloc=dict(stocks=(pos_val / nav * 100) if nav > 0 else 0.0,
                           sleeve=(slv_val / nav * 100) if nav > 0 else 0.0,
                           cash=(cash / nav * 100) if nav > 0 else 0.0))


# ── UI ─────────────────────────────────────────────────────────────────
pf = load_json(PORTFOLIO_FILE)
if pf is None:
    st.error(f"Couldn't read **{PORTFOLIO_FILE}** — make sure it's committed to the "
             f"repo root (it's the file autopilot.py writes).")
    st.stop()

hist = load_json(NAV_HISTORY_FILE)   # may be None until the recorder has run

syms = set()
for pk, pos in (pf.get("positions") or {}).items():
    syms.add((pos.get("ticker") or pk.rsplit("_", 1)[0]).upper())
_slv = pf.get("sleeve") or {}
if float(_slv.get("shares", 0) or 0) > 0:
    syms.add((_slv.get("ticker") or SLEEVE_DEFAULT).upper())
prices = fetch_prices(tuple(sorted(syms)))
d = compute(pf, hist, prices)

st.markdown("### GV · Portfolio")
st.caption(f"as of {datetime.now():%H:%M:%S} · refreshes every {REFRESH_SECONDS}s"
           + (" · live prices" if yf is not None else " · yfinance unavailable — showing cost basis"))

c1, c2, c3 = st.columns(3)
c1.metric("Net Asset Value", f"${d['nav']:,.0f}",
          delta=(f"{d['day']:+.2f}%" if d['day'] is not None else None),
          help="today vs last recorded close")
c2.metric("Total return", (f"{d['tot_ret']:+.2f}%" if d['tot_ret'] is not None else "—"),
          help=(f"since {d['inc_date']}" if d['inc_date'] else "starts once the NAV recorder has run"))
c3.metric("Unrealized P&L", f"${d['pnl']:,.0f}", delta=f"{d['pnl_pct']:+.2f}%",
          help="open positions, marked to live prices vs average cost")

# NAV history chart
st.markdown("###### NAV history")
if d["H"] and HAVE_PLOTLY:
    dates = [pd.to_datetime(h["date"]) for h in d["H"]]
    navs  = [h["nav"] for h in d["H"]]
    up = navs[-1] >= navs[0]
    col = GAIN if up else LOSS
    rgb = "46,212,122" if up else "255,92,108"
    mn, mx = min(navs), max(navs)
    pad = (mx - mn) * 0.12 or (mx * 0.01 or 1)
    fig = go.Figure(go.Scatter(
        x=dates, y=navs, mode="lines",
        line=dict(color=col, width=2), fill="tozeroy",
        fillcolor=f"rgba({rgb},0.12)",
        hovertemplate="%{x|%b %d, %Y}<br>$%{y:,.0f}<extra></extra>"))
    fig.update_layout(
        template="plotly_dark", height=340, margin=dict(l=0, r=0, t=8, b=0),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        hoverlabel=dict(bgcolor="#0c1220"),
        xaxis=dict(showgrid=False, rangeselector=dict(
            buttons=[dict(count=1, label="1M", step="month", stepmode="backward"),
                     dict(count=3, label="3M", step="month", stepmode="backward"),
                     dict(count=6, label="6M", step="month", stepmode="backward"),
                     dict(step="all", label="ALL")],
            bgcolor="#161D2A", activecolor="#28324a", font=dict(color="#8A94A7"))),
        yaxis=dict(range=[mn - pad, mx + pad], tickprefix="$",
                   gridcolor="rgba(35,44,59,.6)"))
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
elif d["H"]:
    st.line_chart(pd.DataFrame({"NAV": [h["nav"] for h in d["H"]]},
                               index=[h["date"] for h in d["H"]]))
else:
    st.info("The NAV chart fills in once the daily recorder has run. Add "
            "`record_nav.py` and `.github/workflows/nav-history.yml` to the repo — "
            "then you can trigger it once from the **Actions** tab to seed the first point.")

# Allocation
st.markdown("###### Allocation")
a = d["alloc"]
if HAVE_PLOTLY:
    figb = go.Figure()
    figb.add_bar(y=["a"], x=[a["stocks"]], name="Stocks", orientation="h", marker_color=STOCKS)
    figb.add_bar(y=["a"], x=[a["sleeve"]], name=f"{d['slv_tkr']} sleeve", orientation="h", marker_color=SLEEVE)
    figb.add_bar(y=["a"], x=[a["cash"]], name="Cash", orientation="h", marker_color=CASH)
    figb.update_layout(barmode="stack", height=96, template="plotly_dark",
                       margin=dict(l=0, r=0, t=2, b=0),
                       paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                       legend=dict(orientation="h", y=-0.5, font=dict(size=11)),
                       xaxis=dict(visible=False, range=[0, 100]), yaxis=dict(visible=False))
    st.plotly_chart(figb, use_container_width=True, config={"displayModeBar": False})
st.caption(f"Stocks {a['stocks']:.1f}%  ·  ${d['pos_val']:,.0f}  ·  {d['count']} pos      "
           f"|      {d['slv_tkr']} sleeve {a['sleeve']:.1f}%  ·  ${d['slv_val']:,.0f}      "
           f"|      Cash {a['cash']:.1f}%  ·  ${d['cash']:,.0f}")

# Positions table
st.markdown("###### Positions")
if d["rows"]:
    df = pd.DataFrame(d["rows"])

    def color_pnl(v):
        return f"color:{GAIN}" if v > 0 else (f"color:{LOSS}" if v < 0 else "")

    styler = df.style.format({
        "Shares": "{:,.2f}", "Avg entry": "${:,.2f}", "Last": "${:,.2f}",
        "Market value": "${:,.0f}", "Weight %": "{:.1f}%",
        "P&L": "${:,.0f}", "P&L %": "{:+.2f}%"})
    try:
        styler = styler.map(color_pnl, subset=["P&L", "P&L %"])
    except AttributeError:
        styler = styler.applymap(color_pnl, subset=["P&L", "P&L %"])
    st.dataframe(styler, use_container_width=True, hide_index=True)
else:
    st.info("No open equity positions — the book is all cash + sleeve.")

if d["stale"] and yf is not None:
    st.caption("⚠️ Some live prices were unavailable this refresh; those rows use "
               "cost basis until the next update.")
