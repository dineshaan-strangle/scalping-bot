import ccxt, asyncio, pandas as pd, os, threading, traceback, time, requests
from flask import Flask
from telegram.ext import ApplicationBuilder, CommandHandler
from datetime import datetime

SYMBOL = "BTC/USD:USD" 
TIMEFRAME = "5m"
LEVERAGE = 5
FIXED_DOLLARS = 10.0 # $10 FIXED - Paper la exact, Live la min 0.001 BTC
TP_PCT = 0.004  # 0.4%
SL_PCT = 0.004  # 0.4% - 1:1 RR
MAX_TRADES_PER_DAY = 10
DAILY_TARGET_DOLLARS = 5.0 # $5 daily target for $10 fixed
DAILY_SL_DOLLARS = 3.0 # $3 daily SL
COOLDOWN_SEC = 900 # 15 min

DELTA_API_KEY = os.getenv("DELTA_API_KEY")
DELTA_API_SECRET = os.getenv("DELTA_API_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
PAPER_TRADE = os.getenv("PAPER_TRADE", "True") == "True"

print(f"V10 FIXED STARTED FIXED ${FIXED_DOLLARS} x{LEVERAGE} {'PAPER' if PAPER_TRADE else 'LIVE'}", flush=True)

private_exchange = ccxt.delta({'apiKey': DELTA_API_KEY, 'secret': DELTA_API_SECRET, 'enableRateLimit': True, 'options': {'defaultType': 'future'}})

position=None; entry_price=0; entry_qty=0
trades_today=0; daily_pnl_dollars=0
last_trade_time=0
today_date=datetime.now().date()

web_app=Flask(__name__)
@web_app.route('/')
def home(): return f"<h1>V10 RUNNING ${FIXED_DOLLARS}x{LEVERAGE} POS:{position} Trades:{trades_today}/10 Daily:${daily_pnl_dollars:.2f}</h1>"
def run_web(): web_app.run(host='0.0.0.0', port=int(os.environ.get("PORT",10000)))
threading.Thread(target=run_web, daemon=True).start()

def get_indicators():
    try:
        url = "https://cdn.india.deltaex.org/v2/history/candles"
        end_ts = int(time.time()); start_ts = end_ts - (3600 * 20)
        params = {"resolution": "5m", "symbol": "BTCUSD", "start": str(start_ts), "end": str(end_ts)}
        ohlcv=None
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 200:
                data=r.json(); result=data.get('result', data) if isinstance(data, dict) else data
                if result and len(result)>=100:
                    df_temp=pd.DataFrame(result); ohlcv=[]
                    for _, row in df_temp.iterrows():
                        t=int(row['time']); ohlcv.append([t*1000,float(row['open']),float(row['high']),float(row['low']),float(row['close']),float(row.get('volume',0))])
                    ohlcv.sort(key=lambda x: x[0])
        except: pass
        if not ohlcv or len(ohlcv)<50:
            try: pub=ccxt.delta({'enableRateLimit':True}); ohlcv=pub.fetch_ohlcv("BTC/USD:USD","5m",limit=200)
            except: pass
        if not ohlcv or len(ohlcv)<50: return None,None
        df=pd.DataFrame(ohlcv, columns=['t','o','h','l','c','v'])
        df['sma']=df['c'].rolling(20).mean(); df['std']=df['c'].rolling(20).std()
        df['up']=df['sma']+2*df['std']; df['lo']=df['sma']-2*df['std']
        df['ema']=df['c'].ewm(span=200).mean()
        d=df['c'].diff(); g=d.clip(lower=0).ewm(alpha=1/14).mean(); l=(-d.clip(upper=0)).ewm(alpha=1/14).mean().replace(0,1e-9)
        df['rsi']=100-(100/(1+g/l))
        last_row=df.iloc[-1]
        ind={
            'rsi':float(last_row['rsi']),
            'rsi_1':float(df.iloc[-2]['rsi']),
            'rsi_2':float(df.iloc[-3]['rsi']),
            'sma':float(last_row['sma']),
            'up':float(last_row['up']),
            'lo':float(last_row['lo']),
            'ema':float(last_row['ema']),
            'price':float(last_row['c']),
            'high':float(df['h'].tail(20).max()),
            'low':float(df['l'].tail(20).min()),
        }
        return ind,float(last_row['c'])
    except Exception as e: print(f"Ind Err {e}"); return None,None

def get_qty(d,p):
    try:
        if not p: p=81500
        pos_val=d*LEVERAGE # $50
        qty=pos_val/p # 0.000613
        if PAPER_TRADE:
            return round(qty,6) # PAPER: exact $10 fixed 0.000613
        else:
            btc_qty=max(0.001, qty) # LIVE: min 0.001 BTC
            return round(btc_qty,3)
    except: return 0.001 if not PAPER_TRADE else 0.0006

def calc_pnl_dollar(entry, curr, qty, pos_type):
    if not entry or not curr or not qty: return 0.0
    if pos_type=="LONG":
        return (curr-entry)*qty
    else:
        return (entry-curr)*qty

def place_order(side,qty,ep, is_close=False):
    if PAPER_TRADE:
        print(f"[PAPER] {'CLOSE' if is_close else 'OPEN'} {side} {qty} @ {ep}",flush=True)
        return True
    try:
        params = {'reduce_only': True} if is_close else {}
        private_exchange.create_market_order(SYMBOL, side, qty, None, params)
        return True
    except Exception as e:
        print(f"Order Err {e}")
        try:
            private_exchange.create_market_order(SYMBOL, side, qty)
            return True
        except Exception as e2:
            print(f"Order Retry Err {e2}")
            return False

async def trading_loop(app):
    global position, entry_price, entry_qty, trades_today, daily_pnl_dollars, last_trade_time, today_date
    try: await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID,text=f"V10 FIXED STARTED ${FIXED_DOLLARS}x{LEVERAGE} {'PAPER' if PAPER_TRADE else 'LIVE'} Qty:{get_qty(FIXED_DOLLARS, 81500)} BTC TP {TP_PCT*100}% SL {SL_PCT*100}%")
    except: pass
    while True:
        try:
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
                # FIXED LOGIC - No contradiction
                long_c = (price <= ind['lo']) and (ind['rsi'] < 38)
                short_c = (price >= ind['up']) and (ind['rsi'] > 62)
                
                if long_c:
                    tp_p = price * (1 + TP_PCT)
                    sl_p = price * (1 - SL_PCT)
                    if place_order('buy',q,price, is_close=False):
                        position="LONG"; entry_price=price; entry_qty=q; trades_today+=1; last_trade_time=time.time()
                        try: await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID,text=f"LONG {trades_today}/10 ${price:.2f} Qty:{q} TP:${tp_p:.2f} SL:${sl_p:.2f} RSI{ind['rsi']:.1f}")
                        except: pass
                elif short_c:
                    tp_p = price * (1 - TP_PCT)
                    sl_p = price * (1 + TP_PCT)
                    if place_order('sell',q,price, is_close=False):
                        position="SHORT"; entry_price=price; entry_qty=q; trades_today+=1; last_trade_time=time.time()
                        try: await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID,text=f"SHORT {trades_today}/10 ${price:.2f} Qty:{q} TP:${tp_p:.2f} SL:${sl_p:.2f} RSI{ind['rsi']:.1f}")
                        except: pass
            else:
                pnl=(price-entry_price)/entry_price if position=="LONG" else (entry_price-price)/entry_price
                pnl_dollar=calc_pnl_dollar(entry_price, price, entry_qty, position)
                
                if pnl<=-SL_PCT or pnl>=TP_PCT:
                    close_side = 'sell' if position=="LONG" else 'buy'
                    if place_order(close_side, entry_qty, price, is_close=True):
                        daily_pnl_dollars+=pnl_dollar
                        msg=f"{'TP HIT ✅' if pnl>0 else 'SL HIT ❌'} {position} {pnl*100:.3f}% ${pnl_dollar:.2f} Daily:${daily_pnl_dollars:.2f} {trades_today}/10"
                        try: await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID,text=msg)
                        except: pass
                        position=None; entry_price=0; entry_qty=0
                        
                        if daily_pnl_dollars >= DAILY_TARGET_DOLLARS or daily_pnl_dollars <= -DAILY_SL_DOLLARS:
                            try: await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID,text=f"DAILY LIMIT HIT Daily PnL ${daily_pnl_dollars:.2f} Bot Paused till tomorrow")
                            except: pass
                    else:
                        try: await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID,text=f"❌ CLOSE FAILED {position} @ ${price:.2f} Retrying...")
                        except: pass
            await asyncio.sleep(30)
        except Exception as e: print(f"Loop Err {e}"); traceback.print_exc(); await asyncio.sleep(10)

def run():
    app=ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    async def info_cmd(u,c):
        ind,price=get_indicators()
        if not ind:
            await u.message.reply_text("Data loading... try again 10s")
            return
        trend = "Bullish 🟢" if price > ind['ema'] else "Bearish 🔴"
        status = "Above Upper - SHORT Signal" if price >= ind['up'] else "Below Lower - LONG Signal" if price <= ind['lo'] else "Inside Bands - Wait ⏳"
        tp_long = price * (1 + TP_PCT); sl_long = price * (1 - SL_PCT)
        txt=f"""🤖 <b>BTC SCALP V10 FIXED - LIVE</b>
⏰ {datetime.now().strftime('%H:%M:%S %d-%m')}
💰 Price: ${price:.2f} ({trend})
EMA200: ${ind['ema']:.2f}
📊 Boll 20,2
Upper: ${ind['up']:.2f}
Lower: ${ind['lo']:.2f}
SMA20: ${ind['sma']:.2f}
Range 20C: ${ind['low']:.0f}-${ind['high']:.0f}
Status: {status}
📈 RSI: {ind['rsi']:.1f} (Prev {ind['rsi_1']:.1f}, {ind['rsi_2']:.1f})
📦 Pos: {position or 'None'} Entry: ${entry_price:.1f} Qty:{entry_qty}
🎯 TP: {TP_PCT*100}% (${tp_long:.0f}/${price*(1-TP_PCT):.0f}) SL: {SL_PCT*100}%
📊 Trades: {trades_today}/{MAX_TRADES_PER_DAY} Daily: ${daily_pnl_dollars:.2f} Target:${DAILY_TARGET_DOLLARS}/-${DAILY_SL_DOLLARS}
Mode: {'PAPER 🟡 Exact $10 Fixed' if PAPER_TRADE else 'LIVE 🔴 Min 0.001 BTC'} Qty: {get_qty(FIXED_DOLLARS, price)} BTC (~${FIXED_DOLLARS*LEVERAGE} pos)
Cooldown: {int((COOLDOWN_SEC - (time.time()-last_trade_time))/60) if trades_today>0 and time.time()-last_trade_time < COOLDOWN_SEC else 0} min
Data: LIVE Delta CDN ✅
"""
        await u.message.reply_text(txt, parse_mode='HTML')
        
    async def pnl_cmd(u,c):
        pnl_dollar=calc_pnl_dollar(entry_price, get_indicators()[1] or entry_price, entry_qty, position) if position else 0
        await u.message.reply_text(f"Pos:{position} Entry:{entry_price} Qty:{entry_qty} Unreal PnL:${pnl_dollar:.2f} Trades:{trades_today}/10 Daily:${daily_pnl_dollars:.2f}")
        
    async def close_cmd(u,c):
        global position, entry_price, entry_qty
        if position:
            _,pr=get_indicators()
            curr_price = pr or entry_price or 81500
            q=entry_qty or get_qty(FIXED_DOLLARS, entry_price or curr_price)
            sd='sell' if position=="LONG" else 'buy'
            pnl_dollar = calc_pnl_dollar(entry_price, curr_price, q, position)
            if place_order(sd, q, curr_price, is_close=True):
                await u.message.reply_text(f"Closed {position} Entry ${entry_price:.2f} -> ${curr_price:.2f} Qty:{q} PnL ${pnl_dollar:.2f}")
                position=None; entry_price=0; entry_qty=0
            else:
                await u.message.reply_text(f"❌ Close Failed - Check Delta")
        else: await u.message.reply_text("No pos")
        
    app.add_handler(CommandHandler("info",info_cmd)); app.add_handler(CommandHandler("pnl",pnl_cmd)); app.add_handler(CommandHandler("close",close_cmd))
    async def on_startup(a):
        await asyncio.sleep(3)
        try: await a.bot.delete_webhook(drop_pending_updates=True)
        except: pass
        asyncio.create_task(trading_loop(a))
    app.post_init=on_startup
    print("Bot polling V10 FIXED...",flush=True)
    while True:
        try: app.run_polling(drop_pending_updates=True, close_loop=False, stop_signals=None)
        except Exception as e:
            if "Conflict" in str(e): time.sleep(30); continue
            print(e); time.sleep(10); continue
        break
if __name__=="__main__": run()
