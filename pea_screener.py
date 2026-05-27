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
import sys
import time
from collections import Counter
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

            # ── Dividende ────────────────────────────────────────────────────────
            div_rate       = _v(sd.get("dividendRate"))
            div_yield_raw  = sd.get("dividendYield")
            div_yield      = _pct(div_yield_raw) if div_yield_raw else None
            ex_div_epoch   = _v(sd.get("exDividendDate"))
            last_div_val   = _v(sd.get("lastDividendValue"))
            last_div_epoch = _v(sd.get("lastDividendDate"))
            ex_div_str     = datetime.fromtimestamp(int(ex_div_epoch)).strftime("%d/%m/%Y") if ex_div_epoch else None
            last_div_str   = datetime.fromtimestamp(int(last_div_epoch)).strftime("%d/%m/%Y") if last_div_epoch else None

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
                # Dividende
                "div_rate":     div_rate,
                "div_yield":    div_yield,
                "ex_div_date":  ex_div_str,
                "last_div_val": last_div_val,
                "last_div_date": last_div_str,
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


# ── Génération rapport HTML ──────────────────────────────────────────────────────

def generate_html_report(output: Path, h1: str, accent: str = "#38bdf8") -> None:
    if not CACHE_FILE.exists():
        print("  Aucun cache pour le rapport HTML.")
        return

    cache     = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    records   = cache["records"]
    cached_at = datetime.fromisoformat(cache["cached_at"]).strftime("%d/%m/%Y %H:%M")
    generated = datetime.now().strftime("%d/%m/%Y %H:%M")
    page_title = h1.replace("<span>", "").replace("</span>", "").replace("&mdash;", "—")

    has_composite = any(r.get("composite") is not None for r in records)
    sort_key = "composite" if has_composite else "score"
    records = sorted(records, key=lambda r: r.get(sort_key) or 0, reverse=True)

    GRADE_COLOR = {
        "strong_buy": "#16a34a", "buy": "#22c55e", "hold": "#eab308",
        "sell": "#f97316", "strong_sell": "#ef4444", "unknown": "#64748b",
    }
    GRADE_LABEL = {
        "strong_buy": "Achat Fort", "buy": "Achat", "hold": "Neutre",
        "sell": "Vente", "strong_sell": "Vente Forte", "unknown": "—",
    }
    COMP_GRADE_COLOR = {
        "S": "#f59e0b", "A+": "#16a34a", "A": "#22c55e", "B+": "#84cc16",
        "B": "#a3e635", "C": "#eab308", "D": "#f97316", "F": "#ef4444",
    }
    ACTION_ICON = {
        "up":   ("▲", "#22c55e"), "down": ("▼", "#ef4444"),
        "init": ("★", "#38bdf8"), "reit": ("=", "#94a3b8"), "main": ("=", "#94a3b8"),
    }
    METRIC_LABELS = [
        ("roe",          "ROE",          "12%", True),
        ("fcf_yield",    "FCF Yield",    "10%", True),
        ("rev_growth",   "Croiss. CA",   " 7%", True),
        ("earn_growth",  "Croiss. Bén.", " 6%", True),
        ("peg_ratio",    "PEG ratio",    "12%", False),
        ("ev_ebitda",    "EV/EBITDA",    " 8%", False),
        ("upside_pct",   "Upside",       " 5%", True),
        ("buy_pct",      "% Achat",      "12%", True),
        ("inv_score",    "Score inv.",   " 8%", True),
        ("eps_revision", "Rév. EPS",    " 9%", True),
        ("mom_52w",      "Mom. 52s",    " 6%", True),
        ("debt_equity",  "Dette/Cap.",   " 5%", False),
    ]

    def _cbadge(key):
        key = (key or "").lower()
        c = GRADE_COLOR.get(key, "#64748b"); l = GRADE_LABEL.get(key, key.upper())
        return f'<span class="badge" style="background:{c}">{l}</span>'

    def _fbadge(norm):
        c = GRADE_COLOR.get(norm, "#64748b"); l = GRADE_LABEL.get(norm, norm)
        return f'<span class="firm-badge" style="background:{c}20;color:{c};border:1px solid {c}40">{l}</span>'

    def _upside(val):
        if val is None: return '<td class="center gray">N/A</td>'
        c = "#16a34a" if val > 0 else "#ef4444"; s = "+" if val > 0 else ""
        return f'<td class="center" style="color:{c};font-weight:600">{s}{val:.1f}%</td>'

    def _div_cell(r):
        rate = r.get("div_rate") or r.get("last_div_val")
        ex_d = r.get("ex_div_date") or r.get("last_div_date")
        cur  = r.get("currency", "")
        if rate is None:
            return '<td class="center gray div-cell">—</td>'
        rate_str = f"{rate:.2f} {cur}".strip()
        dy = r.get("div_yield")
        dy_html   = f'<div class="div-yield">({dy:.2f}%)</div>' if dy else ""
        date_html = f'<div class="div-date">{ex_d}</div>' if ex_d else ""
        return f'<td class="center small div-cell"><div class="div-amount">{rate_str}</div>{dy_html}{date_html}</td>'

    def _compbar(score):
        if score is None: return '<div class="comp-na">N/A</div>'
        c = ("#16a34a" if score >= 80 else "#22c55e" if score >= 60
             else "#eab308" if score >= 50 else "#f97316" if score >= 30 else "#ef4444")
        p = f"{score:.0f}"
        return (f'<div class="comp-wrap"><div class="comp-bar" style="width:{p}%;background:{c}"></div>'
                f'<span class="comp-num" style="color:{c}">{p}<span class="comp-max">/100</span></span></div>')

    def _gbadge(grade):
        if not grade: return '<span class="grade-na">—</span>'
        c = COMP_GRADE_COLOR.get(grade, "#64748b")
        return f'<div class="grade-badge" style="background:{c}20;color:{c};border:2px solid {c}60">{grade}</div>'

    def _buybar(pct_val):
        pct_val = pct_val or 0
        c = "#16a34a" if pct_val >= 70 else ("#eab308" if pct_val >= 50 else "#ef4444")
        return (f'<div class="buy-bar-wrap"><div class="buy-bar" style="width:{pct_val:.0f}%;background:{c}"></div>'
                f'<span class="buy-num">{pct_val:.0f}%</span></div>')

    def _breakdown(r):
        total = r.get("total_trend") or 0
        if total == 0: return '<div class="center gray">-</div>'
        sb = r.get("strong_buy", 0) or 0; b = r.get("buy", 0) or 0
        h  = r.get("hold", 0) or 0; se = r.get("sell", 0) or 0; ss = r.get("strong_sell", 0) or 0
        parts = []
        for cnt, c, lbl in [(sb, "#16a34a", "Achat Fort"), (b, "#22c55e", "Achat"),
                            (h, "#eab308", "Neutre"), (se, "#f97316", "Vente"), (ss, "#ef4444", "Vente Forte")]:
            if cnt > 0:
                parts.append(f'<div class="seg" style="width:{cnt/total*100:.1f}%;background:{c}" title="{lbl}: {cnt}"></div>')
        return f'<div class="breakdown" title="SB:{sb} B:{b} N:{h} V:{se} VF:{ss}">{"".join(parts)}</div>'

    def _score_section(sd):
        if not sd:
            return '<div class="sd-empty">Score non disponible — relancez avec <code>--refresh</code>.</div>'
        cells = ""
        for fname, lbl, weight, asc in METRIC_LABELS:
            pct = sd.get(fname)
            if pct is None:
                cells += (f'<div class="sd-item"><div class="sd-label">{lbl} <span class="sd-weight">{weight}</span></div>'
                          f'<div class="sd-bar-wrap"><div class="sd-bar" style="width:50%;background:#334155"></div></div>'
                          f'<span class="sd-pct gray">N/A</span></div>')
            else:
                c = "#22c55e" if pct >= 70 else ("#eab308" if pct >= 40 else "#ef4444")
                arrow = "&#x2191;" if asc else "&#x2193;"
                cells += (f'<div class="sd-item"><div class="sd-label">{lbl} <span class="sd-weight">{weight}</span> '
                          f'<span class="sd-arrow" style="color:{c}">{arrow}</span></div>'
                          f'<div class="sd-bar-wrap"><div class="sd-bar" style="width:{pct:.0f}%;background:{c}"></div></div>'
                          f'<span class="sd-pct" style="color:{c}">{pct:.0f}</span></div>')
        return (f'<div class="sd-title">Décomposition du score composite (centile 0-100)</div>'
                f'<div class="sd-grid">{cells}</div>')

    def _detail_row(idx, firms, sd):
        score_sec = _score_section(sd)
        if not firms:
            firms_html = '<div class="no-firms">Aucune recommandation de cabinet disponible sur 180 jours.</div>'
        else:
            rows_f = ""
            for f in firms:
                norm = f.get("normalized", "unknown")
                icon, ic = ACTION_ICON.get(f.get("action", ""), ("·", "#64748b"))
                from_g = f.get("from_grade", "")
                from_html = f' <span class="from-grade">depuis {from_g}</span>' if from_g else ""
                rows_f += (f'<tr class="firm-row"><td class="firm-name">{f["firm"]}</td>'
                           f'<td>{_fbadge(norm)}{from_html}</td>'
                           f'<td class="center" style="color:{ic}">{icon} {f.get("action_fr", "")}</td>'
                           f'<td class="center gray">{f.get("date_str", "")}</td></tr>')
            counts = Counter(f.get("normalized", "unknown") for f in firms)
            summary = " &nbsp;·&nbsp; ".join(
                f'<span style="color:{GRADE_COLOR[n]};font-weight:600">{GRADE_LABEL[n]}: {counts[n]}</span>'
                for n in ["strong_buy", "buy", "hold", "sell", "strong_sell"] if counts.get(n, 0)
            )
            firms_html = (f'<div class="firms-header">{len(firms)} cabinets &nbsp;·&nbsp; {summary}</div>'
                          f'<table class="firms-table"><thead><tr>'
                          f'<th>Cabinet</th><th>Recommandation</th><th>Action</th><th>Date</th>'
                          f'</tr></thead><tbody>{rows_f}</tbody></table>')
        return (f'<tr id="detail-{idx}" class="detail-row" style="display:none"><td colspan="13">'
                f'<div class="detail-container"><div class="detail-score">{score_sec}</div>'
                f'<div class="detail-firms">{firms_html}</div></div></td></tr>')

    buy_strong = sum(1 for r in records if r.get("consensus", "").lower() == "strong_buy")
    buy_only   = sum(1 for r in records if r.get("consensus", "").lower() == "buy")
    hold_      = sum(1 for r in records if r.get("consensus", "").lower() == "hold")
    sell_      = sum(1 for r in records if r.get("consensus", "").lower() in ("sell", "strong_sell", "underperform"))
    enriched   = sum(1 for r in records if r.get("firms") is not None)
    top_grades = sum(1 for r in records if r.get("grade") in ("S", "A+", "A"))
    kpi_gold   = (f'<div class="kpi gold"><div class="val">{top_grades}</div>'
                  f'<div class="lbl">Grades S / A+ / A</div></div>' if has_composite else "")

    rows_html = ""
    for i, r in enumerate(records):
        name    = (r.get("name") or r["symbol"])[:40]
        sector  = r.get("sector", "") or ""; country = r.get("country", "") or ""
        price   = r.get("price"); target = r.get("target_price"); cur = r.get("currency", "")
        comp    = r.get("composite"); grade = r.get("grade")
        sd      = r.get("score_detail"); firms = r.get("firms") or []
        n_ana   = r.get("n_analysts", 0) or 0; buy_p = r.get("buy_pct", 0) or 0
        ps      = f"{price:.2f}" if price else "N/A"; ts = f"{target:.2f}" if target else "N/A"
        price_td = f'<div class="price-cur">{ps}</div><div class="price-tgt">{ts} <span class="price-ccy">{cur}</span></div>'
        tbtn = (f'<button class="toggle-btn" onclick="event.stopPropagation();toggleDetail({i})" '
                f'title="{len(firms)} cabinets">&#9658; {len(firms)}</button>'
                if firms else '<span class="no-btn">—</span>')
        rows_html += (f'<tr class="main-row" onclick="toggleDetail({i})" style="cursor:pointer">'
                      f'<td class="center rank">{i+1}</td>'
                      f'<td class="comp-cell">{_compbar(comp)}</td>'
                      f'<td class="center grade-cell">{_gbadge(grade)}</td>'
                      f'<td><span class="ticker">{r["symbol"]}</span></td>'
                      f'<td class="name-cell" title="{r.get("name", "")}">'
                      f'<div class="name">{name}</div><div class="meta">{sector} · {country}</div></td>'
                      f'<td class="center">{n_ana}</td><td>{_breakdown(r)}</td><td>{_buybar(buy_p)}</td>'
                      f'{_upside(r.get("upside_pct"))}'
                      f'<td class="center small price-cell">{price_td}</td>'
                      f'{_div_cell(r)}'
                      f'<td class="center">{_cbadge(r.get("consensus", ""))}</td>'
                      f'<td class="center">{tbtn}</td></tr>'
                      f'{_detail_row(i, firms, sd)}')

    sort_notice = ("Classé par score composite pondéré (0-100)" if has_composite
                   else "Score composite non disponible — relancez avec <code>--refresh</code>.")
    enrich_cls = "enrich-notice" if enriched > 0 else "enrich-notice warn"
    enrich_txt = (f"&#10003; {enriched}/{len(records)} actions enrichies avec les cabinets"
                  if enriched > 0 else
                  "Lancez le screener avec <code>--enrich</code> pour les recommandations par cabinet.")

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{page_title}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
          background: #0f172a; color: #e2e8f0; font-size: 13px; }}
  header {{ background: linear-gradient(135deg, #1e3a5f 0%, #0f172a 100%);
            padding: 32px 40px 24px; border-bottom: 1px solid #1e293b; }}
  header h1 {{ font-size: 26px; font-weight: 700; color: #f8fafc; letter-spacing: -0.5px; }}
  header h1 span {{ color: {accent}; }}
  header p {{ color: #94a3b8; margin-top: 6px; font-size: 13px; }}
  .kpi-row {{ display: flex; gap: 16px; padding: 24px 40px; flex-wrap: wrap; }}
  .kpi {{ background: #1e293b; border: 1px solid #334155; border-radius: 10px; padding: 16px 24px; flex: 1; min-width: 130px; }}
  .kpi .val {{ font-size: 32px; font-weight: 700; }}
  .kpi .lbl {{ color: #64748b; font-size: 11px; margin-top: 2px; text-transform: uppercase; letter-spacing: .5px; }}
  .kpi.green .val {{ color: #22c55e; }} .kpi.lime .val {{ color: #84cc16; }}
  .kpi.yellow .val {{ color: #eab308; }} .kpi.red .val {{ color: #f87171; }}
  .kpi.blue .val {{ color: {accent}; }} .kpi.purple .val {{ color: #a78bfa; }} .kpi.gold .val {{ color: #f59e0b; }}
  .notice {{ margin: 0 40px 12px; padding: 10px 16px; border-radius: 8px; font-size: 12px; }}
  .enrich-notice {{ background: #16a34a20; border: 1px solid #16a34a40; color: #86efac; }}
  .enrich-notice.warn {{ background: #eab30820; border-color: #eab30840; color: #fde68a; }}
  .sort-notice {{ background: {accent}20; border: 1px solid {accent}40; color: #7dd3fc; }}
  .notice code {{ background: #1e293b; padding: 1px 6px; border-radius: 4px; }}
  .table-wrap {{ padding: 0 40px 40px; overflow-x: auto; }}
  .legend {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 16px; color: #64748b; font-size: 11px; align-items: center; }}
  table {{ width: 100%; border-collapse: collapse; }}
  thead tr {{ background: #1e293b; }}
  thead th {{ padding: 10px 12px; text-align: left; color: #94a3b8; font-size: 11px; text-transform: uppercase;
              letter-spacing: .6px; position: sticky; top: 0; background: #1e293b; white-space: nowrap; z-index: 10; }}
  .main-row {{ border-bottom: 1px solid #1e293b; transition: background .12s; }}
  .main-row:hover {{ background: #1e293b; }} .main-row.open {{ background: #162032; border-bottom: none; }}
  td {{ padding: 9px 12px; vertical-align: middle; }}
  .rank {{ color: #475569; font-weight: 600; width: 32px; }}
  .ticker {{ font-family: monospace; font-weight: 700; color: {accent}; font-size: 13px; }}
  .name-cell .name {{ font-weight: 500; color: #e2e8f0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 220px; }}
  .name-cell .meta {{ color: #64748b; font-size: 11px; margin-top: 2px; white-space: nowrap; }}
  .center {{ text-align: center; }} .gray {{ color: #64748b; }} .small {{ font-size: 12px; }}
  .comp-cell {{ min-width: 130px; }}
  .comp-wrap {{ display: flex; align-items: center; gap: 7px; }}
  .comp-bar {{ height: 8px; border-radius: 4px; flex-shrink: 0; min-width: 4px; }}
  .comp-num {{ font-weight: 700; font-size: 13px; white-space: nowrap; }}
  .comp-max {{ font-size: 10px; opacity: .5; font-weight: 400; }} .comp-na {{ color: #475569; font-size: 12px; font-style: italic; }}
  .grade-cell {{ width: 52px; }}
  .grade-badge {{ display: inline-flex; align-items: center; justify-content: center;
                  width: 40px; height: 28px; border-radius: 8px; font-size: 13px; font-weight: 800; letter-spacing: -.3px; }}
  .grade-na {{ color: #334155; }}
  .buy-bar-wrap {{ display: flex; align-items: center; gap: 8px; min-width: 80px; }}
  .buy-bar {{ height: 6px; border-radius: 3px; flex-shrink: 0; }}
  .buy-num {{ font-weight: 600; font-size: 12px; color: #cbd5e1; }}
  .breakdown {{ display: flex; height: 8px; border-radius: 4px; overflow: hidden; width: 100px; gap: 1px; cursor: default; }}
  .seg {{ height: 100%; flex-shrink: 0; }}
  .badge {{ display: inline-block; padding: 3px 8px; border-radius: 20px; font-size: 11px; font-weight: 600; color: #fff; white-space: nowrap; }}
  .price-cell {{ min-width: 100px; }}
  .price-cur {{ font-weight: 500; color: #e2e8f0; font-size: 12px; }}
  .price-tgt {{ color: #64748b; font-size: 11px; margin-top: 2px; }} .price-ccy {{ color: #475569; }}
  .div-cell {{ min-width: 90px; }}
  .div-amount {{ font-weight: 500; color: #e2e8f0; font-size: 12px; }}
  .div-yield {{ color: #22c55e; font-size: 11px; margin-top: 1px; }}
  .div-date {{ color: #64748b; font-size: 11px; margin-top: 2px; }}
  .toggle-btn {{ background: #1e293b; border: 1px solid #334155; color: #94a3b8;
                 padding: 3px 8px; border-radius: 6px; font-size: 11px; cursor: pointer; white-space: nowrap; transition: all .15s; }}
  .toggle-btn:hover {{ background: #334155; color: #e2e8f0; }}
  .toggle-btn.active {{ background: {accent}20; border-color: {accent}; color: {accent}; }}
  .no-btn {{ color: #334155; font-size: 12px; }}
  .detail-row td {{ padding: 0; background: #0c1929; border-bottom: 2px solid #1e3a5f; }}
  .detail-container {{ display: flex; gap: 0; padding: 20px 24px 24px 50px; flex-wrap: wrap; }}
  .detail-score {{ flex: 0 0 auto; min-width: 340px; max-width: 420px; padding-right: 32px; border-right: 1px solid #1e293b; }}
  .detail-firms {{ flex: 1 1 300px; padding-left: 28px; min-width: 280px; }}
  .sd-title {{ font-size: 11px; color: {accent}; text-transform: uppercase; letter-spacing: .7px; margin-bottom: 12px; font-weight: 600; }}
  .sd-empty {{ color: #475569; font-style: italic; font-size: 12px; }}
  .sd-empty code {{ background: #1e293b; padding: 1px 6px; border-radius: 4px; }}
  .sd-grid {{ display: flex; flex-direction: column; gap: 7px; }}
  .sd-item {{ display: flex; align-items: center; gap: 8px; }}
  .sd-label {{ width: 110px; font-size: 12px; color: #94a3b8; flex-shrink: 0; }}
  .sd-weight {{ color: #475569; font-size: 10px; }} .sd-arrow {{ font-size: 10px; }}
  .sd-bar-wrap {{ flex: 1; height: 6px; background: #1e293b; border-radius: 3px; overflow: hidden; }}
  .sd-bar {{ height: 100%; border-radius: 3px; }}
  .sd-pct {{ width: 28px; text-align: right; font-size: 12px; font-weight: 600; flex-shrink: 0; }}
  .firms-header {{ font-size: 12px; color: #94a3b8; margin-bottom: 10px; }}
  .no-firms {{ color: #475569; font-style: italic; font-size: 12px; }}
  .firms-table {{ width: 100%; border-collapse: collapse; max-width: 640px; }}
  .firms-table thead th {{ background: #1e293b; color: #64748b; font-size: 11px; text-transform: uppercase;
                           letter-spacing: .5px; padding: 6px 10px; text-align: left; position: relative; top: auto; }}
  .firm-row {{ border-bottom: 1px solid #1e293b30; }} .firm-row:hover {{ background: #1e293b30; }}
  .firm-row td {{ padding: 5px 10px; color: #cbd5e1; font-size: 12px; }}
  .firm-name {{ font-weight: 500; color: #e2e8f0; min-width: 160px; }}
  .firm-badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }}
  .from-grade {{ color: #475569; font-size: 11px; }}
  footer {{ text-align: center; padding: 20px; color: #334155; font-size: 11px; border-top: 1px solid #1e293b; }}
</style>
</head>
<body>
<header>
  <h1>{h1}</h1>
  <p>Score composite multi-facteurs &bull; Source : Yahoo Finance &bull;
     Données du {cached_at} &bull; Rapport généré le {generated}</p>
</header>
<div class="kpi-row">
  <div class="kpi blue"><div class="val">{len(records)}</div><div class="lbl">Actions analysées</div></div>
  {kpi_gold}
  <div class="kpi green"><div class="val">{buy_strong}</div><div class="lbl">Achat Fort</div></div>
  <div class="kpi lime"><div class="val">{buy_only}</div><div class="lbl">Achat</div></div>
  <div class="kpi yellow"><div class="val">{hold_}</div><div class="lbl">Neutre</div></div>
  <div class="kpi red"><div class="val">{sell_}</div><div class="lbl">Vente / Sous-perf</div></div>
  <div class="kpi purple"><div class="val">{enriched}</div><div class="lbl">Avec cabinets</div></div>
</div>
<div class="notice sort-notice">{sort_notice}</div>
<div class="notice {enrich_cls}">{enrich_txt}</div>
<div class="table-wrap">
  <div class="legend">
    <span>Grades :</span>
    <span style="color:#f59e0b;font-weight:700">S</span> &#x2265;90 &nbsp;
    <span style="color:#16a34a;font-weight:700">A+</span> &#x2265;80 &nbsp;
    <span style="color:#22c55e;font-weight:700">A</span> &#x2265;70 &nbsp;
    <span style="color:#84cc16;font-weight:700">B+</span> &#x2265;60 &nbsp;
    <span style="color:#a3e635;font-weight:700">B</span> &#x2265;50 &nbsp;
    <span style="color:#eab308;font-weight:700">C</span> &#x2265;40 &nbsp;
    <span style="color:#f97316;font-weight:700">D</span> &#x2265;30 &nbsp;
    <span style="color:#ef4444;font-weight:700">F</span> &lt;30 &nbsp;&nbsp;|&nbsp;&nbsp;
    <span>Cliquez sur une ligne pour voir le détail du score et les cabinets d'analyse</span>
  </div>
  <table>
    <thead><tr>
      <th>#</th><th>Score composite</th><th>Note</th><th>Symbole</th>
      <th>Société / Secteur</th><th class="center">Analystes</th>
      <th>Répartition</th><th>% Achat</th><th class="center">Upside</th>
      <th class="center">Prix / Cible</th><th class="center">Dividende</th>
      <th class="center">Consensus</th><th class="center">Cabinets</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>
<footer>
  Score composite : Qualité(35%) · Valorisation(25%) · Consensus(20%) · Momentum(15%) · Bilan(5%) &bull;
  Yahoo Finance &bull; {generated} &bull;
  Pas un conseil en investissement — à titre informatif uniquement.
</footer>
<script>
  function toggleDetail(idx) {{
    const row  = document.getElementById('detail-' + idx);
    const main = document.querySelectorAll('.main-row')[idx];
    const btn  = document.querySelectorAll('.toggle-btn')[idx];
    const isOpen = row.style.display !== 'none';
    row.style.display = isOpen ? 'none' : 'table-row';
    if (main) main.classList.toggle('open', !isOpen);
    if (btn)  btn.classList.toggle('active', !isOpen);
  }}
</script>
</body>
</html>"""

    output.write_text(html, encoding="utf-8")
    print(f"  Rapport HTML : {output.resolve()}")


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
    generate_html_report(Path("pea_report.html"),
                         "PEA Screener &mdash; <span>Trade Republic</span>",
                         accent="#38bdf8")


if __name__ == "__main__":
    main()
