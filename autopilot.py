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
    
    strategic_alpha = ["NOW", "SHOP", "AXON", "SNOW", "CRM", "PLTR", "AMD", "QCOM", "MU", "ZETA", "COIN", "HOOD", "VST", "CELH"]
    cleaned_tickers.extend(strategic_alpha)
    
    final_universe = list(set(cleaned_tickers))
    log_and_print(f"{GREEN}[✓] Total unique assets loaded into matrix: {len(final_universe)}{RESET}")
    return final_universe

# ==========================================
# 3. FAST SCAN & DEEP DIVE MATRICES
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
            if rsi > 55 and ((current_price - closes.iloc[-5])/closes.iloc[-5]*100) > 4: score += 20 
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
                "rev_growth_pct": round(rev_growth * 100, 2)
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
                "algo_risk_status": risk_status
            }
        except: continue
            
    return risk_matrix

# ==========================================
# 5. EXECUTION BRAIN & PORTFOLIO UPDATE
# ==========================================
def query_gemini_brain(buy_candidates, portfolio, risk_matrix):
    total_nav = portfolio["cash_balance"] + sum(p["allocated_capital"] for p in portfolio["positions"].values())
    cash_ratio = portfolio["cash_balance"] / total_nav if total_nav > 0 else 1.0
    
    max_allocation = round(total_nav * 0.10, 2)
    
    system_prompt = f"""
    You are an elite institutional asset manager running Gemini 3.1 Pro. Your mandate is to out-trade Claude and Grok.
    
    Total NAV: ${round(total_nav, 2)}
    Cash Ratio: {round(cash_ratio * 100, 2)}%
    Max Allowed Trade Size (10% of NAV): ${max_allocation}
    
    Active Portfolio: {json.dumps(portfolio)}
    Top 15 Market Setups (Includes Valuations): {json.dumps(buy_candidates)}
    Sell-Side Risk Matrix: {json.dumps(risk_matrix)}
    
    CORE DIRECTIVES:
    1. LET WINNERS RUN: If 'algo_risk_status' is 'SAFE - LET RUN', DO NOT SELL. We use trailing stops to capture massive runs.
    2. ELIMINATE CASH DRAG: If Cash Ratio is > 30%, you MUST be aggressive. Deploy capital into the highest scoring buy setups. 
    3. POSITION SIZING: Your maximum allowed buy order is ${max_allocation}. Do not exceed this to ensure proper diversification.
    4. RUTHLESS EXITS: If 'algo_risk_status' shows 'PROFIT SECURED' or 'CRITICAL', immediately execute a SELL to recycle capital.
    5. MULTI-ACTION CAPABILITY: You can return multiple actions if you need to sell multiple bad assets and buy multiple good ones.
    
    Output strictly in raw JSON format. Return an array of action objects. No markdown blocks.

    [
        {{
          "action": "BUY",
          "ticker": "TICKER",
          "allocation_amount": 5000,
          "analysis_display": "⚡ Apex Predator Matrix Executed... [RATIONALE]"
        }},
        {{
          "action": "SELL",
          "ticker": "TICKER",
          "allocation_amount": "ALL",
          "analysis_display": "🚨 Risk Threshold Breached... [RATIONALE]"
        }}
    ]
    """
    
    log_and_print(f"{YELLOW}[🧠 Gemini] Initiating Deep Thinking sequence via Google GenAI SDK...{RESET}")
    
    try:
        response = ai_client.models.generate_content(
            model='gemini-3.1-pro-preview', 
            contents=system_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json", 
                temperature=1.0,
                thinking_config=types.ThinkingConfig(thinking_level="high")
            )
        )
        
        raw_text = response.text.strip()
        if raw_text.startswith("```json"):
            raw_text = raw_text.replace("```json", "").replace("```", "").strip()
            
        return json.loads(raw_text)
    
    except Exception as e:
        log_and_print(f"{RED}[❌ System] Gemini API Error: {e}{RESET}")
        return []

def execute_trades(agent_decisions, portfolio, buy_candidates, risk_matrix):
    if not agent_decisions:
        log_and_print(f"{YELLOW}[⚠] No actions returned by Gemini.{RESET}")
        return
        
    for decision in agent_decisions:
        action = decision.get("action", "").upper()
        ticker = decision.get("ticker", "").upper()
        amount = decision.get("allocation_amount", 0)
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
            
            msg = f"🔴 *GEMINI EXECUTION: SELL {ticker}*\n\nProceeds: ${round(proceeds, 2)}\nRationale: {rationale}"
            log_and_print(f"{RED}{msg}{RESET}")
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
            else:
                portfolio["positions"][ticker] = {
                    "entry_price": current_price,
                    "shares": shares,
                    "allocated_capital": amount,
                    "highest_price_seen": current_price,
                    "purchase_date": datetime.now(ZoneInfo("America/New_York")).strftime('%Y-%m-%d')
                }
                
            msg = f"🟢 *GEMINI EXECUTION: BUY {ticker}*\n\nAllocated: ${round(amount, 2)}\nShares: {round(shares, 4)} @ ${current_price}\nRationale: {rationale}"
            log_and_print(f"{GREEN}{msg}{RESET}")
            send_notification(msg)

    save_portfolio(portfolio)
    log_and_print(f"{CYAN}[✓] Portfolio updated and saved to disk. Current Cash: ${round(portfolio['cash_balance'], 2)}{RESET}")

# ==========================================
# 6. CLOUD & LOCAL EXECUTION MANAGER
# ==========================================
if __name__ == "__main__":
    # --- CLOUD MODE (GitHub Actions) ---
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
        sys.exit(0) # Forcibly kills the script so GitHub stops billing minutes
        
    # --- LOCAL MODE (Your PC Background Daemon) ---
    log_and_print(f"{CYAN}===================================================={RESET}")
    log_and_print(f"{CYAN}   GEMINI AI FUND MANAGER DAEMON MODE ACTIVATED    {RESET}")
    log_and_print(f"{CYAN}===================================================={RESET}")
    
    SCHEDULE = [
        {"hour": 8, "minute": 30, "name": "PRE-MARKET"},
        {"hour": 16, "minute": 15, "name": "AFTER-CLOSE"}
    ]
    
    last_run_signature = ""

    while True:
        try:
            now_ny = datetime.now(ZoneInfo("America/New_York"))
            current_date_str = now_ny.strftime('%Y-%m-%d')
            
            # 0 = Monday, 4 = Friday.
            if now_ny.weekday() < 5: 
                for session in SCHEDULE:
                    if now_ny.hour == session["hour"] and now_ny.minute == session["minute"]:
                        
                        current_signature = f"{current_date_str}-{session['name']}"
                        
                        if last_run_signature != current_signature:
                            log_and_print(f"{CYAN}[🤖 Autopilot] {session['name']} target time hit! Triggering Matrix...{RESET}")
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