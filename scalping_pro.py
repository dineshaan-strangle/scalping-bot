# V1 SCALPING PRO - RENDER WEB SERVICE FIX - DELTA + PAPER
import ccxt
import time
import asyncio
import pandas as pd
import os
import threading
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ========== RENDER ENV ==========
DELTA_API_KEY = os.getenv("DELTA_API_KEY")
DELTA_API_SECRET = os.getenv("DELTA_API_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SYMBOL = os.getenv("SYMBOL", "BTCUSD")
TIMEFRAME = os.getenv("TIMEFRAME", "5m")
AMOUNT_CONTRACTS = int(os.getenv("AMOUNT", "10"))
PAPER_TRADE = os.getenv("PAPER_TRADE", "True") == "True"

TP_PCT = 0.004
SL_PCT = 0.008
BE_TRIGGER = 0.0025

print(f"STARTING: {SYMBOL} Paper={PAPER_TRADE}")

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
    return f"<h1>🚀 V1 BOT RUNNING</h1><p>Mode: {'PAPER' if PAPER_TRADE else 'REAL'}</p><p>Symbol: {SYMBOL}</p><p>Position: {position}</p><p>Entry: {entry_price}</p>"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host='0.0.0.0', port=port)

threading.Thread(target=run_web, daemon=True).start()
# ============================================

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

def place_order(side, qty):
    if PAPER_TRADE:
        print(f"[PAPER] {side} {qty}")
        return {"id": "paper"}
    else:
        return exchange.create_market_order(SYMBOL, side, qty)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = "PAPER 📝" if PAPER_TRADE else "REAL 💰"
    await update.message.reply_text(f"🚀 V1 DELTA BOT LIVE ON RENDER\nMode: {mode}\nSymbol: {SYMBOL}\nPosition: {position if position else 'Waiting'}")

async def pnl_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if position:
        _, price = get_indicators()
        pnl = (price-entry_price)/entry_price if position=="LONG" else (entry_price-price)/entry_price
        await update.message.reply_text(f"📊 PNL: {pnl*100:.2f}% {position}")
    else:
        await update.message.reply_text("No position")

async def close_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global position
    if position:
        side = 'sell' if position=="LONG" else 'buy'
        place_order(side, AMOUNT_CONTRACTS)
        await update.message.reply_text(f"✅ Closed {position}")
        position = None
    else:
        await update.message.reply_text("No position")

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Paper Mode ON" if PAPER_TRADE else "Real Mode")

async def trading_loop(app):
    global position, entry_price, be_done
    try:
        await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🚀 V1 BOT STARTED ON RENDER (WEB SERVICE)\nMode: {'PAPER 📝' if PAPER_TRADE else 'REAL 💰'}")
    except Exception as e:
        print(f"TG Error: {e}")
    while True:
        try:
            ind, price = get_indicators()
            if ind is None:
                time.sleep(10)
                continue
            if position is None:
                long_cond = (price < ind['lower']*1.001) and (ind['rsi'] < 35) and (price > ind['ema200'])
                short_cond = (price > ind['upper']*0.999) and (ind['rsi'] > 65) and (price < ind['ema200'])
                if long_cond:
                    place_order('buy', AMOUNT_CONTRACTS)
                    position = "LONG"; entry_price = price; be_done=False
                    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🟢 LONG @ {price:.2f} (Paper)")
                elif short_cond:
                    place_order('sell', AMOUNT_CONTRACTS)
                    position = "SHORT"; entry_price = price; be_done=False
                    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🔴 SHORT @ {price:.2f} (Paper)")
            else:
                pnl_pct = (price-entry_price)/entry_price if position=="LONG" else (entry_price-price)/entry_price
                if not be_done and pnl_pct >= BE_TRIGGER:
                    be_done=True
                    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🔒 BE +{pnl_pct*100:.2f}%")
                if pnl_pct <= -SL_PCT or pnl_pct >= TP_PCT:
                    side = 'sell' if position=="LONG" else 'buy'
                    place_order(side, AMOUNT_CONTRACTS)
                    result = "✅ TP" if pnl_pct>0 else "❌ SL"
                    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"{result} {position} {pnl_pct*100:.2f}%")
                    position=None
            time.sleep(30)
        except Exception as e:
            print(f"Loop Error {e}")
            time.sleep(10)

def run():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
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
