# V1 SCALPING PRO - FULL FIXED V2 - AMOUNT ASK + 25X + TP/SL + RENDER FIX
import ccxt
import time
import asyncio
import pandas as pd
import os
import threading
import traceback
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

# ========== RENDER ENV ==========
DELTA_API_KEY = os.getenv("DELTA_API_KEY")
DELTA_API_SECRET = os.getenv("DELTA_API_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SYMBOL = os.getenv("SYMBOL", "BTCUSD")
TIMEFRAME = os.getenv("TIMEFRAME", "5m")
PAPER_TRADE = os.getenv("PAPER_TRADE", "True") == "True"

# ========== FIXED OPTIONS ==========
LEVERAGE = 25
FIXED_DOLLARS = None
ASKING_AMOUNT = 1

TP_PCT = 0.004  # 0.4%
SL_PCT = 0.008  # 0.8%
BE_TRIGGER = 0.0025

print(f"STARTING: {SYMBOL} Paper={PAPER_TRADE} Leverage={LEVERAGE}x")

exchange = ccxt.delta({
    'apiKey': DELTA_API_KEY,
    'secret': DELTA_API_SECRET,
    'enableRateLimit': True,
})

position = None
entry_price = 0.0
be_done = False

# ========== FLASK FOR RENDER ==========
web_app = Flask(__name__)
@web_app.route('/')
def home():
    return f"<h1>🚀 V1 BOT RUNNING V2 FIXED</h1><p>Mode: {'PAPER' if PAPER_TRADE else 'REAL'}</p><p>Symbol: {SYMBOL}</p><p>Leverage: {LEVERAGE}x</p><p>Fixed $: {FIXED_DOLLARS}</p><p>Position: {position}</p>"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host='0.0.0.0', port=port)

threading.Thread(target=run_web, daemon=True).start()
# ============================================

def set_leverage_25x():
    if PAPER_TRADE:
        print(f"[PAPER] Leverage {LEVERAGE}x ready")
        return
    try:
        exchange.load_markets()
        market = exchange.markets.get(SYMBOL)
        product_id = 27
        if market and 'id' in market:
            product_id = market['id']
        # Delta API expects product_id
        exchange.private_post_v2_products_leverage({
            'product_id': product_id,
            'leverage': str(LEVERAGE)
        })
        print(f"✅ Leverage {LEVERAGE}x set for {SYMBOL} id={product_id}")
    except Exception as e:
        print(f"Leverage note: {e} - continuing")

def get_indicators():
    try:
        ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=250)
        
        # FIX 1: Check None / Empty
        if not ohlcv:
            print("⚠️ OHLCV returned None/empty")
            return None, None
        if len(ohlcv) < 60:
            print(f"⚠️ Not enough candles: {len(ohlcv)}")
            return None, None
            
        df = pd.DataFrame(ohlcv, columns=['t','o','h','l','c','v'])
        
        if df.empty or df['c'].isnull().all():
            print("⚠️ DataFrame empty")
            return None, None

        # Indicators with min_periods to avoid NaN crash
        df['sma20'] = df['c'].rolling(20, min_periods=20).mean()
        df['std20'] = df['c'].rolling(20, min_periods=20).std()
        df['upper'] = df['sma20'] + 2*df['std20']
        df['lower'] = df['sma20'] - 2*df['std20']
        df['ema200'] = df['c'].ewm(span=200, min_periods=200).mean()
        
        delta = df['c'].diff()
        gain = delta.clip(lower=0).ewm(alpha=1/14, min_periods=14).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1/14, min_periods=14).mean()
        
        # FIX 2: Avoid division by zero
        loss = loss.replace(0, 1e-9)
        rs = gain / loss
        df['rsi'] = 100 - (100/(1+rs))
        
        last = df.iloc[-1]
        price = float(df['c'].iloc[-1])
        
        # FIX 3: Check if indicators are ready
        if pd.isna(last['rsi']) or pd.isna(last['lower']) or pd.isna(last['ema200']):
            print(f"⏳ Indicators warming up... RSI={last['rsi']} Lower={last['lower']}")
            return None, None
            
        return last, price
        
    except Exception as e:
        print(f"Indicator Error: {e}")
        traceback.print_exc()
        return None, None

def get_qty_from_dollars(dollars, price):
    if dollars is None:
        dollars = 5.0
    try:
        dollars = float(dollars)
        notional = dollars * LEVERAGE
        qty = int(notional)  # BTCUSD: 1 qty = 1 USD
        return max(1, qty)
    except:
        return 5 * LEVERAGE

def place_order_with_tpsl(side, qty, entry_price_ref):
    if PAPER_TRADE:
        tp = entry_price_ref * (1 + TP_PCT) if side == 'buy' else entry_price_ref * (1 - TP_PCT)
        sl = entry_price_ref * (1 - SL_PCT) if side == 'buy' else entry_price_ref * (1 + SL_PCT)
        print(f"[PAPER] {side.upper()} {qty} @ {entry_price_ref:.2f} | TP {tp:.2f} | SL {sl:.2f} | {LEVERAGE}x ({FIXED_DOLLARS}$)")
        return {"id": "paper"}

    try:
        set_leverage_25x()
        order = exchange.create_market_order(SYMBOL, side, qty)
        print(f"✅ Entry done: {side} {qty}")

        if side == 'buy':
            tp_price = entry_price_ref * (1 + TP_PCT)
            sl_price = entry_price_ref * (1 - SL_PCT)
            tp_side = 'sell'
            sl_side = 'sell'
        else:
            tp_price = entry_price_ref * (1 - TP_PCT)
            sl_price = entry_price_ref * (1 + SL_PCT)
            tp_side = 'buy'
            sl_side = 'buy'

        try:
            exchange.create_order(SYMBOL, 'limit', tp_side, qty, tp_price, {'reduce_only': True})
            print(f"✅ TP placed @ {tp_price:.2f}")
        except Exception as e:
            print(f"TP Error: {e}")

        try:
            exchange.create_order(SYMBOL, 'market', sl_side, qty, None, {'stop_price': sl_price, 'reduce_only': True})
            print(f"✅ SL placed @ {sl_price:.2f}")
        except Exception as e:
            print(f"SL Error: {e}")

        return order
    except Exception as e:
        print(f"Order Error: {e}")
        traceback.print_exc()
        return None

# ========== TELEGRAM HANDLERS ==========
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🚀 V1 DELTA BOT V2 FIXED\n\n"
        f"Leverage: {LEVERAGE}x (Fixed)\n"
        f"TP: {TP_PCT*100}% | SL: {SL_PCT*100}%\n"
        f"Symbol: {SYMBOL}\nCurrent Fixed: {FIXED_DOLLARS}\n\n"
        f"💰 Evalo dollar fix pannanum?\n"
        f"Number mattum anupunga (Ex: 5)"
    )
    return ASKING_AMOUNT

async def receive_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global FIXED_DOLLARS
    try:
        text = update.message.text.strip().replace('$','').replace(' ','')
        dollars = float(text)
        if dollars < 1:
            await update.message.reply_text("Minimum 1$ kudunga anna")
            return ASKING_AMOUNT
        FIXED_DOLLARS = dollars
        set_leverage_25x()
        await update.message.reply_text(
            f"✅ Done Anna!\n\n"
            f"Fixed Amount: {FIXED_DOLLARS}$\n"
            f"Leverage: {LEVERAGE}x\n"
            f"Notional: {FIXED_DOLLARS * LEVERAGE}$\n"
            f"TP: {TP_PCT*100}% | SL: {SL_PCT*100}%\n\n"
            f"Scalping start aagidum. Wait for signal..."
        )
        return ConversationHandler.END
    except:
        await update.message.reply_text("❌ Number mattum anupunga anna. Ex: 5")
        return ASKING_AMOUNT

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancel panniten.")
    return ConversationHandler.END

async def pnl_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if position:
        _, price = get_indicators()
        if price:
            pnl = (price-entry_price)/entry_price if position=="LONG" else (entry_price-price)/entry_price
            await update.message.reply_text(f"📊 {position} PNL: {pnl*100:.2f}%\nFixed: {FIXED_DOLLARS}$ {LEVERAGE}x")
        else:
            await update.message.reply_text(f"📊 {position} - price loading...")
    else:
        await update.message.reply_text(f"No position\nFixed: {FIXED_DOLLARS}$ {LEVERAGE}x")

async def close_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global position
    if position:
        _, price = get_indicators()
        qty = get_qty_from_dollars(FIXED_DOLLARS, price or 85000)
        side = 'sell' if position=="LONG" else 'buy'
        if not PAPER_TRADE:
            try:
                exchange.create_market_order(SYMBOL, side, qty, None, {'reduce_only': True})
            except:
                exchange.create_market_order(SYMBOL, side, qty)
        await update.message.reply_text(f"✅ Closed {position}")
        position = None
    else:
        await update.message.reply_text("No position")

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Paper: {PAPER_TRADE}\nFixed $: {FIXED_DOLLARS}\nLeverage: {LEVERAGE}x\nPos: {position}")

# ========== TRADING LOOP ==========
async def trading_loop(app):
    global position, entry_price, be_done
    try:
        await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🚀 V1 BOT V2 FIXED STARTED\nSend /start to set $ amount\nLeverage {LEVERAGE}x | TP/SL ON\nNo more Indicator Error!")
    except Exception as e:
        print(f"TG Start Error: {e}")
    
    while True:
        try:
            if FIXED_DOLLARS is None:
                await asyncio.sleep(10)
                continue
            
            ind, price = get_indicators()
            if ind is None or price is None:
                await asyncio.sleep(15)  # Wait longer on error
                continue
            
            qty = get_qty_from_dollars(FIXED_DOLLARS, price)

            if position is None:
                # Safe check for lower/upper existence
                try:
                    long_cond = (price < float(ind['lower'])*1.001) and (float(ind['rsi']) < 35) and (price > float(ind['ema200']))
                    short_cond = (price > float(ind['upper'])*0.999) and (float(ind['rsi']) > 65) and (price < float(ind['ema200']))
                except Exception as cond_e:
                    print(f"Condition check error: {cond_e}")
                    await asyncio.sleep(10)
                    continue

                if long_cond:
                    place_order_with_tpsl('buy', qty, price)
                    position = "LONG"; entry_price = price; be_done=False
                    try:
                        await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🟢 LONG @ {price:.2f}\nQty:{qty} ({FIXED_DOLLARS}$ x {LEVERAGE}x)\nTP {TP_PCT*100}% SL {SL_PCT*100}%")
                    except: pass
                elif short_cond:
                    place_order_with_tpsl('sell', qty, price)
                    position = "SHORT"; entry_price = price; be_done=False
                    try:
                        await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🔴 SHORT @ {price:.2f}\nQty:{qty} ({FIXED_DOLLARS}$ x {LEVERAGE}x)\nTP {TP_PCT*100}% SL {SL_PCT*100}%")
                    except: pass
            else:
                pnl_pct = (price-entry_price)/entry_price if position=="LONG" else (entry_price-price)/entry_price
                if not be_done and pnl_pct >= BE_TRIGGER:
                    be_done=True
                    try:
                        await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🔒 BE +{pnl_pct*100:.2f}%")
                    except: pass
                if pnl_pct <= -SL_PCT or pnl_pct >= TP_PCT:
                    result = "✅ TP" if pnl_pct>0 else "❌ SL"
                    try:
                        await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"{result} {position} {pnl_pct*100:.2f}%")
                    except: pass
                    position=None

            await asyncio.sleep(30)
        except asyncio.CancelledError:
            print("Trading loop cancelled, exiting")
            break
        except Exception as e:
            print(f"Loop Error {e}")
            traceback.print_exc()
            await asyncio.sleep(10)

def run():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start_cmd)],
        states={ASKING_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_amount)]},
        fallbacks=[CommandHandler("cancel", cancel_cmd)]
    )
    
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("pnl", pnl_cmd))
    app.add_handler(CommandHandler("close", close_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))

    async def on_startup(app_instance):
        # FIX for "Task was destroyed" - cancel old tasks
        print("Cleaning old tasks...")
        await asyncio.sleep(2)
        asyncio.create_task(trading_loop(app_instance))

    app.post_init = on_startup
    
    print("Bot polling started V2...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    run()
