#!/usr/bin/env python3
"""
Serveur MCP Trade Republic — accès en lecture à ton PEA.

⚠️  Trade Republic n'a PAS d'API publique. Ce serveur utilise l'API mobile
non-officielle via la lib `pytr` (reverse-engineered). C'est *ton* compte donc
c'est légitime, mais c'est techniquement contre les CGU de TR et l'API peut
casser sans préavis. Aucun ordre n'est passé : lecture seule.

Flux de connexion (le 2FA est interactif) :
    1. tr_login            → envoie un code 2FA (SMS / notif appli)
    2. tr_complete_login   → tu saisis le code
    (les appels suivants réutilisent la session sauvegardée dans .tr_cookies.txt)

Outils exposés :
    tr_status            état de la session
    tr_login             démarre la connexion (reprend la session si possible)
    tr_complete_login    valide le code 2FA
    tr_cash              liquidités disponibles
    tr_portfolio         positions + valorisation + +/- value
    tr_transactions      historique (ordres, dividendes, versements)
    tr_screener_cross    croise tes positions avec les scores QARP du screener
    tr_raw               debug : souscrit à un type d'API brut

Prérequis : pip install mcp pytr
Identifiants : passe phone_no/pin à tr_login, ou définis TR_PHONE / TR_PIN.
"""

import asyncio
import json
import os
import re
import unicodedata
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from pytr.api import TradeRepublicApi

# ── Config ──────────────────────────────────────────────────────────────────

REPO = Path(__file__).resolve().parent
COOKIES_FILE = REPO / ".tr_cookies.txt"      # session persistée (SENSIBLE, gitignored)
SCREENER_CACHE = REPO / "pea_cache2.json"     # cache du pea_screener2.py
DEFAULT_EXCHANGE = "LSX"                       # Lang & Schwarz (défaut TR webtrading)
RECV_TIMEOUT = 20.0

# Pays UE/EEE éligibles PEA (siège social ; exclut UK post-Brexit, Suisse, US)
PEA_COUNTRIES = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
    "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK",
    "SI", "ES", "SE", "IS", "LI", "NO",
}

mcp = FastMCP("trade-republic")

# état partagé entre appels d'outils (le process serveur reste vivant)
_S: dict = {"tr": None, "pending": False}


# ── Helpers ───────────────────────────────────────────────────────────────--

def _make_api(phone: str, pin: str) -> TradeRepublicApi:
    return TradeRepublicApi(
        phone_no=phone,
        pin=pin,
        locale="fr",
        save_cookies=True,
        cookies_file=str(COOKIES_FILE),
        waf_token=os.environ.get("TR_WAF", "playwright"),
    )


def _require_login() -> TradeRepublicApi | None:
    return _S.get("tr")


async def _fetch(coro, timeout: float = RECV_TIMEOUT):
    """Souscrit (coro = tr.<method>()) et renvoie le premier payload reçu."""
    tr = _S["tr"]
    sub_id = await coro
    try:
        async def waiter():
            while True:
                rid, _sub, payload = await tr.recv()
                if rid == sub_id:
                    return payload
        return await asyncio.wait_for(waiter(), timeout)
    finally:
        try:
            await tr.unsubscribe(sub_id)
        except Exception:
            pass


def _f(v, default=0.0) -> float:
    """Cast robuste vers float (l'API renvoie souvent des strings)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _norm_name(name: str) -> str:
    """Normalise un nom de société pour le matching screener<->TR."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"[.,'&/()-]", " ", s)
    # retire les suffixes juridiques courants
    s = re.sub(r"\b(se|sa|s a|ag|nv|n v|plc|inc|corp|co|ltd|spa|s p a|"
               r"holding|holdings|group|groupe|the|sas|kgaa|asa|ab|oyj)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ── Comptes : mapping PEA (TAX_WRAPPER) vs CTO (DEFAULT) ─────────────────────

async def _account_pairs() -> list[dict]:
    """Liste des comptes (mis en cache pour la session)."""
    if _S.get("accounts") is None:
        ap = await _fetch(_S["tr"].subscribe({"type": "accountPairs"}))
        _S["accounts"] = ap.get("accounts", []) if isinstance(ap, dict) else []
    return _S["accounts"]


def _label_for(product_type: str) -> str:
    return {"TAX_WRAPPER": "PEA", "DEFAULT": "CTO"}.get(product_type, product_type)


async def _cash_account_labels() -> dict[str, str]:
    """{cashAccountNumber: 'PEA'|'CTO'} d'après accountPairs."""
    return {a["cashAccountNumber"]: _label_for(a.get("productType", ""))
            for a in await _account_pairs()}


async def _account_tags() -> dict[str, dict]:
    """Pour chaque ISIN tradé : comptes vus + dernier compte (PEA/CTO).

    Reconstruit depuis l'historique des ordres (champ cashAccountNumber).
    Mis en cache pour la session car ça parcourt toute la timeline."""
    if _S.get("isin_tags") is not None:
        return _S["isin_tags"]
    tr = _S["tr"]
    labels = await _cash_account_labels()
    items, after = [], None
    for _ in range(30):  # ~600 items max
        tl = await _fetch(tr.subscribe({"type": "timelineTransactions", "after": after}))
        if not isinstance(tl, dict):
            break
        items += tl.get("items", [])
        after = (tl.get("cursors") or {}).get("after")
        if not after:
            break
    tags: dict[str, dict] = {}
    for it in items:  # la timeline est du plus récent au plus ancien
        ev = it.get("eventType") or ""
        if "TRADING" not in ev and "SAVINGS" not in ev:
            continue
        m = re.search(r"logos/([A-Z0-9]{12})/", it.get("icon") or "")
        if not m:
            continue
        isin = m.group(1)
        lab = labels.get(it.get("cashAccountNumber"), "?")
        t = tags.setdefault(isin, {"comptes": set(), "dernier": lab})
        t["comptes"].add(lab)
    _S["isin_tags"] = tags
    return tags


def _isin_account(isin: str, tags: dict[str, dict]) -> str:
    """Étiquette compte d'un ISIN : 'PEA', 'CTO', 'PEA+CTO' (ambigu) ou '?'."""
    t = tags.get(isin)
    if not t:
        return "?"
    comptes = t["comptes"]
    if len(comptes) == 1:
        return next(iter(comptes))
    return "+".join(sorted(comptes))


# ── Outils MCP ──────────────────────────────────────────────────────────────

@mcp.tool()
async def tr_status() -> str:
    """État de la session Trade Republic (connecté, en attente de 2FA, ou déconnecté)."""
    if _S.get("tr") and not _S.get("pending"):
        return "Connecté ✓ — la session est active (ou reprise depuis les cookies)."
    if _S.get("pending"):
        return "En attente du code 2FA — appelle tr_complete_login avec le code reçu."
    has_cookie = COOKIES_FILE.exists()
    return ("Déconnecté. Appelle tr_login pour démarrer."
            + (" (un fichier de session existe et sera réutilisé)" if has_cookie else ""))


@mcp.tool()
async def tr_login(phone_no: str = "", pin: str = "") -> str:
    """Démarre la connexion à Trade Republic.

    Reprend la session sauvegardée si possible (pas de 2FA). Sinon, envoie un
    code 2FA et il faut appeler tr_complete_login ensuite.

    Args:
        phone_no: numéro au format international, ex "+33612345678" (ou env TR_PHONE)
        pin: code PIN du compte TR (ou env TR_PIN)
    """
    phone = phone_no or os.environ.get("TR_PHONE", "")
    pin = pin or os.environ.get("TR_PIN", "")
    if not phone or not pin:
        return ("Erreur : fournis phone_no (+33...) et pin, "
                "ou définis les variables d'env TR_PHONE / TR_PIN.")

    tr = _make_api(phone, pin)
    _S["tr"] = tr
    _S["accounts"] = None      # caches de session (mapping comptes / attribution ISIN)
    _S["isin_tags"] = None
    loop = asyncio.get_event_loop()

    # 1) tenter de reprendre une session existante (évite le 2FA)
    try:
        resumed = await loop.run_in_executor(None, tr.resume_websession)
        if resumed:
            _S["pending"] = False
            return "Session reprise depuis les cookies — déjà connecté. Tu peux appeler tr_portfolio."
    except Exception as e:
        # on continue vers le login complet
        pass

    # 2) login complet → déclenche le 2FA
    try:
        countdown = await loop.run_in_executor(None, tr.initiate_weblogin)
    except Exception as e:
        return f"Échec de l'initialisation du login : {e!r}"
    _S["pending"] = True
    return (f"Code 2FA envoyé (validité ~{countdown}s). "
            f"Appelle tr_complete_login avec le code reçu par SMS / notification.")


@mcp.tool()
async def tr_complete_login(code: str) -> str:
    """Valide le code 2FA reçu pour finaliser la connexion.

    Args:
        code: le code à 4 chiffres reçu par SMS ou notification de l'appli TR.
    """
    tr = _require_login()
    if not tr:
        return "Appelle d'abord tr_login."
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, tr.complete_weblogin, code.strip())
    except Exception as e:
        return f"Code refusé ou erreur : {e!r}"
    _S["pending"] = False
    return "Connecté ✓ — session sauvegardée. Appelle tr_portfolio / tr_cash / tr_transactions."


@mcp.tool()
async def tr_cash() -> str:
    """Liquidités disponibles, ventilées par compte (PEA vs CTO)."""
    if not _require_login():
        return "Pas connecté. Appelle tr_login d'abord."
    try:
        accounts = await _account_pairs()
        out = []
        for a in accounts:
            ca = a["cashAccountNumber"]
            data = await _fetch(_S["tr"].subscribe({"type": "availableCash", "accountNumber": ca}))
            montant = _f(data[0].get("amount")) if isinstance(data, list) and data else 0.0
            out.append({
                "compte": _label_for(a.get("productType", "")),
                "type_produit": a.get("productType"),
                "numero_compte_especes": ca,
                "devise": a.get("currency", "EUR"),
                "montant": montant,
            })
    except Exception as e:
        return f"Erreur : {e!r}"
    return json.dumps({
        "liquidites_par_compte": out,
        "total": round(sum(c["montant"] for c in out), 2),
    }, ensure_ascii=False, indent=2)


@mcp.tool()
async def tr_accounts() -> str:
    """Liste tes comptes Trade Republic (PEA / CTO) avec leur cash disponible."""
    if not _require_login():
        return "Pas connecté. Appelle tr_login d'abord."
    return await tr_cash()


async def _current_price(isin: str) -> float:
    """Dernier prix connu pour un ISIN (essaie plusieurs places de cotation)."""
    tr = _S["tr"]
    for ex in (DEFAULT_EXCHANGE, "TDG"):
        try:
            tk = await _fetch(tr.subscribe({"type": "ticker", "id": f"{isin}.{ex}"}), timeout=8)
            last = _f((tk.get("last") or {}).get("price")) or _f((tk.get("bid") or {}).get("price"))
            if last:
                return last
        except Exception:
            continue
    return 0.0


async def _positions_enriched() -> list[dict]:
    """Récupère les positions (compactPortfolioByType) + prix courant pour chacune.

    Le topic moderne renvoie {"categories": [{"categoryType", "positions": [...]}]}
    où chaque position contient déjà isin / name / netSize / averageBuyIn."""
    tr = _S["tr"]
    pf = await _fetch(tr.subscribe({"type": "compactPortfolioByType"}))
    categories = pf.get("categories", []) if isinstance(pf, dict) else []
    tags = await _account_tags()  # attribution PEA/CTO via l'historique
    rows = []
    for cat in categories:
        cat_type = cat.get("categoryType")
        for p in cat.get("positions", []):
            isin = p.get("isin") or p.get("instrumentId") or ""
            qty = _f(p.get("netSize"))
            avg = _f(p.get("averageBuyIn"))
            name = p.get("name") or isin
            last = await _current_price(isin)
            cost = qty * avg
            value = qty * last if last else 0.0
            pnl = (value - cost) if last else None
            rows.append({
                "isin": isin,
                "nom": name,
                "compte": _isin_account(isin, tags),
                "categorie": cat_type,
                "type": p.get("instrumentType"),
                "quantite": qty,
                "prix_revient": round(avg, 4),
                "prix_actuel": round(last, 4) if last else None,
                "cout_total": round(cost, 2),
                "valeur_actuelle": round(value, 2) if last else None,
                "plus_value": round(pnl, 2) if pnl is not None else None,
                "plus_value_pct": round(100 * pnl / cost, 2) if (pnl is not None and cost) else None,
            })
    return rows


@mcp.tool()
async def tr_portfolio() -> str:
    """Positions du portefeuille + valorisation + plus/moins-value (JSON).

    Pour chaque ligne : ISIN, nom, quantité, prix de revient, prix actuel,
    coût total, valeur actuelle et +/- value. Inclut les totaux."""
    if not _require_login():
        return "Pas connecté. Appelle tr_login d'abord."
    try:
        rows = await _positions_enriched()
    except Exception as e:
        return f"Erreur : {e!r}"
    tot_cost = sum(r["cout_total"] for r in rows)
    tot_val = sum(r["valeur_actuelle"] or 0 for r in rows)
    pnl = tot_val - tot_cost
    return json.dumps({
        "positions": rows,
        "totaux": {
            "cout_total": round(tot_cost, 2),
            "valeur_actuelle": round(tot_val, 2),
            "plus_value": round(pnl, 2),
            "plus_value_pct": round(100 * pnl / tot_cost, 2) if tot_cost else None,
        },
    }, ensure_ascii=False, indent=2)


@mcp.tool()
async def tr_pea() -> str:
    """Vue dédiée du PEA : cash PEA + positions attribuées au PEA.

    Les positions sont rattachées au PEA via l'historique des ordres
    (champ cashAccountNumber). Les lignes 'PEA+CTO' sont détenues sur les
    deux comptes — l'API ne permet pas de scinder la quantité exacte."""
    if not _require_login():
        return "Pas connecté. Appelle tr_login d'abord."
    try:
        # cash PEA
        pea_acc = next((a for a in await _account_pairs()
                        if a.get("productType") == "TAX_WRAPPER"), None)
        pea_cash = 0.0
        if pea_acc:
            d = await _fetch(_S["tr"].subscribe(
                {"type": "availableCash", "accountNumber": pea_acc["cashAccountNumber"]}))
            pea_cash = _f(d[0].get("amount")) if isinstance(d, list) and d else 0.0
        rows = [r for r in await _positions_enriched() if "PEA" in (r["compte"] or "")]
    except Exception as e:
        return f"Erreur : {e!r}"
    val = sum(r["valeur_actuelle"] or 0 for r in rows)
    return json.dumps({
        "pea": {
            "cash_disponible": round(pea_cash, 2),
            "valeur_titres": round(val, 2),
            "valeur_totale": round(pea_cash + val, 2),
            "positions": rows,
        },
        "note": "Attribution via l'historique des ordres ; 'PEA+CTO' = ligne sur les deux comptes.",
    }, ensure_ascii=False, indent=2)


@mcp.tool()
async def tr_search(query: str, pea_only: bool = False) -> str:
    """Recherche un instrument sur Trade Republic → ISIN, nom, pays, type.

    Utile pour résoudre l'ISIN d'une valeur et vérifier son éligibilité PEA
    (pays UE/EEE). Args:
        query: nom ou ticker, ex "TotalEnergies" ou "SAP".
        pea_only: si True, ne garde que les actions domiciliées UE/EEE.
    """
    if not _require_login():
        return "Pas connecté. Appelle tr_login d'abord."
    try:
        data = await _fetch(_S["tr"].subscribe({"type": "neonSearch", "data": {
            "q": query, "filter": [{"key": "type", "value": "stock"}],
            "page": 1, "pageSize": 10}}))
    except Exception as e:
        return f"Erreur : {e!r}"
    results = data.get("results", []) if isinstance(data, dict) else []
    out = []
    for r in results:
        country = next((t.get("id") for t in (r.get("tags") or [])
                        if t.get("type") == "country"), None)
        eligible = (country in PEA_COUNTRIES) if country else None
        if pea_only and not eligible:
            continue
        out.append({
            "isin": r.get("isin"),
            "nom": r.get("name"),
            "pays": country,
            "pea_eligible": eligible,
            "type": r.get("instrumentType"),
        })
    return json.dumps({"resultats": out}, ensure_ascii=False, indent=2)


@mcp.tool()
async def tr_transactions(limit: int = 30) -> str:
    """Historique des transactions (ordres, dividendes, versements).

    Args:
        limit: nombre d'éléments à renvoyer (défaut 30).
    """
    if not _require_login():
        return "Pas connecté. Appelle tr_login d'abord."
    try:
        data = await _fetch(_S["tr"].timeline_transactions())
    except Exception as e:
        return f"Erreur : {e!r}"
    items = data.get("items", []) if isinstance(data, dict) else []
    out = []
    for it in items[:limit]:
        amt = it.get("amount") or {}
        out.append({
            "date": it.get("timestamp"),
            "titre": it.get("title"),
            "sous_titre": it.get("subtitle"),
            "type": it.get("eventType") or it.get("type"),
            "montant": _f(amt.get("value")) if isinstance(amt, dict) else None,
            "devise": amt.get("currency") if isinstance(amt, dict) else None,
            "statut": it.get("status"),
        })
    return json.dumps({"transactions": out, "total_disponible": len(items)},
                      ensure_ascii=False, indent=2)


@mcp.tool()
async def tr_screener_cross() -> str:
    """Croise tes positions PEA avec les scores QARP du pea_screener2.py.

    Matche par nom de société normalisé (le screener n'a pas d'ISIN).
    Renvoie pour chaque ligne : valeur, +/- value, et le grade/score QARP
    s'il est trouvé dans le cache du screener."""
    if not _require_login():
        return "Pas connecté. Appelle tr_login d'abord."
    if not SCREENER_CACHE.exists():
        return f"Cache screener introuvable : {SCREENER_CACHE}. Lance d'abord pea_screener2.py."

    try:
        cache = json.loads(SCREENER_CACHE.read_text(encoding="utf-8"))
    except Exception as e:
        return f"Lecture du cache impossible : {e!r}"
    by_name = {}
    for rec in cache.get("records", []):
        by_name[_norm_name(rec.get("name", ""))] = rec

    try:
        rows = await _positions_enriched()
    except Exception as e:
        return f"Erreur portefeuille : {e!r}"

    crossed, unmatched = [], []
    for r in rows:
        key = _norm_name(r["nom"])
        rec = by_name.get(key)
        if not rec:  # fallback : matching partiel
            rec = next((v for k, v in by_name.items()
                        if key and (key in k or k in key)), None)
        entry = {
            "nom": r["nom"],
            "isin": r["isin"],
            "valeur_actuelle": r["valeur_actuelle"],
            "plus_value_pct": r["plus_value_pct"],
        }
        if rec:
            entry.update({
                "screener_grade": rec.get("grade"),
                "screener_score": round(_f(rec.get("composite")), 1),
                "screener_secteur": rec.get("sector"),
                "screener_consensus": rec.get("consensus"),
                "upside_pct": rec.get("upside_pct"),
            })
            crossed.append(entry)
        else:
            unmatched.append(entry)

    crossed.sort(key=lambda x: x.get("screener_score") or -1)
    return json.dumps({
        "positions_notees": crossed,
        "positions_hors_screener": unmatched,
        "note": "Matching par nom de société ; vérifie les correspondances douteuses.",
    }, ensure_ascii=False, indent=2)


@mcp.tool()
async def tr_raw(subscription_type: str, isin: str = "", extra_json: str = "{}") -> str:
    """Debug : souscrit à un type d'API brut et renvoie le payload tel quel.

    Utile pour inspecter la structure réelle des réponses TR.
    Args:
        subscription_type: ex "portfolio", "compactPortfolio", "cash", "portfolioStatus".
        isin: ISIN si le type l'exige (ex "instrument").
        extra_json: clés supplémentaires du payload, en JSON.
    """
    if not _require_login():
        return "Pas connecté. Appelle tr_login d'abord."
    payload = {"type": subscription_type}
    if isin:
        payload["id"] = isin
    try:
        payload.update(json.loads(extra_json or "{}"))
    except Exception as e:
        return f"extra_json invalide : {e!r}"
    try:
        data = await _fetch(_S["tr"].subscribe(payload))
    except Exception as e:
        return f"Erreur : {e!r}"
    return json.dumps(data, ensure_ascii=False, indent=2)[:8000]


if __name__ == "__main__":
    mcp.run()
