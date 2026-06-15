import os
import sys
import json
import time
import logging
import re
import pandas as pd
import numpy as np
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
import yfinance as yf
from google import genai
from google.genai import types
import warnings

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
    format='%(asctime)s | %(levelname)s | %(message)s'
)

def log_and_print(message):
    print(message)
    clean_msg = re.sub(r'\033\[[0-9;]*m', '', message)
    logging.info(clean_msg)

try:
    ai_client = genai.Client(api_key=GEMINI_API_KEY)
except Exception as e:
    log_and_print(f"CRITICAL: Gemini Client failed to initialize: {e}")
    sys.exit(1)

GREEN = "\033[1;32m"
RED = "\033[1;31m"
CYAN = "\033[1;36m"
YELLOW = "\033[1;33m"
RESET = "\033[0m"

# ==========================================
# 2. PORTFOLIO & MARKET REGIME
# ==========================================
def load_portfolio():
    try:
        with open("portfolio.json", "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"cash_balance": 100000.0, "positions": {}}

def save_portfolio(data):
    with open("portfolio.json", "w") as f:
        json.dump(data, f, indent=4)

def send_notification(message):
    if not TELEGRAM_BOT_TOKEN or "YOUR_" in TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        log_and_print(f"Telegram notification failed: {e}")

def get_macro_universe():
    """Fetches S&P500 + Nasdaq 100 + Strategic Alpha Overlays."""
    tickers = set()
    log_and_print(f"{CYAN}[🌐 Universe] Compiling macro universe...{RESET}")
    
    try:
        sp500 = pd.read_html('https://en.wikipedia.org/wiki/List_of_S%26P_500_companies')[0]['Symbol'].tolist()
        tickers.update(sp500)
    except Exception as e:
        log_and_print(f"Warning: Failed to fetch S&P 500. {e}")

    try:
        ndx_tables = pd.read_html('https://en.wikipedia.org/wiki/Nasdaq-100', match='Ticker|Symbol')
        ndx_col = 'Ticker' if 'Ticker' in ndx_tables[0].columns else 'Symbol'
        tickers.update(ndx_tables[0][ndx_col].tolist())
    except Exception as e:
        log_and_print(f"Warning: Failed to fetch Nasdaq 100. {e}")

    strategic_alpha = ["NOW", "SHOP", "AXON", "SNOW", "CRM", "PLTR", "AMD", "QCOM", "MU", "ZETA", "COIN", "HOOD", "VST", "CELH", "AVGO", "MSFT", "LLY"]
    tickers.update(strategic_alpha)

    cleaned_tickers = [str(t).replace('.', '-') for t in tickers if isinstance(t, str)]
    log_and_print(f"{CYAN}[🌐 Universe] Total tracked equities: {len(cleaned_tickers)}{RESET}")
    return list(set(cleaned_tickers))

def check_market_regime():
    """Checks if the broader market is in a structural correction (>5% drawdown)."""
    log_and_print(f"{CYAN}[🔍 Regime] Assessing broader market drawdown levels...{RESET}")
    try:
        spy = yf.Ticker("^GSPC").history(period="60d")
        if spy.empty: 
            return False, 0.0
            
        high_60d = spy['High'].max()
        current_close = spy['Close'].iloc[-1]
        drawdown_pct = ((high_60d - current_close) / high_60d) * 100
        
        is_correction = drawdown_pct >= 5.0
        log_and_print(f"{CYAN}[🔍 Regime] S&P 500 Drawdown: {round(drawdown_pct, 2)}% | Correction Mode: {is_correction}{RESET}")
        return is_correction, drawdown_pct
    except Exception as e:
        log_and_print(f"Warning: Failed to check market regime. Defaulting to safe mode. {e}")
        return False, 0.0

# ==========================================
# 3. INSTITUTIONAL QUALITY SCREENER
# ==========================================
def run_macro_screener(tickers):
    """
    Quality Growth + 200-Week EMA Institutional Support Matrix.
    Prioritizes wide moats trading near historic structural baselines.
    """
    log_and_print(f"{CYAN}[⚙️ Matrix] Running institutional quality and 200-Week EMA screener...{RESET}")
    scored_candidates = {}
    
    for ticker in tickers:
        try:
            ticker_obj = yf.Ticker(ticker)
            daily_hist = ticker_obj.history(period="250d")
            if daily_hist.empty or len(daily_hist) < 200: continue
            
            d_closes = daily_hist['Close']
            current_price = float(d_closes.iloc[-1])
            sma_200 = float(d_closes.iloc[-200:].mean())

            info = ticker_obj.info
            roe = info.get('returnOnEquity') or 0
            rev_growth = info.get('revenueGrowth') or 0
            fwd_pe = info.get('forwardPE') or 0
            operating_cash = info.get('operatingCashflow') or 0
            
            # Strict Baseline Quality Filter
            if roe < 0.12 or operating_cash <= 0 or rev_growth <= 0:
                continue

            # Calculate 200-Week EMA
            weekly_hist = ticker_obj.history(period="5y", interval="1wk")
            if len(weekly_hist) < 200: continue
            w_closes = weekly_hist['Close'].dropna()
            
            ema_200_series = w_closes.ewm(span=200, adjust=False).mean()
            current_w_ema200 = float(ema_200_series.iloc[-1])
            
            # Check proximity to 200-Week EMA (Within 4% above or 2% below)
            price_to_ema_ratio = current_price / current_w_ema200
            is_touching_weekly_ema200 = (0.98 <= price_to_ema_ratio <= 1.04)

            score = 0
            setup_tag = "Standard Trend Growth"
            
            if is_touching_weekly_ema200:
                score += 50
                setup_tag = "GENERATIONAL 200-WEEK EMA SUPPORT"
            elif current_price > sma_200:
                score += 20  
                
            if roe >= 0.22: score += 15
            if rev_growth >= 0.15: score += 15
            if 0 < fwd_pe <= 35: score += 10

            if score >= 40:
                scored_candidates[ticker] = {
                    "price": round(current_price, 2),
                    "macro_score": score,
                    "setup_type": setup_tag,
                    "weekly_ema_200": round(current_w_ema200, 2),
                    "return_on_equity_pct": round(roe * 100, 2),
                    "rev_growth_pct": round(rev_growth * 100, 2),
                    "forward_pe": round(fwd_pe, 2) if fwd_pe else "N/A"
                }
            time.sleep(0.05)
        except:
            continue

    return dict(sorted(scored_candidates.items(), key=lambda x: x[1]['macro_score'], reverse=True)[:12])

# ==========================================
# 4. LIVING FUNDAMENTAL RISK MONITOR (WITH MACD)
# ==========================================
def evaluate_open_positions(portfolio):
    """
    Monitors structural integrity. Allows winners to run unchecked unless 
    technical or fundamental pillars fail. Uses Weekly MACD to spot bullish divergences.
    """
    risk_matrix = {}
    positions_value = 0
    temp_prices = {}
    
    log_and_print(f"{YELLOW}[📊 Audit] Evaluating structural trend and MACD momentum for portfolio holdings...{RESET}")
    
    for pos_key, pos_data in portfolio.get("positions", {}).items():
        ticker = pos_key.split('_')[0]
        try:
            current_price = float(yf.Ticker(ticker).history(period="5d")['Close'].iloc[-1])
            temp_prices[pos_key] = current_price
            positions_value += pos_data["shares"] * current_price
        except:
            temp_prices[pos_key] = pos_data["entry_price"]
            positions_value += pos_data["allocated_capital"]

    total_nav = portfolio["cash_balance"] + positions_value
    
    for pos_key, pos_data in portfolio.get("positions", {}).items():
        ticker = pos_key.split('_')[0]
        current_price = temp_prices[pos_key]
        current_weight = (pos_data["shares"] * current_price) / total_nav
        pnl_pct = ((current_price - pos_data["entry_price"]) / pos_data["entry_price"]) * 100
        
        try:
            ticker_obj = yf.Ticker(ticker)
            
            # Daily Technicals
            hist_d = ticker_obj.history(period="250d")
            closes = hist_d['Close']
            sma_50 = float(closes.iloc[-50:].mean())
            sma_200 = float(closes.iloc[-200:].mean())
            mom_60d = ((current_price - float(closes.iloc[-60])) / float(closes.iloc[-60])) * 100
            
            # Fundamentals
            info = ticker_obj.info
            live_rev_growth = info.get('revenueGrowth', 0) or 0
            
            # Weekly Technicals (200-Week EMA & MACD)
            hist_w = ticker_obj.history(period="5y", interval="1wk")
            w_closes = hist_w['Close'].dropna()
            
            ema_200_w = w_closes.ewm(span=200, adjust=False).mean()
            current_w_ema200 = float(ema_200_w.iloc[-1])
            
            # MACD Calculation
            ema_12 = w_closes.ewm(span=12, adjust=False).mean()
            ema_26 = w_closes.ewm(span=26, adjust=False).mean()
            macd_line = ema_12 - ema_26
            signal_line = macd_line.ewm(span=9, adjust=False).mean()
            macd_hist = macd_line - signal_line
            
            current_hist = macd_hist.iloc[-1]
            prev_hist = macd_hist.iloc[-2]
            
            # Evaluate Exit Signals
            risk_status = "HEALTHY RUNNER: Structural momentum intact. Do not touch."
            
            # 1. Emergency 200-Week Breakdown Check (10%+ drop)
            if current_price < (current_w_ema200 * 0.90):
                if current_hist < prev_hist:
                    risk_status = "CRITICAL WEEKLY BREAKDOWN: Price >10% below 200W EMA & MACD momentum worsening. EMERGENCY SELL."
                else:
                    risk_status = "DEEP DRAWDOWN WITH BULLISH DIVERGENCE: Price >10% below 200W EMA, but MACD histogram is rising. HOLD."
            
            # 2. Standard Daily Rules
            elif current_price < sma_200 and current_price >= (current_w_ema200 * 0.90):
                risk_status = "STRUCTURAL CAUTION: Price below 200-day SMA, but holding above extreme weekly breakdown levels. Potential Trim."
            elif current_price < sma_50 and mom_60d < -12:
                risk_status = "TAILWINDS FADING: Asset experiencing severe intermediate momentum loss. Potential Trim."
            elif live_rev_growth <= 0:
                risk_status = "FUNDAMENTAL REVERSAL: Quarterly revenue growth has flattened or turned negative. SELL."
                
            risk_matrix[pos_key] = {
                "ticker": ticker,
                "current_weight_pct": round(current_weight * 100, 2),
                "pnl_pct": round(pnl_pct, 2),
                "algo_risk_status": risk_status,
                "live_rev_growth_pct": round(live_rev_growth * 100, 2),
                "original_buying_thesis": pos_data.get("buying_thesis", "N/A")
            }
        except Exception as e:
            log_and_print(f"Warning: Failed to evaluate {ticker}. {e}")
            continue
            
    return risk_matrix, total_nav

# ==========================================
# 5. EXECUTION BRAIN & TRADE LOGIC
# ==========================================
def consolidate_decisions(decisions):
    consolidated = {}
    for d in decisions:
        ticker = d.get("ticker", "").upper()
        action = d.get("action", "").upper()
        bucket = d.get("strategy_bucket", "MACRO_SWING").upper()
        
        if not ticker or action == "HOLD":
            continue
            
        if action == "BUY":
            unique_key = f"{ticker}_BUY_{bucket}"
            if unique_key not in consolidated:
                consolidated[unique_key] = d
            else:
                consolidated[unique_key]["allocation_amount"] = float(consolidated[unique_key].get("allocation_amount", 0)) + float(d.get("allocation_amount", 0))
        else:
            unique_key = f"{ticker}_{action}_{bucket}_{len(consolidated)}"
            consolidated[unique_key] = d
                
    return list(consolidated.values())

def query_gemini_brain(buy_candidates, portfolio, risk_matrix, total_nav, is_correction):
    """
    Advanced Institutional AI Engine. 
    Outputs explicit transaction sizing parameters for BUYS, SELLS, and TRIMS.
    """
    
    system_prompt = f"""
    You are an elite Institutional Portfolio Manager executing a Top-Down Macro framework. 

    PORTFOLIO OPERATIONAL STATUS:
    * Current Estimated Portfolio NAV: ${round(total_nav, 2)}
    * Available Execution Cash: ${round(portfolio["cash_balance"], 2)}
    * BROADER MARKET CORRECTION ACTIVE: {is_correction}
    
    Active Positions Risk Registry: {json.dumps(risk_matrix)}
    Top Quant-Filtered Growth Opportunities (Raw Data): {json.dumps(buy_candidates)}

    INSTITUTIONAL DIRECTIVES & TRADING PROTOCOLS:
    1. PRICE ACTION & DRAWDOWN EVALUATION: Treat severe technical drawdowns in secular trends as high-conviction asymmetric buy opportunities where macro risk is already priced in.
    2. HISTORICAL MULTIPLE COMPRESSION: Cross-reference absolute metrics with 5-year historical medians to avoid value traps.
    3. EXACT TRANSACTION SIZING: 
       * For a BUY: Allocate an explicit dollar amount (typically ~10% of total NAV).
       * For a TRIM: Specify the exact dollar amount to liquidate AND the percentage of the current position being cut (e.g., trim 25% or 50% of the holding).
       * For a SELL: Liquidation value must equal 100% of the current asset value.

    INSTRUCTIONS:
    First, write a deep-dive "MARKET COMMENTARY" detailing your macro synthesis.
    Second, output the execution array in a strict JSON structure under the heading "JSON_DATA:".

    Output your response EXACTLY in this format:

    MARKET COMMENTARY:
    [Your institutional macro and price action analysis paragraph.]
    
    JSON_DATA:
    {{
        "trades": [
            {{
              "action": "BUY" or "SELL" or "TRIM" or "HOLD",
              "ticker": "TICKER",
              "strategy_bucket": "LONG_TERM_CORE" or "MACRO_SWING",
              "transaction_value_usd": 5000, 
              "position_action_percentage": 50,
              "analysis_display": "Technical/Fundamental rationale detailing historical multiple compression and drawdown state."
            }}
        ]
    }}
    """
    
    log_and_print(f"{YELLOW}[🧠 Gemini] Submitting data to Macro & Sizing Engine...{RESET}")
    
    try:
        response = ai_client.models.generate_content(
            model='gemini-3.1-pro-preview',
            contents=system_prompt,
            config=types.GenerateContentConfig(
                temperature=0.15,
                thinking_config=types.ThinkingConfig(thinking_level="high")
            )
        )
        
        raw_text = response.text.strip()
        
        commentary = ""
        if "JSON_DATA:" in raw_text:
            parts = raw_text.split("JSON_DATA:")
            commentary = parts[0].replace("MARKET COMMENTARY:", "").strip()
            json_str = parts[1].strip()
        else:
            json_str = raw_text
        
        if json_str.startswith("```json"):
            json_str = json_str.replace("
```json\n", "").replace("```", "").strip()
        elif json_str.startswith("```"):
            json_str = json_str.replace("
```\n", "").replace("```", "").strip()
            
        data = json.loads(json_str)
        trades = data.get("trades", data)
        
        if commentary:
            log_and_print(f"{GREEN}[🧠 Gemini] Price action scan complete.{RESET}")
            send_notification(f"📊 *Institutional Macro & Technical Report:*\n\n{commentary}")
        
        return consolidate_decisions(trades)
        
    except Exception as e:
        log_and_print(f"{RED}[❌ System] GenAI Strategy query failed: {e}{RESET}")
        return []

def execute_trades(agent_decisions, portfolio, buy_candidates, risk_matrix, total_nav, is_correction):
    if not agent_decisions:
        log_and_print(f"{YELLOW}[⚠] No strategic trades ordered today.{RESET}")
        return
    
    # 4% safe cash floor during normal markets, 0.5% during corrections
    CASH_RESERVE_FLOOR = total_nav * 0.005 if is_correction else total_nav * 0.04
    max_allocation = round(total_nav * 0.12, 2)
        
    for decision in agent_decisions:
        action = decision.get("action", "").upper()
        ticker = decision.get("ticker", "").upper()
        # Extract new strict sizing metrics from the AI (fallback to old allocation_amount if needed)
        value = float(decision.get("transaction_value_usd", decision.get("allocation_amount", 0)))
        pct_to_trim = float(decision.get("position_action_percentage", 0))
        bucket = decision.get("strategy_bucket", "MACRO_SWING").upper()
        rationale = decision.get("analysis_display", "")
        
        if action == "HOLD": continue
            
        elif action in ["SELL", "TRIM"]:
            target_key = f"{ticker}_{bucket}"
            keys_to_process = [target_key] if target_key in portfolio["positions"] else [pk for pk in portfolio["positions"] if pk.startswith(ticker)]
            
            for pk in list(keys_to_process):
                pos = portfolio["positions"][pk]
                current_price = risk_matrix.get(pk, {}).get("current_price", pos["entry_price"])
                
                if action == "SELL":
                    total_reclaimed = pos["shares"] * current_price
                    portfolio["cash_balance"] += total_reclaimed
                    del portfolio["positions"][pk]
                    
                    msg = f"🔴 *TOTAL POSITION LIQUIDATION: {ticker}*\n\nAction: Selling 100% of holdings\nTotal Capital Reclaimed: ${round(total_reclaimed, 2)}\nReasoning: {rationale}"
                    log_and_print(f"{RED}{msg}{RESET}"); send_notification(msg)
                    
                elif action == "TRIM":
                    # Determine trim amount via explicit AI percentage, or fallback to AI dollar value
                    if pct_to_trim > 0:
                        trim_amount = (pos["shares"] * current_price) * (pct_to_trim / 100.0)
                    else:
                        trim_amount = min(value, pos["shares"] * current_price)
                        pct_to_trim = (trim_amount / (pos["shares"] * current_price)) * 100 if pos["shares"] > 0 else 0
                        
                    portfolio["cash_balance"] += trim_amount
                    pos["shares"] -= (trim_amount / current_price)
                    pos["allocated_capital"] = pos["shares"] * pos["entry_price"]
                    
                    msg = f"🟠 *PORTFOLIO POSITION TRIM: {ticker}*\n\nAction: Liquidating {round(pct_to_trim, 1)}% of position\nCash Value Reclaimed: ${round(trim_amount, 2)}\nReasoning: {rationale}"
                    log_and_print(f"{YELLOW}{msg}{RESET}"); send_notification(msg)
            
        elif action == "BUY" and ticker in buy_candidates:
            if (portfolio["cash_balance"] - value) < CASH_RESERVE_FLOOR:
                value = max(0.0, portfolio["cash_balance"] - CASH_RESERVE_FLOOR)
                
            current_exposure = sum(p["allocated_capital"] for pk, p in portfolio["positions"].items() if pk.startswith(ticker))
            if current_exposure + value > max_allocation:
                value = max(0.0, max_allocation - current_exposure)
                
            if value <= 500: continue
                
            current_price = buy_candidates[ticker]["price"]
            shares = value / current_price
            portfolio["cash_balance"] -= value
            pos_key = f"{ticker}_{bucket}"
            
            if pos_key in portfolio["positions"]:
                old_shares = portfolio["positions"][pos_key]["shares"]
                old_capital = portfolio["positions"][pos_key]["allocated_capital"]
                portfolio["positions"][pos_key]["shares"] = old_shares + shares
                portfolio["positions"][pos_key]["allocated_capital"] = old_capital + value
                portfolio["positions"][pos_key]["entry_price"] = (old_capital + value) / (old_shares + shares)
            else:
                portfolio["positions"][pos_key] = {
                    "entry_price": current_price,
                    "shares": shares,
                    "allocated_capital": value,
                    "strategy_bucket": bucket,
                    "purchase_date": datetime.now(ZoneInfo("America/New_York")).strftime('%Y-%m-%d'),
                    "buying_thesis": rationale
                }
                
            msg = f"🟢 *STRATEGIC ACQUISITION: {ticker}*\n\nSetup: {buy_candidates[ticker].get('setup_type', 'Standard')}\nAllocated: ${round(value, 2)} @ ${round(current_price, 2)}\nThesis: {rationale}"
            log_and_print(f"{GREEN}{msg}{RESET}"); send_notification(msg)

    save_portfolio(portfolio)

    def check_and_send_weekly_summary(portfolio, risk_matrix, total_nav):
    """
    Evaluates system time. If running a weekend close or forced flag,
    aggregates and transmits portfolio total value, cash position, and holding valuations.
    """
    import sys
    
    # Triggers automatically on Fridays (weekday 4), or via a custom command-line flag
    if "--weekly-summary" in sys.argv or datetime.now(ZoneInfo("America/New_York")).weekday() == 4:
        log_and_print(f"{YELLOW}[📊 System] Generating End-of-Week Portfolio Summary...{RESET}")
        
        cash = portfolio.get("cash_balance", 0)
        total_holdings_value = total_nav - cash
        
        holdings_breakdown = ""
        for ticker, details in risk_matrix.items():
            current_val = details.get("current_value", 0)
            weight = details.get("current_weight_pct", 0)
            
            if current_val > 0:
                holdings_breakdown += f"  • *{ticker.split('_')[0]}*: ${round(current_val, 2)} ({round(weight, 1)}%)\n"
        
        if not holdings_breakdown:
            holdings_breakdown = "  • No active stock positions (100% Cash)\n"
            
        summary_message = (
            "🗓️ *END-OF-WEEK PORTFOLIO VALUATION REPORT*\n"
            "===================================\n\n"
            f"💰 *Total Net Asset Value (NAV):* ${round(total_nav, 2)}\n"
            f"💵 *Available Cash Position:* ${round(cash, 2)}\n"
            f"📈 *Total Stock Equity Value:* ${round(total_holdings_value, 2)}\n\n"
            "📦 *Current Asset Allocations:*\n"
            f"{holdings_breakdown}\n"
            "===================================\n"
            "🚨 _System status: Operational. Next risk matrix scan scheduled for Monday open._"
        )
        
        send_notification(summary_message)
        log_and_print(f"{GREEN}[📊 System] Weekly report delivered successfully.{RESET}")

# ==========================================
# 6. MAIN EXECUTION LOOP
# ==========================================
if __name__ == "__main__":
    log_and_print(f"{CYAN}=================================================={RESET}")
    log_and_print(f"{CYAN}   🚀 INITIALIZING INSTITUTIONAL GROWTH ENGINE    {RESET}")
    log_and_print(f"{CYAN}=================================================={RESET}")
    
    portfolio = load_portfolio()
    universe = get_macro_universe()
    
    # 1. Market Health Check
    is_correction, current_drawdown = check_market_regime()
    
    # 2. Quality Screener (GARP + 200W EMA)
    buy_setups = run_macro_screener(universe)
    
    # 3. Live Risk Monitor (Uncapped + Weekly MACD Divergences)
    risk_matrix, current_nav = evaluate_open_positions(portfolio)
    
    # 4. AI Strategy Decision Engine
    decisions = query_gemini_brain(buy_setups, portfolio, risk_matrix, current_nav, is_correction)
    
    # 5. Broker Execution
    execute_trades(decisions, portfolio, buy_setups, risk_matrix, current_nav, is_correction)

    # ---------------------------------------------------------
    # FINAL STEP: Check if it's Friday and send the weekly wrap-up
    # ---------------------------------------------------------
    check_and_send_weekly_summary(portfolio, risk_matrix, total_nav)
    
    log_and_print(f"{CYAN}=================================================={RESET}")
    log_and_print(f"{CYAN}         ✅ SYSTEM OPERATION COMPLETED            {RESET}")
    log_and_print(f"{CYAN}=================================================={RESET}")
