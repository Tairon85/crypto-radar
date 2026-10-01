# Crypto Radar V0.2.1 - Railway refresh build
# Updated main.py to force a fresh GitHub commit/deploy.
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

app = FastAPI(title="Crypto Radar – Continuous Analyst", version="0.2.1")
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
    c=db()
    decisions=[dict(r) for r in c.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT 80").fetchall()]
    closed=[dict(r) for r in c.execute("SELECT * FROM trades WHERE status='CLOSED' ORDER BY id DESC LIMIT 50").fetchall()]
    c.close()
    now=datetime.now(timezone.utc)
    ots=[]
    for t in open_trades():
        t=dict(t)
        px=float(latest.get(t['symbol'],{}).get('price',t['current_price']))
        t['current_price']=px
        t['pnl_eur']=(px-t['entry_price'])*t['units']
        t['pnl_pct']=(px/t['entry_price']-1)*100 if t['entry_price'] else 0
        try:
            opened=datetime.fromisoformat(t['opened_at'].replace('Z','+00:00'))
            t['age_minutes']=max(0,int((now-opened).total_seconds()/60))
        except Exception:
            t['age_minutes']=0
        ots.append(t)
    unreal=sum(t['pnl_eur'] for t in ots)
    equity=get_cash()+sum(t['amount_eur']+t['pnl_eur'] for t in ots)
    realized_total=sum(float(x.get('pnl_eur') or 0) for x in closed)
    wins=[x for x in closed if float(x.get('pnl_eur') or 0)>0]
    losses=[x for x in closed if float(x.get('pnl_eur') or 0)<0]
    win_rate=(len(wins)/len(closed)*100) if closed else 0
    avg_win=(sum(float(x['pnl_eur']) for x in wins)/len(wins)) if wins else 0
    avg_loss=(sum(float(x['pnl_eur']) for x in losses)/len(losses)) if losses else 0
    return {"mode":APP_MODE,"feed":"eToro" if (API_KEY and USER_KEY and parse_watchlist()) else "SIMULATORE",
            "starting_cash":STARTING_CASH,"cash":round(get_cash(),2),"equity":round(equity,2),
            "unrealized":round(unreal,2),"today_realized":round(today_realized_pnl(),2),
            "realized_total":round(realized_total,2),"closed_count":len(closed),"win_rate":round(win_rate,1),
            "avg_win":round(avg_win,2),"avg_loss":round(avg_loss,2),
            "open":ots,"closed":closed,"decisions":decisions,"reference_positions":REFERENCE_POSITIONS,
            "poll_seconds":POLL_SECONDS,"version":"0.2.1"}

@app.post("/api/reset-paper")
def reset_paper():
    c=db(); c.execute("DELETE FROM trades"); c.execute("DELETE FROM decisions"); c.execute("INSERT OR REPLACE INTO state(k,v) VALUES('cash',?)",(str(STARTING_CASH),)); c.commit(); c.close()
    return {"ok":True,"cash":STARTING_CASH}

DASH='''<!doctype html><html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Crypto Radar</title>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,sans-serif;background:#0b1020;color:#eef2ff;margin:0}.wrap{max-width:1100px;margin:auto;padding:18px}.top{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.card{background:#151c33;border:1px solid #26304d;border-radius:16px;padding:16px;min-width:0}.big{font-size:28px;font-weight:800}.muted{color:#9ba8c7}.positive{color:#56d58a}.negative{color:#ff7070}.grid{display:grid;grid-template-columns:1fr;gap:12px;margin-top:14px}.row{display:flex;justify-content:space-between;align-items:center;gap:10px;border-bottom:1px solid #27304a;padding:12px 0}.row:last-child{border-bottom:0}.right{text-align:right}.badge{padding:6px 10px;border-radius:999px;background:#24304f;font-weight:800;white-space:nowrap}.enter{background:#175d3d}.wait{background:#674b11}.hold{background:#174662}h1{margin:0 0 4px;font-size:34px;line-height:1.05}.section{font-size:23px;margin:0 0 8px}.reason{max-width:66vw}.statline{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}.pill{background:#202a46;border-radius:10px;padding:7px 9px}.protect{color:#ffd166}@media(min-width:850px){.top{grid-template-columns:repeat(4,1fr)}.grid{grid-template-columns:1.1fr 1fr 1fr}.reason{max-width:260px}}
</style></head><body><div class="wrap"><h1>Crypto Radar – Continuous Analyst</h1><div class="muted">V0.2.1 • paper trading • aggiornamento automatico</div><div class="top" id="summary"></div><div class="grid"><div class="card"><h3 class="section">Posizioni simulate</h3><div id="openTrades"></div></div><div class="card"><h3 class="section">Radar</h3><div id="radarList"></div></div><div class="card"><h3 class="section">Storico chiusure</h3><div id="closedTrades"></div></div></div></div><script>
const euro=x=>'€'+Number(x||0).toFixed(2); const cls=x=>Number(x)>=0?'positive':'negative';
const age=m=>m<60?`${m} min`:m<1440?`${(m/60).toFixed(1)} h`:`${(m/1440).toFixed(1)} g`;
async function tick(){
 let s=await (await fetch('/api/status',{cache:'no-store'})).json();
 const summary=document.getElementById('summary'), openEl=document.getElementById('openTrades'), radarEl=document.getElementById('radarList'), closedEl=document.getElementById('closedTrades');
 summary.innerHTML=`<div class=card><div class=muted>Equity</div><div class=big>${euro(s.equity)}</div></div><div class=card><div class=muted>Cash</div><div class=big>${euro(s.cash)}</div></div><div class=card><div class=muted>P/L aperto</div><div class="big ${cls(s.unrealized)}">${euro(s.unrealized)}</div><div class=muted>Realizzato oggi ${euro(s.today_realized)}</div></div><div class=card><div class=muted>Test</div><div class=big>${s.closed_count} trade</div><div class=muted>Win rate ${Number(s.win_rate).toFixed(1)}% • Feed ${s.feed}</div></div>`;
 openEl.innerHTML=s.open.length?s.open.map(x=>`<div class=row><div class=reason><b>${x.symbol}</b> • ${euro(x.amount_eur)}<br><small>${Number(x.entry_price).toFixed(4)} → ${Number(x.current_price).toFixed(4)} • ${age(x.age_minutes)}</small><br><small class=muted>${x.reason_open||''}</small>${x.protected_price?`<br><small class=protect>Profitto protetto da ${Number(x.protected_price).toFixed(4)}</small>`:''}</div><div class=right><b class="${cls(x.pnl_eur)}">${euro(x.pnl_eur)}</b><br><small class="${cls(x.pnl_pct)}">${Number(x.pnl_pct).toFixed(2)}%</small></div></div>`).join(''):'<div class=muted>Nessuna posizione aperta</div>';
 let uniq=[];let seen=new Set();for(const d of s.decisions){if(!seen.has(d.symbol)){seen.add(d.symbol);uniq.push(d)}}
 radarEl.innerHTML=uniq.slice(0,15).map(d=>`<div class=row><span><b>${d.symbol}</b><br><small>${Number(d.price).toFixed(4)} • score ${Number(d.score).toFixed(0)}</small><br><small class=muted>RSI ${d.rsi==null?'—':Number(d.rsi).toFixed(0)} • mom ${d.momentum==null?'—':Number(d.momentum).toFixed(2)}%</small></span><span class="badge ${d.action==='ENTRA'?'enter':d.action==='TIENI'?'hold':'wait'}">${d.action}</span></div>`).join('');
 closedEl.innerHTML=`<div class=statline><span class=pill>Realizzato ${euro(s.realized_total)}</span><span class=pill>Win ${Number(s.win_rate).toFixed(0)}%</span><span class=pill>Media + ${euro(s.avg_win)}</span><span class=pill>Media − ${euro(s.avg_loss)}</span></div>`+(s.closed.length?s.closed.slice(0,12).map(x=>`<div class=row><div><b>${x.symbol}</b><br><small>${Number(x.entry_price).toFixed(4)} → ${Number(x.exit_price||x.current_price).toFixed(4)}</small><br><small class=muted>${x.reason_close||''}</small></div><div class=right><b class="${cls(x.pnl_eur)}">${euro(x.pnl_eur)}</b></div></div>`).join(''):'<div class=muted style="margin-top:12px">Nessuna chiusura ancora</div>');
}
tick();setInterval(tick,5000);</script></body></html>'''

@app.get("/", response_class=HTMLResponse)
def home(): return DASH
