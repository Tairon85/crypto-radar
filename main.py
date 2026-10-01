import json, math, os, random, sqlite3, threading, time, uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

APP_MODE = os.getenv("APP_MODE", "paper").lower()
STARTING_CASH = float(os.getenv("STARTING_CASH_EUR", "50"))
POLL_SECONDS = max(10, int(os.getenv("POLL_SECONDS", "30")))
MAX_TRADE = float(os.getenv("MAX_TRADE_EUR", "10"))
MAX_OPEN = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS_EUR", "2"))
MIN_RR = float(os.getenv("MIN_RR", "1.8"))
API_KEY = os.getenv("ETORO_API_KEY", "").strip()
USER_KEY = os.getenv("ETORO_USER_KEY", "").strip()
WATCHLIST_RAW = os.getenv("ETORO_WATCHLIST_JSON", "").strip()

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "radar.db"
DB.parent.mkdir(parents=True, exist_ok=True)

# Track the user's current experiment portfolio as context only; paper engine starts with its own €50.
REFERENCE_POSITIONS = {
    "TAO": {"entry": 222.0, "note": "existing real position"},
    "UNI": {"entry": 8.8822, "note": "existing real position"},
    "AVAX": {"entry": 10.43, "note": "existing real position"},
    "XRP": {"entry": 2.47724, "note": "existing real position"},
    "SUI": {"entry": None, "note": "existing small position"},
}

DEFAULT_SYMBOLS = ["TAO", "UNI", "AVAX", "XRP", "SUI", "ETH", "SOL", "LINK", "AAVE", "ADA", "XLM"]

app = FastAPI(title="Crypto Radar – Continuous Analyst", version="0.1.0")
prices: Dict[str, deque] = defaultdict(lambda: deque(maxlen=240))
latest: Dict[str, dict] = {}
lock = threading.Lock()
engine_started = False


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS trades (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      symbol TEXT NOT NULL, status TEXT NOT NULL,
      entry_price REAL NOT NULL, current_price REAL NOT NULL,
      amount_eur REAL NOT NULL, units REAL NOT NULL,
      opened_at TEXT NOT NULL, closed_at TEXT,
      exit_price REAL, pnl_eur REAL DEFAULT 0,
      peak_price REAL NOT NULL, protected_price REAL,
      reason_open TEXT, reason_close TEXT
    );
    CREATE TABLE IF NOT EXISTS decisions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts TEXT NOT NULL, symbol TEXT NOT NULL, action TEXT NOT NULL,
      score REAL NOT NULL, price REAL NOT NULL, reason TEXT NOT NULL,
      rsi REAL, fast REAL, slow REAL, momentum REAL, volatility REAL
    );
    """)
    if not c.execute("SELECT 1 FROM state WHERE k='cash'").fetchone():
        c.execute("INSERT INTO state(k,v) VALUES('cash',?)", (str(STARTING_CASH),))
    c.commit(); c.close()


def get_cash() -> float:
    c=db(); r=c.execute("SELECT v FROM state WHERE k='cash'").fetchone(); c.close()
    return float(r[0]) if r else STARTING_CASH


def set_cash(v: float):
    c=db(); c.execute("INSERT OR REPLACE INTO state(k,v) VALUES('cash',?)", (f"{v:.8f}",)); c.commit(); c.close()


def open_trades():
    c=db(); rows=c.execute("SELECT * FROM trades WHERE status='OPEN' ORDER BY id DESC").fetchall(); c.close()
    return [dict(r) for r in rows]


def today_realized_pnl():
    day=datetime.now(timezone.utc).date().isoformat()
    c=db(); r=c.execute("SELECT COALESCE(SUM(pnl_eur),0) FROM trades WHERE status='CLOSED' AND substr(closed_at,1,10)=?", (day,)).fetchone(); c.close()
    return float(r[0] or 0)


def ema(vals: List[float], period: int) -> Optional[float]:
    if len(vals) < period: return None
    a=2/(period+1); e=vals[-period]
    for x in vals[-period+1:]: e=a*x+(1-a)*e
    return e


def rsi(vals: List[float], period=14) -> Optional[float]:
    if len(vals) < period+1: return None
    ds=[vals[i]-vals[i-1] for i in range(len(vals)-period, len(vals))]
    gains=sum(max(d,0) for d in ds)/period
    losses=sum(max(-d,0) for d in ds)/period
    if losses == 0: return 100.0
    rs=gains/losses
    return 100-(100/(1+rs))


def features(symbol: str):
    vals=list(prices[symbol])
    if len(vals)<35: return None
    p=vals[-1]; fast=ema(vals,10); slow=ema(vals,30); rr=rsi(vals,14)
    momentum=(p/vals[-6]-1)*100 if len(vals)>=6 else 0
    rets=[abs(vals[i]/vals[i-1]-1) for i in range(max(1,len(vals)-20),len(vals))]
    vol=(sum(rets)/len(rets))*100 if rets else 0
    hi=max(vals[-20:]); lo=min(vals[-20:])
    return {"price":p,"fast":fast,"slow":slow,"rsi":rr,"momentum":momentum,"volatility":vol,"hi20":hi,"lo20":lo}


def score_setup(f):
    score=50.0; reasons=[]
    if f["fast"] and f["slow"] and f["fast"]>f["slow"]: score+=16; reasons.append("trend breve sopra trend lento")
    else: score-=12; reasons.append("trend non ancora confermato")
    if 45 <= f["rsi"] <= 68: score+=12; reasons.append("RSI costruttivo")
    elif f["rsi"] < 35: score+=4; reasons.append("RSI scarico, serve conferma")
    elif f["rsi"] > 75: score-=18; reasons.append("RSI troppo tirato")
    if f["momentum"]>0.25: score+=10; reasons.append("momentum positivo")
    if f["momentum"]>2.5: score-=8; reasons.append("movimento già esteso")
    if f["price"] <= f["lo20"]*1.025: score+=8; reasons.append("vicino a supporto locale")
    if f["price"] >= f["hi20"]*0.995: score+=5; reasons.append("test breakout")
    if f["volatility"]>2.0: score-=8; reasons.append("volatilità elevata")
    return max(0,min(100,score)), ", ".join(reasons)


def etoro_headers():
    return {"x-api-key":API_KEY,"x-user-key":USER_KEY,"x-request-id":str(uuid.uuid4()),"Accept":"application/json"}


def parse_watchlist():
    if not WATCHLIST_RAW: return {}
    try: return json.loads(WATCHLIST_RAW)
    except Exception: return {}


def fetch_etoro_rates():
    wl=parse_watchlist()
    if not (API_KEY and USER_KEY and wl): return None
    ids=','.join(wl.keys())
    url=f"https://public-api.etoro.com/api/v1/market-data/instruments/rates?instrumentIds={ids}"
    r=requests.get(url,headers=etoro_headers(),timeout=12)
    r.raise_for_status(); body=r.json()
    rows=body.get("rates") or body.get("data") or body.get("items") or []
    out={}
    for row in rows:
        iid=str(row.get("instrumentId") or row.get("instrumentID") or "")
        sym=wl.get(iid) or row.get("symbol")
        px=row.get("lastPrice") or row.get("mid") or row.get("close")
        if sym and isinstance(px,(int,float)): out[sym]=float(px)
    return out or None


sim_base={"TAO":300,"UNI":8.8,"AVAX":10.6,"XRP":1.3,"SUI":0.75,"ETH":2650,"SOL":115,"LINK":11.4,"AAVE":160,"ADA":0.2,"XLM":0.19}
sim_px=dict(sim_base)

def fetch_simulated_rates():
    out={}
    for s in DEFAULT_SYMBOLS:
        px=sim_px[s]
        drift=0.0004*math.sin(time.time()/180 + hash(s)%7)
        shock=random.gauss(0,0.0035)
        px=max(0.0001, px*(1+drift+shock))
        sim_px[s]=px; out[s]=px
    return out


def paper_open(symbol, price, score, reason):
    if APP_MODE != "paper": return
    ots=open_trades()
    if len(ots)>=MAX_OPEN or any(t["symbol"]==symbol for t in ots): return
    if today_realized_pnl() <= -MAX_DAILY_LOSS: return
    cash=get_cash(); amount=min(MAX_TRADE,cash)
    if amount<2: return
    units=amount/price
    c=db(); now=datetime.now(timezone.utc).isoformat()
    c.execute("INSERT INTO trades(symbol,status,entry_price,current_price,amount_eur,units,opened_at,peak_price,protected_price,reason_open) VALUES(?,?,?,?,?,?,?,?,?,?)",
              (symbol,"OPEN",price,price,amount,units,now,price,None,reason))
    c.commit(); c.close(); set_cash(cash-amount)


def paper_manage(symbol, price, f):
    c=db(); row=c.execute("SELECT * FROM trades WHERE status='OPEN' AND symbol=? ORDER BY id DESC LIMIT 1",(symbol,)).fetchone()
    if not row: c.close(); return
    t=dict(row); peak=max(t["peak_price"],price)
    gain=price/t["entry_price"]-1
    protected=t["protected_price"]
    # Dynamic profit protection: wider while trend is strong, tighter after +5%/+10%.
    if gain>=0.10: protected=max(protected or 0, peak*0.965)
    elif gain>=0.05: protected=max(protected or 0, peak*0.975)
    elif gain>=0.025: protected=max(protected or 0, t["entry_price"]*1.005)
    hard_stop=t["entry_price"]*(1-0.03)
    weakening=(f and f["fast"] and f["slow"] and f["fast"]<f["slow"] and f["momentum"]<0)
    reason=None
    if protected and price<=protected: reason="trailing intelligente: profitto protetto"
    elif price<=hard_stop: reason="stop rischio -3%"
    elif gain>0.01 and weakening: reason="struttura/momentum deteriorati"
    if reason:
        pnl=(price-t["entry_price"])*t["units"]
        now=datetime.now(timezone.utc).isoformat()
        c.execute("UPDATE trades SET status='CLOSED',current_price=?,exit_price=?,pnl_eur=?,closed_at=?,peak_price=?,protected_price=?,reason_close=? WHERE id=?",
                  (price,price,pnl,now,peak,protected,reason,t["id"]))
        c.commit(); c.close(); set_cash(get_cash()+t["amount_eur"]+pnl)
    else:
        c.execute("UPDATE trades SET current_price=?,peak_price=?,protected_price=? WHERE id=?",(price,peak,protected,t["id"])); c.commit(); c.close()


def analyze_and_act(symbol, price):
    with lock:
        prices[symbol].append(price); latest[symbol]={"price":price,"ts":datetime.now(timezone.utc).isoformat()}
    f=features(symbol)
    if not f: return
    paper_manage(symbol,price,f)
    score, reason=score_setup(f)
    action="ASPETTA"
    if score>=78 and f["momentum"]<2.5: action="ENTRA"
    if any(t["symbol"]==symbol for t in open_trades()): action="TIENI"
    c=db(); c.execute("INSERT INTO decisions(ts,symbol,action,score,price,reason,rsi,fast,slow,momentum,volatility) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (datetime.now(timezone.utc).isoformat(),symbol,action,score,price,reason,f["rsi"],f["fast"],f["slow"],f["momentum"],f["volatility"])); c.commit(); c.close()
    if action=="ENTRA": paper_open(symbol,price,score,reason)


def loop():
    while True:
        try:
            rates=fetch_etoro_rates() or fetch_simulated_rates()
            for s,p in rates.items(): analyze_and_act(s,float(p))
        except Exception as e:
            print("engine error",repr(e),flush=True)
        time.sleep(POLL_SECONDS)


def start_engine():
    global engine_started
    if engine_started: return
    engine_started=True
    threading.Thread(target=loop,daemon=True).start()


@app.on_event("startup")
def startup():
    init_db(); start_engine()


@app.get("/health")
def health():
    return {"ok":True,"mode":APP_MODE,"feed":"etoro" if (API_KEY and USER_KEY and parse_watchlist()) else "simulator"}


@app.get("/api/status")
def status():
    c=db(); decisions=[dict(r) for r in c.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT 40").fetchall()]; closed=[dict(r) for r in c.execute("SELECT * FROM trades WHERE status='CLOSED' ORDER BY id DESC LIMIT 20").fetchall()]; c.close()
    ots=open_trades(); unreal=sum((latest.get(t['symbol'],{}).get('price',t['current_price'])-t['entry_price'])*t['units'] for t in ots)
    equity=get_cash()+sum(t['amount_eur']+(latest.get(t['symbol'],{}).get('price',t['current_price'])-t['entry_price'])*t['units'] for t in ots)
    return {"mode":APP_MODE,"feed":"eToro" if (API_KEY and USER_KEY and parse_watchlist()) else "SIMULATORE",
            "starting_cash":STARTING_CASH,"cash":round(get_cash(),2),"equity":round(equity,2),"unrealized":round(unreal,2),"today_realized":round(today_realized_pnl(),2),
            "open":ots,"closed":closed,"decisions":decisions,"reference_positions":REFERENCE_POSITIONS,"poll_seconds":POLL_SECONDS}


@app.post("/api/reset-paper")
def reset_paper():
    c=db(); c.execute("DELETE FROM trades"); c.execute("DELETE FROM decisions"); c.execute("INSERT OR REPLACE INTO state(k,v) VALUES('cash',?)",(str(STARTING_CASH),)); c.commit(); c.close()
    return {"ok":True,"cash":STARTING_CASH}


DASH='''<!doctype html><html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Crypto Radar</title>
<style>body{font-family:system-ui;background:#0b1020;color:#eef2ff;margin:0}.wrap{max-width:1100px;margin:auto;padding:18px}.top{display:flex;gap:12px;flex-wrap:wrap}.card{background:#151c33;border:1px solid #26304d;border-radius:16px;padding:16px;flex:1;min-width:150px}.big{font-size:28px;font-weight:800}.muted{color:#9ba8c7}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px;margin-top:14px}.row{display:flex;justify-content:space-between;gap:8px;border-bottom:1px solid #27304a;padding:9px 0}.badge{padding:4px 8px;border-radius:999px;background:#24304f;font-weight:700}.enter{background:#174934}.wait{background:#4d3b16}.hold{background:#183b55}h1{margin-bottom:4px}button{background:#263b68;color:white;border:0;border-radius:10px;padding:9px 12px}</style></head><body><div class="wrap"><h1>Crypto Radar – Continuous Analyst</h1><div class="muted">V0.1 • paper trading • aggiornamento automatico</div><div class="top" id="summary"></div><div class="grid"><div class="card"><h3>Posizioni simulate</h3><div id="open"></div></div><div class="card"><h3>Radar</h3><div id="radar"></div></div><div class="card"><h3>Ultime chiusure</h3><div id="closed"></div></div></div></div><script>
const euro=x=>'€'+Number(x).toFixed(2); async function tick(){let s=await (await fetch('/api/status')).json();
summary.innerHTML=`<div class=card><div class=muted>Equity</div><div class=big>${euro(s.equity)}</div></div><div class=card><div class=muted>Cash</div><div class=big>${euro(s.cash)}</div></div><div class=card><div class=muted>P/L oggi</div><div class=big>${euro(s.today_realized)}</div></div><div class=card><div class=muted>Feed</div><div class=big>${s.feed}</div></div>`;
open.innerHTML=s.open.length?s.open.map(x=>`<div class=row><span><b>${x.symbol}</b><br><small>${x.reason_open||''}</small></span><span>${euro(x.amount_eur)}<br><small>${Number(x.entry_price).toFixed(4)} → ${Number(x.current_price).toFixed(4)}</small></span></div>`).join(''):'<div class=muted>Nessuna posizione</div>';
let uniq=[];let seen=new Set();for(const d of s.decisions){if(!seen.has(d.symbol)){seen.add(d.symbol);uniq.push(d)}} radar.innerHTML=uniq.slice(0,11).map(d=>`<div class=row><span><b>${d.symbol}</b><br><small>${Number(d.price).toFixed(4)} • score ${Number(d.score).toFixed(0)}</small></span><span class="badge ${d.action==='ENTRA'?'enter':d.action==='TIENI'?'hold':'wait'}">${d.action}</span></div>`).join('');
closed.innerHTML=s.closed.length?s.closed.slice(0,8).map(x=>`<div class=row><span><b>${x.symbol}</b><br><small>${x.reason_close||''}</small></span><span>${euro(x.pnl_eur)}</span></div>`).join(''):'<div class=muted>Nessuna chiusura</div>';}
tick();setInterval(tick,5000);</script></body></html>'''

@app.get("/", response_class=HTMLResponse)
def home(): return DASH
