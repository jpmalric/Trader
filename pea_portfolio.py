#!/usr/bin/env python3
"""PEA Portfolio Advisor — http://localhost:5051"""
import sys, time, json, os, uuid, warnings, datetime, threading, webbrowser
import numpy as np, pandas as pd, yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, render_template_string, request
warnings.filterwarnings("ignore")

PORT = 5051
PORTFOLIO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio.json")

# ── Persistence ───────────────────────────────────────────────────────────────

def load_portfolio():
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_portfolio(positions):
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(positions, f, indent=2, ensure_ascii=False)

# ── Technical Analysis ────────────────────────────────────────────────────────

def compute_rsi(prices, period=14):
    delta = prices.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    val = (100 - (100 / (1 + rs))).iloc[-1]
    return round(float(val), 1) if pd.notna(val) else None

def compute_signals(price, buy_price, target, analysts, rsi, ma50, ma200, stop_loss, take_profit):
    pnl_pct = (price - buy_price) / buy_price * 100
    signals = []

    # Stop-loss
    if pnl_pct <= stop_loss:
        signals.append({"niveau": "urgent", "icone": "🔴",
                        "titre": "Stop-loss déclenché",
                        "detail": f"Position à {pnl_pct:.1f}% (seuil : {stop_loss:.0f}%). Vendre pour limiter les pertes."})
    elif stop_loss < 0 and pnl_pct <= stop_loss * 0.5:
        signals.append({"niveau": "warning", "icone": "🟡",
                        "titre": "Approche du stop-loss",
                        "detail": f"Position à {pnl_pct:.1f}%, stop-loss fixé à {stop_loss:.0f}%. Surveiller de près."})

    # Take-profit
    if pnl_pct >= take_profit:
        signals.append({"niveau": "profit", "icone": "💰",
                        "titre": "Objectif de gain atteint",
                        "detail": f"Position à +{pnl_pct:.1f}% (objectif : +{take_profit:.0f}%). Envisager de prendre les bénéfices."})
    elif pnl_pct >= take_profit * 0.75:
        signals.append({"niveau": "warning", "icone": "🟡",
                        "titre": "Proche de l'objectif de gain",
                        "detail": f"+{pnl_pct:.1f}% — objectif +{take_profit:.0f}%. Suivre l'évolution."})

    # Analyst target
    if target and price:
        upside = (target - price) / price * 100
        if upside <= -5:
            signals.append({"niveau": "urgent", "icone": "🔴",
                            "titre": "Au-dessus de la cible analystes",
                            "detail": f"Le prix ({price:.2f}€) dépasse la cible consensus ({target:.2f}€). Potentiel limité, envisager la vente."})
        elif upside <= 5:
            signals.append({"niveau": "warning", "icone": "🟡",
                            "titre": "Cible analystes quasi atteinte",
                            "detail": f"Upside résiduel : {upside:.1f}%. Cible consensus : {target:.2f}€. Surveiller."})
        elif upside >= 20 and (analysts or 0) >= 3:
            signals.append({"niveau": "positif", "icone": "🔵",
                            "titre": "Fort potentiel selon les analystes",
                            "detail": f"Upside consensus : +{upside:.1f}% vers {target:.2f}€ ({analysts} analystes). Conserver."})

    # RSI
    if rsi is not None:
        if rsi > 80:
            signals.append({"niveau": "urgent", "icone": "🔴",
                            "titre": "RSI en forte surachat",
                            "detail": f"RSI à {rsi:.0f} — signal de vente technique fort. Correction probable à court terme."})
        elif rsi > 70:
            signals.append({"niveau": "warning", "icone": "🟡",
                            "titre": "RSI en surachat",
                            "detail": f"RSI à {rsi:.0f} — zone de prudence. Surveiller un potentiel retournement."})
        elif rsi < 30:
            signals.append({"niveau": "positif", "icone": "🔵",
                            "titre": "RSI en survente",
                            "detail": f"RSI à {rsi:.0f} — potentiel rebond technique. Conserver si fondamentaux solides."})

    # Moving averages
    if ma50 and price:
        if price < ma50 * 0.95:
            signals.append({"niveau": "warning", "icone": "🟡",
                            "titre": "Prix sous la MA50",
                            "detail": f"Prix ({price:.2f}€) sous la moyenne 50 jours ({ma50:.2f}€). Tendance court terme baissière."})
        elif price > ma50 * 1.05:
            signals.append({"niveau": "positif", "icone": "🔵",
                            "titre": "Prix au-dessus de la MA50",
                            "detail": f"Prix ({price:.2f}€) au-dessus de la MA50 ({ma50:.2f}€). Tendance court terme haussière."})

    if ma200 and price:
        if price < ma200:
            signals.append({"niveau": "warning", "icone": "🟡",
                            "titre": "Prix sous la MA200",
                            "detail": f"Prix ({price:.2f}€) sous la moyenne 200 jours ({ma200:.2f}€). Tendance long terme baissière."})

    # Default if nothing triggered
    if not signals:
        signals.append({"niveau": "ok", "icone": "🟢",
                        "titre": "Position saine",
                        "detail": "Aucun signal d'alerte. Conserver la position."})

    # Overall recommendation
    niveaux = [s["niveau"] for s in signals]
    if "urgent" in niveaux:
        return signals, "VENDRE", "danger", "🔴"
    if "profit" in niveaux and niveaux.count("warning") >= 1:
        return signals, "VENDRE PARTIEL", "warning", "🟠"
    if "profit" in niveaux:
        return signals, "PRENDRE PROFIT", "success", "💰"
    if niveaux.count("warning") >= 2:
        return signals, "VENDRE PARTIEL", "warning", "🟠"
    if "warning" in niveaux:
        return signals, "SURVEILLER", "warning", "🟡"
    return signals, "CONSERVER", "success", "🟢"

def compute_sell_score(pnl_pct, rsi, upside, price, ma50, ma200, stop_loss, take_profit):
    score, bd = 0, []

    # P&L vs objectif (max 30 pts)
    if pnl_pct >= take_profit:
        p = 30
    elif pnl_pct > 0:
        p = round(pnl_pct / take_profit * 30, 1)
    elif pnl_pct <= stop_loss:
        p = 20
    else:
        p = 0
    score += p; bd.append({"l": "P&L vs objectif", "p": p, "m": 30})

    # RSI surachat (max 25 pts)
    p = round(max(0, min((rsi - 55) / 25 * 25, 25)), 1) if rsi is not None and rsi >= 55 else 0
    score += p; bd.append({"l": "RSI surachat", "p": p, "m": 25})

    # Cible analystes (max 20 pts)
    p = round(max(0, min((20 - upside) / 20 * 20, 20)), 1) if upside is not None and upside <= 20 else 0
    score += p; bd.append({"l": "Cible analystes", "p": p, "m": 20})

    # Tendance technique (max 15 pts)
    p = 0
    if ma50  and price and price < ma50  * 0.97: p += 8
    if ma200 and price and price < ma200:         p += 7
    p = min(p, 15)
    score += p; bd.append({"l": "Tendance technique", "p": p, "m": 15})

    # Approche stop-loss (max 10 pts)
    if pnl_pct <= stop_loss:
        p = 10
    elif stop_loss < 0 and pnl_pct < 0:
        p = round(min(pnl_pct / stop_loss * 10, 10), 1)
    else:
        p = 0
    score += p; bd.append({"l": "Approche stop-loss", "p": p, "m": 10})

    return round(min(score, 100), 1), bd

# ── Data Fetching ─────────────────────────────────────────────────────────────

def _val(df, keys):
    if df is None or df.empty:
        return None
    for k in keys:
        if k in df.index:
            v = df.loc[k].iloc[0]
            if pd.notna(v) and v != 0:
                return float(v)
    return None

def fetch_position(pos):
    ticker = pos["ticker"]
    time.sleep(0.1)
    try:
        t    = yf.Ticker(ticker)
        info = t.info or {}
        try:
            hist = t.history(period="1y", timeout=15)
        except Exception:
            hist = pd.DataFrame()

        price = info.get("currentPrice") or info.get("regularMarketPrice")
        if not price and not hist.empty:
            price = float(hist["Close"].iloc[-1])
        if not price:
            return {**pos, "error": "Prix non disponible", "signals": [],
                    "reco": "ERREUR", "reco_col": "secondary", "reco_icon": "❓"}

        target   = info.get("targetMeanPrice")
        analysts = int(info.get("numberOfAnalystOpinions") or 0)
        pe_fwd   = info.get("forwardPE")
        sector   = info.get("sector") or info.get("category") or "--"

        # ROIC
        roic = None
        try:
            fin, bs = t.financials, t.balance_sheet
            ebit = _val(fin, ["EBIT", "Operating Income", "OperatingIncome"])
            tax  = max(0.0, min(float(info.get("effectiveTaxRate") or 0.25), 0.60))
            eq   = _val(bs, ["Stockholders Equity", "Total Stockholder Equity", "Common Stock Equity"])
            ltd  = _val(bs, ["Long Term Debt", "LongTermDebt"]) or 0
            std  = _val(bs, ["Current Debt", "Short Term Debt"]) or 0
            cash = _val(bs, ["Cash And Cash Equivalents", "Cash"]) or 0
            if ebit and eq:
                ic = eq + ltd + std - cash
                if ic > 0:
                    roic = round(ebit * (1 - tax) / ic * 100, 2)
        except Exception:
            roe = info.get("returnOnEquity")
            if roe and -1 < roe < 5:
                roic = round(roe * 100, 2)

        # Technicals
        rsi   = None
        ma50  = None
        ma200 = None
        if len(hist) >= 15:
            try:
                rsi = compute_rsi(hist["Close"])
            except Exception:
                pass
        if len(hist) >= 50:
            ma50  = round(float(hist["Close"].rolling(50).mean().iloc[-1]),  2)
        if len(hist) >= 200:
            ma200 = round(float(hist["Close"].rolling(200).mean().iloc[-1]), 2)

        qty         = pos["quantite"]
        buy_price   = pos["prix_achat"]
        stop_loss   = pos.get("stop_loss",   -15)
        take_profit = pos.get("take_profit",  30)

        pnl_eur = round((price - buy_price) * qty, 2)
        pnl_pct = round((price - buy_price) / buy_price * 100, 2)
        valeur  = round(price * qty, 2)
        investi = round(buy_price * qty, 2)
        upside  = round((target - price) / price * 100, 2) if target and price > 0 else None

        signals, reco, reco_col, reco_icon = compute_signals(
            price, buy_price, target, analysts, rsi, ma50, ma200, stop_loss, take_profit)
        sell_score, sell_bd = compute_sell_score(
            pnl_pct, rsi, upside, price, ma50, ma200, stop_loss, take_profit)

        return {
            "id": pos["id"], "ticker": ticker,
            "nom": pos.get("nom") or info.get("longName") or ticker,
            "quantite": qty, "prix_achat": buy_price,
            "date_achat": pos.get("date_achat", ""),
            "stop_loss": stop_loss, "take_profit": take_profit,
            "prix": round(price, 2),
            "cible": round(target, 2) if target else None,
            "upside": upside, "analystes": analysts,
            "pe_fwd": round(pe_fwd, 1) if pe_fwd else None,
            "roic": roic, "rsi": rsi, "ma50": ma50, "ma200": ma200,
            "pnl_eur": pnl_eur, "pnl_pct": pnl_pct,
            "valeur": valeur, "investi": investi, "secteur": sector,
            "signals": signals, "reco": reco,
            "reco_col": reco_col, "reco_icon": reco_icon,
            "sell_score": sell_score, "sell_bd": sell_bd, "error": None,
        }
    except Exception as e:
        print(f"  {ticker}: {e}", file=sys.stderr)
        return {**pos, "error": str(e), "signals": [],
                "reco": "ERREUR", "reco_col": "secondary", "reco_icon": "❓"}

def fetch_all_positions(positions, cb=None):
    results, done = [], 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(fetch_position, p): p for p in positions}
        for fut in as_completed(futs):
            done += 1
            row = fut.result()
            if row:
                results.append(row)
            if cb:
                cb(done, len(positions))
    order = {p["id"]: i for i, p in enumerate(positions)}
    results.sort(key=lambda r: order.get(r.get("id"), 999))
    return results

# ── Flask App ─────────────────────────────────────────────────────────────────

app    = Flask(__name__)
_state = {"data": None, "last_update": None, "loading": False, "progress": [0, 1]}

HTML = """<!DOCTYPE html>
<html lang='fr'>
<head>
<meta charset='UTF-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>PEA Portfolio Advisor</title>
<link href='https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css' rel='stylesheet'>
<link href='https://cdn.datatables.net/1.13.8/css/dataTables.bootstrap5.min.css' rel='stylesheet'>
<script src='https://cdn.jsdelivr.net/npm/chart.js@4.4.2/dist/chart.umd.min.js'></script>
<style>
:root{--p:#1a3a5c;--a:#e8a020}
body{background:#f0f4f8;font-family:Arial,sans-serif;font-size:13px}
.navbar{background:var(--p)!important}
.navbar-brand{font-size:1.2rem;font-weight:700}
.card{border:none;border-radius:10px;box-shadow:0 2px 8px rgba(0,0,0,.07)}
.card-header{background:var(--p);color:#fff;font-weight:600;border-radius:10px 10px 0 0!important}
th{white-space:nowrap;font-size:12px!important}
.pnl-pos{color:#198754;font-weight:700}
.pnl-neg{color:#dc3545;font-weight:700}
.pnl-neu{color:#6c757d}
#ov{position:fixed;inset:0;background:rgba(26,58,92,.92);z-index:9999;
  display:flex;flex-direction:column;align-items:center;justify-content:center;color:#fff}
.ring{width:60px;height:60px;border:5px solid rgba(255,255,255,.3);
  border-top-color:#fff;border-radius:50%;animation:spin 1s linear infinite;margin-bottom:20px}
@keyframes spin{to{transform:rotate(360deg)}}
.sig-urgent{border-left:4px solid #dc3545;background:#fff5f5;padding:8px 12px;border-radius:4px;margin-bottom:6px}
.sig-warning{border-left:4px solid #ffc107;background:#fffbf0;padding:8px 12px;border-radius:4px;margin-bottom:6px}
.sig-profit{border-left:4px solid #198754;background:#f0fff4;padding:8px 12px;border-radius:4px;margin-bottom:6px}
.sig-positif{border-left:4px solid #0d6efd;background:#f0f7ff;padding:8px 12px;border-radius:4px;margin-bottom:6px}
.sig-ok{border-left:4px solid #198754;background:#f0fff4;padding:8px 12px;border-radius:4px;margin-bottom:6px}
.empty-state{text-align:center;padding:80px 20px;color:#6c757d}
.empty-state .big-icon{font-size:4rem;margin-bottom:16px}
.btn-add{background:var(--a)!important;border:none!important;color:#fff!important;font-weight:700}
.btn-add:hover{background:#c8881a!important}
.badge-reco{font-size:11px;padding:4px 10px;border-radius:20px;white-space:nowrap}
.sc-bar{width:72px}
.sc-bar-inner{height:7px;border-radius:4px}
.sc-val{font-size:11px;font-weight:700;text-align:center;letter-spacing:-.3px}
.sc-bd-row{display:flex;align-items:center;gap:6px;margin-bottom:5px;font-size:12px}
.sc-bd-bar{flex:1;height:6px;background:#e9ecef;border-radius:3px;overflow:hidden}
.sc-bd-fill{height:100%;border-radius:3px}
</style>
</head>
<body>
<nav class='navbar navbar-dark px-3 py-2'>
  <span class='navbar-brand'>📊 PEA Portfolio Advisor</span>
  <div class='d-flex gap-2 align-items-center'>
    <span class='text-white-50 small' id='lu'></span>
    <button class='btn btn-sm btn-outline-light' onclick='doRefresh()'>↻ Actualiser</button>
    <button class='btn btn-sm btn-add px-3' onclick='openAddModal()'>+ Ajouter</button>
  </div>
</nav>

<div id='ov'>
  <div class='ring'></div>
  <div class='fw-bold fs-5 mb-2'>Analyse du portefeuille...</div>
  <div class='text-white-50 small mb-3' id='om'>Connexion Yahoo Finance</div>
  <div style='width:320px'>
    <div class='progress' style='height:8px;background:rgba(255,255,255,.2)'>
      <div id='ob' class='progress-bar' style='width:0%;background:#e8a020'></div>
    </div>
    <div class='text-center small mt-1' id='oc'>0 / 0</div>
  </div>
</div>

<div class='container-fluid py-3 px-4' id='mn' style='display:none'>
  <div id='es' style='display:none'>
    <div class='empty-state'>
      <div class='big-icon'>📋</div>
      <h4>Aucune position dans le portefeuille</h4>
      <p>Ajoutez vos actions pour recevoir des conseils personnalisés sur quand vendre.</p>
      <button class='btn btn-add px-4 py-2' onclick='openAddModal()'>+ Ajouter ma première position</button>
    </div>
  </div>

  <div id='pc'>
    <div class='row g-3 mb-3' id='kp'></div>
    <div id='alr' class='mb-3'></div>

    <div class='row g-3 mb-3'>
      <div class='col-lg-5'>
        <div class='card h-100'>
          <div class='card-header'>Répartition du portefeuille</div>
          <div class='card-body'><canvas id='cD' height='210'></canvas></div>
        </div>
      </div>
      <div class='col-lg-7'>
        <div class='card h-100'>
          <div class='card-header'>P&L par position (%)</div>
          <div class='card-body'><canvas id='cB' height='210'></canvas></div>
        </div>
      </div>
    </div>

    <div class='card'>
      <div class='card-header d-flex justify-content-between align-items-center'>
        <span>Positions</span>
        <span class='badge bg-light text-dark' id='rc'></span>
      </div>
      <div class='card-body p-0'>
        <div class='table-responsive'>
          <table id='tb' class='table table-sm table-hover mb-0 w-100'>
            <thead><tr>
              <th>Ticker</th><th>Nom</th><th>Qté</th><th>Px Achat</th>
              <th>Px Actuel</th><th>Valeur €</th><th>P&L €</th><th>P&L %</th>
              <th>Upside</th><th>RSI</th><th>MA50</th><th>MA200</th><th>Timing Vente</th><th>Recommandation</th><th>Actions</th>
            </tr></thead>
            <tbody id='tbd'></tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Modal Ajouter/Modifier -->
<div class='modal fade' id='mPos' tabindex='-1'>
  <div class='modal-dialog modal-lg'>
    <div class='modal-content'>
      <div class='modal-header' style='background:var(--p);color:#fff'>
        <h5 class='modal-title' id='mPosTitle'>Ajouter une position</h5>
        <button type='button' class='btn-close btn-close-white' data-bs-dismiss='modal'></button>
      </div>
      <div class='modal-body'>
        <input type='hidden' id='fId'>
        <div class='row g-3'>
          <div class='col-md-4'>
            <label class='form-label fw-semibold'>Ticker * <small class='text-muted'>(Yahoo Finance)</small></label>
            <input class='form-control' id='fTicker' placeholder='ex: MC.PA, ASML.AS'
              oninput="this.value=this.value.toUpperCase()">
          </div>
          <div class='col-md-4'>
            <label class='form-label fw-semibold'>Nom (optionnel)</label>
            <input class='form-control' id='fNom' placeholder='ex: LVMH'>
          </div>
          <div class='col-md-4'>
            <label class='form-label fw-semibold'>Date d'achat</label>
            <input type='date' class='form-control' id='fDate'>
          </div>
          <div class='col-md-4'>
            <label class='form-label fw-semibold'>Quantité *</label>
            <input type='number' class='form-control' id='fQty' min='0.001' step='1' placeholder='ex: 10'>
          </div>
          <div class='col-md-4'>
            <label class='form-label fw-semibold'>Prix d'achat (€) *</label>
            <input type='number' class='form-control' id='fPrix' min='0.001' step='0.01' placeholder='ex: 650.00'>
          </div>
          <div class='col-md-4'>
            <label class='form-label fw-semibold'>Investi (€)</label>
            <input class='form-control bg-light' id='fInvesti' readonly>
          </div>
          <div class='col-12'><hr class='my-1'><p class='text-muted small mb-2'>Paramètres de gestion du risque :</p></div>
          <div class='col-md-6'>
            <label class='form-label fw-semibold'>Stop-loss (%)</label>
            <div class='input-group'>
              <input type='number' class='form-control' id='fSL' value='-15' step='1'>
              <span class='input-group-text'>%</span>
            </div>
            <div class='form-text'>Signal VENDRE si la perte dépasse ce seuil</div>
          </div>
          <div class='col-md-6'>
            <label class='form-label fw-semibold'>Take-profit (%)</label>
            <div class='input-group'>
              <input type='number' class='form-control' id='fTP' value='30' step='1'>
              <span class='input-group-text'>%</span>
            </div>
            <div class='form-text'>Signal PRENDRE PROFIT si le gain dépasse ce seuil</div>
          </div>
        </div>
      </div>
      <div class='modal-footer'>
        <button class='btn btn-secondary' data-bs-dismiss='modal'>Annuler</button>
        <button class='btn btn-add px-4' onclick='savePosition()'>Enregistrer</button>
      </div>
    </div>
  </div>
</div>

<!-- Modal Détail -->
<div class='modal fade' id='mDet' tabindex='-1'>
  <div class='modal-dialog modal-lg'>
    <div class='modal-content'>
      <div class='modal-header' style='background:var(--p);color:#fff'>
        <h5 class='modal-title' id='mDetTitle'>Analyse détaillée</h5>
        <button type='button' class='btn-close btn-close-white' data-bs-dismiss='modal'></button>
      </div>
      <div class='modal-body' id='mDetBody'></div>
    </div>
  </div>
</div>

<script src='https://code.jquery.com/jquery-3.7.1.min.js'></script>
<script src='https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js'></script>
<script src='https://cdn.datatables.net/1.13.8/js/jquery.dataTables.min.js'></script>
<script src='https://cdn.datatables.net/1.13.8/js/dataTables.bootstrap5.min.js'></script>
<script>
var all=[], dt=null, cDon=null, cBar=null;

function fv(v,d,s){
  if(v==null||isNaN(v))return'<span class=text-muted>--</span>';
  return(+v).toFixed(d)+(s||'');
}
function fpnl(v,d,s){
  if(v==null||isNaN(v))return'<span class=text-muted>--</span>';
  var c=v>0?'pnl-pos':v<0?'pnl-neg':'pnl-neu';
  return'<span class="'+c+'">'+(v>0?'+':'')+v.toFixed(d)+(s||'')+'</span>';
}
function fma(prix,ma){
  if(ma==null||prix==null)return'<span class=text-muted>--</span>';
  var above=prix>=ma;
  var c=above?'#198754':'#dc3545';
  return'<span style="color:'+c+';font-weight:600">'+ma.toFixed(2)+'</span><span style="color:'+c+'">'+(above?' ↑':' ↓')+'</span>';
}
function frsi(v){
  if(v==null)return'<span class=text-muted>--</span>';
  var c=v>70?'text-danger fw-bold':v<30?'text-primary fw-bold':'text-success';
  return'<span class="'+c+'">'+v.toFixed(0)+'</span>';
}
function freco(r,col,icon){
  var bg={danger:'#dc3545',success:'#198754',warning:'#ffc107',secondary:'#6c757d'}[col]||'#6c757d';
  var tc=col==='warning'?'#000':'#fff';
  return'<span class="badge badge-reco" style="background:'+bg+';color:'+tc+'">'+icon+' '+r+'</span>';
}
function scColor(v){
  return v>=70?'#dc3545':v>=50?'#fd7e14':v>=30?'#ffc107':'#198754';
}
function fScore(v){
  if(v==null)return'<span class=text-muted>--</span>';
  var c=scColor(v);
  var bar='<div style="height:7px;background:#e9ecef;border-radius:4px"><div class=sc-bar-inner style="width:'+v+'%;background:'+c+'"></div></div>';
  return'<div class=sc-bar>'+bar+'<div class=sc-val style="color:'+c+'">'+v.toFixed(0)+'/100</div></div>';
}

function buildKPIs(data){
  var totalVal=data.reduce(function(s,d){return s+(d.valeur||0);},0);
  var totalInv=data.reduce(function(s,d){return s+(d.investi||0);},0);
  var totalPnl=totalVal-totalInv;
  var totalPct=totalInv>0?totalPnl/totalInv*100:0;
  var enProfit=data.filter(function(d){return(d.pnl_pct||0)>0;}).length;
  var urgents=data.filter(function(d){return d.reco_col==='danger';}).length;
  var surveiller=data.filter(function(d){return d.reco_col==='warning';}).length;
  var ks=[
    {l:'Valeur totale',v:totalVal.toLocaleString('fr-FR',{minimumFractionDigits:0})+'€',i:'💼',c:''},
    {l:'P&L total',v:(totalPnl>=0?'+':'')+totalPnl.toLocaleString('fr-FR',{minimumFractionDigits:0})+'€',
     i:totalPnl>=0?'📈':'📉',c:totalPnl>=0?'#d4edda':'#f8d7da'},
    {l:'Performance',v:(totalPct>=0?'+':'')+totalPct.toFixed(2)+'%',
     i:totalPct>=0?'✅':'⚠️',c:totalPct>=0?'#d4edda':'#f8d7da'},
    {l:'Positions',v:data.length+' ('+enProfit+' en profit)',i:'📋',c:''},
    {l:'Alertes vente',v:urgents,i:'🔴',c:urgents>0?'#f8d7da':''},
    {l:'À surveiller',v:surveiller,i:'🟡',c:surveiller>0?'#fff3cd':''},
  ];
  document.getElementById('kp').innerHTML=ks.map(function(k){
    return'<div class="col-6 col-md-4 col-lg-2"><div class="card text-center py-2"'+(k.c?' style="background:'+k.c+'"':'')+'>'+
      '<div style="font-size:1.4rem">'+k.i+'</div>'+
      '<div class="fw-bold fs-5">'+k.v+'</div>'+
      '<div class="text-muted small">'+k.l+'</div></div></div>';
  }).join('');
}

function buildAlerts(data){
  var urgents=data.filter(function(d){return d.reco_col==='danger';});
  var html='';
  if(urgents.length>0){
    html='<div class="alert alert-danger mb-0"><strong>🔴 Signaux de vente actifs :</strong> ';
    html+=urgents.map(function(d){
      return'<span class="badge bg-danger me-1" style="cursor:pointer;font-size:12px" onclick="showDetail(&#39;'+d.id+'&#39;)">'+d.ticker+' ('+d.pnl_pct.toFixed(1)+'%)</span>';
    }).join('');
    html+=' — cliquer pour voir le détail.</div>';
  }
  document.getElementById('alr').innerHTML=html;
}

var PIE_COLORS=['#1a3a5c','#e8a020','#198754','#dc3545','#0d6efd','#6f42c1','#fd7e14','#20c997','#6c757d','#d63384'];

function buildCharts(data){
  if(cDon)cDon.destroy();
  cDon=new Chart(document.getElementById('cD'),{type:'doughnut',
    data:{labels:data.map(function(d){return d.ticker;}),
    datasets:[{data:data.map(function(d){return d.valeur||0;}),
    backgroundColor:data.map(function(_,i){return PIE_COLORS[i%PIE_COLORS.length];}),
    borderWidth:2,borderColor:'#fff'}]},
    options:{responsive:true,maintainAspectRatio:false,
    plugins:{legend:{position:'right',labels:{font:{size:11},boxWidth:14}}}}});

  if(cBar)cBar.destroy();
  var sorted=data.slice().sort(function(a,b){return(b.pnl_pct||0)-(a.pnl_pct||0);});
  cBar=new Chart(document.getElementById('cB'),{type:'bar',
    data:{labels:sorted.map(function(d){return d.ticker;}),
    datasets:[{label:'P&L %',data:sorted.map(function(d){return d.pnl_pct||0;}),
    borderRadius:4,
    backgroundColor:sorted.map(function(d){
      return(d.pnl_pct||0)>=0?'rgba(25,135,84,.75)':'rgba(220,53,69,.75)';})}]},
    options:{responsive:true,maintainAspectRatio:false,
    plugins:{legend:{display:false}},
    scales:{y:{grid:{color:'#eee'},ticks:{callback:function(v){return v+'%';}}},
    x:{grid:{display:false}}}}});
}

function buildTable(data){
  document.getElementById('rc').textContent=data.length+' positions';
  if(dt){dt.destroy();dt=null;}
  var tb=document.getElementById('tbd'); tb.innerHTML='';
  data.forEach(function(d){
    var rowcl=d.reco_col==='danger'?'table-danger':'';
    tb.innerHTML+='<tr class="'+rowcl+'">'
      +'<td><code style="font-size:12px">'+d.ticker+'</code></td>'
      +'<td>'+d.nom+'</td>'
      +'<td class="text-center">'+d.quantite+'</td>'
      +'<td class="text-end">'+fv(d.prix_achat,2,'€')+'</td>'
      +'<td class="text-end">'+fv(d.prix,2,'€')+'</td>'
      +'<td class="text-end">'+fv(d.valeur,0,'€')+'</td>'
      +'<td class="text-end">'+fpnl(d.pnl_eur,0,'€')+'</td>'
      +'<td class="text-end">'+fpnl(d.pnl_pct,2,'%')+'</td>'
      +'<td class="text-end">'+(d.upside!=null?fpnl(d.upside,1,'%'):'<span class=text-muted>--</span>')+'</td>'
      +'<td class="text-center">'+frsi(d.rsi)+'</td>'
      +'<td class="text-end">'+fma(d.prix,d.ma50)+'</td>'
      +'<td class="text-end">'+fma(d.prix,d.ma200)+'</td>'
      +'<td class="text-center">'+fScore(d.sell_score)+'</td>'
      +'<td>'+freco(d.reco,d.reco_col,d.reco_icon)+'</td>'
      +'<td style="white-space:nowrap">'
        +'<button class="btn btn-outline-primary btn-sm me-1 py-0 px-2" onclick="showDetail(&#39;'+d.id+'&#39;)">🔍</button>'
        +'<button class="btn btn-outline-secondary btn-sm me-1 py-0 px-2" onclick="openEditModal(&#39;'+d.id+'&#39;)">✏️</button>'
        +'<button class="btn btn-outline-danger btn-sm py-0 px-2" onclick="deletePos(&#39;'+d.id+'&#39;)">🗑</button>'
      +'</td></tr>';
  });
  dt=$('#tb').DataTable({pageLength:25,order:[],
    language:{search:'Rechercher:',lengthMenu:'_MENU_/page',
    info:'_START_-_END_ sur _TOTAL_',paginate:{next:'›',previous:'‹'}}});
}

function buildUI(data){
  var empty=!data||data.length===0;
  document.getElementById('es').style.display=empty?'block':'none';
  document.getElementById('pc').style.display=empty?'none':'block';
  if(!empty){
    buildKPIs(data);buildAlerts(data);buildCharts(data);buildTable(data);
  }
  document.getElementById('ov').style.display='none';
  document.getElementById('mn').style.display='block';
}

function showDetail(id){
  var d=all.find(function(x){return x.id===id;}); if(!d)return;
  document.getElementById('mDetTitle').textContent='📊 '+d.nom+' ('+d.ticker+')';
  var html='<div class="row g-2 mb-3">';
  var items=[
    ['Prix achat',fv(d.prix_achat,2,'€')],
    ['Prix actuel',fv(d.prix,2,'€')],
    ['P&L',fpnl(d.pnl_eur,0,'€')+'<br>'+fpnl(d.pnl_pct,2,'%')],
    ['Valeur totale',fv(d.valeur,0,'€')],
    ['Cible analystes',d.cible?fv(d.cible,2,'€')+'<br><small class=text-muted>'+d.analystes+' analystes</small>':'<span class=text-muted>--</span>'],
    ['RSI',frsi(d.rsi)],
    ['P/E fwd',d.pe_fwd?d.pe_fwd+'x':'<span class=text-muted>--</span>'],
    ['ROIC',d.roic?d.roic.toFixed(1)+'%':'<span class=text-muted>--</span>'],
  ];
  items.forEach(function(it){
    html+='<div class="col-6 col-md-3"><div class="card text-center p-2">'
      +'<div class="text-muted small">'+it[0]+'</div>'
      +'<div class="fw-bold">'+it[1]+'</div></div></div>';
  });
  html+='</div>';
  html+='<div class="text-center mb-3">Recommandation : '+freco(d.reco,d.reco_col,d.reco_icon)+'</div>';
  if(d.sell_score!=null){
    var sc=d.sell_score,cc=scColor(sc);
    var lbl=sc>=70?'VENDRE MAINTENANT':sc>=50?'Envisager la vente':sc>=30?'Surveiller':'Conserver';
    html+='<div class="p-3 mb-3 rounded" style="background:#f8f9fa;border:1px solid #dee2e6">';
    html+='<div class="d-flex align-items-center gap-3 mb-2">';
    html+='<div style="font-size:2rem;font-weight:900;color:'+cc+'">'+sc.toFixed(0)+'</div>';
    html+='<div><div class="fw-bold" style="color:'+cc+'">Score de timing : '+lbl+'</div>';
    html+='<div style="height:10px;width:200px;background:#dee2e6;border-radius:5px;margin-top:4px">';
    html+='<div style="height:10px;width:'+sc+'%;background:'+cc+';border-radius:5px"></div></div></div></div>';
    if(d.sell_bd&&d.sell_bd.length){
      d.sell_bd.forEach(function(b){
        var pct=b.m>0?b.p/b.m*100:0;
        var bc=pct>=70?'#dc3545':pct>=40?'#fd7e14':'#198754';
        html+='<div class=sc-bd-row>'
          +'<div style="width:140px;color:#495057">'+b.l+'</div>'
          +'<div class=sc-bd-bar><div class=sc-bd-fill style="width:'+pct.toFixed(0)+'%;background:'+bc+'"></div></div>'
          +'<div style="width:50px;text-align:right;font-weight:600;color:'+bc+'">'+b.p+' / '+b.m+'</div></div>';
      });
    }
    html+='</div>';
  }
  html+='<h6 class="fw-bold mb-2">Signaux détaillés :</h6>';
  if(d.signals&&d.signals.length){
    d.signals.forEach(function(s){
      html+='<div class="sig-'+s.niveau+'">'
        +'<div class="fw-semibold">'+s.icone+' '+s.titre+'</div>'
        +'<div class="text-muted small mt-1">'+s.detail+'</div></div>';
    });
  }
  html+='<div class="mt-3 p-2 bg-light rounded small">'
    +'<strong>Paramètres :</strong>'
    +' Stop-loss : '+d.stop_loss+'%'
    +' | Take-profit : +'+d.take_profit+'%'
    +(d.date_achat?' | Achat le : '+d.date_achat:'')
    +(d.ma50?' | MA50 : '+d.ma50+'€':'')
    +(d.ma200?' | MA200 : '+d.ma200+'€':'')
    +' | Secteur : '+d.secteur+'</div>';
  document.getElementById('mDetBody').innerHTML=html;
  new bootstrap.Modal(document.getElementById('mDet')).show();
}

document.addEventListener('input',function(e){
  if(['fQty','fPrix'].includes(e.target.id)){
    var q=parseFloat(document.getElementById('fQty').value)||0;
    var p=parseFloat(document.getElementById('fPrix').value)||0;
    document.getElementById('fInvesti').value=q&&p?(q*p).toFixed(2)+' €':'';
  }
});

function openAddModal(){
  document.getElementById('fId').value='';
  document.getElementById('mPosTitle').textContent='Ajouter une position';
  ['fTicker','fNom','fDate','fQty','fPrix','fInvesti'].forEach(function(i){document.getElementById(i).value='';});
  document.getElementById('fSL').value='-15';
  document.getElementById('fTP').value='30';
  new bootstrap.Modal(document.getElementById('mPos')).show();
}

function openEditModal(id){
  var pos=all.find(function(x){return x.id===id;}); if(!pos)return;
  document.getElementById('fId').value=pos.id;
  document.getElementById('mPosTitle').textContent='Modifier '+pos.ticker;
  document.getElementById('fTicker').value=pos.ticker;
  document.getElementById('fNom').value=pos.nom||'';
  document.getElementById('fDate').value=pos.date_achat||'';
  document.getElementById('fQty').value=pos.quantite;
  document.getElementById('fPrix').value=pos.prix_achat;
  document.getElementById('fInvesti').value=(pos.quantite*pos.prix_achat).toFixed(2)+' €';
  document.getElementById('fSL').value=pos.stop_loss;
  document.getElementById('fTP').value=pos.take_profit;
  new bootstrap.Modal(document.getElementById('mPos')).show();
}

function savePosition(){
  var id=document.getElementById('fId').value;
  var tk=document.getElementById('fTicker').value.trim();
  var qty=document.getElementById('fQty').value;
  var px=document.getElementById('fPrix').value;
  if(!tk||!qty||!px){alert('Ticker, quantité et prix sont obligatoires.');return;}
  var body={ticker:tk,nom:document.getElementById('fNom').value,
    quantite:qty,prix_achat:px,date_achat:document.getElementById('fDate').value,
    stop_loss:document.getElementById('fSL').value,
    take_profit:document.getElementById('fTP').value};
  bootstrap.Modal.getInstance(document.getElementById('mPos')).hide();
  document.getElementById('ov').style.display='flex';
  document.getElementById('mn').style.display='none';
  document.getElementById('ob').style.width='0%';
  fetch(id?'/api/positions/'+id:'/api/positions',
    {method:id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
    .then(function(){startPoll();});
}

function deletePos(id){
  var d=all.find(function(x){return x.id===id;});
  if(!d||!confirm('Supprimer la position '+d.ticker+' ?'))return;
  fetch('/api/positions/'+id,{method:'DELETE'}).then(function(){
    document.getElementById('ov').style.display='flex';
    document.getElementById('mn').style.display='none';
    setTimeout(load,500);
  });
}

var _poll=null, _pollCount=0, _POLL_MAX=120;
function poll(){
  _pollCount++;
  if(_pollCount>_POLL_MAX){clearInterval(_poll);_poll=null;load();return;}
  fetch('/api/progress').then(function(r){return r.json();}).then(function(p){
    var pct=Math.round(p.done/Math.max(p.total,1)*100);
    document.getElementById('ob').style.width=pct+'%';
    document.getElementById('oc').textContent=p.done+' / '+p.total;
    document.getElementById('om').textContent=p.loading?'Analyse Yahoo Finance...':'Finalisation...';
    if(!p.loading){clearInterval(_poll);_poll=null;setTimeout(load,400);}
  }).catch(function(){clearInterval(_poll);_poll=null;setTimeout(load,1000);});
}

function startPoll(){
  if(_poll)clearInterval(_poll);
  _pollCount=0;
  _poll=setInterval(poll,1000);
}

function load(){
  fetch('/api/data').then(function(r){return r.json();}).then(function(d){
    if(d.loading){startPoll();return;}
    all=d.data||[];
    if(d.last_update)document.getElementById('lu').textContent='Mis à jour : '+d.last_update;
    buildUI(all);
  }).catch(function(err){
    console.error('Erreur /api/data:',err);
    setTimeout(load,3000);
  });
}

function doRefresh(){
  document.getElementById('ov').style.display='flex';
  document.getElementById('mn').style.display='none';
  document.getElementById('ob').style.width='0%';
  fetch('/api/refresh',{method:'POST'}).then(function(){startPoll();});
}

window.addEventListener('DOMContentLoaded',load);
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/data")
def api_data():
    if _state["loading"]:
        return jsonify({"loading": True})
    if _state["data"] is None:
        _start_fetch()
        return jsonify({"loading": True})
    return jsonify({"data": _state["data"], "last_update": _state["last_update"], "loading": False})

@app.route("/api/progress")
def api_progress():
    return jsonify({"done": _state["progress"][0], "total": _state["progress"][1], "loading": _state["loading"]})

@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    if not _state["loading"]:
        _state["data"] = None
        _start_fetch()
    return jsonify({"ok": True})

@app.route("/api/positions", methods=["GET"])
def api_get_positions():
    return jsonify(load_portfolio())

@app.route("/api/positions", methods=["POST"])
def api_add_position():
    d = request.json
    positions = load_portfolio()
    pos = {
        "id":          str(uuid.uuid4())[:8],
        "ticker":      d["ticker"].strip().upper(),
        "nom":         d.get("nom", "").strip(),
        "quantite":    float(d["quantite"]),
        "prix_achat":  float(d["prix_achat"]),
        "date_achat":  d.get("date_achat", ""),
        "stop_loss":   float(d.get("stop_loss",   -15)),
        "take_profit": float(d.get("take_profit",  30)),
    }
    positions.append(pos)
    save_portfolio(positions)
    _state["data"] = None
    _start_fetch()
    return jsonify({"ok": True, "id": pos["id"]})

@app.route("/api/positions/<pid>", methods=["PUT"])
def api_update_position(pid):
    d = request.json
    positions = load_portfolio()
    for p in positions:
        if p["id"] == pid:
            p.update({
                "ticker":      d.get("ticker",      p["ticker"]).strip().upper(),
                "nom":         d.get("nom",          p.get("nom", "")),
                "quantite":    float(d.get("quantite",    p["quantite"])),
                "prix_achat":  float(d.get("prix_achat",  p["prix_achat"])),
                "date_achat":  d.get("date_achat",   p.get("date_achat", "")),
                "stop_loss":   float(d.get("stop_loss",   p.get("stop_loss",   -15))),
                "take_profit": float(d.get("take_profit", p.get("take_profit",  30))),
            })
            break
    save_portfolio(positions)
    _state["data"] = None
    _start_fetch()
    return jsonify({"ok": True})

@app.route("/api/positions/<pid>", methods=["DELETE"])
def api_delete_position(pid):
    positions = [p for p in load_portfolio() if p["id"] != pid]
    save_portfolio(positions)
    _state["data"] = None
    if not positions:
        _state["data"] = []
        _state["last_update"] = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")
        _state["loading"] = False
    else:
        _start_fetch()
    return jsonify({"ok": True})

def _cb(done, total):
    _state["progress"] = [done, total]

def _thread():
    try:
        positions = load_portfolio()
        if not positions:
            _state["data"] = []
        else:
            _state["data"] = fetch_all_positions(positions, cb=_cb)
        _state["last_update"] = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")
        t = _state["progress"][1]
        if t > 0:
            _state["progress"] = [t, t]
    except Exception as e:
        print("Erreur:", e, file=sys.stderr)
        _state["data"] = []
        _state["last_update"] = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")
    finally:
        _state["loading"] = False

def _start_fetch():
    if not _state["loading"]:
        _state["loading"] = True   # doit être avant le démarrage du thread
        _state["progress"] = [0, 1]
        threading.Thread(target=_thread, daemon=True).start()

if __name__ == "__main__":
    import traceback
    try:
        print("=" * 50)
        print(f"  PEA Portfolio Advisor -> http://localhost:{PORT}")
        print("  Ctrl+C pour arreter")
        print("=" * 50)
        _start_fetch()
        def _br():
            time.sleep(1.5)
            webbrowser.open(f"http://localhost:{PORT}")
        threading.Thread(target=_br, daemon=True).start()
        app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)
    except Exception:
        print(traceback.format_exc())
        with open("portfolio_error.log", "w") as f:
            f.write(traceback.format_exc())
        input("Erreur - appuie sur Entree pour fermer...")
