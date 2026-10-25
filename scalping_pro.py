import ccxt, asyncio, pandas as pd, os, threading, time, requests, json
from flask import Flask
from telegram.ext import ApplicationBuilder, CommandHandler
from datetime import datetime, timezone, timedelta

CONFIG_FILE = "bot_config.json"
IST = timezone(timedelta(hours=5, minutes=30))

def load_config():
    data = {"LEVERAGE": 10, "FIXED_DOLLARS": 20.0, "TP_PCT": 0.004, "SL_PCT": 0.004}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                data.update(json.load(f))
                print(f"Loaded: {data}", flush=True)
        except: pass
    return data

def save_config():
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump({"LEVERAGE": LEVERAGE, "FIXED_DOLLARS": FIXED_DOLLARS, "TP_PCT": TP_PCT, "SL_PCT": SL_PCT}, f)
        print(f"Saved: {LEVERAGE} {FIXED_DOLLARS}", flush=True)
    except Exception as e:
        print(f"Save error {e}", flush=True)

cfg = load_config()
SYMBOL = "BTC/USD:USD" 
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
PAPER_TRADE = os.getenv("PAPER_TRADE", "True").strip().lower() == "true"

print(f"V13 PNL FIXED ${FIXED_DOLLARS} x{LEVERAGE} PAPER:{PAPER_TRADE}", flush=True)

private_exchange = ccxt.delta({'apiKey': DELTA_API_KEY, 'secret': DELTA_API_SECRET, 'enableRateLimit': True, 'options': {'defaultType': 'future'}})

position=None; entry_price=0; entry_qty=0
trades_today=0; daily_pnl_dollars=0
last_trade_time=0
today_date=datetime.now(IST).date()
daily_limit_hit=False

web_app=Flask(__name__)
@web_app.route('/')
def home():
    daily_str = f"${daily_pnl_dollars:.2f}"
    return f"<h1>V13 PNL ${FIXED_DOLLARS}x{LEVERAGE} Trades:{trades_today}/10 PnL:{daily_str} IST:{today_date}</h1>"
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
        df['up']=df['sma']+1.5*df['std']; df['lo']=df['sma']-1.5*df['std']
        d=df['c'].diff(); g=d.clip(lower=0).ewm(alpha=1/14).mean(); l=(-d.clip(upper=0)).ewm(alpha=1/14).mean().replace(0,1e-9)
        df['rsi']=100-(100/(1+g/l))
        last_row=df.iloc[-1]
        ind={'rsi':float(last_row['rsi']),'sma':float(last_row['sma']),'up':float(last_row['up']),'lo':float(last_row['lo']),'price':float(last_row['c'])}
        return ind,float(last_row['c'])
    except: return None,None

def get_qty(d,p):
    try:
        if not p: p=81500
        qty=(d*LEVERAGE)/p
        return round(qty,6) if PAPER_TRADE else round(max(0.001, qty),3)
    except: return 0.001

def calc_pnl_dollar(entry, curr, qty, pos_type):
    if not entry or not curr or not qty: return 0.0
    return (curr-entry)*qty if pos_type=="LONG" else (entry-curr)*qty

def place_order(side,qty,ep, is_close=False):
    if PAPER_TRADE: return True
    try:
        params = {'reduce_only': 'true'} if is_close else {}
        private_exchange.create_market_order(SYMBOL, side, qty, None, params)
        return True
    except:
        try:
            private_exchange.create_market_order(SYMBOL, side, qty)
            return True
        except Exception as e:
            print(f"Order fail {e}", flush=True)
            return False

async def trading_loop(app):
    global position, entry_price, entry_qty, trades_today, daily_pnl_dollars, last_trade_time, today_date, daily_limit_hit
    try: await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID,text=f"🤖 V13 READY\n📅 IST:{today_date}\n💵 ${FIXED_DOLLARS} x{LEVERAGE}\n🎯 TP{TP_PCT*100}% SL{SL_PCT*100}%\n/pnl command added ✅")
    except: pass
    while True:
        try:
            curr_ist_date = datetime.now(IST).date()
            if curr_ist_date != today_date:
                today_date = curr_ist_date
                trades_today = 0; daily_pnl_dollars = 0; daily_limit_hit = False
                print(f"NEW DAY RESET IST {today_date}", flush=True)
                try: await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🌅 NEW DAY IST RESET\n📅 {today_date}\nTrades: 0/10\nDaily: $0")
                except: pass
            if daily_limit_hit:
                if daily_pnl_dollars >= DAILY_TARGET_DOLLARS or daily_pnl_dollars <= -DAILY_SL_DOLLARS:
                    await asyncio.sleep(300); continue
                else: daily_limit_hit=False
            if trades_today>=MAX_TRADES_PER_DAY: await asyncio.sleep(300); continue
            if time.time()-last_trade_time < COOLDOWN_SEC and trades_today>0: await asyncio.sleep(30); continue
            ind,price=get_indicators()
            if not ind: await asyncio.sleep(15); continue
            q=get_qty(FIXED_DOLLARS,price)
            if position is None:
                long_c = (price <= ind['lo'] + 15) and (ind['rsi'] < 48)
                short_c = (price >= ind['up'] - 15) and (ind['rsi'] > 52)
                if long_c:
                    if place_order('buy',q,price):
                        position="LONG"; entry_price=price; entry_qty=q; trades_today+=1; last_trade_time=time.time()
                        try: await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID,text=f"🟢 LONG {trades_today}/10\n${price:.2f}\nQty:{q}\nRSI:{ind['rsi']:.1f}")
                        except: pass
                elif short_c:
                    if place_order('sell',q,price):
                        position="SHORT"; entry_price=price; entry_qty=q; trades_today+=1; last_trade_time=time.time()
                        try: await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID,text=f"🔴 SHORT {trades_today}/10\n${price:.2f}\nQty:{q}\nRSI:{ind['rsi']:.1f}")
                        except: pass
            else:
                pnl=(price-entry_price)/entry_price if position=="LONG" else (entry_price-price)/entry_price
                pnl_dollar=calc_pnl_dollar(entry_price, price, entry_qty, position)
                if pnl<=-SL_PCT or pnl>=TP_PCT:
                    close_side = 'sell' if position=="LONG" else 'buy'
                    if place_order(close_side, entry_qty, price, is_close=True):
                        daily_pnl_dollars+=pnl_dollar
                        if daily_pnl_dollars >= DAILY_TARGET_DOLLARS:
                            daily_limit_hit=True
                            try: await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID,text=f"🎯 TARGET HIT ${daily_pnl_dollars:.2f}\nStopped today")
                            except: pass
                        if daily_pnl_dollars <= -DAILY_SL_DOLLARS:
                            daily_limit_hit=True
                            try: await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID,text=f"🛑 SL HIT ${daily_pnl_dollars:.2f}\nStopped today")
                            except: pass
                        try: await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID,text=f"{'TP ✅' if pnl>0 else 'SL ❌'} {position}\nEntry:${entry_price:.2f} Exit:${price:.2f}\nPnL: ${pnl_dollar:.2f} ({pnl*100:.2f}%)\nDaily:${daily_pnl_dollars:.2f}")
                        except: pass
                        position=None; entry_price=0; entry_qty=0
            await asyncio.sleep(30)
        except Exception as e:
            print(f"Loop error {e}", flush=True)
            await asyncio.sleep(10)

def run():
    app=ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    async def info_cmd(u,c):
        ind,price=get_indicators()
        price_str = f"${price:.2f}" if price else "$0"
        entry_str = f"@${entry_price:.2f}" if position else ""
        daily_str = f"${daily_pnl_dollars:.2f}"
        await u.message.reply_text(f"🤖 V13 FIXED\n💰 {price_str}\n📅 IST: {today_date}\n📦 Pos: {position or 'None'} {entry_str}\n⚙️ ${FIXED_DOLLARS} x{LEVERAGE}\n🎯 TP: {TP_PCT*100}% SL: {SL_PCT*100}%\n📊 Trades: {trades_today}/10\n💵 Daily:{daily_str}\nPAPER:{PAPER_TRADE}")

    async def pnl_cmd(u,c):
        if position is None:
            await u.message.reply_text(f"📭 No open position\n💵 Daily PnL: ${daily_pnl_dollars:.2f}\nTrades: {trades_today}/10")
        else:
            ind,price=get_indicators()
            if not price: price=entry_price
            pnl_dollar=calc_pnl_dollar(entry_price, price, entry_qty, position)
            pnl_pct=(price-entry_price)/entry_price*100 if position=="LONG" else (entry_price-price)/entry_price*100
            await u.message.reply_text(f"💰 LIVE PNL\n📦 Pos: {position} @${entry_price:.2f}\n💵 Now: ${price:.2f}\n📏 Qty: {entry_qty}\n\n📈 Live: ${pnl_dollar:.2f} ({pnl_pct:.2f}%)\n💵 Daily Total: ${daily_pnl_dollars:.2f}\n🎯 TP: {TP_PCT*100}% SL: {SL_PCT*100}%")

    async def settings_cmd(u,c):
        await u.message.reply_text(f"⚙️ SETTINGS\nDate IST: {today_date}\n💵 ${FIXED_DOLLARS}\n📈 x{LEVERAGE}\n🎯 TP: {TP_PCT*100}%\n🛑 SL: {SL_PCT*100}%\nTrades: {trades_today}/10\n\nBOT COMMANDS:\n/setdollar 10\n/setleverage 10\n/settp 1\n/setsl 0.5\n/pnl\n/info")

    async def setdollar_cmd(u,c):
        global FIXED_DOLLARS
        try: FIXED_DOLLARS=float(c.args[0]); save_config(); await u.message.reply_text(f"✅ ${FIXED_DOLLARS} Saved ✅")
        except: await u.message.reply_text("Use: /setdollar 20")
    async def setleverage_cmd(u,c):
        global LEVERAGE
        try: LEVERAGE=int(c.args[0]); save_config(); await u.message.reply_text(f"✅ x{LEVERAGE} Saved ✅\nNew Qty calc will use this")
        except: await u.message.reply_text("Use: /setleverage 10")
    async def settp_cmd(u,c):
        global TP_PCT
        try: v=float(c.args[0]); TP_PCT=v/100 if v>=0.1 else v; save_config(); await u.message.reply_text(f"✅ TP {TP_PCT*100}% Saved")
        except: await u.message.reply_text("Use: /settp 1")
    async def setsl_cmd(u,c):
        global SL_PCT
        try: v=float(c.args[0]); SL_PCT=v/100 if v>=0.1 else v; save_config(); await u.message.reply_text(f"✅ SL {SL_PCT*100}% Saved")
        except: await u.message.reply_text("Use: /setsl 0.5")

    app.add_handler(CommandHandler("info",info_cmd))
    app.add_handler(CommandHandler("pnl",pnl_cmd))
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
    print("Bot V13 PNL starting...",flush=True)
    while True:
        try: app.run_polling(drop_pending_updates=True, close_loop=False, stop_signals=None)
        except Exception as e:
            if "Conflict" in str(e): time.sleep(30); continue
            print(e); time.sleep(10); continue
        break
if __name__=="__main__": run()
