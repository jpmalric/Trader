#!/usr/bin/env python3
"""
SUPER_TRADE — Assistant de décision PEA / Trade Republic
=========================================================

Se connecte à ton compte Trade Republic, lit ton PEA (et ton CTO) en direct,
le compare à ton allocation cible, et te dit **quoi acheter / vendre** ce mois-ci
en s'appuyant sur les scores QARP de ton screener (pea_screener2.py).

⚠️  HONNÊTETÉ AVANT TOUT :
  · Aucun programme ne « prédit le bon moment » du marché de façon fiable.
  · SUPER_TRADE est un MOTEUR DE RÈGLES, pas une boule de cristal : il applique
    TA stratégie (long terme, risque modéré, DCA) de manière disciplinée.
  · LECTURE SEULE : il ne passe AUCUN ordre. Tu valides et exécutes dans l'appli TR.
  · Ce n'est pas un conseil financier réglementé.

Le « en fonction du moment » =
  · cadence mensuelle (DCA) : combien verser / investir ce mois-ci,
  · priorisation des valeurs les plus attractives MAINTENANT
    (qualité × valorisation × momentum, d'après le screener).

Usage :
    python SUPER_TRADE.py                 # rapport complet
    python SUPER_TRADE.py --months 10     # DCA sur 10 mois (défaut 12)
    python SUPER_TRADE.py --equity 0.55   # part actions cible (défaut 0.60)

Identifiants : env TR_PHONE / TR_PIN, sinon demandés au lancement.
Prérequis : la session du connecteur (tr_mcp_server.py) + pea_cache2.json.
"""

import argparse
import asyncio
import getpass
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# On réutilise les helpers éprouvés du connecteur MCP (connexion, fetch, comptes…)
import tr_mcp_server as M

REPO = Path(__file__).resolve().parent
SCREENER = REPO / "pea_screener2.py"
SCREENER_CACHE = REPO / "pea_cache2.json"
REPORT_FILE = REPO / "SUPER_TRADE_report.md"
REPORT_HTML = REPO / "SUPER_TRADE_report.html"

# Au-delà de cet âge, le cache screener est rafraîchi automatiquement au démarrage.
CACHE_MAX_AGE_H = 20

# ── STRATÉGIE (éditable) ─────────────────────────────────────────────────────
# Profil : surtout long terme, risque modéré, DCA. Cf. mémoire du projet.

SAFETY_CUSHION_EUR = 5000.0     # coussin de sécurité gardé en CASH (hors PEA)
EQUITY_TARGET_PCT  = 0.60       # part actions cible du patrimoine total (modéré)
CORE_PCT           = 0.70       # part « cœur ETF » dans la poche actions construite
QARP_PCT           = 0.30       # part « stock-picking QARP »
DCA_MONTHS         = 12         # étalement du déploiement (mois)
MIN_ORDER_EUR      = 100.0      # on n'émet pas d'ordre plus petit que ça
MAX_LINE_PCT       = 0.15       # aucune ligne action > 15 % de la poche actions
SELL_GRADE_MAX     = "D"        # vend/allège une valeur QARP tombée à ce grade ou pire
PEA_CEILING_EUR    = 150000.0   # plafond légal de versements PEA
PRICE_DIVERGENCE_PCT = 0.15     # écart prix Yahoo↔Trade Republic au-delà duquel on écarte un titre
SCREENER_TOP_N     = 12         # nb de titres du screener COMPLET ajoutés à la short-list d'achat
SCREENER_MIN_GRADE = "B+"       # grade minimum pour qu'un titre du screener entre dans la short-list
ISIN_MAP_FILE      = REPO / "isin_map.json"  # cache symbole Yahoo → ISIN (résolu via Trade Republic)

# Cœur de portefeuille : ETF large PEA-éligible. Par défaut l'Amundi S&P 500
# (FR0011550185) que tu détiens déjà. Tu peux mettre un ETF MSCI World PEA.
CORE_ETF = {"isin": "FR0011550185", "nom": "Amundi S&P 500 UCITS (Acc) — cœur PEA"}

# Socle de CONVICTIONS imposées (toujours candidates, ISIN fiables vérifiés à la main).
# La short-list d'achat = ce socle UNION le TOP du screener complet (cf. build_universe).
QARP_UNIVERS = [
    {"isin": "NL0000395903", "symbol": "WKL.AS",    "nom": "Wolters Kluwer",   "secteur": "Industrie/Info"},
    {"isin": "DK0062498333", "symbol": "NOVO-B.CO", "nom": "Novo Nordisk",     "secteur": "Santé"},
    {"isin": "FR0000120271", "symbol": "TTE.PA",    "nom": "TotalEnergies",    "secteur": "Énergie"},
    {"isin": "NL0012866412", "symbol": "BESI.AS",   "nom": "BE Semiconductor", "secteur": "Semi-conducteurs"},
    {"isin": "ES0148396007", "symbol": "ITX.MC",    "nom": "Inditex",          "secteur": "Conso/Distribution"},
    {"isin": "IT0005239360", "symbol": "UCG.MI",    "nom": "UniCredit",        "secteur": "Finance"},
    {"isin": "DE0005557508", "symbol": "DTE.DE",    "nom": "Deutsche Telekom", "secteur": "Télécom"},
    {"isin": "DE0007164600", "symbol": "SAP.DE",    "nom": "SAP",              "secteur": "Logiciel/Tech"},
]


# ── Connexion ────────────────────────────────────────────────────────────────

def _normalize_phone(phone: str) -> str:
    """Met le numéro au format international E.164 attendu par Trade Republic.
    Accepte '0618574651', '06 18 57 46 51', '+33618574651', etc.
    """
    p = "".join(phone.split())  # enlève espaces/points éventuels
    if p.startswith("+"):
        return p
    if p.startswith("00"):
        return "+" + p[2:]
    if p.startswith("0"):          # numéro français national → +33
        return "+33" + p[1:]
    return p


def connect():
    """Connexion TR via les helpers du connecteur. Reprend la session, sinon 2FA."""
    phone = os.environ.get("TR_PHONE") or input("Numéro Trade Republic (06… ou +33…) : ").strip()
    phone = _normalize_phone(phone)
    pin = os.environ.get("TR_PIN") or getpass.getpass("PIN Trade Republic : ").strip()
    api = M._make_api(phone, pin)
    M._S.update({"tr": api, "pending": False, "accounts": None, "isin_tags": None})
    if api.resume_websession():
        print("✓ Session reprise (pas de 2FA).")
        return
    print("Connexion… un code 2FA va être envoyé.")
    countdown = api.initiate_weblogin()
    code = input(f"Code 2FA reçu (valide ~{countdown}s) : ").strip()
    api.complete_weblogin(code)
    print("✓ Connecté.")


# ── Récupération de l'état du compte ─────────────────────────────────────────

async def _available_cash(cash_account: str) -> float:
    d = await M._fetch(M._S["tr"].subscribe(
        {"type": "availableCash", "accountNumber": cash_account}))
    return M._f(d[0].get("amount")) if isinstance(d, list) and d else 0.0


async def fetch_state() -> dict:
    accounts = await M._account_pairs()
    pea = next((a for a in accounts if a.get("productType") == "TAX_WRAPPER"), None)
    cto = next((a for a in accounts if a.get("productType") == "DEFAULT"), None)
    pea_cash = await _available_cash(pea["cashAccountNumber"]) if pea else 0.0
    cto_cash = await _available_cash(cto["cashAccountNumber"]) if cto else 0.0
    positions = await M._positions_enriched()  # déjà taguées PEA/CTO
    return {"pea_cash": pea_cash, "cto_cash": cto_cash, "positions": positions}


# ── Screener ─────────────────────────────────────────────────────────────────

def _cache_age_hours() -> float | None:
    """Âge du cache screener en heures, ou None s'il est absent/illisible."""
    if not SCREENER_CACHE.exists():
        return None
    try:
        data = json.loads(SCREENER_CACHE.read_text(encoding="utf-8"))
        delta = datetime.now() - datetime.fromisoformat(data["cached_at"])
        return delta.total_seconds() / 3600
    except Exception:
        return None


def refresh_screener_cache(force: bool = False) -> None:
    """Régénère pea_cache2.json via pea_screener2.py si le cache est trop vieux.

    Lance le screener en sous-processus (Yahoo Finance) avec --refresh.
    `force=True` rafraîchit même si le cache est récent.
    """
    age = _cache_age_hours()
    if not force and age is not None and age < CACHE_MAX_AGE_H:
        print(f"✓ Cache screener à jour ({age:.1f} h) — pas de rafraîchissement.")
        return
    if not SCREENER.exists():
        print(f"⚠ {SCREENER.name} introuvable — rafraîchissement ignoré.")
        return
    raison = "absent/illisible" if age is None else f"vieux de {age:.1f} h"
    print(f"⟳ Rafraîchissement du cache screener ({raison})… cela peut prendre 1-2 min.")
    try:
        subprocess.run(
            [sys.executable, str(SCREENER), "--refresh"],
            cwd=str(REPO),
            check=True,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        print("✓ Cache screener rafraîchi.")
    except subprocess.CalledProcessError as e:
        print(f"⚠ Échec du rafraîchissement (code {e.returncode}) — "
              "utilisation du cache existant.")
    except Exception as e:
        print(f"⚠ Rafraîchissement impossible ({e}) — utilisation du cache existant.")


def load_screener() -> dict:
    """Retourne {symbol: record} + alerte si le cache est ancien."""
    if not SCREENER_CACHE.exists():
        print("⚠ pea_cache2.json absent — lance pea_screener2.py d'abord.")
        return {}
    data = json.loads(SCREENER_CACHE.read_text(encoding="utf-8"))
    age_days = (datetime.now() - datetime.fromisoformat(data["cached_at"])).days
    if age_days > 5:
        print(f"⚠ Données screener vieilles de {age_days} j — pense à --refresh.")
    return {r["symbol"]: r for r in data.get("records", [])}


async def cross_check_prices(screener: dict, universe: list[dict]) -> list[dict]:
    """Compare le prix Yahoo (cache screener) au prix live Trade Republic.

    Sert de garde-fou anti-données corrompues : si l'écart relatif dépasse
    PRICE_DIVERGENCE_PCT, le titre est marqué suspect (`_price_suspect`) pour être
    écarté des recommandations. Retourne la liste des alertes.
    """
    warnings = []
    for u in universe:
        rec = screener.get(u["symbol"])
        if not rec:
            continue
        y_price = M._f(rec.get("price"))
        try:
            tr_price = await M._current_price(u["isin"])
        except Exception:
            tr_price = 0.0
        # Sans prix TR fiable, on ne peut pas trancher : on ne pénalise pas.
        if not y_price or not tr_price:
            continue
        divergence = abs(y_price - tr_price) / tr_price
        if divergence > PRICE_DIVERGENCE_PCT:
            rec["_price_suspect"] = True
            warnings.append({
                "nom": u["nom"], "isin": u["isin"], "symbol": u["symbol"],
                "yahoo": y_price, "tr": tr_price, "ecart_pct": round(divergence * 100, 1),
            })
    return warnings


def _load_isin_map() -> dict:
    """Cache disque symbole Yahoo → ISIN (évite de re-résoudre via TR à chaque run)."""
    if ISIN_MAP_FILE.exists():
        try:
            return json.loads(ISIN_MAP_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_isin_map(m: dict) -> None:
    try:
        ISIN_MAP_FILE.write_text(json.dumps(m, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    except Exception:
        pass


async def _resolve_isin(query: str) -> str | None:
    """Résout l'ISIN d'une valeur via la recherche Trade Republic (neonSearch)."""
    if not query:
        return None
    try:
        data = await M._fetch(M._S["tr"].subscribe({"type": "neonSearch", "data": {
            "q": query, "filter": [{"key": "type", "value": "stock"}],
            "page": 1, "pageSize": 5}}))
    except Exception:
        return None
    results = data.get("results", []) if isinstance(data, dict) else []
    return results[0].get("isin") if results else None


async def build_universe(screener: dict) -> list[dict]:
    """Short-list d'achat = socle de convictions UNION le TOP du screener complet.

    Les convictions (QARP_UNIVERS) ont des ISIN fiables. Pour les titres issus du
    screener, l'ISIN est résolu via Trade Republic puis mis en cache disque.
    """
    isin_map = _load_isin_map()
    universe, seen = [], set()

    # 1) Convictions imposées (toujours présentes)
    for u in QARP_UNIVERS:
        universe.append({**u, "source": "conviction"})
        seen.add(u["symbol"])
        isin_map.setdefault(u["symbol"], u["isin"])

    # 2) TOP du screener complet, par composite décroissant, au-dessus du grade plancher
    eligibles = [r for r in screener.values()
                 if grade_rank(r.get("grade")) >= grade_rank(SCREENER_MIN_GRADE)]
    eligibles.sort(key=lambda r: -M._f(r.get("composite")))

    added = 0
    for rec in eligibles:
        if added >= SCREENER_TOP_N:
            break
        sym = rec["symbol"]
        if sym in seen:
            continue
        isin = isin_map.get(sym)
        if not isin:                       # résolution TR (nom puis symbole sans suffixe)
            isin = await _resolve_isin(rec.get("name") or sym) \
                or await _resolve_isin(sym.split(".")[0])
            if isin:
                isin_map[sym] = isin
        if not isin:                       # pas d'ISIN → pas d'ordre possible, on saute
            continue
        universe.append({
            "isin": isin, "symbol": sym,
            "nom": rec.get("name") or sym,
            "secteur": rec.get("sector") or "—",
            "source": "screener",
        })
        seen.add(sym)
        added += 1

    _save_isin_map(isin_map)
    return universe


def grade_rank(grade: str) -> int:
    """S = meilleur (7) … F = pire (0). Pour comparer aux seuils de grade."""
    # Échelle alignée sur les grades émis par pea_screener2 (SCORE_GRADE).
    order = ["F", "D", "C", "B", "B+", "A", "A+", "S"]
    g = (grade or "").strip()
    return order.index(g) if g in order else -1


# ── Moteur de recommandations ────────────────────────────────────────────────

def build_reco(state: dict, screener: dict, universe: list[dict],
               months: int, equity_pct: float,
               price_warnings: list[dict] | None = None) -> dict:
    positions = state["positions"]
    pea_cash, cto_cash = state["pea_cash"], state["cto_cash"]
    total_cash = pea_cash + cto_cash
    equity_now = sum(p["valeur_actuelle"] or 0 for p in positions)
    total = equity_now + total_cash

    equity_target = equity_pct * total
    equity_gap = max(0.0, equity_target - equity_now)         # à investir au total
    deployable = max(0.0, total_cash - SAFETY_CUSHION_EUR)    # cash mobilisable
    to_invest_total = min(equity_gap, deployable)
    monthly = to_invest_total / months if months else 0.0

    # budget du mois : cœur ETF + poche QARP
    core_budget = monthly * CORE_PCT
    qarp_budget = monthly * QARP_PCT

    # ── ACHATS : on classe l'univers (convictions + TOP screener) par composite ───
    # Signal unifié = le score composite QARP de pea_screener2 (qualité+croissance+
    # valorisation+momentum+santé), sans recalcul parallèle. À score égal, tes
    # convictions imposées (source="conviction") passent devant.
    ranked = []
    for u in universe:
        rec = screener.get(u["symbol"])
        if not rec:
            continue
        if rec.get("_price_suspect"):        # prix Yahoo↔TR incohérent → on écarte
            continue
        ranked.append({**u,
                       "grade": rec.get("grade"),
                       "score": M._f(rec.get("composite")),
                       "upside_pct": rec.get("upside_pct"),
                       "mom_52w": rec.get("mom_52w"),
                       "signal": round(M._f(rec.get("composite")), 1)})
    ranked.sort(key=lambda x: (-x["signal"], x.get("source") != "conviction"))
    # on alloue le budget QARP du mois aux meilleurs signaux, par ordres ≥ MIN_ORDER
    n_orders = max(1, int(qarp_budget // MIN_ORDER_EUR))
    picks = ranked[:max(1, min(n_orders, 3))]                 # 1 à 3 valeurs / mois
    per_pick = round(qarp_budget / len(picks), 0) if picks else 0
    qarp_buys = [{"action": "ACHAT", **p, "montant_eur": per_pick}
                 for p in picks if per_pick >= MIN_ORDER_EUR]

    core_buy = ({"action": "ACHAT", **CORE_ETF, "montant_eur": round(core_budget, 0)}
                if core_budget >= MIN_ORDER_EUR else None)

    # versement PEA à prévoir ce mois (le cash PEA dispo couvre-t-il le tranche ?)
    invest_pea_month = (core_buy["montant_eur"] if core_buy else 0) + sum(b["montant_eur"] for b in qarp_buys)
    versement = max(0.0, invest_pea_month - pea_cash)

    # ── VENTES / ALLÈGEMENTS ──────────────────────────────────────────────────
    # La surpondération se mesure vs la poche actions CIBLE (sinon, en début de
    # déploiement, toute ligne paraît énorme et on conseillerait de vendre à tort).
    ref_equity = max(equity_now, equity_target)
    sells = []
    qarp_isins = {u["isin"]: u for u in universe}
    for p in positions:
        rec = None
        u = qarp_isins.get(p["isin"])
        if u:
            rec = screener.get(u["symbol"])
        # 1) valeur QARP dont la qualité s'est dégradée
        if rec and grade_rank(rec.get("grade")) <= grade_rank(SELL_GRADE_MAX) and "PEA" in (p["compte"] or ""):
            sells.append({"action": "VENDRE", "nom": p["nom"], "isin": p["isin"],
                          "raison": f"qualité dégradée (grade {rec.get('grade')}, score {M._f(rec.get('composite')):.0f})",
                          "valeur_eur": p["valeur_actuelle"]})
        # 2) ligne devenue trop grosse vs la poche actions cible
        elif ref_equity and (p["valeur_actuelle"] or 0) > MAX_LINE_PCT * ref_equity and p["type"] != "fund":
            sells.append({"action": "ALLÉGER", "nom": p["nom"], "isin": p["isin"],
                          "raison": f"surpondérée (> {MAX_LINE_PCT:.0%} de la poche actions cible)",
                          "valeur_eur": p["valeur_actuelle"]})

    return {
        "total": total, "equity_now": equity_now, "equity_target": equity_target,
        "equity_pct_now": (100 * equity_now / total) if total else 0,
        "pea_cash": pea_cash, "cto_cash": cto_cash,
        "to_invest_total": to_invest_total, "monthly": monthly,
        "core_buy": core_buy, "qarp_buys": qarp_buys, "versement_pea": versement,
        "sells": sells, "ranked": ranked, "months": months, "equity_pct": equity_pct,
        "price_warnings": price_warnings or [],
    }


# ── Rapport ──────────────────────────────────────────────────────────────────

def render(r: dict) -> str:
    L = []
    a = L.append
    a("# 🦾 SUPER_TRADE — Recommandations\n")
    a(f"*{datetime.now().strftime('%d/%m/%Y %H:%M')} · DCA {r['months']} mois · "
      f"cible actions {r['equity_pct']:.0%} · LECTURE SEULE (tu exécutes les ordres)*\n")

    a("## 📊 Situation")
    a(f"- Patrimoine total : **{r['total']:,.0f} €**")
    a(f"- Actions actuelles : **{r['equity_now']:,.0f} €** ({r['equity_pct_now']:.0f} %) "
      f"→ cible {r['equity_target']:,.0f} €")
    a(f"- Cash : PEA {r['pea_cash']:,.0f} € · CTO {r['cto_cash']:,.0f} € "
      f"(coussin gardé : {SAFETY_CUSHION_EUR:,.0f} €)")
    a(f"- À investir au total (étalé) : **{r['to_invest_total']:,.0f} €** "
      f"→ ~**{r['monthly']:,.0f} €/mois**\n")

    a("## 🟢 À ACHETER ce mois-ci")
    if r["versement_pea"] > 0:
        a(f"> 💸 **Versement PEA recommandé : {r['versement_pea']:,.0f} €** "
          f"(vire depuis ton cash CTO avant d'acheter)\n")
    if r["core_buy"]:
        c = r["core_buy"]
        a(f"- **Cœur ETF** — {c['nom']} (`{c['isin']}`) : **{c['montant_eur']:,.0f} €**")
    for b in r["qarp_buys"]:
        a(f"- **QARP** — {b['nom']} (`{b['isin']}`, {b['secteur']}) : "
          f"**{b['montant_eur']:,.0f} €**  · grade {b['grade']} · signal {b['signal']}/100")
    if not r["core_buy"] and not r["qarp_buys"]:
        a("- *Rien à acheter ce mois (cible actions atteinte ou cash insuffisant).*")
    a("")

    a("## 🔴 À VENDRE / ALLÉGER")
    if r["sells"]:
        for s in r["sells"]:
            a(f"- **{s['action']}** {s['nom']} (`{s['isin']}`) — {s['raison']} "
              f"(~{s['valeur_eur']:,.0f} €)")
    else:
        a("- *Aucune vente recommandée (rien ne déclenche les règles de sortie).*")
    a("")

    if r.get("price_warnings"):
        a("## ⚠️ Données de prix suspectes (écartées)")
        a(f"*Titres dont le prix Yahoo diverge de >{PRICE_DIVERGENCE_PCT:.0%} du prix Trade Republic "
          "— exclus des recommandations le temps de vérifier la source.*\n")
        for w in r["price_warnings"]:
            a(f"- **{w['nom']}** (`{w['isin']}`) — Yahoo {w['yahoo']:.2f} € vs "
              f"TR {w['tr']:.2f} € (écart {w['ecart_pct']:.0f} %)")
        a("")

    a("## 🎯 Classement « acheter maintenant » (univers QARP)")
    a("| Valeur | Secteur | Grade | Score | Upside | Mom.52s | Signal |")
    a("|---|---|---|--:|--:|--:|--:|")
    for x in r["ranked"]:
        up = f"{x['upside_pct']:+.0f}%" if isinstance(x["upside_pct"], (int, float)) else "-"
        mo = f"{x['mom_52w']:+.0f}%" if isinstance(x["mom_52w"], (int, float)) else "-"
        nom = f"★ {x['nom']}" if x.get("source") == "conviction" else x["nom"]
        a(f"| {nom} | {x['secteur']} | {x['grade']} | {x['score']:.0f} | {up} | {mo} | **{x['signal']}** |")
    a("\n---")
    a("*★ = conviction imposée. Signal = score composite QARP du screener (qualité + croissance "
      "+ valorisation + momentum + santé fin.). Convictions priorisées à score égal. Règles, pas prédiction.*")
    a("*SUPER_TRADE ne passe aucun ordre : valide et exécute toi-même dans l'appli TR.*")
    return "\n".join(L)


def render_html(r: dict) -> str:
    """Rapport HTML autonome (CSS inline), style tableau de bord."""
    pct_now = r["equity_pct_now"]
    pct_target = r["equity_pct"] * 100
    fill = min(100, pct_now / pct_target * 100) if pct_target else 0

    def euro(x):
        return f"{x:,.0f} €".replace(",", " ")

    # cartes situation
    cards = "".join(f"""
      <div class="card"><div class="lbl">{lbl}</div><div class="val">{val}</div>
        {('<div class="sub">'+sub+'</div>') if sub else ''}</div>"""
        for lbl, val, sub in [
            ("Patrimoine total", euro(r["total"]), ""),
            ("Actions actuelles", f"{pct_now:.0f} %",
             f"{euro(r['equity_now'])} → cible {euro(r['equity_target'])}"),
            ("Cash PEA", euro(r["pea_cash"]), "à déployer"),
            ("Cash CTO", euro(r["cto_cash"]), f"coussin gardé {euro(SAFETY_CUSHION_EUR)}"),
            ("À investir / mois", euro(r["monthly"]),
             f"total {euro(r['to_invest_total'])} sur {r['months']} mois"),
        ])

    # achats
    buys = ""
    if r["versement_pea"] > 0:
        buys += (f'<div class="versement">💸 Versement PEA recommandé : '
                 f'<b>{euro(r["versement_pea"])}</b> — vire ce montant du CTO vers le PEA '
                 f'avant d\'acheter</div>')
    rows = []
    if r["core_buy"]:
        c = r["core_buy"]
        rows.append(f'<tr><td><span class="tag core">CŒUR ETF</span></td>'
                    f'<td>{c["nom"]}</td><td class="isin">{c["isin"]}</td>'
                    f'<td class="amt">{euro(c["montant_eur"])}</td><td>—</td></tr>')
    for b in r["qarp_buys"]:
        rows.append(f'<tr><td><span class="tag qarp">QARP · {b["secteur"]}</span></td>'
                    f'<td>{b["nom"]}</td><td class="isin">{b["isin"]}</td>'
                    f'<td class="amt">{euro(b["montant_eur"])}</td>'
                    f'<td>grade {b["grade"]} · signal {b["signal"]}</td></tr>')
    if not rows:
        rows.append('<tr><td colspan="5" class="muted">Rien à acheter ce mois.</td></tr>')
    buys += f'<table class="t"><tr><th></th><th>Valeur</th><th>ISIN</th><th>Montant</th><th></th></tr>{"".join(rows)}</table>'

    # ventes
    if r["sells"]:
        srows = "".join(f'<tr><td><span class="tag sell">{s["action"]}</span></td>'
                        f'<td>{s["nom"]}</td><td class="isin">{s["isin"]}</td>'
                        f'<td>{s["raison"]}</td><td class="amt">{euro(s["valeur_eur"] or 0)}</td></tr>'
                        for s in r["sells"])
        sells_html = f'<table class="t">{srows}</table>'
    else:
        sells_html = '<div class="ok">✓ Aucune vente recommandée — phase de construction.</div>'

    # classement
    rk = []
    for x in r["ranked"]:
        up = f"{x['upside_pct']:+.0f}%" if isinstance(x["upside_pct"], (int, float)) else "-"
        mo = f"{x['mom_52w']:+.0f}%" if isinstance(x["mom_52w"], (int, float)) else "-"
        rk.append(f'<tr><td>{x["nom"]}</td><td class="muted">{x["secteur"]}</td>'
                  f'<td>{x["grade"]}</td><td>{x["score"]:.0f}</td><td>{up}</td><td>{mo}</td>'
                  f'<td><div class="bar"><div style="width:{x["signal"]}%"></div>'
                  f'<span>{x["signal"]}</span></div></td></tr>')
    rank_html = ('<table class="t rank"><tr><th>Valeur</th><th>Secteur</th><th>Grade</th>'
                 '<th>Score</th><th>Upside</th><th>Mom.52s</th><th>Signal « acheter maintenant »</th></tr>'
                 + "".join(rk) + '</table>')

    # alertes prix (cross-check Yahoo↔TR)
    warn_html = ""
    if r.get("price_warnings"):
        wrows = "".join(
            f'<tr><td>{w["nom"]}</td><td class="isin">{w["isin"]}</td>'
            f'<td class="amt">{w["yahoo"]:.2f} €</td><td class="amt">{w["tr"]:.2f} €</td>'
            f'<td class="warn">écart {w["ecart_pct"]:.0f} %</td></tr>'
            for w in r["price_warnings"])
        warn_html = (
            f'<h2 class="warn">⚠️ Données de prix suspectes (écartées)</h2>'
            f'<div class="muted" style="margin-bottom:8px">Prix Yahoo divergeant de '
            f'&gt;{PRICE_DIVERGENCE_PCT:.0%} du prix Trade Republic — exclus des recommandations '
            f'le temps de vérifier la source.</div>'
            f'<table class="t"><tr><th>Valeur</th><th>ISIN</th><th>Yahoo</th><th>Trade Republic</th><th></th></tr>'
            f'{wrows}</table>')

    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SUPER_TRADE — Recommandations</title>
<style>
  :root{{--bg:#0f1419;--card:#1a212b;--line:#2a333f;--txt:#e6edf3;--mut:#8b98a5;
    --grn:#2ea043;--red:#f85149;--blu:#388bfd;--gold:#d29922;}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--txt);
    font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;padding:28px}}
  .wrap{{max-width:980px;margin:0 auto}}
  h1{{font-size:26px;margin:0 0 4px}} .meta{{color:var(--mut);margin-bottom:24px;font-size:13px}}
  h2{{font-size:18px;margin:28px 0 12px;border-bottom:1px solid var(--line);padding-bottom:6px}}
  .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}}
  .card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}}
  .card .lbl{{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.4px}}
  .card .val{{font-size:24px;font-weight:700;margin-top:4px}}
  .card .sub{{color:var(--mut);font-size:12px;margin-top:4px}}
  .prog{{height:8px;background:var(--line);border-radius:6px;margin:14px 0 4px;overflow:hidden}}
  .prog>div{{height:100%;background:linear-gradient(90deg,var(--blu),var(--grn))}}
  .versement{{background:rgba(210,153,34,.12);border:1px solid var(--gold);border-radius:10px;
    padding:12px 14px;margin-bottom:14px}}
  table.t{{width:100%;border-collapse:collapse;background:var(--card);
    border:1px solid var(--line);border-radius:10px;overflow:hidden}}
  .t th{{text-align:left;color:var(--mut);font-size:12px;font-weight:600;padding:10px 12px;
    background:#141a22;text-transform:uppercase;letter-spacing:.3px}}
  .t td{{padding:11px 12px;border-top:1px solid var(--line)}}
  .amt{{font-weight:700;text-align:right;white-space:nowrap}}
  .isin{{font-family:ui-monospace,Menlo,Consolas,monospace;color:var(--mut);font-size:13px}}
  .muted{{color:var(--mut)}} .ok{{color:var(--grn);padding:8px 0}}
  .tag{{display:inline-block;padding:3px 8px;border-radius:6px;font-size:11px;font-weight:700;white-space:nowrap}}
  .tag.core{{background:rgba(56,139,253,.18);color:#79c0ff}}
  .tag.qarp{{background:rgba(46,160,67,.18);color:#56d364}}
  .tag.sell{{background:rgba(248,81,73,.18);color:#ff7b72}}
  .bar{{position:relative;height:18px;background:var(--line);border-radius:5px;min-width:140px}}
  .bar>div{{height:100%;background:linear-gradient(90deg,var(--blu),var(--grn));border-radius:5px}}
  .bar>span{{position:absolute;right:6px;top:0;line-height:18px;font-size:12px;font-weight:700}}
  .foot{{color:var(--mut);font-size:12px;margin-top:26px;border-top:1px solid var(--line);padding-top:12px}}
  .warn{{color:var(--gold)}}
</style></head><body><div class="wrap">
  <h1>🦾 SUPER_TRADE — Recommandations</h1>
  <div class="meta">{datetime.now().strftime('%d/%m/%Y %H:%M')} · DCA {r['months']} mois ·
    cible actions {pct_target:.0f} % · <span class="warn">LECTURE SEULE — tu exécutes les ordres</span></div>

  <h2>📊 Situation</h2>
  <div class="cards">{cards}</div>
  <div class="prog"><div style="width:{fill:.0f}%"></div></div>
  <div class="muted" style="font-size:12px">Progression vers la cible actions : {pct_now:.0f} % / {pct_target:.0f} %</div>

  <h2>🟢 À acheter ce mois-ci</h2>
  {buys}

  <h2>🔴 À vendre / alléger</h2>
  {sells_html}

  {warn_html}

  <h2>🎯 Classement « acheter maintenant » (univers QARP)</h2>
  {rank_html}

  <div class="foot">
    Signal = score composite QARP du screener (qualité + croissance + valorisation + momentum + santé) — règles, pas prédiction de marché.<br>
    SUPER_TRADE ne passe aucun ordre : valide et exécute toi-même dans l'appli Trade Republic.
    Ce n'est pas un conseil financier réglementé.
  </div>
</div></body></html>"""


async def main_async(months: int, equity_pct: float):
    state = await fetch_state()
    screener = load_screener()
    universe = await build_universe(screener)
    n_conv = sum(1 for u in universe if u.get("source") == "conviction")
    print(f"Univers d'achat : {len(universe)} titres "
          f"({n_conv} convictions + {len(universe) - n_conv} issus du screener).")
    price_warnings = await cross_check_prices(screener, universe)
    if price_warnings:
        print(f"⚠ {len(price_warnings)} titre(s) à prix suspect écarté(s) : "
              + ", ".join(w["nom"] for w in price_warnings))
    reco = build_reco(state, screener, universe, months, equity_pct, price_warnings)
    report = render(reco)
    print("\n" + report)
    REPORT_FILE.write_text(report, encoding="utf-8")
    REPORT_HTML.write_text(render_html(reco), encoding="utf-8")
    print(f"\n📄 Rapports écrits : {REPORT_FILE.name} + {REPORT_HTML.name}")
    if not os.environ.get("SUPER_TRADE_NO_OPEN"):
        try:
            import webbrowser
            webbrowser.open(REPORT_HTML.as_uri())
        except Exception:
            pass
    try:
        await M._S["tr"].close()
    except Exception:
        pass


def main():
    p = argparse.ArgumentParser(description="SUPER_TRADE — assistant de décision PEA")
    p.add_argument("--months", type=int, default=DCA_MONTHS, help="étalement DCA (mois)")
    p.add_argument("--equity", type=float, default=EQUITY_TARGET_PCT,
                   help="part actions cible 0-1 (défaut 0.60)")
    p.add_argument("--refresh", action="store_true",
                   help="force le rafraîchissement du cache screener au démarrage")
    p.add_argument("--no-refresh", action="store_true",
                   help="ne rafraîchit jamais le cache (utilise tel quel)")
    args = p.parse_args()
    if not args.no_refresh:
        refresh_screener_cache(force=args.refresh)
    connect()
    asyncio.run(main_async(args.months, args.equity))


if __name__ == "__main__":
    main()
