#!/usr/bin/env python3
"""PEA Screener — http://localhost:5050"""
import sys, time, warnings, datetime, threading, webbrowser
import numpy as np, pandas as pd, yfinance as yf
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from flask import Flask, jsonify, render_template_string
warnings.filterwarnings("ignore")

CAC40 = {
    "AIR.PA":"Airbus","AI.PA":"Air Liquide","ALO.PA":"Alstom","MT.AS":"ArcelorMittal",
    "CS.PA":"AXA","BNP.PA":"BNP Paribas","EN.PA":"Bouygues","CAP.PA":"Capgemini",
    "CA.PA":"Carrefour","ACA.PA":"Credit Agricole","BN.PA":"Danone","DSY.PA":"Dassault Systemes",
    "ENGI.PA":"Engie","EL.PA":"EssilorLuxottica","ERF.PA":"Eurofins","RMS.PA":"Hermes",
    "KER.PA":"Kering","OR.PA":"L Oreal","LR.PA":"Legrand","MC.PA":"LVMH",
    "ML.PA":"Michelin","ORA.PA":"Orange","RI.PA":"Pernod Ricard","PUB.PA":"Publicis",
    "RNO.PA":"Renault","SAF.PA":"Safran","SGO.PA":"Saint-Gobain","SAN.PA":"Sanofi",
    "SU.PA":"Schneider Electric","GLE.PA":"Soc Generale","STM.PA":"STMicroelectronics",
    "TEP.PA":"Teleperformance","HO.PA":"Thales","TTE.PA":"TotalEnergies",
    "VIE.PA":"Veolia","DG.PA":"Vinci","VIV.PA":"Vivendi","WLN.PA":"Worldline",
}
EUROSTOXX = {
    "SAP.DE":"SAP","SIE.DE":"Siemens","ALV.DE":"Allianz","BMW.DE":"BMW",
    "BAS.DE":"BASF","BAYN.DE":"Bayer","MBG.DE":"Mercedes-Benz","MUV2.DE":"Munich Re",
    "DTE.DE":"Deutsche Telekom","ADS.DE":"Adidas","IFX.DE":"Infineon","RWE.DE":"RWE",
    "DHL.DE":"DHL Group","DB1.DE":"Deutsche Borse","VOW3.DE":"Volkswagen",
    "HEN3.DE":"Henkel","MRK.DE":"Merck KGaA","BEI.DE":"Beiersdorf",
    "ASML.AS":"ASML","PHIA.AS":"Philips","UNA.AS":"Unilever","INGA.AS":"ING Groep",
    "HEIA.AS":"Heineken","WKL.AS":"Wolters Kluwer","AD.AS":"Ahold Delhaize",
    "AKZA.AS":"Akzo Nobel","BESI.AS":"BE Semiconductor",
    "ITX.MC":"Inditex","SAN.MC":"Banco Santander","TEF.MC":"Telefonica",
    "IBE.MC":"Iberdrola","BBVA.MC":"BBVA","REP.MC":"Repsol","AMS.MC":"Amadeus IT",
    "ENI.MI":"ENI","ENEL.MI":"Enel","ISP.MI":"Intesa Sanpaolo","UCG.MI":"UniCredit","G.MI":"Generali",
    "UCB.BR":"UCB","ABI.BR":"AB InBev","KBC.BR":"KBC",
    "ERIC-B.ST":"Ericsson","VOLV-B.ST":"Volvo","ATCO-A.ST":"Atlas Copco",
    "SAND.ST":"Sandvik","HM-B.ST":"H&M","ABB.ST":"ABB",
    "NOVO-B.CO":"Novo Nordisk","MAERSK-B.CO":"Maersk","CARL-B.CO":"Carlsberg",
    "NOKIA.HE":"Nokia","KNEBV.HE":"Kone","VER.VI":"Verbund","EBS.VI":"Erste Group",
}
ETF_PEA = {
    "CW8.PA":"Amundi MSCI World","PAEEM.PA":"Amundi Emerging PEA",
    "PE500.PA":"Amundi PEA S&P500","PANX.PA":"Amundi PEA Nasdaq",
    "ESE.PA":"Amundi MSCI Europe","C6E.PA":"Amundi EuroStoxx50",
}

def _val(df, keys):
    if df is None or df.empty:
        return None
    for k in keys:
        if k in df.index:
            v = df.loc[k].iloc[0]
            if pd.notna(v) and v != 0:
                return float(v)
    return None

def compute_roic(t, info):
    try:
        fin, bs = t.financials, t.balance_sheet
        ebit = _val(fin, ["EBIT","Operating Income","OperatingIncome"])
        tax  = max(0.0, min(float(info.get("effectiveTaxRate") or 0.25), 0.60))
        eq   = _val(bs, ["Stockholders Equity","Total Stockholder Equity","Common Stock Equity"])
        ltd  = _val(bs, ["Long Term Debt","LongTermDebt"]) or 0
        std  = _val(bs, ["Current Debt","Short Term Debt"]) or 0
        cash = _val(bs, ["Cash And Cash Equivalents","Cash"]) or 0
        if ebit and eq:
            ic = eq + ltd + std - cash
            if ic > 0:
                return round(ebit*(1-tax)/ic*100, 2)
    except Exception:
        pass
    roe = info.get("returnOnEquity")
    if roe and -1 < roe < 5:
        return round(roe*100, 2)
    return None

def fetch_one(ticker, name, cat):
    time.sleep(0.1)
    try:
        t    = yf.Ticker(ticker)
        info = t.info or {}
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        if not price:
            return None
        target = info.get("targetMeanPrice")
        n      = int(info.get("numberOfAnalystOpinions") or 0)
        pe     = info.get("forwardPE")
        upside = round((target-price)/price*100, 2) if target and price > 0 else None
        roic   = compute_roic(t, info) if cat != "ETF PEA" else None
        score  = None
        if roic and roic > 0 and pe and pe > 0 and upside is not None and n >= 2:
            score = round((roic/pe)*(1+upside*np.sqrt(n)/100), 4)
        return {
            "ticker":ticker,"nom":name,"categorie":cat,
            "secteur":info.get("sector") or info.get("category") or "--",
            "prix":round(price,2),"cible":round(target,2) if target else None,
            "upside":upside,"roic":roic,"pe_fwd":round(pe,1) if pe else None,
            "analystes":n,"score":score,
            "mkt_cap_m":round(info.get("marketCap",0)/1e6) if info.get("marketCap") else None,
        }
    except Exception as e:
        print(f"  {ticker}: {e}", file=sys.stderr)
        return None

def fetch_all(cb=None):
    tasks = ([(t,n,"CAC 40") for t,n in CAC40.items()] +
             [(t,n,"EuroStoxx") for t,n in EUROSTOXX.items()] +
             [(t,n,"ETF PEA") for t,n in ETF_PEA.items()])
    total, done, results = len(tasks), 0, []
    ex = ThreadPoolExecutor(max_workers=5)
    futs = {ex.submit(fetch_one,t,n,c):t for t,n,c in tasks}
    pending = set(futs.keys())
    deadline = time.time() + 60
    while pending and time.time() < deadline:
        finished, pending = wait(pending, timeout=2, return_when=FIRST_COMPLETED)
        for fut in finished:
            done += 1
            try:
                row = fut.result()
            except Exception:
                row = None
            if row:
                results.append(row)
            if cb:
                cb(done, total)
    if cb and done < total:
        cb(total, total)
    ex.shutdown(wait=False)  # Don't block on threads still hanging (e.g. stuck t.history())
    return results

app = Flask(__name__)
_cache = {"data":None,"last_update":None,"loading":False,"progress":[0,1]}

HTML = """<!DOCTYPE html>
<html lang='fr'>
<head>
<meta charset='UTF-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>PEA Screener</title>
<link href='https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css' rel='stylesheet'>
<link href='https://cdn.datatables.net/1.13.8/css/dataTables.bootstrap5.min.css' rel='stylesheet'>
<script src='https://cdn.jsdelivr.net/npm/chart.js@4.4.2/dist/chart.umd.min.js'></script>
<style>
:root{--p:#1a3a5c;--a:#e8a020}
body{background:#f0f4f8;font-family:Arial,sans-serif;font-size:13px}
.navbar{background:var(--p) !important}
.navbar-brand{font-size:1.2rem;font-weight:700}
.card{border:none;border-radius:10px;box-shadow:0 2px 8px rgba(0,0,0,.07)}
.card-header{background:var(--p);color:#fff;font-weight:600;border-radius:10px 10px 0 0 !important}
.bc{font-size:.75rem;padding:3px 7px;border-radius:10px;display:inline-block}
.bca{background:#d0e8ff;color:#004080}
.bce{background:#d4edda;color:#155724}
.bcf{background:#fff3cd;color:#856404}
th{white-space:nowrap;font-size:12px !important}
.sh{color:#198754;font-weight:700}.sm{color:#e8a020;font-weight:600}.sl{color:#dc3545}
.up{color:#198754}.dn{color:#dc3545}
.rk{background:#fef9e7 !important}
#ov{position:fixed;inset:0;background:rgba(26,58,92,.92);z-index:9999;
display:flex;flex-direction:column;align-items:center;justify-content:center;color:#fff}
.ring{width:60px;height:60px;border:5px solid rgba(255,255,255,.3);
border-top-color:#fff;border-radius:50%;animation:spin 1s linear infinite;margin-bottom:20px}
@keyframes spin{to{transform:rotate(360deg)}}
.fm{background:#1a3a5c;color:#e8f4fd;border-radius:8px;padding:10px 16px;font-family:monospace}
.fm b{color:#e8a020}
</style>
</head>
<body>
<nav class='navbar navbar-dark px-3 py-2'>
<span class='navbar-brand'>PEA Screener</span>
<div class='d-flex gap-3 align-items-center'>
<span class='text-white-50 small' id='lu'></span>
<button class='btn btn-sm btn-warning fw-bold' onclick='doRefresh()'>Actualiser</button>
</div>
</nav>

<div id='ov'>
<div class='ring'></div>
<div class='fw-bold fs-5 mb-2'>Chargement des donnees...</div>
<div class='text-white-50 small mb-3' id='om'>Connexion Yahoo Finance</div>
<div style='width:320px'>
<div class='progress' style='height:8px;background:rgba(255,255,255,.2)'>
<div id='ob' class='progress-bar' style='width:0%;background:#e8a020'></div>
</div>
<div class='text-center small mt-1' id='oc'>0 / 0</div>
</div>
</div>

<div class='container-fluid py-3 px-4' id='mn' style='display:none'>
<div class='row g-3 mb-3' id='kp'></div>
<div class='card mb-3'><div class='card-body py-2'>
<div class='fm'>S = <b>(ROIC / P/E_fwd)</b> x <b>(1 + Upside% x sqrt(N) / 100)</b>
&nbsp;|&nbsp; ROIC: retour sur capitaux &nbsp;|&nbsp; P/E_fwd: estime 12 mois
&nbsp;|&nbsp; Upside: cible consensus &nbsp;|&nbsp; N: nb analystes</div>
</div></div>

<div class='card mb-3'><div class='card-body py-2'>
<div class='row g-2 align-items-end'>
<div class='col-auto'><label class='form-label mb-1 small fw-semibold'>Categorie</label>
<select class='form-select form-select-sm' id='fC' onchange='filt()'>
<option value=''>Toutes</option>
<option value='CAC 40'>CAC 40</option>
<option value='EuroStoxx'>EuroStoxx</option>
<option value='ETF PEA'>ETF PEA</option>
</select></div>
<div class='col-auto'><label class='form-label mb-1 small fw-semibold'>Analystes min.</label>
<input type='number' class='form-control form-control-sm' id='fN' value='5' style='width:80px' onchange='filt()'></div>
<div class='col-auto'><label class='form-label mb-1 small fw-semibold'>Upside min.%</label>
<input type='number' class='form-control form-control-sm' id='fU' value='5' style='width:80px' onchange='filt()'></div>
<div class='col-auto'><label class='form-label mb-1 small fw-semibold'>Score min.</label>
<input type='number' class='form-control form-control-sm' id='fS' value='0.8' step='0.1' style='width:80px' onchange='filt()'></div>
<div class='col-auto'><button class='btn btn-sm btn-outline-secondary' onclick='rst()'>Reset</button></div>
</div></div></div>

<div class='row g-3 mb-3'>
<div class='col-lg-6'><div class='card h-100'>
<div class='card-header'>Top 15 Score Composite</div>
<div class='card-body'><canvas id='cB' height='220'></canvas></div>
</div></div>
<div class='col-lg-6'><div class='card h-100'>
<div class='card-header'>ROIC vs Upside</div>
<div class='card-body'><canvas id='cS' height='220'></canvas></div>
</div></div>
</div>

<div class='card'>
<div class='card-header d-flex justify-content-between align-items-center'>
<span>Classement complet</span>
<span class='badge bg-light text-dark' id='rc'></span>
</div>
<div class='card-body p-0'><div class='table-responsive'>
<table id='tb' class='table table-sm table-hover mb-0 w-100'>
<thead><tr>
<th>#</th><th>Ticker</th><th>Nom</th><th>Cat.</th><th>Secteur</th>
<th>Prix</th><th>Cible</th><th>Upside%</th><th>ROIC%</th><th>P/E</th>
<th>Analystes</th><th>Score</th><th>Cap ME</th>
</tr></thead>
<tbody id='tbd'></tbody>
</table>
</div></div></div>
</div>

<script src='https://code.jquery.com/jquery-3.7.1.min.js'></script>
<script src='https://cdn.datatables.net/1.13.8/js/jquery.dataTables.min.js'></script>
<script src='https://cdn.datatables.net/1.13.8/js/dataTables.bootstrap5.min.js'></script>
<script>
var all=[],dt=null,cBar=null,cBub=null;

function fv(v,d,s){
  if(v==null||isNaN(v))return'<span class=text-muted>--</span>';
  return(+v).toFixed(d)+(s||'');
}
function fs(v){
  if(v==null)return'<span class=text-muted>--</span>';
  var c=v>=1.5?'sh':v>=0.5?'sm':'sl';
  return'<span class='+c+'>'+v.toFixed(3)+'</span>';
}
function fu(v){
  if(v==null)return'<span class=text-muted>--</span>';
  var c=v>=0?'up':'dn';
  return'<span class='+c+'>'+(v>=0?'+':'')+v.toFixed(1)+'%</span>';
}
function fc(c){
  var m={'CAC 40':'bca','EuroStoxx':'bce','ETF PEA':'bcf'};
  return'<span class="bc '+(m[c]||'')+'">'+c+'</span>';
}

function buildTable(data){
  var sc=data.filter(function(d){return d.score!=null;}).sort(function(a,b){return b.score-a.score;});
  var ns=data.filter(function(d){return d.score==null;});
  var rows=sc.concat(ns);
  document.getElementById('rc').textContent=rows.length+' instruments';
  if(dt){dt.destroy();dt=null;}
  var tb=document.getElementById('tbd');tb.innerHTML='';
  rows.forEach(function(d,i){
    var rk=d.score!=null?i+1:'--';
    var rc=rk<=5?'rk':'';
    var cap=d.mkt_cap_m?Number(d.mkt_cap_m).toLocaleString('fr-FR'):'--';
    tb.innerHTML+='<tr class="'+rc+'">'
      +'<td class="text-center fw-bold">'+rk+'</td>'
      +'<td><code style="font-size:12px">'+d.ticker+'</code></td>'
      +'<td>'+d.nom+'</td><td>'+fc(d.categorie)+'</td>'
      +'<td class=text-muted>'+d.secteur+'</td>'
      +'<td>'+fv(d.prix,2)+'</td><td>'+fv(d.cible,2)+'</td>'
      +'<td>'+fu(d.upside)+'</td>'
      +'<td>'+fv(d.roic,1)+'</td>'
      +'<td>'+fv(d.pe_fwd,1,'x')+'</td>'
      +'<td class=text-center>'+(d.analystes||'--')+'</td>'
      +'<td>'+fs(d.score)+'</td>'
      +'<td class="text-end text-muted">'+cap+'</td></tr>';
  });
  dt=$('#tb').DataTable({pageLength:25,order:[[11,'desc']],
    language:{search:'Rechercher:',lengthMenu:'_MENU_/page',
    info:'_START_-_END_ sur _TOTAL_',
    paginate:{next:'next',previous:'prev'}}});
}

function buildCharts(data){
  var top=data.filter(function(d){return d.score!=null;})
    .sort(function(a,b){return b.score-a.score;}).slice(0,15);
  if(cBar)cBar.destroy();
  cBar=new Chart(document.getElementById('cB'),{type:'bar',
    data:{labels:top.map(function(d){return d.ticker;}),
    datasets:[{label:'Score',data:top.map(function(d){return d.score;}),
    borderRadius:4,
    backgroundColor:top.map(function(_,i){return i<3?'rgba(232,160,32,.85)':'rgba(26,58,92,.7)';})}]},
    options:{responsive:true,maintainAspectRatio:false,
    plugins:{legend:{display:false}},
    scales:{y:{beginAtZero:true,grid:{color:'#eee'}},x:{grid:{display:false}}}}});
  if(cBub)cBub.destroy();
  var sc=data.filter(function(d){return d.roic!=null&&d.upside!=null&&d.score!=null;});
  cBub=new Chart(document.getElementById('cS'),{type:'bubble',
    data:{datasets:[{label:'',
    data:sc.map(function(d){return{x:d.roic,y:d.upside,
      r:Math.min(Math.max(Math.sqrt(d.analystes||1)*2.5,4),18),t:d.ticker,s:d.score};}),
    backgroundColor:'rgba(26,58,92,.55)',borderColor:'rgba(26,58,92,.8)'}]},
    options:{responsive:true,maintainAspectRatio:false,
    plugins:{legend:{display:false},
    tooltip:{callbacks:{label:function(c){
      return c.raw.t+' ROIC:'+c.raw.x.toFixed(1)+'% Up:'+c.raw.y.toFixed(1)+'% Score:'+c.raw.s.toFixed(3);
    }}}},
    scales:{x:{title:{display:true,text:'ROIC (%)'},grid:{color:'#eee'}},
    y:{title:{display:true,text:'Upside (%)'},grid:{color:'#eee'}}}}});
}

function buildKPIs(data){
  var sc=data.filter(function(d){return d.score!=null;});
  var top=sc.slice().sort(function(a,b){return b.score-a.score;})[0];
  var avgSc=sc.length?sc.reduce(function(s,d){return s+d.score;},0)/sc.length:0;
  var ups=data.filter(function(d){return d.upside!=null;});
  var avgUp=ups.length?ups.reduce(function(s,d){return s+d.upside;},0)/ups.length:0;
  var ks=[
    {l:'Analyses',v:data.length,i:'&#128269;'},
    {l:'Avec score',v:sc.length,i:'&#9989;'},
    {l:'Score moyen',v:avgSc.toFixed(3),i:'&#128202;'},
    {l:'Upside moyen',v:avgUp.toFixed(1)+'%',i:'&#128200;'},
    {l:'Top ticker',v:top?top.ticker:'--',i:'&#127942;'},
    {l:'Score #1',v:top?top.score.toFixed(3):'--',i:'&#11088;'},
  ];
  document.getElementById('kp').innerHTML=ks.map(function(k){
    return'<div class="col-6 col-md-4 col-lg-2"><div class="card text-center py-2">'
      +'<div style="font-size:1.4rem">'+k.i+'</div>'
      +'<div class="fw-bold fs-5">'+k.v+'</div>'
      +'<div class="text-muted small">'+k.l+'</div></div></div>';
  }).join('');
}

function filt(){
  var cat=document.getElementById('fC').value;
  var mn=+document.getElementById('fN').value||0;
  var mu=+document.getElementById('fU').value||-999;
  var ms=+document.getElementById('fS').value||0;
  var d=all.filter(function(x){
    if(cat&&x.categorie!==cat)return false;
    if(x.analystes<mn)return false;
    if(x.upside!=null&&x.upside<mu)return false;
    if(x.score!=null&&x.score<ms)return false;
    return true;
  });
  buildTable(d);buildCharts(d);
}

function rst(){
  document.getElementById('fC').value='';
  document.getElementById('fN').value='5';
  document.getElementById('fU').value='5';
  document.getElementById('fS').value='0.8';
  filt();
}

var _poll=null;
function poll(){
  fetch('/api/progress').then(function(r){return r.json();}).then(function(p){
    var pct=Math.round(p.done/Math.max(p.total,1)*100);
    document.getElementById('ob').style.width=pct+'%';
    document.getElementById('oc').textContent=p.done+' / '+p.total;
    document.getElementById('om').textContent=p.loading?'Recuperation Yahoo Finance...':'Finalisation...';
    if(!p.loading){clearInterval(_poll);_poll=null;setTimeout(load,400);}
  }).catch(function(){});
}

function load(){
  fetch('/api/data').then(function(r){return r.json();}).then(function(d){
    if(d.loading){if(!_poll)_poll=setInterval(poll,1000);return;}
    all=d.data||[];
    document.getElementById('lu').textContent='Mis a jour: '+(d.last_update||'');
    document.getElementById('ov').style.display='none';
    document.getElementById('mn').style.display='block';
    try{buildKPIs(all);}catch(e){console.error('buildKPIs:',e);}
    try{filt();}catch(e){console.error('filt:',e);}
  }).catch(function(e){
    console.error('load error:',e);
    document.getElementById('ov').style.display='none';
    document.getElementById('mn').style.display='block';
  });
}

function doRefresh(){
  document.getElementById('ov').style.display='flex';
  document.getElementById('mn').style.display='none';
  document.getElementById('ob').style.width='0%';
  if(_poll)clearInterval(_poll);_poll=null;
  fetch('/api/refresh',{method:'POST'}).then(function(){_poll=setInterval(poll,1000);});
}

window.addEventListener('DOMContentLoaded',load);
</script>
</body>
</html>"""

@app.route("/")
def index(): return render_template_string(HTML)

@app.route("/api/data")
def api_data():
    if _cache["loading"]: return jsonify({"loading":True})
    if _cache["data"] is None: _start_fetch(); return jsonify({"loading":True})
    return jsonify({"data":_cache["data"],"last_update":_cache["last_update"],"loading":False})

@app.route("/api/progress")
def api_progress():
    return jsonify({"done":_cache["progress"][0],"total":_cache["progress"][1],"loading":_cache["loading"]})

@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    if not _cache["loading"]: _cache["data"]=None; _start_fetch()
    return jsonify({"ok":True})

def _cb(done, total): _cache["progress"]=[done,total]

def _thread():
    _cache["loading"]=True; _cache["progress"]=[0,1]
    try:
        rows=fetch_all(cb=_cb)
        _cache["data"]=rows or []
        _cache["last_update"]=datetime.datetime.now().strftime("%d/%m/%Y %H:%M")
        t=_cache["progress"][1]
        if t>0: _cache["progress"]=[t,t]
    except Exception as e:
        print("Erreur:",e,file=sys.stderr)
        _cache["data"]=[]
        _cache["last_update"]=datetime.datetime.now().strftime("%d/%m/%Y %H:%M")
    finally:
        _cache["loading"]=False

def _start_fetch(): threading.Thread(target=_thread,daemon=True).start()

if __name__ == "__main__":
    import traceback
    PORT = 5050
    try:
        print("="*50)
        print("  PEA Screener -> http://localhost:"+str(PORT))
        print("  Ctrl+C pour arreter")
        print("="*50)
        _start_fetch()
        def _br():
            time.sleep(1.5)
            webbrowser.open("http://localhost:"+str(PORT))
        threading.Thread(target=_br,daemon=True).start()
        app.run(host="0.0.0.0",port=PORT,debug=False,use_reloader=False)
    except Exception:
        print(traceback.format_exc())
        with open("pea_error.log","w") as f: f.write(traceback.format_exc())
        input("Erreur - appuie sur Entree pour fermer...")
