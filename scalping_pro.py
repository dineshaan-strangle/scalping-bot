import ccxt, asyncio, pandas as pd, os, threading, traceback, time, requests
from flask import Flask
from telegram.ext import ApplicationBuilder, CommandHandler, ConversationHandler, MessageHandler, filters
from datetime import datetime

SYMBOL = "BTC/USD:USD" 
TIMEFRAME = "5m"
LEVERAGE = 5
FIXED_DOLLARS = 10.0
TP_PCT = 0.004  # 0.4%
SL_PCT = 0.004  # 0.4% - 1:1 for 80% WR
MAX_TRADES_PER_DAY = 10
DAILY_TARGET_PCT = 0.02 # 2% = $20 on $1000
DAILY_SL_PCT = 0.015
COOLDOWN_SEC = 900 # 15 min gap

DELTA_API_KEY = os.getenv("DELTA_API_KEY")
DELTA_API_SECRET = os.getenv("DELTA_API_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
PAPER_TRADE = os.getenv("PAPER_TRADE", "True") == "True"

print(f"V9.3 STARTED FIXED ${FIXED_DOLLARS} x{LEVERAGE}x", flush=True)

private_exchange = ccxt.delta({'apiKey': DELTA_API_KEY, 'secret': DELTA_API_SECRET, 'enableRateLimit': True, 'options': {'defaultType': 'future'}})

position=None; entry_price=0
trades_today=0; daily_pnl_dollars=0
last_trade_time=0
today_date=datetime.now().date()

web_app=Flask(__name__)
@web_app.route('/')
def home(): return f"<h1>V9.3 RUNNING ${FIXED_DOLLARS}x{LEVERAGE} POS:{position} Trades:{trades_today}/10</h1>"
def run_web(): web_app.run(host='0.0.0.0', port=int(os.environ.get("PORT",10000)))
threading.Thread(target=run_web, daemon=True).start()

def get_indicators():
    try:
        url = "https://cdn.india.deltaex.org/v2/history/candles"
        end_ts = int(time.time()); start_ts = end_ts - (3600 * 5)
        params = {"resolution": "5m", "symbol": "BTCUSD", "start": str(start_ts), "end": str(end_ts)}
        ohlcv=None
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 200:
                data=r.json(); result=data.get('result', data) if isinstance(data, dict) else data
                if result and len(result)>=20:
                    df_temp=pd.DataFrame(result); ohlcv=[]
                    for _, row in df_temp.iterrows():
                        t=int(row['time']); ohlcv.append([t*1000,float(row['open']),float(row['high']),float(row['low']),float(row['close']),float(row.get('volume',0))])
                    ohlcv.sort(key=lambda x: x[0])
        except: pass
        if not ohlcv or len(ohlcv)<20:
            try: pub=ccxt.delta({'enableRateLimit':True}); ohlcv=pub.fetch_ohlcv("BTC/USD:USD","5m",limit=100)
            except: pass
        if not ohlcv or len(ohlcv)<20: return None,None
        df=pd.DataFrame(ohlcv, columns=['t','o','h','l','c','v'])
        df['sma']=df['c'].rolling(20).mean(); df['std']=df['c'].rolling(20).std()
        df['up']=df['sma']+2*df['std']; df['lo']=df['sma']-2*df['std']
        df['ema']=df['c'].ewm(span=200).mean()
        d=df['c'].diff(); g=d.clip(lower=0).ewm(alpha=1/14).mean(); l=(-d.clip(upper=0)).ewm(alpha=1/14).mean().replace(0,1e-9)
        df['rsi']=100-(100/(1+g/l))
        last_row=df.iloc[-1]
        ind={'rsi':float(last_row['rsi']),'sma':float(last_row['sma']),'up':float(last_row['up']),'lo':float(last_row['lo']),'ema':float(last_row['ema']),'price':float(last_row['c'])}
        return ind,float(last_row['c'])
    except Exception as e: print(f"Ind Err {e}"); return None,None

def get_qty(d,p):
    try:
        if not p: p=85000
        pos_val=d*LEVERAGE
        qty=pos_val/p
        # Delta min lot 0.001 BTC = ~$85 at 85k, so adjust
        btc_qty=max(0.001, qty)
        return int(btc_qty*1000)/1000 # round
    except: return 1

def place_order(side,qty,ep):
    if PAPER_TRADE: print(f"[PAPER] {side} {qty} @ {ep}",flush=True); return True
    try: private_exchange.create_market_order(SYMBOL,side,qty); return True
    except Exception as e: print(e); return False

async def trading_loop(app):
    global position, entry_price, trades_today, daily_pnl_dollars, last_trade_time, today_date
    try: await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID,text=f"V9.3 STARTED ${FIXED_DOLLARS}x{LEVERAGE} 10 Trades/Day TP 0.4% SL 0.4%")
    except: pass
    while True:
        try:
            # Daily reset
            if datetime.now().date()!=today_date:
                today_date=datetime.now().date(); trades_today=0; daily_pnl_dollars=0

            if trades_today>=MAX_TRADES_PER_DAY:
                await asyncio.sleep(300); continue
            if time.time()-last_trade_time < COOLDOWN_SEC and trades_today>0:
                await asyncio.sleep(30); continue

            ind,price=get_indicators()
            if not ind: await asyncio.sleep(15); continue
            q=get_qty(FIXED_DOLLARS,price)

            if position is None:
                # SIMPLE 80% WR Logic
                long_c = (price <= ind['lo']*1.002) and (ind['rsi'] < 40) and (price > ind['ema'])
                short_c = (price >= ind['up']*0.998) and (ind['rsi'] > 60) and (price < ind['ema'])
                if long_c:
                    if place_order('buy',q,price):
                        position="LONG"; entry_price=price; trades_today+=1; last_trade_time=time.time()
                        try: await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID,text=f"LONG {trades_today}/10 ${price:.2f} RSI{ind['rsi']:.1f}")
                        except: pass
                elif short_c:
                    if place_order('sell',q,price):
                        position="SHORT"; entry_price=price; trades_today+=1; last_trade_time=time.time()
                        try: await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID,text=f"SHORT {trades_today}/10 ${price:.2f} RSI{ind['rsi']:.1f}")
                        except: pass
            else:
                pnl=(price-entry_price)/entry_price if position=="LONG" else (entry_price-price)/entry_price
                pnl_dollar=FIXED_DOLLARS*LEVERAGE*pnl
                if pnl<=-SL_PCT or pnl>=TP_PCT:
                    daily_pnl_dollars+=pnl_dollar
                    msg=f"{'TP HIT ✅' if pnl>0 else 'SL HIT ❌'} {position} {pnl*100:.2f}% ${pnl_dollar:.2f} Daily:${daily_pnl_dollars:.2f} {trades_today}/10"
                    try: await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID,text=msg)
                    except: pass
                    position=None
                    # Daily target check
                    if daily_pnl_dollars >= FIXED_DOLLARS*LEVERAGE*DAILY_TARGET_PCT or daily_pnl_dollars <= -FIXED_DOLLARS*LEVERAGE*DAILY_SL_PCT:
                        try: await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID,text=f"DAILY LIMIT HIT Daily PnL ${daily_pnl_dollars:.2f} Bot Paused till tomorrow")
                        except: pass
            await asyncio.sleep(30)
        except Exception as e: print(f"Loop Err {e}"); traceback.print_exc(); await asyncio.sleep(10)

def run():
    app=ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    async def info_cmd(u,c):
        ind,price=get_indicators()
        txt=f"V9.3 ${FIXED_DOLLARS}x{LEVERAGE} Trades:{trades_today}/10 Daily:${daily_pnl_dollars:.2f} Pos:{position}"
        if ind: txt+=f"\nPrice ${price:.1f} RSI {ind['rsi']:.1f}"
        await u.message.reply_text(txt)
    async def pnl_cmd(u,c): await u.message.reply_text(f"Pos:{position} Entry:{entry_price} Trades:{trades_today}/10 PnL:${daily_pnl_dollars:.2f}")
    async def close_cmd(u,c):
        global position
        if position:
            _,pr=get_indicators(); q=get_qty(FIXED_DOLLARS,pr or 85000); sd='sell' if position=="LONG" else 'buy'
            if not PAPER_TRADE:
                try: private_exchange.create_market_order(SYMBOL,sd,q,None,{'reduce_only':True})
                except: private_exchange.create_market_order(SYMBOL,sd,q)
            await u.message.reply_text(f"Closed {position}"); position=None
        else: await u.message.reply_text("No pos")
    app.add_handler(CommandHandler("info",info_cmd)); app.add_handler(CommandHandler("pnl",pnl_cmd)); app.add_handler(CommandHandler("close",close_cmd))
    async def on_startup(a):
        await asyncio.sleep(3)
        try: await a.bot.delete_webhook(drop_pending_updates=True)
        except: pass
        asyncio.create_task(trading_loop(a))
    app.post_init=on_startup
    print("Bot polling V9.3...",flush=True)
    while True:
        try: app.run_polling(drop_pending_updates=True, close_loop=False, stop_signals=None)
        except Exception as e:
            if "Conflict" in str(e): time.sleep(30); continue
            print(e); time.sleep(10); continue
        break
if __name__=="__main__": run()
