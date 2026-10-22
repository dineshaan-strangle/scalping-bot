# V1 SCALPING PRO - FINAL - AMOUNT ASK + 25X + REAL TP/SL
import ccxt
import time
import asyncio
import pandas as pd
import os
import threading
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

# ========== PUTHU OPTIONS ==========
LEVERAGE = 25  # Option 2: 25x fix
FIXED_DOLLARS = None  # Option 1: User kitta ketkura amount
ASKING_AMOUNT = 1

TP_PCT = 0.004  # 0.4% TP
SL_PCT = 0.008  # 0.8% SL
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

# ========== FLASK FOR RENDER PORT FIX ==========
web_app = Flask(__name__)
@web_app.route('/')
def home():
    return f"<h1>🚀 V1 BOT RUNNING</h1><p>Mode: {'PAPER' if PAPER_TRADE else 'REAL'}</p><p>Symbol: {SYMBOL}</p><p>Leverage: {LEVERAGE}x</p><p>Fixed $: {FIXED_DOLLARS}</p><p>TP: {TP_PCT*100}% SL: {SL_PCT*100}%</p><p>Position: {position}</p>"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host='0.0.0.0', port=port)

threading.Thread(target=run_web, daemon=True).start()

# ========== FUNCTIONS ==========
def set_leverage_25x():
    if PAPER_TRADE:
        print(f"[PAPER] Leverage {LEVERAGE}x set")
        return
    try:
        exchange.private_post_v2_products_leverage({
            'product_id': 27,
            'leverage': str(LEVERAGE)
        })
        print(f"✅ Leverage {LEVERAGE}x set")
    except Exception as e:
        print(f"Leverage info: {e}")

def get_indicators():
    try:
        ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=250)
        df = pd.DataFrame(ohlcv, columns=['t','o','h','l','c','v'])
        df['sma20'] = df['c'].rolling(20).mean()
        df['std20'] = df['c'].rolling(20).std()
        df['upper'] = df['sma20'] + 2*df['std20']
        df['lower'] = df['sma20'] - 2*df['std20']
        df['ema200'] = df['c'].ewm(span=200).mean()
        delta = df['c'].diff()
        gain = delta.clip(lower=0).ewm(alpha=1/14).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1/14).mean()
        df['rsi'] = 100 - (100/(1+gain/loss))
        return df.iloc[-1], df['c'].iloc[-1]
    except Exception as e:
        print(f"Indicator Error: {e}")
        return None, None

def get_qty_from_dollars(dollars, price):
    if dollars is None:
        dollars = 10
    notional = dollars * LEVERAGE
    qty = int(notional)
    return max(1, qty)

def place_order_with_tpsl(side, qty, entry_price_ref):
    if PAPER_TRADE:
        tp = entry_price_ref * (1 + TP_PCT) if side == 'buy' else entry_price_ref * (1 - TP_PCT)
        sl = entry_price_ref * (1 - SL_PCT) if side == 'buy' else entry_price_ref * (1 + SL_PCT)
        print(f"[PAPER] {side} {qty} @ {entry_price_ref} | TP {tp:.2f} | SL {sl:.2f} | {LEVERAGE}x")
        return {"id": "paper"}

    try:
        set_leverage_25x()
        order = exchange.create_market_order(SYMBOL, side, qty)
        print(f"Entry done: {side} {qty}")

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

        # TP Limit
        try:
            exchange.create_order(SYMBOL, 'limit', tp_side, qty, tp_price, {'reduce_only': True})
            print(f"✅ TP placed @ {tp_price}")
        except Exception as e:
            print(f"TP Error: {e}")

        # SL Stop Market
        try:
            exchange.create_order(SYMBOL, 'market', sl_side, qty, None, {'stop_price': sl_price, 'reduce_only': True, 'stop_order_type': 'stop_loss_order'})
            # fallback simple stop
            exchange.create_order(SYMBOL, 'limit', sl_side, qty, sl_price, {'stop_price': sl_price, 'reduce_only': True})
            print(f"✅ SL placed @ {sl_price}")
        except Exception as e:
            print(f"SL Error (trying alt): {e}")
            try:
                exchange.create_order(SYMBOL, 'market', sl_side, qty, None, {'stop_price': sl_price})
            except Exception as e2:
                print(f"SL final error: {e2}")

        return order
    except Exception as e:
        print(f"Order Error: {e}")
        return None

# ========== TELEGRAM HANDLERS ==========
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🚀 V1 DELTA BOT\n\n"
        f"Leverage: {LEVERAGE}x (Fixed)\n"
        f"TP: {TP_PCT*100}% | SL: {SL_PCT*100}%\n"
        f"Symbol: {SYMBOL}\n\n"
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
            f"Scalping start aagidum. /pnl check pannalam."
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
            await update.message.reply_text(f"📊 {position} PNL: {pnl*100:.2f}%\nEntry: {entry_price}\nFixed: {FIXED_DOLLARS}$ {LEVERAGE}x")
    else:
        await update.message.reply_text(f"No position\nFixed: {FIXED_DOLLARS}$ {LEVERAGE}x")

async def close_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global position
    if position:
        _, price = get_indicators()
        qty = get_qty_from_dollars(FIXED_DOLLARS, price or 85000)
        side = 'sell' if position=="LONG" else 'buy'
        if PAPER_TRADE:
            print(f"[PAPER] close {side}")
        else:
            try:
                exchange.create_market_order(SYMBOL, side, qty, None, {'reduce_only': True})
            except:
                exchange.create_market_order(SYMBOL, side, qty)
        await update.message.reply_text(f"✅ Closed {position}")
        position = None
    else:
        await update.message.reply_text("No position")

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Paper: {PAPER_TRADE}\nFixed $: {FIXED_DOLLARS}\nLeverage: {LEVERAGE}x\nTP/SL: {TP_PCT*100}% / {SL_PCT*100}%")

# ========== TRADING LOOP ==========
async def trading_loop(app):
    global position, entry_price, be_done
    try:
        await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🚀 V1 BOT STARTED\nSend /start to set $ amount\nLeverage {LEVERAGE}x | TP/SL ON")
    except Exception as e:
        print(f"TG Error: {e}")
    
    while True:
        try:
            if FIXED_DOLLARS is None:
                time.sleep(10)
                continue
            
            ind, price = get_indicators()
            if ind is None:
                time.sleep(10)
                continue
            
            qty = get_qty_from_dollars(FIXED_DOLLARS, price)

            if position is None:
                long_cond = (price < ind['lower']*1.001) and (ind['rsi'] < 35) and (price > ind['ema200'])
                short_cond = (price > ind['upper']*0.999) and (ind['rsi'] > 65) and (price < ind['ema200'])
                if long_cond:
                    place_order_with_tpsl('buy', qty, price)
                    position = "LONG"; entry_price = price; be_done=False
                    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🟢 LONG @ {price:.2f}\nQty:{qty} ({FIXED_DOLLARS}$ x {LEVERAGE}x)\nTP {TP_PCT*100}% SL {SL_PCT*100}%")
                elif short_cond:
                    place_order_with_tpsl('sell', qty, price)
                    position = "SHORT"; entry_price = price; be_done=False
                    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🔴 SHORT @ {price:.2f}\nQty:{qty} ({FIXED_DOLLARS}$ x {LEVERAGE}x)\nTP {TP_PCT*100}% SL {SL_PCT*100}%")
            else:
                pnl_pct = (price-entry_price)/entry_price if position=="LONG" else (entry_price-price)/entry_price
                if not be_done and pnl_pct >= BE_TRIGGER:
                    be_done=True
                    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🔒 BE HIT +{pnl_pct*100:.2f}%")
                if pnl_pct <= -SL_PCT or pnl_pct >= TP_PCT:
                    result = "✅ TP" if pnl_pct>0 else "❌ SL"
                    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"{result} {position} {pnl_pct*100:.2f}%")
                    position=None

            time.sleep(30)
        except Exception as e:
            print(f"Loop Error {e}")
            time.sleep(10)

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
    
    loop = asyncio.new_event_loop()
    def start_loop():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(trading_loop(app))
    threading.Thread(target=start_loop, daemon=True).start()
    app.run_polling()

if __name__ == "__main__":
    run()
