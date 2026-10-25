import ccxt, asyncio, pandas as pd, os, threading, traceback, time, requests, json
from flask import Flask
from telegram.ext import ApplicationBuilder, CommandHandler
from datetime import datetime

# === CONFIG FILE ===
CONFIG_FILE = "bot_config.json"

def load_config():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
    except: pass
    return {"LEVERAGE": 10, "FIXED_DOLLARS": 20.0, "TP_PCT": 0.004, "SL_PCT": 0.004}

def save_config():
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump({
                "LEVERAGE": LEVERAGE, 
                "FIXED_DOLLARS": FIXED_DOLLARS,
                "TP_PCT": TP_PCT,
                "SL_PCT": SL_PCT
            }, f)
    except: pass

cfg = load_config()
SYMBOL = "BTC/USD:USD" 
TIMEFRAME = "5m"
LEVERAGE = cfg.get("LEVERAGE", 10)
FIXED_DOLLARS = cfg.get("FIXED_DOLLARS", 20.0)
TP_PCT = cfg.get("TP_PCT", 0.004)
SL_PCT = cfg.get("SL_PCT", 0.004)
MAX_TRADES_PER_DAY = 10
DAILY_TARGET_DOLLARS = 5.0
DAILY_SL_DOLLARS = 3.0
COOLDOWN_SEC = 900

DELTA_API_KEY = os.getenv("DELTA_API_KEY")
DELTA_API_SECRET = os.getenv("DELTA_API_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
PAPER_TRADE = os.getenv("PAPER_TRADE", "True") == "True"

print(f"V11 FINAL ${FIXED_DOLLARS} x{LEVERAGE} TP:{TP_PCT*100}% SL:{SL_PCT*100}% {'PAPER' if PAPER_TRADE else 'LIVE'}", flush=True)

private_exchange = ccxt.delta({'apiKey': DELTA_API_KEY, 'secret': DELTA_API_SECRET, 'enableRateLimit': True, 'options': {'defaultType': 'future'}})

position=None; entry_price=0; entry_qty=0
trades_today=0; daily_pnl_dollars=0
last_trade_time=0
today_date=datetime.now().date()
daily_limit_hit=False

web_app=Flask(__name__)
@web_app.route('/')
def home(): return f"<h1>V11 FINAL ${FIXED_DOLLARS}x{LEVERAGE} TP{TP_PCT*100}% SL{SL_PCT*100}% POS:{position} Trades:{trades_today}/10 Daily:${daily_pnl_dollars:.2f}</h1>"
def run_web(): web_app.run(host='0.0.0.0', port=int(os.environ.get("PORT",10000)))
threading.Thread(target=run_web, daemon=True).start()

def sync_position_from_exchange():
    global position, entry_price, entry_qty
    if PAPER_TRADE: return
    try:
        positions = private_exchange.fetch_positions([SYMBOL])
        for p in positions:
            if p['symbol']==SYMBOL and float(p.get('contracts',0))!=0:
                position="LONG" if p['side']=='long' else "SHORT"
                entry_price=float(p['entryPrice'])
                entry_qty=float(p['contracts'])
                return
        position=None; entry_price=0; entry_qty=0
    except Exception as e: print(f"Sync Err {e}")

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
        df['up']=df['sma']+1.5*df['std']; df['lo']=df['sma']-1.5*df['std']
        df['ema']=df['c'].ewm(span=200).mean()
        d=df['c'].diff(); g=d.clip(lower=0).ewm(alpha=1/14).mean(); l=(-d.clip(upper=0)).ewm(alpha=1/14).mean().replace(0,1e-9)
        df['rsi']=100-(100/(1+g/l))
        last_row=df.iloc[-1]
        ind={'rsi':float(last_row['rsi']),'rsi_1':float(df.iloc[-2]['rsi']),'rsi_2':float(df.iloc[-3]['rsi']),'sma':float(last_row['sma']),'up':float(last_row['up']),'lo':float(last_row['lo']),'ema':float(last_row['ema']),'price':float(last_row['c']),'high':float(df['h'].tail(20).max()),'low':float(df['l'].tail(20).min()),}
        return ind,float(last_row['c'])
    except Exception as e: print(f"Ind Err {e}"); return None,None

def get_qty(d,p):
    try:
        if not p: p=81500
        pos_val=d*LEVERAGE
        qty=pos_val/p
        if PAPER_TRADE: return round(qty,6)
        else: return round(max(0.001, qty),3)
    except: return 0.001 if not PAPER_TRADE else 0.0006

def calc_pnl_dollar(entry, curr, qty, pos_type):
    if not entry or not curr or not qty: return 0.0
    return (curr-entry)*qty if pos_type=="LONG" else (entry-curr)*qty

def place_order(side,qty,ep, is_close=False):
    if PAPER_TRADE:
        print(f"[PAPER] {'CLOSE' if is_close else 'OPEN'} {side} {qty} @ {ep}",flush=True)
        return True
    try:
        params = {'reduce_only': 'true'} if is_close else {}
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
    global position, entry_price, entry_qty, trades_today, daily_pnl_dollars, last_trade_time, today_date, daily_limit_hit
    sync_position_from_exchange()
    try: await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID,text=f"V11 FINAL READY ${FIXED_DOLLARS}x{LEVERAGE} TP{TP_PCT*100}% SL{SL_PCT*100}% Qty:{get_qty(FIXED_DOLLARS, 81500)}")
    except: pass
    while True:
        try:
            if datetime.now().date()!=today_date:
                today_date=datetime.now().date(); trades_today=0; daily_pnl_dollars=0; daily_limit_hit=False
            if daily_limit_hit or trades_today>=MAX_TRADES_PER_DAY:
                await asyncio.sleep(300); continue
            if time.time()-last_trade_time < COOLDOWN_SEC and trades_today>0:
                await asyncio.sleep(30); continue
            ind,price=get_indicators()
            if not ind: await asyncio.sleep(15); continue
            q=get_qty(FIXED_DOLLARS,price)
            if position is None:
                long_c = (price <= ind['lo'] + 15) and (ind['rsi'] < 48)
                short_c = (price >= ind['up'] - 15) and (ind['rsi'] > 52)
                if long_c:
                    if place_order('buy',q,price, is_close=False):
                        position="LONG"; entry_price=price; entry_qty=q; trades_today+=1; last_trade_time=time.time()
                        try: await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID,text=f"LONG {trades_today}/10 ${price:.2f} Qty:{q} TP:{TP_PCT*100}% SL:{SL_PCT*100}% RSI{ind['rsi']:.1f}")
                        except: pass
                elif short_c:
                    if place_order('sell',q,price, is_close=False):
                        position="SHORT"; entry_price=price; entry_qty=q; trades_today+=1; last_trade_time=time.time()
                        try: await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID,text=f"SHORT {trades_today}/10 ${price:.2f} Qty:{q} TP:{TP_PCT*100}% SL:{SL_PCT*100}% RSI{ind['rsi']:.1f}")
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
                            daily_limit_hit=True
            await asyncio.sleep(30)
        except Exception as e: print(f"Loop Err {e}"); traceback.print_exc(); await asyncio.sleep(10)

def run():
    app=ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    async def info_cmd(u,c):
        ind,price=get_indicators()
        if not ind:
            await u.message.reply_text("Data loading..."); return
        txt=f"""🤖 <b>BTC SCALP V11 FINAL</b>
💰 Price: ${price:.2f}
📦 Pos: {position or 'None'} Entry: ${entry_price:.1f} Qty:{entry_qty}
⚙️ Dollar: ${FIXED_DOLLARS} Lev: x{LEVERAGE}
Qty Now: {get_qty(FIXED_DOLLARS, price)} BTC
🎯 TP: {TP_PCT*100}% SL: {SL_PCT*100}%
📊 Trades: {trades_today}/10 Daily: ${daily_pnl_dollars:.2f}
Mode: {'PAPER 🟡' if PAPER_TRADE else 'LIVE 🔴'}

<b>Commands:</b>
/settings - Show all
/setdollar 20
/setleverage 10
/settp 0.8 (0.8% TP)
/setsl 0.5 (0.5% SL)
"""
        await u.message.reply_text(txt, parse_mode='HTML')
        
    async def pnl_cmd(u,c):
        pnl_dollar=calc_pnl_dollar(entry_price, get_indicators()[1] or entry_price, entry_qty, position) if position else 0
        await u.message.reply_text(f"Pos:{position} Entry:{entry_price} Qty:{entry_qty} Unreal:${pnl_dollar:.2f} Daily:${daily_pnl_dollars:.2f}")
        
    async def close_cmd(u,c):
        global position, entry_price, entry_qty
        if position:
            _,pr=get_indicators(); curr_price = pr or entry_price or 81500
            q=entry_qty or get_qty(FIXED_DOLLARS, entry_price or curr_price)
            sd='sell' if position=="LONG" else 'buy'
            pnl_dollar = calc_pnl_dollar(entry_price, curr_price, q, position)
            if place_order(sd, q, curr_price, is_close=True):
                await u.message.reply_text(f"MANUAL CLOSE {position} PnL ${pnl_dollar:.2f} ✅")
                position=None; entry_price=0; entry_qty=0
            else: await u.message.reply_text(f"❌ Close Failed")
        else: await u.message.reply_text("No pos")

    async def settings_cmd(u,c):
        await u.message.reply_text(f"⚙️ SETTINGS\n💵 Fixed Dollars: ${FIXED_DOLLARS}\n📈 Leverage: x{LEVERAGE}\n🎯 TP: {TP_PCT*100}%\n🛑 SL: {SL_PCT*100}%\nQty Now: {get_qty(FIXED_DOLLARS, 81500)} BTC\n\nCommands:\n/setdollar 20\n/setleverage 10\n/settp 1\n/setsl 0.5")

    async def setdollar_cmd(u,c):
        global FIXED_DOLLARS
        try:
            val = float(c.args[0])
            FIXED_DOLLARS = val
            save_config()
            await u.message.reply_text(f"✅ Dollars ${val}\nNew Qty: {get_qty(val, 81500)} BTC\nSaved ✅")
        except:
            await u.message.reply_text("Use: /setdollar 20")

    async def setleverage_cmd(u,c):
        global LEVERAGE
        try:
            val = int(c.args[0])
            LEVERAGE = val
            save_config()
            await u.message.reply_text(f"✅ Leverage x{val}\nNew Qty: {get_qty(FIXED_DOLLARS, 81500)} BTC\nSaved ✅")
        except:
            await u.message.reply_text("Use: /setleverage 10")

    async def settp_cmd(u,c):
        global TP_PCT
        try:
            val = float(c.args[0])
            TP_PCT = val / 100
            save_config()
            await u.message.reply_text(f"✅ TP changed to {val}%\nSaved ✅")
        except:
            await u.message.reply_text("Use: /settp 0.8\nExample: /settp 1 = 1% TP")

    async def setsl_cmd(u,c):
        global SL_PCT
        try:
            val = float(c.args[0])
            SL_PCT = val / 100
            save_config()
            await u.message.reply_text(f"✅ SL changed to {val}%\nSaved ✅")
        except:
            await u.message.reply_text("Use: /setsl 0.5\nExample: /setsl 0.5 = 0.5% SL")
        
    app.add_handler(CommandHandler("info",info_cmd))
    app.add_handler(CommandHandler("pnl",pnl_cmd))
    app.add_handler(CommandHandler("close",close_cmd))
    app.add_handler(CommandHandler("settings",settings_cmd))
    app.add_handler(CommandHandler("setdollar",setdollar_cmd))
    app.add_handler(CommandHandler("setleverage",setleverage_cmd))
    app.add_handler(CommandHandler("settp",settp_cmd))
    app.add_handler(CommandHandler("setsl",setsl_cmd))
    
    async def on_startup(a):
        await asyncio.sleep(3)
        try: await a.bot.delete_webhook(drop_pending_updates=True)
        except: pass
        asyncio.create_task(trading_loop(a))
    app.post_init=on_startup
    print("Bot polling V11 FINAL...",flush=True)
    while True:
        try: app.run_polling(drop_pending_updates=True, close_loop=False, stop_signals=None)
        except Exception as e:
            if "Conflict" in str(e): time.sleep(30); continue
            print(e); time.sleep(10); continue
        break
if __name__=="__main__": run()
