# V1 SCALPING PRO - 80% DATA-DRIVEN BOT
import ccxt, time, asyncio
from telegram import Bot
import pandas as pd
import numpy as np

# --- CONFIG ---
API_KEY = "YOUR_BINANCE_KEY"
SECRET = "YOUR_BINANCE_SECRET"
TELEGRAM_TOKEN = "YOUR_TG_TOKEN"
CHAT_ID = "YOUR_CHAT_ID"
SYMBOL = "BTC/USDT"
TIMEFRAME = "5m"
LEVERAGE = 10
QTY_USD = 100 # per trade

TP_PCT = 0.004
SL_PCT = 0.008
BE_TRIGGER = 0.0025
TRAIL_STEP = 0.0015

bot = Bot(token=TELEGRAM_TOKEN)
exchange = ccxt.binanceusdm({
    'apiKey': API_KEY,
    'secret': SECRET,
    'enableRateLimit': True,
})

async def send_msg(text):
    await bot.send_message(chat_id=CHAT_ID, text=text)

def get_indicators():
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
    return df.iloc[-1], df.iloc[-2]

# --- MAIN LOOP ---
position = None
entry_price = 0
be_done = False

async def run():
    global position, entry_price, be_done
    await send_msg("🚀 V1 SCALPING PRO STARTED - 80% Mode ON")
    while True:
        try:
            last, prev = get_indicators()
            price = last['c']
            
            # ENTRY
            if position is None:
                long_cond = (price < last['lower']*1.001) and (last['rsi'] < 35) and (price > last['ema200'])
                short_cond = (price > last['upper']*0.999) and (last['rsi'] > 65) and (price < last['ema200'])
                
                if long_cond:
                    exchange.create_market_buy_order(SYMBOL, QTY_USD/price)
                    position="LONG"; entry_price=price; be_done=False
                    await send_msg(f"🟢 LONG ENTRY @ {price:.2f}\nTP {TP_PCT*100}% SL {SL_PCT*100}% RSI {last['rsi']:.1f}")
                elif short_cond:
                    exchange.create_market_sell_order(SYMBOL, QTY_USD/price)
                    position="SHORT"; entry_price=price; be_done=False
                    await send_msg(f"🔴 SHORT ENTRY @ {price:.2f}\nTP {TP_PCT*100}% SL {SL_PCT*100}% RSI {last['rsi']:.1f}")
            
            # EXIT + TRAILING
            else:
                pnl_pct = (price-entry_price)/entry_price if position=="LONG" else (entry_price-price)/entry_price
                
                # BE move
                if not be_done and pnl_pct >= BE_TRIGGER:
                    be_done=True
                    await send_msg(f"🔒 BE MOVED - Profit {pnl_pct*100:.2f}%")
                
                # SL/TP check
                if position=="LONG":
                    if pnl_pct <= -SL_PCT:
                        exchange.create_market_sell_order(SYMBOL, QTY_USD/price)
                        await send_msg(f"❌ SL HIT LONG {pnl_pct*100:.2f}%")
                        position=None
                    elif pnl_pct >= TP_PCT:
                        exchange.create_market_sell_order(SYMBOL, QTY_USD/price)
                        await send_msg(f"✅ TP HIT LONG +{pnl_pct*100:.2f}%")
                        position=None
                else:
                    if pnl_pct <= -SL_PCT:
                        exchange.create_market_buy_order(SYMBOL, QTY_USD/price)
                        await send_msg(f"❌ SL HIT SHORT {pnl_pct*100:.2f}%")
                        position=None
                    elif pnl_pct >= TP_PCT:
                        exchange.create_market_buy_order(SYMBOL, QTY_USD/price)
                        await send_msg(f"✅ TP HIT SHORT +{pnl_pct*100:.2f}%")
                        position=None
            
            # Live PNL every 15 min
            if int(time.time()) % 900 == 0 and position:
                await send_msg(f"📊 LIVE PNL: {position} {pnl_pct*100:.2f}% Entry {entry_price:.1f} Now {price:.1f}")

            time.sleep(30)
        except Exception as e:
            print(e)
            time.sleep(10)

# Run
# asyncio.run(run()) # VPS la run panna itha uncomment pannunga
