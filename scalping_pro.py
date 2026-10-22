import ccxt, asyncio, pandas as pd, os, threading, traceback, time
from flask import Flask
from telegram.ext import ApplicationBuilder, CommandHandler, ConversationHandler, MessageHandler, filters

SYMBOL = "BTC/USD:USD" 
TIMEFRAME = "5m"
LEVERAGE = 25
FIXED_DOLLARS = 2.0
ASKING_AMOUNT = 1
TP_PCT = 0.004
SL_PCT = 0.008

DELTA_API_KEY = os.getenv("DELTA_API_KEY")
DELTA_API_SECRET = os.getenv("DELTA_API_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
PAPER_TRADE = os.getenv("PAPER_TRADE", "True") == "True"

print(f"START V5 PRO: {SYMBOL} Paper={PAPER_TRADE} Fixed={FIXED_DOLLARS}", flush=True)

exchange = ccxt.delta({'apiKey': DELTA_API_KEY, 'secret': DELTA_API_SECRET, 'enableRateLimit': True, 'options': {'defaultType': 'future'}})
try:
    exchange.load_markets()
    print(f"Markets loaded", flush=True)
except Exception as e:
    print(f"Market load warning {e}", flush=True)

position=None; entry_price=0

web_app=Flask(__name__)
@web_app.route('/')
def home(): return f"<h1>V5 PRO RUNNING {SYMBOL} POS:{position} FIX:{FIXED_DOLLARS}</h1>"
def run_web(): web_app.run(host='0.0.0.0', port=int(os.environ.get("PORT",10000)))
threading.Thread(target=run_web, daemon=True).start()

def get_indicators():
    try:
        ohlcv = None
        for sym in ["BTC/USD:USD", "BTC/USD", "BTCUSD"]:
            try:
                ohlcv = exchange.fetch_ohlcv(sym, TIMEFRAME, limit=250)
                if ohlcv and len(ohlcv) >= 60:
                    break
            except: continue
        
        if not ohlcv or len(ohlcv) < 60:
            print("No ohlcv data", flush=True)
            return None, None
            
        df=pd.DataFrame(ohlcv, columns=['t','o','h','l','c','v'])
        df['sma']=df['c'].rolling(20).mean()
        df['std']=df['c'].rolling(20).std()
        df['up']=df['sma']+2*df['std']
        df['lo']=df['sma']-2*df['std']
        df['ema']=df['c'].ewm(span=200).mean()
        d=df['c'].diff(); g=d.clip(lower=0).ewm(alpha=1/14).mean()
        l=(-d.clip(upper=0)).ewm(alpha=1/14).mean().replace(0,1e-9)
        df['rsi']=100-(100/(1+g/l))
        last=df.iloc[-1]; price=float(df['c'].iloc[-1])
        if pd.isna(last['rsi']):
            return None, price
        print(f"Price {price:.1f} RSI {float(last['rsi']):.1f}", flush=True)
        return last, price
    except Exception as e:
        print(f"Ind Err {e}", flush=True)
        traceback.print_exc()
        return None, None

def get_qty(d,p): return max(1,int((d or 2)*LEVERAGE))
def place_order(side,qty,ep):
    if PAPER_TRADE: print(f"[PAPER] {side} {qty} @ {ep}",flush=True); return {}
    try:
        exchange.create_market_order(SYMBOL,side,qty)
        tp=ep*1.004 if side=='buy' else ep*0.996
        sl=ep*0.992 if side=='buy' else ep*1.008
        ts='sell' if side=='buy' else 'buy'
        try: exchange.create_order(SYMBOL,'limit',ts,qty,tp,{'reduce_only':True})
        except: pass
        try: exchange.create_order(SYMBOL,'market',ts,qty,None,{'stop_price':sl,'reduce_only':True})
        except: pass
        return {}
    except Exception as e: print(e); return None

def format_market_msg(ind, price):
    try:
        rsi = float(ind['rsi'])
        ema = float(ind['ema'])
        up = float(ind['up'])
        lo = float(ind['lo'])
        sma = float(ind['sma'])
        
        if rsi < 30: rsi_sig = "Oversold - LONG Opportunity"
        elif rsi > 70: rsi_sig = "Overbought - SHORT Opportunity"
        elif rsi < 35: rsi_sig = "Near Oversold - LONG Ready"
        elif rsi > 65: rsi_sig = "Near Overbought - SHORT Ready"
        else: rsi_sig = "Neutral - Waiting"

        if price < lo: bb_sig = "Below Lower Band - LONG Signal"
        elif price > up: bb_sig = "Above Upper Band - SHORT Signal"
        else: bb_sig = "Inside Bands - No Signal"

        trend = "Bullish" if price > ema else "Bearish"

        msg = f"""MARKET ANALYSIS (5m)
Price: ${price:.2f}
RSI(14): {rsi:.2f} - {rsi_sig}
Bollinger Bands:
  Upper: ${up:.2f}
  SMA20: ${sma:.2f}
  Lower: ${lo:.2f}
  Status: {bb_sig}
EMA200: ${ema:.2f} - {trend}

Bot: {SYMBOL} {LEVERAGE}x
Fixed Amount: ${FIXED_DOLLARS}
Position: {position}
TP: 0.4% | SL: 0.8%
"""
        return msg
    except Exception as e:
        return f"Market: ${price:.2f} RSI:{float(ind['rsi']):.1f}"

async def start_cmd(u,c):
    ind, price = get_indicators()
    if ind is not None:
        await u.message.reply_text(format_market_msg(ind, price))
    else:
        await asyncio.sleep(2)
        ind, price = get_indicators()
        if ind is not None:
            await u.message.reply_text(format_market_msg(ind, price))
        else:
            await u.message.reply_text(f"V5 Pro Bot {SYMBOL}\nMarket data loading, please try /info\nFixed: ${FIXED_DOLLARS}")
    
    await u.message.reply_text(f"Enter fixed amount in USD (Current: ${FIXED_DOLLARS})")
    return ASKING_AMOUNT

async def recv_amt(u,c):
    global FIXED_DOLLARS
    try:
        d=float(u.message.text.strip().replace('$',''))
        if d<1: await u.message.reply_text("Minimum $1"); return ASKING_AMOUNT
        FIXED_DOLLARS=d
        ind, price = get_indicators()
        if ind is not None:
            details = format_market_msg(ind, price)
            await u.message.reply_text(f"Fixed Amount Set: ${FIXED_DOLLARS} x {LEVERAGE}x\nBot Auto Started - Waiting for signal...\n\n{details}")
        else:
            await u.message.reply_text(f"Fixed Amount Set: ${FIXED_DOLLARS} x {LEVERAGE}x\nBot Auto Started - Market data will be available shortly. Use /info")
        return ConversationHandler.END
    except: await u.message.reply_text("Please send a number only. Example: 2"); return ASKING_AMOUNT

async def cancel_cmd(u,c): await u.message.reply_text("Cancelled"); return ConversationHandler.END
async def info_cmd(u,c):
    ind, price = get_indicators()
    if ind is not None: await u.message.reply_text(format_market_msg(ind, price))
    else: await u.message.reply_text("Market data loading... Please try again in 10 seconds")

async def pnl_cmd(u,c): await u.message.reply_text(f"Position: {position}\nEntry: {entry_price}\nFixed: ${FIXED_DOLLARS}\nLeverage: {LEVERAGE}x")
async def close_cmd(u,c):
    global position
    if position:
        _,pr=get_indicators(); q=get_qty(FIXED_DOLLARS,pr or 85000); sd='sell' if position=="LONG" else 'buy'
        if not PAPER_TRADE:
            try: exchange.create_market_order(SYMBOL,sd,q,None,{'reduce_only':True})
            except: exchange.create_market_order(SYMBOL,sd,q)
        await u.message.reply_text(f"Closed {position}"); position=None
    else: await u.message.reply_text("No open position")
async def balance_cmd(u,c): 
    ind, price = get_indicators()
    txt = f"Paper: {PAPER_TRADE}\nFixed: ${FIXED_DOLLARS}\nSymbol: {SYMBOL}\nPosition: {position}"
    if ind is not None: txt += f"\n\n{format_market_msg(ind, price)}"
    await u.message.reply_text(txt)

async def trading_loop(app):
    global position, entry_price
    try: await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID,text=f"V5 PRO BOT STARTED {SYMBOL} Fixed:${FIXED_DOLLARS}\nUse /start to see market details")
    except: pass
    while True:
        try:
            ind,price=get_indicators()
            if not ind: await asyncio.sleep(15); continue
            q=get_qty(FIXED_DOLLARS,price)
            if position is None:
                long_c=price<float(ind['lo'])*1.001 and float(ind['rsi'])<35 and price>float(ind['ema'])
                short_c=price>float(ind['up'])*0.999 and float(ind['rsi'])>65 and price<float(ind['ema'])
                if long_c:
                    place_order('buy',q,price); position="LONG"; entry_price=price
                    try: await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID,text=f"LONG ENTRY ${price:.2f}\nRSI {float(ind['rsi']):.1f}\n{format_market_msg(ind, price)}")
                    except: pass
                elif short_c:
                    place_order('sell',q,price); position="SHORT"; entry_price=price
                    try: await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID,text=f"SHORT ENTRY ${price:.2f}\nRSI {float(ind['rsi']):.1f}\n{format_market_msg(ind, price)}")
                    except: pass
            else:
                pnl=(price-entry_price)/entry_price if position=="LONG" else (entry_price-price)/entry_price
                if pnl<=-SL_PCT or pnl>=TP_PCT:
                    try: await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID,text=f"{'TP HIT' if pnl>0 else 'SL HIT'} {position} {pnl*100:.2f}%\nPrice ${price:.2f}")
                    except: pass
                    position=None
            await asyncio.sleep(30)
        except Exception as e: print(e); await asyncio.sleep(10)

def run():
    app=ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    conv=ConversationHandler(entry_points=[CommandHandler("start",start_cmd)],states={ASKING_AMOUNT:[MessageHandler(filters.TEXT & ~filters.COMMAND,recv_amt)]},fallbacks=[CommandHandler("cancel",cancel_cmd)])
    app.add_handler(conv); app.add_handler(CommandHandler("info",info_cmd)); app.add_handler(CommandHandler("pnl",pnl_cmd)); app.add_handler(CommandHandler("close",close_cmd)); app.add_handler(CommandHandler("balance",balance_cmd))
    async def on_startup(a):
        await asyncio.sleep(5)
        try: await a.bot.delete_webhook(drop_pending_updates=True); print("Webhook deleted",flush=True)
        except: pass
        asyncio.create_task(trading_loop(a))
    app.post_init=on_startup
    print("Bot polling V5 PRO...",flush=True)
    while True:
        try: app.run_polling(drop_pending_updates=True, close_loop=False, stop_signals=None)
        except Exception as e:
            if "Conflict" in str(e): print("Conflict wait 30s",flush=True); time.sleep(30); continue
            print(e); time.sleep(10); continue
        break
if __name__=="__main__": run()
