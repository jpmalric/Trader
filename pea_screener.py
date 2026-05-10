#!/usr/bin/env python3
"""
PEA Screener - actions eligibles PEA (Trade Republic) classees par score
composite pondéré multi-facteurs.

Facteurs intégrés (par ordre d'importance décroissant) :
  1. Qualité fondamentale : ROE, FCF yield, croissance CA, croissance bénéfices
  2. Valorisation         : PEG ratio, EV/EBITDA, upside prix cible
  3. Consensus analyste   : % achat, score moyen
  4. Momentum             : révisions EPS 90j, performance 52 semaines
  5. Bilan                : dette/capitaux

Usage:
    python pea_screener.py                   # cache si dispo, sinon refresh
    python pea_screener.py --refresh         # force mise a jour complète (~5 min)
    python pea_screener.py --enrich          # ajoute les cabinets d'analyse au cache
    python pea_screener.py --top 30          # top 30
    python pea_screener.py --min-score 80    # score composite minimum (0-100)
    python pea_screener.py --detail          # détail complet des métriques

Prerequis:
    pip install curl-cffi pandas
"""

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from curl_cffi import requests as cr

# ── Configuration ────────────────────────────────────────────────────────────────

CACHE_FILE       = Path("pea_cache.json")
CACHE_TTL_H      = 20
MIN_ANALYSTS     = 5
TOP_N            = 50
DELAY_S          = 1.5
RETRY_MAX        = 3
BASE_QUERY       = "https://query2.finance.yahoo.com"
FIRMS_LOOKBACK_D = 180

# ── Pondérations du score composite (total = 1.0) ────────────────────────────────
# (nom_champ, poids, sens) — sens True = plus grand = meilleur
SCORE_WEIGHTS: list[tuple[str, float, bool]] = [
    # Qualité fondamentale (35%)
    ("roe",           0.12, True),   # Return on Equity
    ("fcf_yield",     0.10, True),   # Free Cash Flow / Market Cap
    ("rev_growth",    0.07, True),   # Croissance CA
    ("earn_growth",   0.06, True),   # Croissance bénéfices
    # Valorisation (25%)
    ("peg_ratio",     0.12, False),  # PEG ratio (plus bas = meilleur)
    ("ev_ebitda",     0.08, False),  # EV/EBITDA (plus bas = meilleur)
    ("upside_pct",    0.05, True),   # Upside vs prix cible
    # Consensus analyste (20%)
    ("buy_pct",       0.12, True),   # % analystes achat
    ("inv_score",     0.08, True),   # Score moyen inversé (6 - mean_score)
    # Momentum (15%)
    ("eps_revision",  0.09, True),   # Révision EPS +90j
    ("mom_52w",       0.06, True),   # Performance cours 52 semaines
    # Bilan (5%)
    ("debt_equity",   0.05, False),  # Dette/Capitaux (plus bas = meilleur)
]

assert abs(sum(w for _, w, _ in SCORE_WEIGHTS) - 1.0) < 1e-9, "Poids invalides"

SCORE_GRADE = [(90,"S"),(80,"A+"),(70,"A"),(60,"B+"),(50,"B"),(40,"C"),(30,"D"),(0,"F")]

def score_to_grade(s: float) -> str:
    for threshold, grade in SCORE_GRADE:
        if s >= threshold:
            return grade
    return "F"

# ── Normalisation grades analyste ────────────────────────────────────────────────

GRADE_MAP: dict[str, str] = {
    "strong buy":"strong_buy","strong-buy":"strong_buy","top pick":"strong_buy",
    "conviction buy":"strong_buy","speculative buy":"strong_buy",
    "buy":"buy","outperform":"buy","overweight":"buy","accumulate":"buy",
    "add":"buy","positive":"buy","long-term buy":"buy","market outperform":"buy",
    "sector outperform":"buy","trading buy":"buy","action list buy":"buy",
    "hold":"hold","neutral":"hold","market perform":"hold","sector perform":"hold",
    "equal weight":"hold","equal-weight":"hold","in-line":"hold","inline":"hold",
    "peer perform":"hold","market weight":"hold","fair value":"hold",
    "maintain":"hold","perform":"hold","mixed":"hold","mp":"hold","restricted":"hold",
    "sell":"sell","underperform":"sell","underweight":"sell",
    "sector underperform":"sell","reduce":"sell","negative":"sell",
    "market underperform":"sell","trim":"sell",
    "strong sell":"strong_sell","strong-sell":"strong_sell",
}
GRADE_FR  = {"strong_buy":"Achat Fort","buy":"Achat","hold":"Neutre",
             "sell":"Vente","strong_sell":"Vente Forte","unknown":"—"}
ACTION_FR = {"up":"Relevé","down":"Abaissé","main":"Maintenu",
             "init":"Initié","reit":"Réitéré"}

def normalize_grade(grade: str) -> str:
    return GRADE_MAP.get(grade.lower().strip(), "unknown")

# ── Univers PEA ──────────────────────────────────────────────────────────────────

STOCKS_PEA: dict[str, list[str]] = {
    "CAC40_SBF": [
        "AIR.PA","AI.PA","ALO.PA","BN.PA","BNP.PA","CA.PA","CAP.PA","ACA.PA",
        "DSY.PA","ENGI.PA","EL.PA","RMS.PA","KER.PA","OR.PA","LR.PA","MC.PA",
        "ML.PA","ORA.PA","RI.PA","PUB.PA","RNO.PA","SAF.PA","SGO.PA","SAN.PA",
        "SU.PA","GLE.PA","STM.PA","HO.PA","TTE.PA","DG.PA","VIV.PA","WLN.PA",
        "ERF.PA","EN.PA","TEP.PA","VIE.PA","FR.PA","CS.PA","SBT.PA","RCO.PA",
    ],
    "DAX40": [
        "ADS.DE","ALV.DE","BAS.DE","BAYN.DE","BMW.DE","CBK.DE","DB1.DE",
        "DBK.DE","DHL.DE","DTE.DE","EOAN.DE","FRE.DE","HEI.DE","HEN3.DE",
        "IFX.DE","MBG.DE","MRK.DE","MUV2.DE","PAH3.DE","RHM.DE","RWE.DE",
        "SAP.DE","SIE.DE","SRT3.DE","VOW3.DE","VNA.DE","ZAL.DE","BEI.DE",
        "CON.DE","ENR.DE","MTX.DE","P911.DE","PUM.DE","QGEN.DE","SHL.DE",
        "SY1.DE","FME.DE","AIR.DE",
    ],
    "IBEX35": [
        "ACS.MC","AENA.MC","AMS.MC","ANA.MC","BBVA.MC","CABK.MC","CLNX.MC",
        "ELE.MC","ENG.MC","GRF.MC","IAG.MC","IBE.MC","ITX.MC","MAP.MC",
        "MEL.MC","NTGY.MC","RED.MC","REP.MC","SAB.MC","SAN.MC","TEF.MC",
        "ACX.MC","COL.MC","IDR.MC","ROVI.MC","SCYR.MC","SOL.MC","UNI.MC",
    ],
    "AEX": [
        "ABN.AS","ADYEN.AS","AKZA.AS","MT.AS","ASM.AS","ASML.AS","BESI.AS",
        "DSM.AS","HEIA.AS","IMCD.AS","INGA.AS","KPN.AS","NN.AS","PHIA.AS",
        "PRX.AS","RAND.AS","SHELL.AS","UNA.AS","WKL.AS","OCI.AS",
    ],
    "FTSEMIB": [
        "A2A.MI","AZM.MI","BAMI.MI","CNHI.MI","ENEL.MI","ENI.MI","ERG.MI",
        "FBK.MI","G.MI","HER.MI","ISP.MI","LDO.MI","MB.MI","MONC.MI",
        "RACE.MI","SPM.MI","TEN.MI","TIT.MI","UCG.MI","PRY.MI",
    ],
    "BEL20": [
        "ABI.BR","ARGX.BR","COLR.BR","ELI.BR","GBLB.BR","KBC.BR","PROX.BR",
        "SOLB.BR","UCB.BR","WDP.BR","AGS.BR","UMI.BR",
    ],
    "OMX_NORD": [
        "ERIC-B.ST","VOLV-B.ST","SAND.ST","SEB-A.ST","SHB-A.ST","SWED-A.ST",
        "NIBE-B.ST","ALFA.ST","ATCO-A.ST","NDA-SE.ST","NOVO-B.CO","ORSTED.CO",
        "DSV.CO","CARL-B.CO","NESTE.HE","KNEBV.HE","WRT1V.HE",
        "EQNR.OL","DNB.OL","TEL.OL","ORK.OL",
    ],
    "ATX": ["EBS.VI","OMV.VI","VER.VI","VOE.VI"],
}

ALL_TICKERS: list[str] = []
_seen: set[str] = set()
for _lst in STOCKS_PEA.values():
    for _t in _lst:
        if _t not in _seen:
            _seen.add(_t)
            ALL_TICKERS.append(_t)


# ── Cache ────────────────────────────────────────────────────────────────────────

def load_cache() -> list | None:
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01"))
        if datetime.now() - cached_at < timedelta(hours=CACHE_TTL_H):
            age = datetime.now() - cached_at
            h, m = divmod(age.seconds // 60, 60)
            print(f"Cache valide ({h}h{m:02}m) — {CACHE_FILE}. "
                  "Utilisez --refresh pour mettre à jour.")
            return data.get("records", [])
    except Exception:
        pass
    return None

def save_cache(records: list) -> None:
    CACHE_FILE.write_text(
        json.dumps({"cached_at": datetime.now().isoformat(), "records": records},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── Session Yahoo Finance ────────────────────────────────────────────────────────

class YahooSession:
    def __init__(self) -> None:
        self._session = cr.Session(impersonate="chrome124")
        self._crumb: str | None = None

    def _gdpr_consent(self) -> None:
        r = self._session.get("https://finance.yahoo.com/", timeout=20, allow_redirects=True)
        if "consent.yahoo.com" in str(r.url):
            ms = re.search(r'name="sessionId"[^>]*value="([^"]+)"', r.text)
            mc = re.search(r'name="csrfToken"[^>]*value="([^"]+)"', r.text)
            self._session.post("https://consent.yahoo.com/v2/collectConsent", data={
                "agree": ["agree","agree"], "consentUUID": "default",
                "sessionId": ms.group(1) if ms else "",
                "csrfToken": mc.group(1) if mc else "",
                "originalDoneUrl": "https://finance.yahoo.com/", "namespace": "yahoo",
            }, timeout=20)
            self._session.get("https://finance.yahoo.com/", timeout=20)

    def _get_crumb(self, seed: str = "ASML.AS") -> str:
        r = self._session.get(f"https://finance.yahoo.com/quote/{seed}/",
                              timeout=20, headers={"Referer": "https://finance.yahoo.com/"})
        r.raise_for_status()
        m = re.search(r'"crumb"\s*:\s*"([^"]{5,30})"', r.text)
        if not m:
            raise RuntimeError("Crumb non trouvé dans la page")
        return m.group(1)

    def setup(self) -> None:
        print("Initialisation session Yahoo Finance...")
        self._gdpr_consent()
        self._crumb = self._get_crumb()
        print("  Session prête.")

    def quote_summary(self, symbol: str,
                      modules: str = (
                          "financialData,recommendationTrend,quoteType,"
                          "summaryProfile,upgradeDowngradeHistory,"
                          "defaultKeyStatistics,earningsTrend,summaryDetail"
                      )) -> dict:
        r = self._session.get(
            f"{BASE_QUERY}/v10/finance/quoteSummary/{symbol}",
            params={"modules": modules, "crumb": self._crumb, "formatted": "false"},
            headers={"Referer": f"https://finance.yahoo.com/quote/{symbol}/"},
            timeout=20,
        )
        r.raise_for_status()
        result = r.json().get("quoteSummary", {}).get("result")
        return result[0] if result else {}


# ── Helpers ──────────────────────────────────────────────────────────────────────

def _v(obj) -> float | int | str | None:
    """Extrait la valeur brute d'un champ YF (dict {raw:x} ou scalaire)."""
    return obj.get("raw") if isinstance(obj, dict) else obj

def _pct(x) -> float | None:
    """Convertit une décimale YF en pourcentage (0.15 → 15.0)."""
    v = _v(x)
    return round(float(v) * 100, 2) if v is not None else None


# ── Recommandations par cabinet ──────────────────────────────────────────────────

def parse_firm_recommendations(history: list) -> list[dict]:
    cutoff_ts = (datetime.now() - timedelta(days=FIRMS_LOOKBACK_D)).timestamp()
    firm_latest: dict[str, dict] = {}
    for item in history:
        epoch    = item.get("epochGradeDate", 0) or 0
        to_grade = (item.get("toGrade") or "").strip()
        if epoch < cutoff_ts or not to_grade:
            continue
        firm = (item.get("firm") or "Inconnu").strip()
        if firm not in firm_latest or epoch > firm_latest[firm]["epoch"]:
            norm = normalize_grade(to_grade)
            firm_latest[firm] = {
                "firm": firm, "grade": to_grade, "normalized": norm,
                "grade_fr":  GRADE_FR.get(norm, to_grade),
                "action":    item.get("action",""),
                "action_fr": ACTION_FR.get(item.get("action",""), item.get("action","")),
                "from_grade":(item.get("fromGrade") or "").strip(),
                "date_str":  datetime.fromtimestamp(epoch).strftime("%d/%m/%Y"),
                "epoch":     epoch,
            }
    return sorted(firm_latest.values(), key=lambda x: x["epoch"], reverse=True)


# ── Récupération données par action ──────────────────────────────────────────────

def fetch_analyst_data(session: YahooSession, symbol: str) -> dict | None:
    for attempt in range(RETRY_MAX):
        try:
            data = session.quote_summary(symbol)
            fd  = data.get("financialData",         {})
            rt  = data.get("recommendationTrend",   {})
            qt  = data.get("quoteType",              {})
            sp  = data.get("summaryProfile",         {})
            uh  = data.get("upgradeDowngradeHistory",{}).get("history", [])
            ks  = data.get("defaultKeyStatistics",   {})
            et  = data.get("earningsTrend",          {})
            sd  = data.get("summaryDetail",          {})

            mean  = _v(fd.get("recommendationMean"))
            count = _v(fd.get("numberOfAnalystOpinions"))
            if mean is None or count is None or count == 0:
                return None

            # ── Consensus analyste ──────────────────────────────────────────────
            trend_list = rt.get("trend", [])
            sb = b = h = se = ss = 0
            if trend_list:
                cur = trend_list[0]
                sb = cur.get("strongBuy",  0) or 0
                b  = cur.get("buy",         0) or 0
                h  = cur.get("hold",        0) or 0
                se = cur.get("sell",        0) or 0
                ss = cur.get("strongSell",  0) or 0
            total_trend = sb + b + h + se + ss
            buy_count   = sb + b
            buy_pct     = round(buy_count / total_trend * 100, 1) if total_trend > 0 else 0

            # ── Prix & upside ───────────────────────────────────────────────────
            price  = _v(fd.get("currentPrice")) or _v(fd.get("regularMarketPrice"))
            target = _v(fd.get("targetMeanPrice"))
            upside = round((target / price - 1) * 100, 1) if (target and price and price > 0) else None

            # ── Qualité fondamentale ────────────────────────────────────────────
            roe         = _pct(fd.get("returnOnEquity"))      # %
            rev_growth  = _pct(fd.get("revenueGrowth"))       # %
            earn_growth = _pct(fd.get("earningsGrowth"))      # %
            market_cap  = _v(sd.get("marketCap"))
            fcf_abs     = _v(fd.get("freeCashflow"))
            fcf_yield   = round(fcf_abs / market_cap * 100, 2) if (fcf_abs and market_cap and market_cap > 0) else None

            # ── Valorisation ────────────────────────────────────────────────────
            peg_ratio = _v(ks.get("pegRatio"))
            ev_ebitda = _v(ks.get("enterpriseToEbitda"))
            fwd_pe    = _v(ks.get("forwardPE"))

            # ── Bilan ───────────────────────────────────────────────────────────
            debt_equity = _v(fd.get("debtToEquity"))          # ratio × 100 dans YF

            # ── Momentum cours ──────────────────────────────────────────────────
            mom_52w = _pct(ks.get("52WeekChange"))             # % YoY

            # ── Révisions EPS (90j) ─────────────────────────────────────────────
            eps_revision = None
            et_trend = et.get("trend", [])
            if et_trend:
                eps_rev = et_trend[0].get("epsRevisions", {})
                up30  = _v(eps_rev.get("upLast30days"))  or 0
                dn30  = _v(eps_rev.get("downLast30days")) or 0
                up90  = _v(eps_rev.get("upLast7days"))   or 0   # proxy
                total_rev = (up30 or 0) + (dn30 or 0)
                if total_rev > 0:
                    eps_revision = round((up30 - dn30) / total_rev * 100, 1)

            return {
                # Identité
                "symbol":       symbol,
                "name":         qt.get("longName") or qt.get("shortName") or "",
                "sector":       sp.get("sector",""),
                "country":      sp.get("country",""),
                "currency":     fd.get("financialCurrency",""),
                # Consensus
                "mean_score":   round(float(mean), 2),
                "inv_score":    round(6.0 - float(mean), 2),
                "n_analysts":   int(count),
                "consensus":    fd.get("recommendationKey",""),
                "strong_buy":   sb, "buy": b, "hold": h,
                "sell":         se, "strong_sell": ss,
                "total_trend":  total_trend,
                "buy_count":    buy_count,
                "buy_pct":      buy_pct,
                # Prix
                "price":        price,
                "target_price": target,
                "upside_pct":   upside,
                # Qualité
                "roe":          roe,
                "rev_growth":   rev_growth,
                "earn_growth":  earn_growth,
                "fcf_yield":    fcf_yield,
                # Valorisation
                "peg_ratio":    peg_ratio,
                "ev_ebitda":    ev_ebitda,
                "fwd_pe":       fwd_pe,
                # Bilan
                "debt_equity":  debt_equity,
                # Momentum
                "mom_52w":      mom_52w,
                "eps_revision": eps_revision,
                # Score intermédiaire (remplacé par compute_composite_scores)
                "score":        int(count) * (6.0 - float(mean)),
                "composite":    None,
                "grade":        None,
                # Cabinets
                "firms":        parse_firm_recommendations(uh),
            }

        except Exception as e:
            if attempt < RETRY_MAX - 1:
                time.sleep(15 * (attempt + 1))
            else:
                return None
    return None


# ── Score composite pondéré ──────────────────────────────────────────────────────

def _percentile_ranks(values: list[float | None], ascending: bool) -> list[float]:
    """
    Retourne les rangs centiles (0-100) de chaque valeur dans la liste.
    Les valeurs None reçoivent 50 (neutre).
    ascending=True  → plus grand = meilleur (centile élevé)
    ascending=False → plus petit = meilleur (centile élevé)
    """
    n = len(values)
    valid = [(i, v) for i, v in enumerate(values) if v is not None]
    valid_sorted = sorted(valid, key=lambda x: x[1])          # croissant

    ranks = [50.0] * n
    m = len(valid_sorted)
    for pos, (orig_idx, _) in enumerate(valid_sorted):
        pct = pos / (m - 1) * 100 if m > 1 else 50.0
        ranks[orig_idx] = pct if ascending else (100.0 - pct)
    return ranks


def compute_composite_scores(records: list[dict]) -> list[dict]:
    """
    Calcule le score composite pondéré (0-100) pour chaque action.
    Utilise les rangs centiles intra-univers pour chaque métrique.
    """
    n = len(records)
    if n == 0:
        return records

    # Pré-traitement : PEG négatif et EV/EBITDA négatif → None (non informatif)
    def clean_peg(v):
        return v if (v is not None and 0 < v < 50) else None
    def clean_ev(v):
        return v if (v is not None and v > 0) else None
    def clean_de(v):
        # D/E dans YF est parfois exprimé × 100 (ex: 150 = 1.5)
        if v is None: return None
        return v / 100 if v > 20 else v   # normalise si nécessaire

    # Construire les vecteurs pour chaque métrique
    field_vectors: dict[str, list] = {}
    for fname, _, _ in SCORE_WEIGHTS:
        if fname == "peg_ratio":
            field_vectors[fname] = [clean_peg(r.get(fname)) for r in records]
        elif fname == "ev_ebitda":
            field_vectors[fname] = [clean_ev(r.get(fname))  for r in records]
        elif fname == "debt_equity":
            field_vectors[fname] = [clean_de(r.get(fname))  for r in records]
        else:
            field_vectors[fname] = [r.get(fname) for r in records]

    # Calculer les rangs centiles pour chaque métrique
    percentile_matrix: dict[str, list[float]] = {}
    for fname, _, ascending in SCORE_WEIGHTS:
        percentile_matrix[fname] = _percentile_ranks(field_vectors[fname], ascending)

    # Score composite
    for i, rec in enumerate(records):
        composite = sum(
            w * percentile_matrix[fname][i]
            for fname, w, _ in SCORE_WEIGHTS
        )
        rec["composite"] = round(composite, 1)
        rec["grade"]     = score_to_grade(composite)

        # Détail par composante (pour le rapport)
        rec["score_detail"] = {
            fname: round(percentile_matrix[fname][i], 1)
            for fname, _, _ in SCORE_WEIGHTS
        }

    return records


# ── Enrichissement cabinets (mode --enrich) ──────────────────────────────────────

def enrich_with_firms() -> pd.DataFrame:
    records = load_cache()
    if records is None:
        print("Aucun cache. Lancez d'abord --refresh.")
        sys.exit(1)

    already = sum(1 for r in records if r.get("firms") is not None)
    if already == len(records):
        print(f"Cache déjà enrichi ({already} actions).")
        records = compute_composite_scores(records)
        save_cache(records)
        return pd.DataFrame(records)

    session = YahooSession()
    session.setup()
    print(f"\nEnrichissement de {len(records)} actions avec les cabinets analystes...")
    for i, rec in enumerate(records):
        if rec.get("firms") is not None:
            continue
        sys.stdout.write(f"\r  [{(i+1)/len(records)*100:5.1f}%]  {rec['symbol']:<15}   ")
        sys.stdout.flush()
        try:
            data = session.quote_summary(rec["symbol"], modules="upgradeDowngradeHistory")
            uh = data.get("upgradeDowngradeHistory", {}).get("history", [])
            rec["firms"] = parse_firm_recommendations(uh)
        except Exception:
            rec["firms"] = []
        time.sleep(DELAY_S)

    print(f"\n\n  Enrichissement terminé.")
    records = compute_composite_scores(records)
    save_cache(records)
    print(f"  Cache mis à jour → {CACHE_FILE}\n")
    return pd.DataFrame(records)


# ── Screener principal ────────────────────────────────────────────────────────────

def run_screener(force_refresh: bool = False,
                 min_analysts: int = MIN_ANALYSTS) -> pd.DataFrame:

    if not force_refresh:
        records = load_cache()
        if records is not None:
            # Recalcul du score composite si absent
            if any(r.get("composite") is None for r in records):
                records = compute_composite_scores(records)
                save_cache(records)
            return pd.DataFrame(records)

    session = YahooSession()
    session.setup()

    print(f"\nRecupération données pour {len(ALL_TICKERS)} actions PEA...")
    est = len(ALL_TICKERS) * DELAY_S / 60
    print(f"Délai : {DELAY_S}s/requête  |  Durée estimée : ~{est:.0f} min\n")

    records: list[dict] = []
    errors = 0
    for i, sym in enumerate(ALL_TICKERS):
        pct = (i + 1) / len(ALL_TICKERS) * 100
        sys.stdout.write(
            f"\r  [{pct:5.1f}%]  {i+1:>3}/{len(ALL_TICKERS)}  {sym:<15}  "
            f"ok={len(records):>3}  err={errors}   "
        )
        sys.stdout.flush()
        data = fetch_analyst_data(session, sym)
        if data and data["n_analysts"] >= min_analysts:
            records.append(data)
        elif data is None:
            errors += 1
        time.sleep(DELAY_S)

    print(f"\n\n  {len(records)} actions avec couverture analyste.")
    if errors:
        print(f"  {errors} sans données ou erreur.")

    records = compute_composite_scores(records)
    save_cache(records)
    print(f"  Cache → {CACHE_FILE}\n")
    return pd.DataFrame(records)


# ── Affichage terminal ───────────────────────────────────────────────────────────

LABEL = {
    "strong_buy":"ACHAT FORT","buy":"ACHAT","hold":"NEUTRE",
    "underperform":"SOUS-PERF","sell":"VENTE","strong_sell":"VENTE FORTE",
}

def fmt_pct(v, decimals=1):
    if v is None: return "   N/A"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.{decimals}f}%"

def fmt_x(v, decimals=1):
    if v is None: return "  N/A"
    return f"{v:.{decimals}f}x"


def display_results(df: pd.DataFrame, top_n: int, min_composite: float,
                    detail: bool = False) -> None:
    if df.empty:
        print("Aucun résultat.")
        return

    df = df.copy()
    if "composite" in df.columns:
        df = df[df["composite"].notna() & (df["composite"] >= min_composite)]
        df = df.sort_values("composite", ascending=False)
    else:
        df = df.sort_values("score", ascending=False)

    df = df.head(top_n).reset_index(drop=True)
    W = 145 if detail else 118

    print()
    print("=" * W)
    print(f"{'TOP ACTIONS PEA — SCORE COMPOSITE PONDÉRÉ':^{W}}")
    print(f"{'Qualité(35%) · Valorisation(25%) · Consensus(20%) · Momentum(15%) · Bilan(5%)':^{W}}")
    print("=" * W)

    if detail:
        print(f"{'#':>3}  {'Sym':<12} {'Nom':<28}  {'Score':>6} {'Grd':>3}  "
              f"{'Buy%':>5} {'PEG':>6} {'ROE':>6} {'FCF%':>6} "
              f"{'Rev+':>6} {'EPS↑':>6} {'Mom':>6}  {'Upside':>7}  Consensus")
    else:
        print(f"{'#':>3}  {'Sym':<12} {'Nom':<30} {'Secteur':<18}  "
              f"{'Score':>6} {'Grd':>3}  {'Buy%':>5} {'Upside':>7}  Consensus")
    print("-" * W)

    for idx, row in df.iterrows():
        key   = str(row.get("consensus","")).lower()
        label = LABEL.get(key, key.upper())
        grade = row.get("grade","?")
        comp  = row.get("composite", 0) or 0

        if detail:
            print(
                f"{idx+1:>3}  {str(row['symbol']):<12} {str(row.get('name',''))[:27]:<28}"
                f"  {comp:>6.1f} {grade:>3} "
                f"  {row.get('buy_pct',0):>4.0f}%"
                f" {fmt_x(row.get('peg_ratio')):>6}"
                f" {fmt_pct(row.get('roe'),0):>6}"
                f" {fmt_pct(row.get('fcf_yield'),0):>6}"
                f" {fmt_pct(row.get('rev_growth'),0):>6}"
                f" {fmt_pct(row.get('eps_revision'),0):>6}"
                f" {fmt_pct(row.get('mom_52w'),0):>6}"
                f"  {fmt_pct(row.get('upside_pct')):>7}"
                f"  {label}"
            )
        else:
            print(
                f"{idx+1:>3}  {str(row['symbol']):<12} {str(row.get('name',''))[:29]:<30}"
                f" {str(row.get('sector',''))[:17]:<18}"
                f"  {comp:>6.1f} {grade:>3}"
                f"  {row.get('buy_pct',0):>4.0f}%"
                f" {fmt_pct(row.get('upside_pct')):>7}"
                f"  {label}"
            )

    print("-" * W)
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    print(f"\n  {len(df)} actions | score >= {min_composite} | Source: Yahoo Finance | {now}\n")


# ── Main ─────────────────────────────────────────────────────────────────────────

def main() -> None:
    global MIN_ANALYSTS

    parser = argparse.ArgumentParser(
        description="PEA Screener — score composite multi-facteurs, Trade Republic")
    parser.add_argument("--refresh",       action="store_true",
                        help="Force mise à jour complète du cache")
    parser.add_argument("--enrich",        action="store_true",
                        help="Ajoute les recommandations par cabinet d'analyse")
    parser.add_argument("--top",           type=int,   default=TOP_N)
    parser.add_argument("--min-composite", type=float, default=0.0,
                        help="Score composite minimum 0-100 (defaut: 0 = tout afficher)")
    parser.add_argument("--min-analysts",  type=int,   default=MIN_ANALYSTS)
    parser.add_argument("--detail",        action="store_true",
                        help="Affiche toutes les métriques")
    args = parser.parse_args()

    MIN_ANALYSTS = args.min_analysts

    if args.enrich:
        enrich_with_firms()
    else:
        df = run_screener(force_refresh=args.refresh, min_analysts=args.min_analysts)
        display_results(df, top_n=args.top, min_composite=args.min_composite, detail=args.detail)

    print("\nGeneration du rapport HTML...")
    result = subprocess.run([sys.executable, "generate_report.py"], capture_output=True, text=True)
    if result.returncode == 0:
        print(result.stdout.strip())
    else:
        print(f"Erreur rapport : {result.stderr.strip()}")


if __name__ == "__main__":
    main()
