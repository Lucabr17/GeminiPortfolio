#!/usr/bin/env python3
"""
record_nav.py — append today's NAV to nav_history.json
======================================================
Runs in GitHub Actions once per trading day (see nav-history.yml). It reads
the same portfolio.json the autopilot commits, marks it to the day's close
with yfinance, and appends ONE point to nav_history.json:

    {"date","nav","cash","sleeve_value","positions_value","positions","complete"}

That file is the data source for the NAV line chart in dashboard.py. This
script never touches trading logic — it only reads portfolio.json and writes
nav_history.json.

Dedup: re-running on the same date overwrites that date's point (so a manual
re-run or a retried Action can't create duplicates). Weekends are skipped.
"""

import os
import sys
import json
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

try:
    import yfinance as yf
except Exception:
    print("record_nav: yfinance not importable — cannot compute NAV.", file=sys.stderr)
    sys.exit(0)   # exit 0 so the workflow doesn't hard-fail on a transient import issue

PORTFOLIO_FILE   = os.environ.get("PORTFOLIO_FILE", "portfolio.json")
NAV_HISTORY_FILE = os.environ.get("NAV_HISTORY_FILE", "nav_history.json")
SLEEVE_DEFAULT   = "SPY"
SKIP_WEEKENDS    = os.environ.get("NAV_SKIP_WEEKENDS", "1") == "1"


def _price(sym: str):
    """Last close (or last trade) for one symbol, or None."""
    try:
        t = yf.Ticker(sym)
        try:
            fi = t.fast_info
            for get in (lambda: fi["last_price"], lambda: fi["lastPrice"],
                        lambda: getattr(fi, "last_price", None)):
                try:
                    v = get()
                except Exception:
                    v = None
                if v:
                    v = float(v)
                    if v > 0:
                        return v
        except Exception:
            pass
        h = t.history(period="1d")
        if h is not None and not h.empty:
            v = float(h["Close"].iloc[-1])
            if v > 0:
                return v
    except Exception:
        pass
    return None


def _atomic_write(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def main():
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if SKIP_WEEKENDS and now_et.weekday() >= 5:      # 5=Sat, 6=Sun
        print(f"record_nav: {now_et:%Y-%m-%d} is a weekend — skipping.")
        return
    today = now_et.strftime("%Y-%m-%d")

    if not os.path.exists(PORTFOLIO_FILE):
        print(f"record_nav: {PORTFOLIO_FILE} not found — nothing to record.", file=sys.stderr)
        return
    with open(PORTFOLIO_FILE) as f:
        pf = json.load(f)

    positions = pf.get("positions", {}) or {}
    cash      = float(pf.get("cash_balance", 0) or 0)
    sleeve    = pf.get("sleeve") or {}
    slv_tkr   = (sleeve.get("ticker") or SLEEVE_DEFAULT).upper()
    slv_sh    = float(sleeve.get("shares", 0) or 0)

    # Price everything once.
    syms = set()
    for pk, pos in positions.items():
        syms.add((pos.get("ticker") or pk.rsplit("_", 1)[0]).upper())
    if slv_sh > 0:
        syms.add(slv_tkr)
    prices = {s: _price(s) for s in sorted(syms)}

    positions_value, complete = 0.0, True
    for pk, pos in positions.items():
        tkr    = (pos.get("ticker") or pk.rsplit("_", 1)[0]).upper()
        shares = float(pos.get("shares", 0) or 0)
        entry  = float(pos.get("entry_price", 0) or 0)
        px     = prices.get(tkr)
        if not px:                       # fall back to entry so NAV stays continuous
            px, complete = entry, False
        positions_value += shares * px

    slv_px    = prices.get(slv_tkr) if slv_sh > 0 else None
    slv_value = (slv_sh * slv_px) if (slv_sh > 0 and slv_px) else 0.0
    if slv_sh > 0 and not slv_px:
        complete = False
    nav = cash + slv_value + positions_value

    # Load existing history, drop any point for today, append fresh, sort.
    history = []
    if os.path.exists(NAV_HISTORY_FILE):
        try:
            with open(NAV_HISTORY_FILE) as f:
                history = json.load(f)
            if not isinstance(history, list):
                history = []
        except Exception:
            history = []
    history = [h for h in history if h.get("date") != today]
    history.append({
        "date": today,
        "nav": round(nav, 2),
        "cash": round(cash, 2),
        "sleeve_value": round(slv_value, 2),
        "positions_value": round(positions_value, 2),
        "positions": len(positions),
        "complete": complete,
    })
    history.sort(key=lambda h: h.get("date", ""))
    _atomic_write(NAV_HISTORY_FILE, history)

    print(f"record_nav: {today}  NAV ${nav:,.2f}  "
          f"(cash ${cash:,.0f} + {slv_tkr} ${slv_value:,.0f} + stocks ${positions_value:,.0f})  "
          f"points={len(history)}  complete={complete}")


if __name__ == "__main__":
    main()
