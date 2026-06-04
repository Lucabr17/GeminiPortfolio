import os
import sys
import json
import time
import logging
import re
import pandas as pd
from datetime import datetime, time as datetime_time
from zoneinfo import ZoneInfo
import requests
import yfinance as yf
from google import genai
from google.genai import types
import warnings

# Suppress pandas warnings for a clean terminal output
warnings.filterwarnings('ignore')

# ==========================================
# 1. INITIALIZATION & CORE CREDENTIALS
# ==========================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")

logging.basicConfig(
    filename='agent.log', 
    level=logging.INFO, 
    format='%(asctime)s | %(message)s'
)

def log_and_print(message):
    print(message)
    clean_msg = re.sub(r'\033\[[0-9;]*m', '', message)
    logging.info(clean_msg)

# Initialize Gemini Client
try:
    ai_client = genai.Client(api_key=GEMINI_API_KEY)
except Exception as e:
    log_and_print(f"Warning: Gemini Client failed to initialize. Check API Key. Error: {e}")

GREEN = "\033[1;32m"
RED = "\033[1;31m"
CYAN = "\033[1;36m"
YELLOW = "\033[1;33m"
RESET = "\033[0m"

# ==========================================
# 2. PORTFOLIO MEMORY & 1000+ ASSET UNIVERSE
# ==========================================
def load_portfolio():
    try:
        with open("portfolio.json", "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"cash_balance": 50000.0, "positions": {}}

def save_portfolio(data):
    with open("portfolio.json", "w") as f:
        json.dump(data, f, indent=4)

def send_notification(message):
    if not TELEGRAM_BOT_TOKEN or "YOUR_" in TELEGRAM_BOT_TOKEN:
        log_and_print("Telegram keys missing. Skipping broadcast.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        log_and_print(f"Failed to transmit alert: {e}")

def get_dynamic_universe():
    """Scrapes S&P 500, Nasdaq 100, and Top Small/Mid Caps (Russell 2000 proxy)."""
    log_and_print(f"{CYAN}[⚙️ System] Fetching massive live universe (S&P 500, Nasdaq 100, Mid/Small Caps)...{RESET}")
    tickers = []
    
    try:
        sp500_url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
        tables = pd.read_html(sp500_url)
        tickers.extend(tables[0]['Symbol'].tolist())
    except: pass
        
    try:
        ndx_url = 'https://en.wikipedia.org/wiki/Nasdaq-100'
        ndx_tables = pd.read_html(ndx_url, match='Ticker|Symbol')
        if 'Ticker' in ndx_tables[0].columns: tickers.extend(ndx_tables[0]['Ticker'].tolist())
        elif 'Symbol' in ndx_tables[0].columns: tickers.extend(ndx_tables[0]['Symbol'].tolist())
    except: pass

    try:
        mdy_tables = pd.read_html('https://en.wikipedia.org/wiki/List_of_S%26P_400_companies')
        tickers.extend(mdy_tables[0]['Symbol'].tolist() if 'Symbol' in mdy_tables[0].columns else mdy_tables[0]['Ticker symbol'].tolist())
        
        spsm_tables = pd.read_html('https://en.wikipedia.org/wiki/List_of_S%26P_600_companies')
        tickers.extend(spsm_tables[0]['Symbol'].tolist() if 'Symbol' in spsm_tables[0].columns else spsm_tables[0]['Ticker symbol'].tolist())
    except: pass

    cleaned_tickers = [str(t).replace('.', '-') for t in tickers if isinstance(t, str)]
    
    strategic_alpha = ["NOW", "SHOP", "AXON", "SNOW", "CRM", "PLTR", "AMD", "QCOM", "MU", "ZETA", "COIN", "HOOD", "VST", "CELH", "AVGO", "MSFT", "LLY"]
    cleaned_tickers.extend(strategic_alpha)
    
    final_universe = list(set(cleaned_tickers))
    log_and_print(f"{GREEN}[✓] Total unique assets loaded into matrix: {len(final_universe)}{RESET}")
    return final_universe

# ==========================================
# 3. FAST SCAN & DEEP DIVE MATRICES (WITH ATR ENGINE)
# ==========================================
def compute_rsi(prices, period=14):
    if len(prices) < period + 1: return 50
    deltas = prices.diff().dropna()
    gains = deltas.clip(lower=0)
    losses = -deltas.clip(upper=0)
    avg_gain = gains.rolling(window=period).mean()
    avg_loss = losses.rolling(window=period).mean()
    rs = avg_gain.iloc[-1] / (avg_loss.iloc[-1] + 1e-9)
    return 100 - (100 / (1 + rs))

def compute_atr_pct(df, period=14):
    """Calculates Average True Range as a percentage of the current stock price for volatility sizing."""
    if len(df) < period + 1: return 4.0  # Default fallback volatility proxy
    high = df['High']
    low = df['Low']
    close_prev = df['Close'].shift(1)
    
    tr1 = high - low
    tr2 = (high - close_prev).abs()
    tr3 = (low - close_prev).abs()
    
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean().iloc[-1]
    current_price = df['Close'].iloc[-1]
    
    return round((atr / current_price) * 100, 2) if current_price > 0 else 4.0

def run_two_stage_screener(tickers):
    log_and_print(f"{CYAN}[⚙️ Matrix] Running Stage 1 Fast-Scan on {len(tickers)} assets...{RESET}")
    
    fast_data = yf.download(tickers, period="30d", interval="1d", group_by='ticker', progress=False)
    fast_candidates = []
    
    for ticker in tickers:
        try:
            td = fast_data[ticker] if len(tickers) > 1 else fast_data
            if td.empty or len(td) < 20: continue
            
            c_price = td['Close'].iloc[-1]
            mom_5d = ((c_price - td['Close'].iloc[-5]) / td['Close'].iloc[-5]) * 100
            
            avg_vol = td['Volume'].iloc[-21:-1].mean()
            vol_ratio = td['Volume'].iloc[-1] / avg_vol if avg_vol > 0 else 1
            
            if vol_ratio > 1.20 or mom_5d > 3.0 or mom_5d < -5.0:
                fast_candidates.append(ticker)
        except: continue
            
    log_and_print(f"{CYAN}[⚙️ Matrix] Stage 2 Deep-Dive Structural Analysis on {len(fast_candidates)} filtered assets...{RESET}")
    
    deep_data = yf.download(fast_candidates, period="250d", interval="1d", group_by='ticker', progress=False)
    scored_candidates = {}
    
    for ticker in fast_candidates:
        try:
            td = deep_data[ticker] if len(fast_candidates) > 1 else deep_data
            if td.empty or len(td) < 200: continue
                
            closes = td['Close']
            current_price = round(closes.iloc[-1], 2)
            
            sma_50 = closes.iloc[-50:].mean()
            sma_200 = closes.iloc[-200:].mean()
            dist_sma50 = ((current_price - sma_50) / sma_50) * 100
            dist_sma200 = ((current_price - sma_200) / sma_200) * 100
            
            low_90d = td['Low'].iloc[-90:].min()
            dist_from_90d_floor = ((current_price - low_90d) / low_90d) * 100
            rsi = compute_rsi(closes)
            atr_volatility_pct = compute_atr_pct(td)
            
            # Contextual Directional Component: Measure performance over the last 5 days
            mom_5d = ((current_price - closes.iloc[-5]) / closes.iloc[-5]) * 100
            
            try:
                info = yf.Ticker(ticker).info
                fwd_pe = info.get('forwardPE', 0) or 0
                rev_growth = info.get('revenueGrowth', 0) or 0
            except:
                fwd_pe, rev_growth = 0, 0

            score = 0
            if rsi < 40: score += 25                      
            if dist_sma200 > -2: score += 15              
            if 0 < dist_from_90d_floor < 10: score += 25  
            if rsi > 55 and mom_5d > 4: score += 20 
            if rev_growth > 0.15: score += 15
            if 0 < fwd_pe < 25: score += 10

            scored_candidates[ticker] = {
                "price": current_price,
                "composite_alpha_score": score,
                "dist_sma50_pct": round(dist_sma50, 2),
                "dist_sma200_pct": round(dist_sma200, 2),
                "dist_from_90d_floor_pct": round(dist_from_90d_floor, 2),
                "rsi_14d": round(rsi, 2),
                "forward_pe": round(fwd_pe, 2),
                "rev_growth_pct": round(rev_growth * 100, 2),
                "atr_volatility_pct": atr_volatility_pct,
                "recent_5d_return_pct": round(mom_5d, 2)
            }
        except: continue

    return dict(sorted(scored_candidates.items(), key=lambda x: x[1]['composite_alpha_score'], reverse=True)[:15])

# ==========================================
# 4. INSTITUTIONAL RISK MATRIX (NO STUPID SELLS)
# ==========================================
def evaluate_open_positions(portfolio):
    risk_matrix = {}
    now_ny = datetime.now(ZoneInfo("America/New_York")).date()
    
    for ticker, pos_data in portfolio.get("positions", {}).items():
        try:
            hist = yf.Ticker(ticker).history(period="250d")
            if hist.empty or len(hist) < 200: continue
            
            closes = hist['Close']
            current_price = round(closes.iloc[-1], 2)
            entry_price = pos_data["entry_price"]
            strategy_bucket = pos_data.get("strategy_bucket", "HIGH_GROWTH")
            
            highest_price = pos_data.get("highest_price_seen", entry_price)
            if current_price > highest_price:
                highest_price = current_price
                pos_data["highest_price_seen"] = highest_price 
                
            purchase_date = datetime.strptime(pos_data["purchase_date"], '%Y-%m-%d').date()
            days_held = (now_ny - purchase_date).days
            
            pnl_pct = ((current_price - entry_price) / entry_price) * 100
            drawdown_from_peak = ((highest_price - current_price) / highest_price) * 100
            
            sma_200 = closes.iloc[-200:].mean()
            dist_sma200 = ((current_price - sma_200) / sma_200) * 100
            
            risk_status = "SAFE - LET RUN"
            
            if highest_price > (entry_price * 1.15): 
                if drawdown_from_peak > 15.0:
                    risk_status = "PROFIT SECURED: 15% Trailing Stop Triggered"
            elif pnl_pct < -15.0:
                risk_status = "CRITICAL: Hard Stop Hit (-15%)"
            elif dist_sma200 < -8.0:
                risk_status = "CRITICAL: 200-Day Structural Breakdown Verified"
                
            risk_matrix[ticker] = {
                "days_held": days_held,
                "current_price": current_price,
                "pnl_pct": round(pnl_pct, 2),
                "peak_profit_pct": round(((highest_price - entry_price) / entry_price) * 100, 2),
                "dist_sma200_pct": round(dist_sma200, 2),
                "algo_risk_status": risk_status,
                "strategy_bucket": strategy_bucket
            }
        except: continue
            
    return risk_matrix

# ==========================================
# 5. EXECUTION BRAIN & PORTFOLIO UPDATE
# ==========================================
def query_gemini_brain(buy_candidates, portfolio, risk_matrix):
    total_nav = portfolio["cash_balance"] + sum(p["allocated_capital"] for p in portfolio["positions"].values())
    cash_ratio = portfolio["cash_balance"] / total_nav if total_nav > 0 else 1.0
    
    core_allocated = sum(p["allocated_capital"] for p in portfolio["positions"].values() if p.get("strategy_bucket") == "CORE")
    growth_allocated = sum(p["allocated_capital"] for p in portfolio["positions"].values() if p.get("strategy_bucket") == "HIGH_GROWTH")
    
    min_allocation = round(total_nav * 0.02, 2)
    max_allocation = round(total_nav * 0.10, 2)
    cash_threshold = round(total_nav * 0.07, 2)
    
    system_prompt = f"""
    You are an elite hedge fund portfolio manager executing a 50/50 BARBELL ASSET ALLOCATION STRATEGY.
    Your mission is to balance slow, macroeconomic foundational compounders with high-velocity alpha setups.
    
    PORTFOLIO LEDGER CONTEXT:
    * Total Account NAV: ${round(total_nav, 2)}
    * Free Investable Cash: ${round(portfolio["cash_balance"], 2)} (Ratio: {round(cash_ratio * 100, 2)}%)
    * Core Bucket Allocation: ${round(core_allocated, 2)} / Target: ${round(total_nav * 0.50, 2)} (50%)
    * High Growth Bucket Allocation: ${round(growth_allocated, 2)} / Target: ${round(total_nav * 0.50, 2)} (50%)
    
    RISK SIZING LIMITS:
    * Min Target Single Position: ${min_allocation}
    * Max Target Single Position: ${max_allocation}
    * Emergency Liquidity Threshold: ${cash_threshold}
    
    Active Allocation Manifest: {json.dumps(portfolio)}
    Top Available Candidates Matrix (Includes Metrics, ATR %, and 5-Day Returns): {json.dumps(buy_candidates)}
    Active Positions Structural Risk Monitor: {json.dumps(risk_matrix)}
    
    BARBELL MODEL ALLOCATION REQUIREMENTS:
    1. BUCKET 1: CORE PORTFOLIO (50% Target Weight) -> Execute Macro Paradigm.
       Target high-conviction institutional anchors with structural momentum and clear long-term secular trends (Examples: AVGO, MSFT, LLY, VST, NOW). Evaluate macro trends, institutional volume, and fundamental growth durability. You must assign "strategy_bucket": "CORE".
       
    2. BUCKET 2: HIGH GROWTH PORTFOLIO (50% Target Weight) -> Maintain Our Tactical Tech Framework.
       Target explosive multi-factor structural dip-buying or velocity setups using the original strategy rules (90-day chart floors, structural technical breakdowns, near-term RSI exhaustion reversals). You must assign "strategy_bucket": "HIGH_GROWTH".
       
    3. THE VOLATILITY EXPANSION & ATR RULE (CRITICAL):
       Cross-reference "atr_volatility_pct" with "recent_5d_return_pct" to separate GOOD volatility from BAD volatility.
       * GOOD VOLATILITY (UPSIDE MOMENTUM BREAKOUTS): If a stock has a high ATR (>5%) but its "recent_5d_return_pct" is strongly positive, or it jumped 6-9% in a day, this is a bullish breakout. NEVER trim or sell an asset solely because it has high upside volatility or huge green days. Let winners run and maximize the trend! You can size these up safely.
       * BAD VOLATILITY (CHOP / RISK): Only scale down your buy sizes toward the minimum threshold (${min_allocation}) if the high ATR is accompanied by negative or directionless, choppy price returns.
       
    4. CAPITAL REALLOCATION VIA TRIMS:
       If Free Cash drops beneath the low cash threshold (${cash_threshold}) and you need to fund a perfect high-scoring opportunity to correct a bucket imbalance, you are authorized to issue a "TRIM" order to capture partial gains ONLY from assets that are extended and showing signs of momentum exhaustion—NEVER trim a fresh breakout.
       
    5. RUTHLESS EXITS:
       If 'algo_risk_status' shows 'PROFIT SECURED' or 'CRITICAL', immediately execute a full SELL to recycle capital back into cash buying power.

    Output strictly in raw JSON format matching this object structure:
    {{
        "trades": [
            {{
              "action": "BUY",
              "ticker": "TICKER",
              "strategy_bucket": "CORE",
              "allocation_amount": 4000,
              "analysis_display": "⚡ Core Allocation Sized via ATR Risk Scaling... [DETAILED MACRO/MOMENTUM THESIS]"
            }},
            {{
              "action": "SELL",
              "ticker": "TICKER",
              "strategy_bucket": "HIGH_GROWTH",
              "allocation_amount": "ALL",
              "analysis_display": "🚨 Risk Threshold Breached... [RATIONALE]"
            }}
        ]
    }}
    """
    
    log_and_print(f"{YELLOW}[🧠 Gemini] Initiating Barbell Deep Thinking sequence via Google GenAI SDK...{RESET}")
    
    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-pro', 
            contents=system_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json", 
                temperature=1.0,
                thinking_config=types.ThinkingConfig(thinking_level="high")
            )
        )
        
        raw_text = response.text.strip()
        data = json.loads(raw_text)
        
        # Parse the object wrapper safely
        if isinstance(data, dict) and "trades" in data:
            return data["trades"]
        elif isinstance(data, list):
            return data
        else:
            log_and_print(f"{YELLOW}[⚠] Unexpected JSON structure from Gemini. Raw output: {raw_text}{RESET}")
            return []
            
    except Exception as e:
        log_and_print(f"{RED}[❌ System] Gemini API Error / Parsing Failure: {e}{RESET}")
        return []

def execute_trades(agent_decisions, portfolio, buy_candidates, risk_matrix):
    if not agent_decisions:
        log_and_print(f"{YELLOW}[⚠] No actions returned by Gemini.{RESET}")
        return
        
    for decision in agent_decisions:
        action = decision.get("action", "").upper()
        ticker = decision.get("ticker", "").upper()
        amount = decision.get("allocation_amount", 0)
        bucket = decision.get("strategy_bucket", "HIGH_GROWTH").upper()
        rationale = decision.get("analysis_display", "No rationale provided.")
        
        if action == "HOLD":
            continue
            
        elif action == "SELL" and ticker in portfolio["positions"]:
            pos = portfolio["positions"][ticker]
            current_price = risk_matrix.get(ticker, {}).get("current_price", pos["entry_price"])
            shares = pos["shares"]
            proceeds = shares * current_price
            
            portfolio["cash_balance"] += proceeds
            del portfolio["positions"][ticker]
            
            msg = f"🔴 *GEMINI EXECUTION: FULL SELL {ticker} [{bucket}]*\n\nProceeds: ${round(proceeds, 2)}\nRationale: {rationale}"
            log_and_print(f"{RED}{msg}{RESET}")
            send_notification(msg)

        elif action == "TRIM" and ticker in portfolio["positions"]:
            pos = portfolio["positions"][ticker]
            current_price = risk_matrix.get(ticker, {}).get("current_price", pos["entry_price"])
            trim_amount = float(amount)
            current_position_value = pos["shares"] * current_price
            
            if trim_amount >= current_position_value:
                proceeds = pos["shares"] * current_price
                portfolio["cash_balance"] += proceeds
                del portfolio["positions"][ticker]
                msg = f"✂️ *GEMINI EXECUTION: FULL LIQUIDATION VIA TRIM {ticker} [{bucket}]*\n\nProceeds: ${round(proceeds, 2)}\nRationale: {rationale}"
            else:
                shares_to_sell = trim_amount / current_price
                portfolio["cash_balance"] += trim_amount
                pos["shares"] -= shares_to_sell
                pos["allocated_capital"] = pos["shares"] * pos["entry_price"]
                msg = f"✂️ *GEMINI EXECUTION: TRIM {ticker} [{bucket}]*\n\nExtracted: ${round(trim_amount, 2)} | Remaining Value: ${round(pos['shares'] * current_price, 2)}\nRationale: {rationale}"
                
            log_and_print(f"{YELLOW}{msg}{RESET}")
            send_notification(msg)
            
        elif action == "BUY" and ticker in buy_candidates:
            amount = float(amount)
            if amount > portfolio["cash_balance"]:
                amount = portfolio["cash_balance"]
            
            if amount <= 0:
                continue
                
            current_price = buy_candidates[ticker]["price"]
            shares = amount / current_price
            
            portfolio["cash_balance"] -= amount
            if ticker in portfolio["positions"]:
                old_shares = portfolio["positions"][ticker]["shares"]
                old_capital = portfolio["positions"][ticker]["allocated_capital"]
                new_total_shares = old_shares + shares
                new_total_capital = old_capital + amount
                
                portfolio["positions"][ticker]["shares"] = new_total_shares
                portfolio["positions"][ticker]["allocated_capital"] = new_total_capital
                portfolio["positions"][ticker]["entry_price"] = new_total_capital / new_total_shares
                portfolio["positions"][ticker]["strategy_bucket"] = bucket
            else:
                portfolio["positions"][ticker] = {
                    "entry_price": current_price,
                    "shares": shares,
                    "allocated_capital": amount,
                    "highest_price_seen": current_price,
                    "strategy_bucket": bucket,
                    "purchase_date": datetime.now(ZoneInfo("America/New_York")).strftime('%Y-%m-%d')
                }
                
            msg = f"🟢 *GEMINI EXECUTION: BUY {ticker} [{bucket}]*\n\nAllocated: ${round(amount, 2)}\nShares: {round(shares, 4)} @ ${current_price}\nRationale: {rationale}"
            log_and_print(f"{GREEN}{msg}{RESET}")
            send_notification(msg)

    save_portfolio(portfolio)
    log_and_print(f"{CYAN}[✓] Barbell Portfolio balanced and committed to disk. Current Cash: ${round(portfolio['cash_balance'], 2)}{RESET}")

# ==========================================
# 6. CLOUD & LOCAL EXECUTION MANAGER (UTC SYNCHRONIZED)
# ==========================================
if __name__ == "__main__":
    # --- CLOUD MODE (GitHub Actions Standalone Integration) ---
    if "--single-run" in sys.argv:
        log_and_print(f"{CYAN}[⚙️ Cloud] Triggering standalone strategic matrix execution...{RESET}")
        send_notification("🤖 *Gemini Autopilot:* Initializing cloud execution cycle...")
        
        portfolio = load_portfolio()
        universe = get_dynamic_universe()
        buy_setups = run_two_stage_screener(universe)
        risk_matrix = evaluate_open_positions(portfolio)
        
        decisions = query_gemini_brain(buy_setups, portfolio, risk_matrix)
        execute_trades(decisions, portfolio, buy_setups, risk_matrix)
        
        log_and_print(f"{GREEN}[✓] Cloud matrix cycle complete. Shutting down server.{RESET}")
        sys.exit(0)
        
    # --- LOCAL MODE (Your Background Machine Daemon) ---
    log_and_print(f"{CYAN}===================================================={RESET}")
    log_and_print(f"{CYAN}  GEMINI BARBELL MODEL DAEMON STATUS: OPERATIONAL   {RESET}")
    log_and_print(f"{CYAN}===================================================={RESET}")
    
    SCHEDULE = [
        {"hour": 13, "minute": 0, "name": "PRE-MARKET"},    # 13:00 UTC = 9:00 AM New York / 10:30 PM Darwin
        {"hour": 20, "minute": 15, "name": "AFTER-CLOSE"}   # 20:15 UTC = 4:15 PM New York / 5:45 AM Darwin
    ]
    
    last_run_signature = ""

    while True:
        try:
            now_utc = datetime.now(ZoneInfo("UTC"))
            current_date_str = now_utc.strftime('%Y-%m-%d')
            
            if now_utc.weekday() < 5: 
                for session in SCHEDULE:
                    if now_utc.hour == session["hour"] and now_utc.minute == session["minute"]:
                        
                        current_signature = f"{current_date_str}-{session['name']}"
                        
                        if last_run_signature != current_signature:
                            log_and_print(f"{CYAN}[🤖 Autopilot] UTC Target {session['name']} Hit! Initiating Matrix...{RESET}")
                            send_notification(f"🤖 *Gemini Autopilot:* Launching {session['name']} matrix scan...")
                            
                            portfolio = load_portfolio()
                            universe = get_dynamic_universe()
                            buy_setups = run_two_stage_screener(universe)
                            risk_matrix = evaluate_open_positions(portfolio)
                            
                            decisions = query_gemini_brain(buy_setups, portfolio, risk_matrix)
                            execute_trades(decisions, portfolio, buy_setups, risk_matrix)
                            
                            last_run_signature = current_signature
                            log_and_print(f"{GREEN}[✓] {session['name']} cycle complete. Returning to background sleep.{RESET}")
            
            time.sleep(30)
            
        except Exception as e:
            log_and_print(f"{RED}[❌ Daemon Error] {e}{RESET}")
            time.sleep(60)
