# V1 SCALPING PRO - V3 FINAL - FIXED CCXT MARKET BUG + 25X + TP/SL
import ccxt
import asyncio
import pandas as pd
import os
import threading
import traceback
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

DELTA_API_KEY = os.getenv("DELTA_API_KEY")
DELTA_API_SECRET = os.getenv("DELTA_API_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SYMBOL = os.getenv("SYMBOL", "BTC/USD:USD")
TIMEFRAME = os.getenv("TIMEFRAME", "5m")
PAPER_TRADE = os.getenv("PAPER_TRADE", "True") == "True"

LEVERAGE = 25
FIXED_DOLLARS = None
ASKING_AMOUNT = 1
TP_PCT = 0.004
SL_PCT = 0.008
BE_TRIGGER = 0.0025

print(f"STARTING V3 FINAL: {SYMBOL} Paper={PAPER_TRADE} Leverage={LEVERAGE}x", flush=True)

exchange = ccxt.delta({
    'apiKey': DELTA_API_KEY,
    'secret': DELTA_API_SECRET,
    'enableRateLimit': True,
})

try:
    print("⏳ Loading Delta markets...", flush=True)
    exchange.load_markets()
    print(f"✅ Markets loaded: {len(exchange.markets)}", flush=True)
    if SYMBOL not in exchange.markets:
        print(f"⚠️ {SYMBOL} not found, checking alternatives", flush=True)
        for alt in ["BTC/USD:USD", "BTC/USD", "BTC-PERP", "BTCUSD"]:
            if alt in exchange.markets:
                print(f"✅ Using symbol: {alt}", flush=True)
                SYMBOL = alt
                break
    print(f"FINAL SYMBOL: {SYMBOL}", flush=True)
except Exception as e:
    print(f"Market load error: {e}", flush=True)

position = None
entry_price = 0.0
be_done = False

web_app = Flask(__name__)
@web_app.route('/')
def home():
    return f"<h1>V3 RUNNING</h1><p>Symbol: {SYMBOL}</p><p>Mode: {'PAPER' if PAPER_TRADE else 'REAL'}</p><p>Fixed $: {FIXED_DOLLARS}</p><p>Position: {position}</p>"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host='0.0.0.0', port=port)

threading.Thread(target=run_web, daemon=True).start()

def set_leverage_25x():
    if PAPER_TRADE:
        return
    try:
        if not exchange.markets:
            exchange.load_markets()
        market = exchange.markets.get(SYMBOL)
        product_id = market.get('id', 27) if market else 27
        exchange.private_post_v2_products_leverage({
            'product_id': product_id,
            'leverage': str(LEVERAGE)
        })
        print(f"✅ Leverage {LEVERAGE}x set")
    except Exception as e:
        print(f"Leverage note: {e}")

def get_indicators():
    try:
        if not exchange.markets:
            exchange.load_markets()
        ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=250)
        if not ohlcv or len(ohlcv) < 60:
            print(f"⚠️ OHLCV short: {len(ohlcv) if ohlcv else 0}", flush=True)
            return None, None
        df = pd.DataFrame(ohlcv, columns=['t','o','h','l','c','v'])
        if df.empty:
            return None, None
        df['sma20'] = df['c'].rolling(20, min_periods=20).mean()
        df['std20'] = df['c'].rolling(20, min_periods=20).std()
        df['upper'] = df['sma20'] + 2*df['std20']
        df['lower'] = df['sma20'] - 2*df['std20']
        df['ema200'] = df['c'].ewm(span=200, min_periods=200).mean()
        delta = df['c'].diff()
        gain = delta.clip(lower=0).ewm(alpha=1/14, min_periods=14).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1/14, min_periods=14).mean()
        loss = loss.replace(0, 1e-9)
        rs = gain / loss
        df['rsi'] = 100 - (100/(1+rs))
        last = df.iloc[-1]
        price = float(df['c'].iloc[-1])
        if pd.isna(last['rsi']) or pd.isna(last['lower']):
            print(f"⏳ Warming up...", flush=True)
            return None, None
        return last, price
    except Exception as e:
        print(f"Indicator Error: {e}", flush=True)
        traceback.print_exc()
        return None, None

def get_qty_from_dollars(dollars, price):
    if dollars is None:
        dollars = 5.0
    try:
        notional = float(dollars) * LEVERAGE
        qty = int(notional)
        return max(1, qty)
    except:
        return 125

def place_order_with_tpsl(side, qty, entry_price_ref):
    if PAPER_TRADE:
        tp = entry_price_ref * (1 + TP_PCT) if side == 'buy' else entry_price_ref * (1 - TP_PCT)
        sl = entry_price_ref * (1 - SL_PCT) if side == 'buy' else entry_price_ref * (1 + SL_PCT)
        print(f"[PAPER] {side.upper()} {qty} @ {entry_price_ref:.2f} | TP {tp:.2f} | SL {sl:.2f}", flush=True)
        return {"id": "paper"}
    try:
        set_leverage_25x()
        order = exchange.create_market_order(SYMBOL, side, qty)
        print(f"✅ Entry: {side} {qty}")
        if side == 'buy':
            tp_price = entry_price_ref * (1 + TP_PCT)
            sl_price = entry_price_ref * (1 - SL_PCT)
            tp_side = 'sell'; sl_side = 'sell'
        else:
            tp_price = entry_price_ref * (1 - TP_PCT)
            sl_price = entry_price_ref * (1 + SL_PCT)
            tp_side = 'buy'; sl_side = 'buy'
        try:
            exchange.create_order(SYMBOL, 'limit', tp_side, qty, tp_price, {'reduce_only': True})
            print(f"✅ TP @ {tp_price:.2f}")
        except Exception as e:
            print(f"TP Error: {e}")
        try:
            exchange.create_order(SYMBOL, 'market', sl_side, qty, None, {'stop_price': sl_price, 'reduce_only': True})
            print(f"✅ SL @ {sl_price:.2f}")
        except Exception as e:
            print(f"SL Error: {e}")
        return order
    except Exception as e:
        print(f"Order Error: {e}")
        traceback.print_exc()
        return None

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🚀 V3 BOT FIXED\n\nLeverage: {LEVERAGE}x\nTP: {TP_PCT*100}% | SL: {SL_PCT*100}%\nSymbol: {SYMBOL}\nCurrent Fixed: {FIXED_DOLLARS}\n\n💰 Evalo dollar fix pannanum?\nNumber mattum anupunga (Ex: 5)")
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
        await update.message.reply_text(f"✅ Done Anna!\n\nFixed: {FIXED_DOLLARS}$\nLeverage: {LEVERAGE}x\nNotional: {FIXED_DOLLARS * LEVERAGE}$\nTP: {TP_PCT*100}% | SL: {SL_PCT*100}%\n\nScalping start aagidum.")
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
            await update.message.reply_text(f"📊 {position} PNL: {pnl*100:.2f}%")
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
    await update.message.reply_text(f"Paper: {PAPER_TRADE}\nFixed $: {FIXED_DOLLARS}\nLeverage: {LEVERAGE}x\nSymbol: {SYMBOL}\nPos: {position}")

async def trading_loop(app):
    global position, entry_price, be_done
    try:
        await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🚀 V3 BOT STARTED - NO ERROR\nSymbol {SYMBOL}\nLeverage {LEVERAGE}x | TP/SL ON\nSend /start to set $")
    except Exception as e:
        print(f"TG Error: {e}")
    while True:
        try:
            if FIXED_DOLLARS is None:
                await asyncio.sleep(10)
                continue
            ind, price = get_indicators()
            if ind is None or price is None:
                await asyncio.sleep(15)
                continue
            qty = get_qty_from_dollars(FIXED_DOLLARS, price)
            if position is None:
                try:
                    long_cond = (price < float(ind['lower'])*1.001) and (float(ind['rsi']) < 35) and (price > float(ind['ema200']))
                    short_cond = (price > float(ind['upper'])*0.999) and (float(ind['rsi']) > 65) and (price < float(ind['ema200']))
                except:
                    await asyncio.sleep(10)
                    continue
                if long_cond:
                    place_order_with_tpsl('buy', qty, price)
                    position = "LONG"; entry_price = price; be_done=False
                    try:
                        await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🟢 LONG @ {price:.2f}\nQty:{qty} ({FIXED_DOLLARS}$ x {LEVERAGE}x)")
                    except: pass
                elif short_cond:
                    place_order_with_tpsl('sell', qty, price)
                    position = "SHORT"; entry_price = price; be_done=False
                    try:
                        await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🔴 SHORT @ {price:.2f}\nQty:{qty} ({FIXED_DOLLARS}$ x {LEVERAGE}x)")
                    except: pass
            else:
                pnl_pct = (price-entry_price)/entry_price if position=="LONG" else (entry_price-price)/entry_price
                if not be_done and pnl_pct >= BE_TRIGGER:
                    be_done=True
                if pnl_pct <= -SL_PCT or pnl_pct >= TP_PCT:
                    result = "✅ TP" if pnl_pct>0 else "❌ SL"
                    try:
                        await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"{result} {position} {pnl_pct*100:.2f}%")
                    except: pass
                    position=None
            await asyncio.sleep(30)
        except asyncio.CancelledError:
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
        await asyncio.sleep(2)
        asyncio.create_task(trading_loop(app_instance))
    app.post_init = on_startup
    print("Bot polling started V3...", flush=True)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    run()
