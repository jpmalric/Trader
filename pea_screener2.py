#!/usr/bin/env python3
"""
PEA Screener v2 — Algorithme QARP (Quality at a Reasonable Price)

Améliorations majeures vs v1 :
  · 6 nouvelles métriques : ROA, marge brute, marge opérationnelle, P/FCF,
    ratio courant, trésorerie nette / cap, dette / EBITDA
  · P/FCF remplace FCF yield (mesure plus directe et moins manipulable)
  · Normalisation sectorielle : blend 65 % univers / 35 % secteur Yahoo Finance
  · Badge QARP : conviction quand (rentab.×0.6 + crois.×0.4) ≥ 65 ET valo ≥ 60
  · Pénalité value-trap : score capé à 50 si rentabilité < 30e centile
  · 5 piliers indépendants affichés avec décomposition dans le rapport HTML
  · Table triable par colonne, filtre secteur + QARP + grade minimum

Piliers (total = 100 %) :
  Rentabilité    30 % : ROE 9 · ROA 7 · Marge brute 7 · Marge opérat. 7
  Croissance     20 % : Croiss. CA 7 · Croiss. bén. 8 · Révision EPS 5
  Valorisation   25 % : P/FCF 10 · EV/EBITDA 9 · PEG 6
  Momentum       15 % : Perf 52s 6 · % Achat 5 · Upside cible 4
  Santé fin.     10 % : Ratio courant 4 · Trés. nette 3 · Dette/EBITDA 3

Usage :
    python pea_screener2.py                  # cache si dispo
    python pea_screener2.py --refresh        # force mise à jour (~5 min)
    python pea_screener2.py --enrich         # ajoute les cabinets d'analyse
    python pea_screener2.py --top 30
    python pea_screener2.py --min-score 60

Prérequis : pip install curl-cffi pandas
"""

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from curl_cffi import requests as cr

# ── Config ─────────────────────────────────────────────────────────────────────

CACHE_FILE       = Path("pea_cache2.json")
CACHE_TTL_H      = 20
MIN_ANALYSTS     = 5
TOP_N            = 50
DELAY_S          = 1.5
RETRY_MAX        = 3
BASE_QUERY       = "https://query2.finance.yahoo.com"
FIRMS_LOOKBACK_D = 180
SECTOR_BLEND     = 0.35   # poids de la normalisation sectorielle

# ── Piliers & pondérations ─────────────────────────────────────────────────────

SCORE_WEIGHTS: list[tuple[str, float, bool]] = [
    # Rentabilité (30 %)
    ("roe",            0.09, True),
    ("roa",            0.07, True),
    ("gross_margin",   0.07, True),
    ("op_margin",      0.07, True),
    # Croissance (20 %)
    ("rev_growth",     0.07, True),
    ("earn_growth",    0.08, True),
    ("eps_revision",   0.05, True),
    # Valorisation (25 %)
    ("pfcf",           0.10, False),
    ("ev_ebitda",      0.09, False),
    ("peg_ratio",      0.06, False),
    # Momentum (15 %)
    ("mom_52w",        0.06, True),
    ("buy_pct",        0.05, True),
    ("upside_pct",     0.04, True),
    # Santé financière (10 %)
    ("current_ratio",  0.04, True),
    ("net_cash_yield", 0.03, True),
    ("debt_cover",     0.03, False),
]

assert abs(sum(w for _, w, _ in SCORE_WEIGHTS) - 1.0) < 1e-9, "Poids invalides"

# (nom, [(champ, poids), ...], couleur_accent)
PILLARS = [
    ("Rentabilité",   [("roe",0.09),("roa",0.07),("gross_margin",0.07),("op_margin",0.07)],  "#38bdf8"),
    ("Croissance",    [("rev_growth",0.07),("earn_growth",0.08),("eps_revision",0.05)],       "#34d399"),
    ("Valorisation",  [("pfcf",0.10),("ev_ebitda",0.09),("peg_ratio",0.06)],                 "#f59e0b"),
    ("Momentum",      [("mom_52w",0.06),("buy_pct",0.05),("upside_pct",0.04)],               "#a78bfa"),
    ("Santé fin.",    [("current_ratio",0.04),("net_cash_yield",0.03),("debt_cover",0.03)],   "#fb7185"),
]

PILLAR_ABBREV = {"Rentabilité": "R", "Croissance": "C", "Valorisation": "V",
                 "Momentum": "M", "Santé fin.": "S"}

FACTOR_LABELS = {
    "roe": "ROE", "roa": "ROA", "gross_margin": "Marge br.",
    "op_margin": "Marge op.", "rev_growth": "Croiss. CA",
    "earn_growth": "Croiss. bén.", "eps_revision": "Rév. EPS",
    "pfcf": "P/FCF", "ev_ebitda": "EV/EBITDA", "peg_ratio": "PEG",
    "mom_52w": "Perf. 52s", "buy_pct": "% Achat", "upside_pct": "Upside",
    "current_ratio": "Ratio cour.", "net_cash_yield": "Trés. nette",
    "debt_cover": "Dette/EBITDA",
}

SCORE_GRADE = [(90,"S"),(80,"A+"),(70,"A"),(60,"B+"),(50,"B"),(40,"C"),(30,"D"),(0,"F")]
COMP_GRADE_COLOR = {
    "S":"#f59e0b","A+":"#16a34a","A":"#22c55e","B+":"#84cc16",
    "B":"#a3e635","C":"#eab308","D":"#f97316","F":"#ef4444",
}

def score_to_grade(s: float) -> str:
    for t, g in SCORE_GRADE:
        if s >= t: return g
    return "F"

def _score_color(v: float | None) -> str:
    if v is None: return "#475569"
    if v >= 80:   return "#16a34a"
    if v >= 65:   return "#22c55e"
    if v >= 50:   return "#eab308"
    if v >= 35:   return "#f97316"
    return "#ef4444"

# ── Normalisation grades analyste ──────────────────────────────────────────────

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

# ── Univers PEA ────────────────────────────────────────────────────────────────

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

# ── Cache ──────────────────────────────────────────────────────────────────────

def load_cache() -> list | None:
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01"))
        if datetime.now() - cached_at < timedelta(hours=CACHE_TTL_H):
            age = datetime.now() - cached_at
            h, m = divmod(age.seconds // 60, 60)
            print(f"Cache v2 valide ({h}h{m:02}m) — {CACHE_FILE}. Utilisez --refresh pour màj.")
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

# ── Session Yahoo Finance ──────────────────────────────────────────────────────

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

# ── Helpers ────────────────────────────────────────────────────────────────────

def _v(obj) -> float | int | str | None:
    return obj.get("raw") if isinstance(obj, dict) else obj

def _pct(x) -> float | None:
    v = _v(x)
    return round(float(v) * 100, 2) if v is not None else None

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

# ── Récupération des données ───────────────────────────────────────────────────

def fetch_analyst_data(session: YahooSession, symbol: str) -> dict | None:
    for attempt in range(RETRY_MAX):
        try:
            data = session.quote_summary(symbol)
            fd  = data.get("financialData",          {})
            rt  = data.get("recommendationTrend",    {})
            qt  = data.get("quoteType",               {})
            sp  = data.get("summaryProfile",          {})
            uh  = data.get("upgradeDowngradeHistory", {}).get("history", [])
            ks  = data.get("defaultKeyStatistics",    {})
            et  = data.get("earningsTrend",           {})
            sd  = data.get("summaryDetail",           {})

            mean  = _v(fd.get("recommendationMean"))
            count = _v(fd.get("numberOfAnalystOpinions"))
            if mean is None or count is None or count == 0:
                return None

            # Consensus
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

            # Prix & upside
            price  = _v(fd.get("currentPrice")) or _v(fd.get("regularMarketPrice"))
            target = _v(fd.get("targetMeanPrice"))
            upside = round((target / price - 1) * 100, 1) if (target and price and price > 0) else None

            # Rentabilité v1
            roe        = _pct(fd.get("returnOnEquity"))
            market_cap = _v(sd.get("marketCap"))
            fcf_abs    = _v(fd.get("freeCashflow"))

            # ── Nouvelles métriques v2 ─────────────────────────────────────────
            roa          = _pct(fd.get("returnOnAssets"))
            gross_margin = _pct(fd.get("grossMargins"))
            op_margin    = _pct(fd.get("operatingMargins"))
            current_ratio = _v(fd.get("currentRatio"))
            total_debt   = _v(fd.get("totalDebt"))
            total_cash   = _v(fd.get("totalCash"))

            # EBITDA (direct ou reconstitué depuis la marge)
            ebitda_val = _v(fd.get("ebitda"))
            if ebitda_val is None:
                tot_rev  = _v(fd.get("totalRevenue"))
                ebitda_m = _v(fd.get("ebitdaMargins"))
                if tot_rev and ebitda_m and tot_rev > 0:
                    ebitda_val = tot_rev * ebitda_m

            # P/FCF — meilleur indicateur de valorisation (bas = meilleur)
            pfcf = None
            if fcf_abs and market_cap and market_cap > 0 and fcf_abs > 0:
                pfcf = round(market_cap / fcf_abs, 1)

            # Trésorerie nette / capitalisation (%)
            net_cash_yield = None
            if market_cap and market_cap > 0:
                nc = (total_cash or 0) - (total_debt or 0)
                net_cash_yield = round(nc / market_cap * 100, 2)

            # Dette / EBITDA (bas = meilleur)
            debt_cover = None
            if ebitda_val and ebitda_val > 0:
                debt_cover = round((total_debt or 0) / ebitda_val, 2)
            elif total_debt is not None and (total_debt or 0) == 0:
                debt_cover = 0.0

            # Croissance
            rev_growth  = _pct(fd.get("revenueGrowth"))
            earn_growth = _pct(fd.get("earningsGrowth"))

            # Valorisation
            peg_ratio = _v(ks.get("pegRatio"))
            ev_ebitda = _v(ks.get("enterpriseToEbitda"))
            fwd_pe    = _v(ks.get("forwardPE"))

            # Momentum
            mom_52w = _pct(ks.get("52WeekChange"))

            # Révisions EPS (30j proxy 90j)
            eps_revision = None
            et_trend = et.get("trend", [])
            if et_trend:
                eps_rev = et_trend[0].get("epsRevisions", {})
                up30 = _v(eps_rev.get("upLast30days"))  or 0
                dn30 = _v(eps_rev.get("downLast30days")) or 0
                total_rev = (up30 or 0) + (dn30 or 0)
                if total_rev > 0:
                    eps_revision = round((up30 - dn30) / total_rev * 100, 1)

            # Dividende
            div_rate       = _v(sd.get("dividendRate"))
            div_yield_raw  = sd.get("dividendYield")
            div_yield      = _pct(div_yield_raw) if div_yield_raw else None
            ex_div_epoch   = _v(sd.get("exDividendDate"))
            last_div_val   = _v(sd.get("lastDividendValue"))
            last_div_epoch = _v(sd.get("lastDividendDate"))
            ex_div_str  = datetime.fromtimestamp(int(ex_div_epoch)).strftime("%d/%m/%Y") if ex_div_epoch else None
            last_div_str = datetime.fromtimestamp(int(last_div_epoch)).strftime("%d/%m/%Y") if last_div_epoch else None

            return {
                "symbol":        symbol,
                "name":          qt.get("longName") or qt.get("shortName") or "",
                "sector":        sp.get("sector",""),
                "industry":      sp.get("industry",""),
                "country":       sp.get("country",""),
                "currency":      fd.get("financialCurrency",""),
                # Consensus
                "mean_score":    round(float(mean), 2),
                "n_analysts":    int(count),
                "consensus":     fd.get("recommendationKey",""),
                "strong_buy": sb, "buy": b, "hold": h, "sell": se, "strong_sell": ss,
                "total_trend":   total_trend,
                "buy_count":     buy_count,
                "buy_pct":       buy_pct,
                # Prix
                "price":         price,
                "target_price":  target,
                "upside_pct":    upside,
                # Rentabilité
                "roe":           roe,
                "roa":           roa,
                "gross_margin":  gross_margin,
                "op_margin":     op_margin,
                # Croissance
                "rev_growth":    rev_growth,
                "earn_growth":   earn_growth,
                "eps_revision":  eps_revision,
                # Valorisation
                "pfcf":          pfcf,
                "ev_ebitda":     ev_ebitda,
                "peg_ratio":     peg_ratio,
                "fwd_pe":        fwd_pe,
                # Momentum
                "mom_52w":       mom_52w,
                # Santé financière
                "current_ratio": current_ratio,
                "net_cash_yield":net_cash_yield,
                "debt_cover":    debt_cover,
                "total_debt":    total_debt,
                "total_cash":    total_cash,
                # Dividende
                "div_rate":      div_rate,
                "div_yield":     div_yield,
                "ex_div_date":   ex_div_str,
                "last_div_val":  last_div_val,
                "last_div_date": last_div_str,
                # Scores (calculés en post-traitement)
                "composite":     None,
                "grade":         None,
                "conviction":    False,
                "pillar_scores": None,
                "score_detail":  None,
                "firms":         parse_firm_recommendations(uh),
            }

        except Exception:
            if attempt < RETRY_MAX - 1:
                time.sleep(15 * (attempt + 1))
            else:
                return None
    return None

# ── Normalisation centile ──────────────────────────────────────────────────────

def _percentile_ranks(values: list, ascending: bool) -> list[float]:
    n = len(values)
    valid = [(i, v) for i, v in enumerate(values) if v is not None]
    valid_sorted = sorted(valid, key=lambda x: x[1])
    ranks = [50.0] * n
    m = len(valid_sorted)
    for pos, (orig_idx, _) in enumerate(valid_sorted):
        pct = pos / (m - 1) * 100 if m > 1 else 50.0
        ranks[orig_idx] = pct if ascending else (100.0 - pct)
    return ranks

def _sector_percentile_ranks(records: list[dict], values: list, ascending: bool) -> list[float]:
    """Centiles intra-secteur Yahoo Finance (fallback 50 si secteur trop petit)."""
    n = len(records)
    sector_idx: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(records):
        sector = (r.get("sector") or "Unknown").strip() or "Unknown"
        sector_idx[sector].append(i)

    ranks = [50.0] * n
    for sector, indices in sector_idx.items():
        if len(indices) < 4:
            continue
        items = [(i, values[i]) for i in indices]
        valid = [(i, v) for i, v in items if v is not None]
        m = len(valid)
        if m < 2:
            continue
        valid_sorted = sorted(valid, key=lambda x: x[1])
        for pos, (orig_idx, _) in enumerate(valid_sorted):
            pct = pos / (m - 1) * 100 if m > 1 else 50.0
            ranks[orig_idx] = pct if ascending else (100.0 - pct)

    return ranks

# ── Score composite QARP ───────────────────────────────────────────────────────

def compute_composite_scores(records: list[dict]) -> list[dict]:
    if not records:
        return records

    # Nettoyage des valeurs aberrantes
    def clean_pfcf(v):     return v if (v is not None and 0 < v <= 150) else None
    def clean_peg(v):      return v if (v is not None and 0 < v < 50)  else None
    def clean_ev(v):       return v if (v is not None and v > 0)       else None
    def clean_dc(v):       return v if (v is not None and 0 <= v <= 25) else None
    def clean_cr(v):       return v if (v is not None and 0 < v <= 8)  else None
    def clean_gm(v):       return v if (v is not None and -30 <= v <= 100) else None

    CLEANERS = {
        "pfcf": clean_pfcf, "peg_ratio": clean_peg, "ev_ebitda": clean_ev,
        "debt_cover": clean_dc, "current_ratio": clean_cr, "gross_margin": clean_gm,
    }

    # Vecteurs de valeurs nettoyées
    field_vectors: dict[str, list] = {}
    for fname, _, _ in SCORE_WEIGHTS:
        cleaner = CLEANERS.get(fname, lambda x: x)
        field_vectors[fname] = [cleaner(r.get(fname)) for r in records]

    # Centiles blend : 65 % univers global + 35 % secteur
    percentile_matrix: dict[str, list[float]] = {}
    for fname, _, ascending in SCORE_WEIGHTS:
        g = _percentile_ranks(field_vectors[fname], ascending)
        s = _sector_percentile_ranks(records, field_vectors[fname], ascending)
        percentile_matrix[fname] = [
            (1 - SECTOR_BLEND) * gv + SECTOR_BLEND * sv
            for gv, sv in zip(g, s)
        ]

    for i, rec in enumerate(records):
        # Score brut
        composite = sum(w * percentile_matrix[f][i] for f, w, _ in SCORE_WEIGHTS)

        # Scores par pilier (0-100, normalisés à leur poids total)
        pillar_scores: dict[str, float] = {}
        for pname, pfields, _ in PILLARS:
            tw = sum(w for _, w in pfields)
            ps = sum(w * percentile_matrix[f][i] for f, w in pfields) / tw if tw > 0 else 50.0
            pillar_scores[pname] = round(ps, 1)

        score_detail = {f: round(percentile_matrix[f][i], 1) for f, _, _ in SCORE_WEIGHTS}

        # ── QARP : rentabilité-croissance ≥ 65 ET valorisation ≥ 60 ──────────
        rent = pillar_scores.get("Rentabilité", 50)
        croi = pillar_scores.get("Croissance",  50)
        valo = pillar_scores.get("Valorisation", 50)
        qual_growth = rent * 0.60 + croi * 0.40   # blend qualité+croissance
        is_qarp = qual_growth >= 65 and valo >= 60

        # Pénalité value-trap : si la rentabilité est trop faible, cap le score
        if rent < 30:
            composite = min(composite, 50.0)

        rec["composite"]    = round(composite, 1)
        rec["grade"]        = score_to_grade(composite)
        rec["conviction"]   = is_qarp
        rec["pillar_scores"]= pillar_scores
        rec["score_detail"] = score_detail

    return records

# ── Enrichissement cabinets ────────────────────────────────────────────────────

def enrich_with_firms() -> pd.DataFrame:
    records = load_cache()
    if records is None:
        print("Aucun cache v2. Lancez d'abord le screener sans --enrich.")
        sys.exit(1)
    already = sum(1 for r in records if r.get("firms") is not None)
    if already == len(records):
        print(f"Cache déjà enrichi ({already} actions).")
        records = compute_composite_scores(records)
        save_cache(records)
        return pd.DataFrame(records)
    session = YahooSession()
    session.setup()
    print(f"\nEnrichissement {len(records)} actions...")
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
    print("\n  Enrichissement terminé.")
    records = compute_composite_scores(records)
    save_cache(records)
    return pd.DataFrame(records)

# ── Screener principal ─────────────────────────────────────────────────────────

def run_screener(force_refresh: bool = False, min_analysts: int = MIN_ANALYSTS) -> pd.DataFrame:
    if not force_refresh:
        records = load_cache()
        if records is not None:
            if any(r.get("composite") is None for r in records):
                records = compute_composite_scores(records)
                save_cache(records)
            return pd.DataFrame(records)

    session = YahooSession()
    session.setup()
    est = len(ALL_TICKERS) * DELAY_S / 60
    print(f"\nRécupération {len(ALL_TICKERS)} actions PEA — algorithme QARP v2")
    print(f"Délai : {DELAY_S}s · Durée estimée : ~{est:.0f} min\n")

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
        print(f"  {errors} erreurs / données manquantes.")

    records = compute_composite_scores(records)
    save_cache(records)
    print(f"  Cache v2 → {CACHE_FILE}\n")
    return pd.DataFrame(records)

# ── Affichage terminal ─────────────────────────────────────────────────────────

CONSENSUS_LABEL = {
    "strong_buy":"ACHAT FORT","buy":"ACHAT","hold":"NEUTRE",
    "underperform":"SOUS-PERF","sell":"VENTE","strong_sell":"VENTE FORTE",
}

def display_results(df: pd.DataFrame, top_n: int, min_composite: float) -> None:
    if df.empty:
        print("Aucun résultat.")
        return
    df = df.copy()
    if "composite" in df.columns:
        df = df[df["composite"].notna() & (df["composite"] >= min_composite)]
        df = df.sort_values("composite", ascending=False)
    df = df.head(top_n).reset_index(drop=True)
    W = 160
    print()
    print("=" * W)
    print(f"{'TOP PEA — ALGORITHME QARP v2  (Quality at a Reasonable Price)':^{W}}")
    print(f"{'Rent.(30%) · Crois.(20%) · Valo.(25%) · Mom.(15%) · Santé(10%)  ·  blend 65 % univers / 35 % secteur':^{W}}")
    print("=" * W)
    print(f"{'#':>3}  {'Sym':<12} {'Nom':<28}  {'Score':>6} {'G':>3} {'QARP':>5}"
          f"  {'Rent':>5} {'Croi':>5} {'Valo':>5} {'Mom':>5} {'Snt':>5}"
          f"  {'P/FCF':>7} {'Upside':>7}  Consensus")
    print("-" * W)
    for idx, row in df.iterrows():
        ps    = row.get("pillar_scores") or {}
        qarp  = " ★QARP" if row.get("conviction") else "      "
        pfcf  = row.get("pfcf")
        pfcf_s = f"{pfcf:.1f}x" if pfcf else "  N/A "
        up    = row.get("upside_pct")
        up_s  = (f"+{up:.1f}%" if up and up > 0 else f"{up:.1f}%" if up else "  N/A ").rjust(7)
        label = CONSENSUS_LABEL.get(str(row.get("consensus","")).lower(), str(row.get("consensus","")).upper())
        print(
            f"{idx+1:>3}  {str(row['symbol']):<12} {str(row.get('name',''))[:27]:<28}"
            f"  {row.get('composite',0):>6.1f} {row.get('grade','?'):>3}{qarp}"
            f"  {ps.get('Rentabilité',0):>5.0f}"
            f" {ps.get('Croissance',0):>5.0f}"
            f" {ps.get('Valorisation',0):>5.0f}"
            f" {ps.get('Momentum',0):>5.0f}"
            f" {ps.get('Santé fin.',0):>5.0f}"
            f"  {pfcf_s:>7} {up_s}  {label}"
        )
    print("-" * W)
    n_qarp = int(df["conviction"].sum()) if "conviction" in df.columns else 0
    print(f"\n  {len(df)} actions | score ≥ {min_composite} | {n_qarp} QARP | {datetime.now().strftime('%d/%m/%Y %H:%M')}\n")

# ── Génération rapport HTML ────────────────────────────────────────────────────

def generate_html_report(output: Path) -> None:
    if not CACHE_FILE.exists():
        print("  Aucun cache v2 pour le rapport HTML.")
        return

    cache     = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    records   = cache["records"]
    cached_at = datetime.fromisoformat(cache["cached_at"]).strftime("%d/%m/%Y %H:%M")
    generated = datetime.now().strftime("%d/%m/%Y %H:%M")

    # Recalcul si nécessaire (cache sans scores v2)
    if any(r.get("pillar_scores") is None for r in records):
        records = compute_composite_scores(records)

    records = sorted(records, key=lambda r: r.get("composite") or 0, reverse=True)

    # KPI
    n_qarp   = sum(1 for r in records if r.get("conviction"))
    n_top    = sum(1 for r in records if r.get("grade") in ("S","A+","A"))
    n_sb     = sum(1 for r in records if r.get("consensus","").lower() == "strong_buy")
    n_buy    = sum(1 for r in records if r.get("consensus","").lower() == "buy")
    n_hold   = sum(1 for r in records if r.get("consensus","").lower() == "hold")
    n_sell   = sum(1 for r in records if r.get("consensus","").lower() in ("sell","strong_sell","underperform"))
    n_cab    = sum(1 for r in records if r.get("firms"))
    sectors  = sorted({(r.get("sector") or "").strip() for r in records if (r.get("sector") or "").strip()})
    sector_opts = "".join(f'<option value="{s}">{s}</option>' for s in sectors)

    # ── Helpers HTML ───────────────────────────────────────────────────────────

    GRADE_COLOR = {
        "strong_buy":"#16a34a","buy":"#22c55e","hold":"#eab308",
        "sell":"#f97316","strong_sell":"#ef4444","unknown":"#64748b",
    }
    GRADE_LABEL = {
        "strong_buy":"Achat Fort","buy":"Achat","hold":"Neutre",
        "sell":"Vente","strong_sell":"Vente Forte","unknown":"—",
    }
    ACTION_ICON = {
        "up":("▲","#22c55e"),"down":("▼","#ef4444"),
        "init":("★","#38bdf8"),"reit":("=","#94a3b8"),"main":("=","#94a3b8"),
    }

    def _sc(v):
        return _score_color(v)

    def _compbar(score):
        if score is None: return '<div class="comp-na">N/A</div>'
        c = _sc(score)
        p = f"{score:.0f}"
        return (f'<div class="comp-wrap">'
                f'<div class="comp-bar" style="width:{p}%;background:{c}"></div>'
                f'<span class="comp-num" style="color:{c}">{p}'
                f'<span class="comp-max">/100</span></span></div>')

    def _gbadge(grade):
        if not grade: return '<span class="grade-na">—</span>'
        c = COMP_GRADE_COLOR.get(grade, "#64748b")
        return f'<div class="grade-badge" style="background:{c}20;color:{c};border:2px solid {c}60">{grade}</div>'

    def _qarp_badge(is_qarp):
        if not is_qarp: return '<td class="center qarp-cell"></td>'
        return '<td class="center qarp-cell"><span class="qarp-badge" title="Quality at a Reasonable Price&#10;Rentabilité+Croissance ≥ 65 · Valorisation ≥ 60">★ QARP</span></td>'

    def _pillar_mini(ps):
        if not ps: return '<td class="center"><div class="pm-row">—</div></td>'
        items = ""
        for pname, _, _ in PILLARS:
            score = ps.get(pname, 50)
            c = _sc(score)
            abbr = PILLAR_ABBREV.get(pname, pname[0])
            items += (f'<span class="pm" style="background:{c}" '
                      f'title="{pname}: {score:.0f}/100">{abbr}</span>')
        return f'<td class="center"><div class="pm-row">{items}</div></td>'

    def _buybar(pct_val):
        pct_val = pct_val or 0
        c = "#16a34a" if pct_val >= 70 else ("#eab308" if pct_val >= 50 else "#ef4444")
        return (f'<div class="buy-bar-wrap">'
                f'<div class="buy-bar" style="width:{pct_val:.0f}%;background:{c}"></div>'
                f'<span class="buy-num">{pct_val:.0f}%</span></div>')

    def _breakdown(r):
        total = r.get("total_trend") or 0
        if total == 0: return '<div class="center gray">—</div>'
        sb = r.get("strong_buy",0) or 0; b = r.get("buy",0) or 0
        h  = r.get("hold",0) or 0;      se = r.get("sell",0) or 0
        ss = r.get("strong_sell",0) or 0
        parts = []
        for cnt, c, lbl in [(sb,"#16a34a","Achat Fort"),(b,"#22c55e","Achat"),
                            (h,"#eab308","Neutre"),(se,"#f97316","Vente"),(ss,"#ef4444","Vente Forte")]:
            if cnt > 0:
                parts.append(f'<div class="seg" style="width:{cnt/total*100:.1f}%;background:{c}" title="{lbl}: {cnt}"></div>')
        return f'<div class="breakdown" title="SB:{sb} B:{b} N:{h} V:{se} VF:{ss}">{"".join(parts)}</div>'

    def _upside(val):
        if val is None: return '<td class="center gray">N/A</td>'
        c = "#16a34a" if val > 0 else "#ef4444"
        s = "+" if val > 0 else ""
        return f'<td class="center" style="color:{c};font-weight:600">{s}{val:.1f}%</td>'

    def _pfcf_cell(pfcf):
        if pfcf is None: return '<td class="center gray small">N/A</td>'
        c = "#16a34a" if pfcf < 15 else ("#22c55e" if pfcf < 25 else ("#eab308" if pfcf < 40 else "#ef4444"))
        return f'<td class="center" style="color:{c};font-weight:600">{pfcf:.1f}x</td>'

    def _div_cell(r):
        rate = r.get("div_rate") or r.get("last_div_val")
        ex_d = r.get("ex_div_date") or r.get("last_div_date")
        cur  = r.get("currency","")
        if rate is None: return '<td class="center gray div-cell">—</td>'
        rate_str = f"{rate:.2f} {cur}".strip()
        dy = r.get("div_yield")
        dy_html   = f'<div class="div-yield">({dy:.2f}%)</div>' if dy else ""
        date_html = f'<div class="div-date">{ex_d}</div>' if ex_d else ""
        return f'<td class="center small div-cell"><div class="div-amount">{rate_str}</div>{dy_html}{date_html}</td>'

    def _cbadge(key):
        key = (key or "").lower()
        c = GRADE_COLOR.get(key,"#64748b"); l = GRADE_LABEL.get(key, key.upper())
        return f'<span class="badge" style="background:{c}">{l}</span>'

    def _fbadge(norm):
        c = GRADE_COLOR.get(norm,"#64748b"); l = GRADE_LABEL.get(norm, norm)
        return f'<span class="firm-badge" style="background:{c}20;color:{c};border:1px solid {c}40">{l}</span>'

    def _pillar_section(ps, sd):
        if not ps:
            return '<div class="sd-empty">Score non calculé — relancez avec <code>--refresh</code>.</div>'
        html_parts = ['<div class="pillar-section">']
        for pname, pfields, pcolor in PILLARS:
            pscore = ps.get(pname, 50)
            c = _sc(pscore)
            tw = sum(w for _, w in pfields)
            wpct = round(tw * 100)
            factor_parts = []
            if sd:
                for f, _ in pfields:
                    fval = sd.get(f)
                    lbl  = FACTOR_LABELS.get(f, f)
                    if fval is not None:
                        fc = _sc(fval)
                        factor_parts.append(
                            f'<span style="color:{fc}">{lbl}: <b>{fval:.0f}</b></span>'
                        )
                    else:
                        factor_parts.append(f'<span class="gray">{lbl}: N/A</span>')
            factors_html = ' &nbsp;·&nbsp; '.join(factor_parts)
            html_parts.append(
                f'<div class="pi-item">'
                f'<div class="pi-header">'
                f'<span class="pi-name" style="color:{pcolor}">{pname}</span>'
                f'<span class="pi-weight">{wpct}%</span>'
                f'<span class="pi-score" style="color:{c}">{pscore:.0f}'
                f'<span class="pi-max">/100</span></span>'
                f'</div>'
                f'<div class="pi-bar-wrap"><div class="pi-bar" style="width:{pscore:.0f}%;background:{c}"></div></div>'
                f'<div class="pi-factors">{factors_html}</div>'
                f'</div>'
            )
        html_parts.append('</div>')
        return "".join(html_parts)

    def _detail_row(idx, firms, ps, sd):
        pillar_sec = _pillar_section(ps, sd)
        if not firms:
            firms_html = '<div class="no-firms">Aucune recommandation de cabinet (180 j).</div>'
        else:
            rows_f = ""
            for f in firms:
                norm  = f.get("normalized","unknown")
                icon, ic = ACTION_ICON.get(f.get("action",""), ("·","#64748b"))
                from_g = f.get("from_grade","")
                from_html = f' <span class="from-grade">depuis {from_g}</span>' if from_g else ""
                rows_f += (f'<tr class="firm-row">'
                           f'<td class="firm-name">{f["firm"]}</td>'
                           f'<td>{_fbadge(norm)}{from_html}</td>'
                           f'<td class="center" style="color:{ic}">{icon} {f.get("action_fr","")}</td>'
                           f'<td class="center gray">{f.get("date_str","")}</td></tr>')
            counts = Counter(f.get("normalized","unknown") for f in firms)
            summary = " &nbsp;·&nbsp; ".join(
                f'<span style="color:{GRADE_COLOR[n]};font-weight:600">{GRADE_LABEL[n]}: {counts[n]}</span>'
                for n in ["strong_buy","buy","hold","sell","strong_sell"] if counts.get(n,0)
            )
            firms_html = (f'<div class="firms-header">{len(firms)} cabinets &nbsp;·&nbsp; {summary}</div>'
                          f'<table class="firms-table"><thead><tr>'
                          f'<th>Cabinet</th><th>Recommandation</th><th>Action</th><th>Date</th>'
                          f'</tr></thead><tbody>{rows_f}</tbody></table>')
        return (f'<tr id="detail-{idx}" class="detail-row" style="display:none">'
                f'<td colspan="14">'
                f'<div class="detail-container">'
                f'<div class="detail-pillars">{pillar_sec}</div>'
                f'<div class="detail-firms">{firms_html}</div>'
                f'</div></td></tr>')

    # ── Génération des lignes ──────────────────────────────────────────────────
    rows_html = ""
    for i, r in enumerate(records):
        name   = (r.get("name") or r["symbol"])[:38]
        sector = r.get("sector","") or ""
        comp   = r.get("composite")
        grade  = r.get("grade","")
        ps     = r.get("pillar_scores") or {}
        sd     = r.get("score_detail")
        firms  = r.get("firms") or []
        conv   = r.get("conviction", False)
        price  = r.get("price"); target = r.get("target_price"); cur = r.get("currency","")
        buy_p  = r.get("buy_pct",0) or 0
        pfcf   = r.get("pfcf")
        upside = r.get("upside_pct")

        ps_str = f"{ps.get('Rentabilité',0):.0f}/{ps.get('Croissance',0):.0f}/{ps.get('Valorisation',0):.0f}/{ps.get('Momentum',0):.0f}/{ps.get('Santé fin.',0):.0f}"
        ps_val = {k: f"{v:.0f}" for k, v in ps.items()}

        price_td = (f'<div class="price-cur">{price:.2f}</div>'
                    f'<div class="price-tgt">{target:.2f} <span class="price-ccy">{cur}</span></div>'
                    if price else '<div class="price-cur gray">N/A</div>')

        tbtn = (f'<button class="toggle-btn" onclick="event.stopPropagation();toggleDetail({i})" '
                f'title="{len(firms)} cabinets">&#9658; {len(firms)}</button>'
                if firms else '<span class="no-btn">—</span>')

        rows_html += (
            f'<tr class="main-row" '
            f'data-composite="{comp or 0}" '
            f'data-sector="{sector}" '
            f'data-grade="{grade}" '
            f'data-symbol="{r["symbol"]}" '
            f'data-pfcf="{pfcf or 0}" '
            f'data-upside="{upside or 0}" '
            f'data-buy="{buy_p}" '
            f'data-rent="{ps.get("Rentabilité",0):.0f}" '
            f'data-croi="{ps.get("Croissance",0):.0f}" '
            f'data-valo="{ps.get("Valorisation",0):.0f}" '
            f'data-mom="{ps.get("Momentum",0):.0f}" '
            f'data-sante="{ps.get("Santé fin.",0):.0f}" '
            f'data-qarp="{1 if conv else 0}" '
            f'onclick="toggleDetail({i})" style="cursor:pointer">'
            f'<td class="center rank">{i+1}</td>'
            f'<td class="comp-cell">{_compbar(comp)}</td>'
            f'<td class="center grade-cell">{_gbadge(grade)}</td>'
            f'{_qarp_badge(conv)}'
            f'<td><span class="ticker">{r["symbol"]}</span></td>'
            f'<td class="name-cell" title="{r.get("name","")}">'
            f'<div class="name">{name}</div>'
            f'<div class="meta">{sector}</div></td>'
            f'{_pillar_mini(ps)}'
            f'<td>{_buybar(buy_p)}</td>'
            f'{_pfcf_cell(pfcf)}'
            f'{_upside(upside)}'
            f'<td class="center small price-cell">{price_td}</td>'
            f'{_div_cell(r)}'
            f'<td class="center">{_cbadge(r.get("consensus",""))}</td>'
            f'<td class="center">{tbtn}</td>'
            f'</tr>'
            f'{_detail_row(i, firms, ps, sd)}'
        )

    # ── Template HTML ──────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PEA Screener v2 — QARP</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
          background: #0f172a; color: #e2e8f0; font-size: 13px; }}
  header {{ background: linear-gradient(135deg, #1c1e0f 0%, #0f172a 100%);
            padding: 32px 40px 24px; border-bottom: 2px solid #f59e0b40; }}
  header h1 {{ font-size: 26px; font-weight: 700; color: #f8fafc; letter-spacing: -0.5px; }}
  header h1 span {{ color: #f59e0b; }}
  header p {{ color: #94a3b8; margin-top: 6px; font-size: 13px; }}
  .algo-bar {{ background: #1c1e0f; border-bottom: 1px solid #f59e0b20;
               padding: 10px 40px; font-size: 11px; color: #78716c; display: flex; gap: 20px; flex-wrap: wrap; }}
  .algo-bar span {{ color: #f59e0b; font-weight: 600; }}
  .kpi-row {{ display: flex; gap: 14px; padding: 20px 40px; flex-wrap: wrap; }}
  .kpi {{ background: #1e293b; border: 1px solid #334155; border-radius: 10px; padding: 14px 20px; flex: 1; min-width: 110px; }}
  .kpi .val {{ font-size: 28px; font-weight: 700; }}
  .kpi .lbl {{ color: #64748b; font-size: 11px; margin-top: 2px; text-transform: uppercase; letter-spacing: .5px; }}
  .kpi.amber .val {{ color: #f59e0b; }} .kpi.green .val {{ color: #22c55e; }}
  .kpi.lime .val {{ color: #84cc16; }} .kpi.yellow .val {{ color: #eab308; }}
  .kpi.red .val {{ color: #f87171; }} .kpi.blue .val {{ color: #38bdf8; }}
  .kpi.purple .val {{ color: #a78bfa; }} .kpi.gold .val {{ color: #f59e0b; }}
  .filter-bar {{ padding: 12px 40px; display: flex; gap: 10px; flex-wrap: wrap; align-items: center;
                 background: #1e293b; border-bottom: 1px solid #334155; }}
  .filter-bar label {{ font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: .5px; }}
  .filter-bar select {{ background: #0f172a; border: 1px solid #334155; color: #e2e8f0;
                        padding: 5px 10px; border-radius: 6px; font-size: 12px; cursor: pointer; }}
  .filter-bar select:focus {{ outline: none; border-color: #f59e0b; }}
  .fbtn {{ background: #0f172a; border: 1px solid #334155; color: #94a3b8;
           padding: 5px 12px; border-radius: 6px; font-size: 12px; cursor: pointer; transition: all .15s; white-space: nowrap; }}
  .fbtn:hover {{ border-color: #64748b; color: #e2e8f0; }}
  .fbtn.active {{ background: #f59e0b20; border-color: #f59e0b; color: #f59e0b; font-weight: 600; }}
  .sep {{ color: #334155; }}
  .pillar-legend {{ padding: 10px 40px; display: flex; gap: 16px; flex-wrap: wrap; align-items: center;
                    font-size: 11px; border-bottom: 1px solid #1e293b; }}
  .pl-item {{ display: flex; align-items: center; gap: 5px; }}
  .pl-dot {{ width: 10px; height: 10px; border-radius: 2px; }}
  .table-wrap {{ padding: 0 40px 40px; overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
  thead tr {{ background: #1e293b; }}
  thead th {{ padding: 9px 10px; text-align: left; color: #94a3b8; font-size: 11px;
              text-transform: uppercase; letter-spacing: .6px; position: sticky; top: 0;
              background: #1e293b; white-space: nowrap; z-index: 10; }}
  thead th.sortable {{ cursor: pointer; user-select: none; }}
  thead th.sortable:hover {{ color: #f59e0b; }}
  .sort-arrow {{ color: #475569; margin-left: 3px; }}
  .main-row {{ border-bottom: 1px solid #1e293b; transition: background .12s; }}
  .main-row:hover {{ background: #1e293b; }}
  .main-row.open {{ background: #1a1c0e; border-bottom: none; }}
  td {{ padding: 8px 10px; vertical-align: middle; }}
  .rank {{ color: #475569; font-weight: 600; width: 30px; }}
  .ticker {{ font-family: monospace; font-weight: 700; color: #f59e0b; font-size: 13px; }}
  .name-cell .name {{ font-weight: 500; color: #e2e8f0; white-space: nowrap; overflow: hidden;
                      text-overflow: ellipsis; max-width: 200px; }}
  .name-cell .meta {{ color: #64748b; font-size: 11px; margin-top: 1px; }}
  .center {{ text-align: center; }} .gray {{ color: #64748b; }} .small {{ font-size: 12px; }}
  .comp-cell {{ min-width: 140px; }}
  .comp-wrap {{ display: flex; align-items: center; gap: 6px; }}
  .comp-bar {{ height: 8px; border-radius: 4px; flex-shrink: 0; min-width: 4px; }}
  .comp-num {{ font-weight: 700; font-size: 13px; white-space: nowrap; }}
  .comp-max {{ font-size: 10px; opacity: .5; font-weight: 400; }}
  .comp-na {{ color: #475569; font-size: 12px; font-style: italic; }}
  .grade-cell {{ width: 48px; }}
  .grade-badge {{ display: inline-flex; align-items: center; justify-content: center;
                  width: 38px; height: 26px; border-radius: 7px; font-size: 13px; font-weight: 800; }}
  .grade-na {{ color: #334155; }}
  .qarp-cell {{ width: 72px; }}
  .qarp-badge {{ display: inline-block; background: #f59e0b20; color: #f59e0b;
                 border: 1px solid #f59e0b60; padding: 2px 7px; border-radius: 12px;
                 font-size: 11px; font-weight: 700; white-space: nowrap; cursor: default; }}
  .pm-row {{ display: flex; gap: 2px; justify-content: center; }}
  .pm {{ display: inline-flex; align-items: center; justify-content: center;
         width: 18px; height: 18px; border-radius: 3px; font-size: 10px; font-weight: 700;
         color: #fff; cursor: default; }}
  .buy-bar-wrap {{ display: flex; align-items: center; gap: 7px; min-width: 90px; }}
  .buy-bar {{ height: 6px; border-radius: 3px; flex-shrink: 0; }}
  .buy-num {{ font-weight: 600; font-size: 12px; color: #cbd5e1; }}
  .breakdown {{ display: flex; height: 7px; border-radius: 3px; overflow: hidden; width: 90px; gap: 1px; }}
  .seg {{ height: 100%; flex-shrink: 0; }}
  .badge {{ display: inline-block; padding: 3px 8px; border-radius: 20px; font-size: 11px;
            font-weight: 600; color: #fff; white-space: nowrap; }}
  .price-cell {{ min-width: 90px; }}
  .price-cur {{ font-weight: 500; color: #e2e8f0; font-size: 12px; }}
  .price-tgt {{ color: #64748b; font-size: 11px; margin-top: 1px; }}
  .price-ccy {{ color: #475569; }}
  .div-cell {{ min-width: 85px; }}
  .div-amount {{ font-weight: 500; color: #e2e8f0; font-size: 12px; }}
  .div-yield {{ color: #22c55e; font-size: 11px; }}
  .div-date {{ color: #64748b; font-size: 11px; }}
  .toggle-btn {{ background: #1e293b; border: 1px solid #334155; color: #94a3b8;
                 padding: 3px 7px; border-radius: 5px; font-size: 11px; cursor: pointer;
                 white-space: nowrap; transition: all .15s; }}
  .toggle-btn:hover {{ background: #334155; color: #e2e8f0; }}
  .toggle-btn.active {{ background: #f59e0b20; border-color: #f59e0b; color: #f59e0b; }}
  .no-btn {{ color: #334155; font-size: 12px; }}
  .detail-row td {{ padding: 0; background: #0a0e04; border-bottom: 2px solid #f59e0b30; }}
  .detail-container {{ display: flex; gap: 0; padding: 20px 20px 24px 48px; flex-wrap: wrap; }}
  .detail-pillars {{ flex: 0 0 auto; min-width: 360px; max-width: 480px;
                     padding-right: 28px; border-right: 1px solid #1e293b; }}
  .detail-firms {{ flex: 1 1 280px; padding-left: 24px; min-width: 260px; }}
  .pillar-section {{ display: flex; flex-direction: column; gap: 11px; }}
  .pi-item {{ display: flex; flex-direction: column; gap: 4px; }}
  .pi-header {{ display: flex; align-items: baseline; gap: 6px; }}
  .pi-name {{ font-size: 12px; font-weight: 700; }}
  .pi-weight {{ font-size: 10px; color: #475569; }}
  .pi-score {{ margin-left: auto; font-weight: 800; font-size: 15px; }}
  .pi-max {{ font-size: 10px; opacity: .45; font-weight: 400; }}
  .pi-bar-wrap {{ height: 5px; background: #1e293b; border-radius: 3px; overflow: hidden; }}
  .pi-bar {{ height: 100%; border-radius: 3px; }}
  .pi-factors {{ font-size: 11px; color: #64748b; line-height: 1.6; }}
  .sd-empty {{ color: #475569; font-style: italic; font-size: 12px; }}
  .sd-empty code {{ background: #1e293b; padding: 1px 6px; border-radius: 4px; }}
  .firms-header {{ font-size: 12px; color: #94a3b8; margin-bottom: 10px; }}
  .no-firms {{ color: #475569; font-style: italic; font-size: 12px; padding: 8px 0; }}
  .firms-table {{ width: 100%; border-collapse: collapse; max-width: 600px; }}
  .firms-table thead th {{ background: #1e293b; color: #64748b; font-size: 11px; text-transform: uppercase;
                           letter-spacing: .5px; padding: 6px 10px; text-align: left;
                           position: relative; top: auto; }}
  .firm-row {{ border-bottom: 1px solid #1e293b30; }}
  .firm-row:hover {{ background: #1e293b30; }}
  .firm-row td {{ padding: 5px 10px; color: #cbd5e1; font-size: 12px; }}
  .firm-name {{ font-weight: 500; color: #e2e8f0; min-width: 150px; }}
  .firm-badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }}
  .from-grade {{ color: #475569; font-size: 11px; }}
  footer {{ text-align: center; padding: 18px; color: #334155; font-size: 11px; border-top: 1px solid #1e293b; }}
</style>
</head>
<body>
<header>
  <h1>PEA Screener v2 &mdash; <span>Algorithme QARP</span></h1>
  <p>Quality at a Reasonable Price &bull; Source : Yahoo Finance &bull;
     Données du {cached_at} &bull; Rapport généré le {generated}</p>
</header>
<div class="algo-bar">
  <span>Rentabilité 30 %</span> ROE · ROA · Marge brute · Marge opérat. &nbsp;|&nbsp;
  <span>Croissance 20 %</span> Croiss. CA · Croiss. bén. · Rév. EPS &nbsp;|&nbsp;
  <span>Valorisation 25 %</span> P/FCF · EV/EBITDA · PEG &nbsp;|&nbsp;
  <span>Momentum 15 %</span> Perf 52s · % Achat · Upside &nbsp;|&nbsp;
  <span>Santé 10 %</span> Ratio cour. · Trés. nette · Dette/EBITDA &nbsp;|&nbsp;
  <span>Blend</span> 65 % univers / 35 % secteur
</div>
<div class="kpi-row">
  <div class="kpi blue"><div class="val">{len(records)}</div><div class="lbl">Actions analysées</div></div>
  <div class="kpi amber"><div class="val">{n_qarp}</div><div class="lbl">★ QARP</div></div>
  <div class="kpi gold"><div class="val">{n_top}</div><div class="lbl">Grades S / A+ / A</div></div>
  <div class="kpi green"><div class="val">{n_sb}</div><div class="lbl">Achat Fort</div></div>
  <div class="kpi lime"><div class="val">{n_buy}</div><div class="lbl">Achat</div></div>
  <div class="kpi yellow"><div class="val">{n_hold}</div><div class="lbl">Neutre</div></div>
  <div class="kpi red"><div class="val">{n_sell}</div><div class="lbl">Vente / Sous-perf</div></div>
  <div class="kpi purple"><div class="val">{n_cab}</div><div class="lbl">Avec cabinets</div></div>
</div>
<div class="filter-bar">
  <label>Secteur</label>
  <select id="sector-filter" onchange="applyFilters()">
    <option value="">Tous ({len(records)})</option>
    {sector_opts}
  </select>
  <span class="sep">|</span>
  <button class="fbtn" id="qarp-btn" onclick="toggleQarp()">★ QARP uniquement</button>
  <span class="sep">|</span>
  <label>Grade min.</label>
  <button class="fbtn active" id="g-all" onclick="filterGrade(this,'')">Tous</button>
  <button class="fbtn" id="g-a" onclick="filterGrade(this,'A')">≥ A</button>
  <button class="fbtn" id="g-bp" onclick="filterGrade(this,'B+')">≥ B+</button>
  <button class="fbtn" id="g-b" onclick="filterGrade(this,'B')">≥ B</button>
  <span class="sep">|</span>
  <button class="fbtn" onclick="resetFilters()">&#8635; Réinitialiser</button>
</div>
<div class="pillar-legend">
  <span style="color:#94a3b8;font-size:11px">Piliers :</span>
  <div class="pl-item"><div class="pl-dot" style="background:#38bdf8"></div><span style="color:#38bdf8">R = Rentabilité</span></div>
  <div class="pl-item"><div class="pl-dot" style="background:#34d399"></div><span style="color:#34d399">C = Croissance</span></div>
  <div class="pl-item"><div class="pl-dot" style="background:#f59e0b"></div><span style="color:#f59e0b">V = Valorisation</span></div>
  <div class="pl-item"><div class="pl-dot" style="background:#a78bfa"></div><span style="color:#a78bfa">M = Momentum</span></div>
  <div class="pl-item"><div class="pl-dot" style="background:#fb7185"></div><span style="color:#fb7185">S = Santé fin.</span></div>
  <span style="color:#475569;margin-left:8px">· Cliquez sur une ligne pour le détail ·</span>
  <span style="color:#475569">P/FCF : <span style="color:#16a34a">&lt;15x</span> excellent · <span style="color:#22c55e">&lt;25x</span> bon · <span style="color:#eab308">&lt;40x</span> correct · <span style="color:#ef4444">≥40x</span> cher</span>
</div>
<div class="table-wrap">
  <table>
    <thead><tr>
      <th>#</th>
      <th class="sortable" onclick="sortBy('composite')">Score composite <span class="sort-arrow" id="arr-composite">↓</span></th>
      <th>Note</th>
      <th>QARP</th>
      <th class="sortable" onclick="sortBy('symbol')">Symbole <span class="sort-arrow" id="arr-symbol">↕</span></th>
      <th>Société / Secteur</th>
      <th class="center" title="R=Rentab. C=Crois. V=Valo. M=Mom. S=Santé">Piliers</th>
      <th class="sortable center" onclick="sortBy('buy')">% Achat <span class="sort-arrow" id="arr-buy">↕</span></th>
      <th class="sortable center" onclick="sortBy('pfcf')">P/FCF <span class="sort-arrow" id="arr-pfcf">↕</span></th>
      <th class="sortable center" onclick="sortBy('upside')">Upside <span class="sort-arrow" id="arr-upside">↕</span></th>
      <th class="center">Prix / Cible</th>
      <th class="center">Dividende</th>
      <th class="center">Consensus</th>
      <th class="center">Cabinets</th>
    </tr></thead>
    <tbody id="tbody">{rows_html}</tbody>
  </table>
</div>
<footer>
  QARP v2 : Rentab.(30%) · Crois.(20%) · Valo.(25%) · Mom.(15%) · Santé(10%) &bull;
  16 facteurs · blend 65 % univers / 35 % secteur Yahoo Finance &bull;
  Yahoo Finance · {generated} &bull;
  Pas un conseil en investissement — à titre informatif uniquement.
</footer>
<script>
  let filterState = {{ sector: '', qarp: false, grade: '' }};
  let sortState   = {{ col: 'composite', asc: false }};

  const GRADE_ORDER = ['S','A+','A','B+','B','C','D','F'];

  function getPairs() {{
    const tb = document.getElementById('tbody');
    const all = Array.from(tb.children);
    const pairs = [];
    for (let i = 0; i < all.length; i++) {{
      if (all[i].classList.contains('main-row')) {{
        const det = all[i+1];
        if (det && det.classList.contains('detail-row')) {{
          pairs.push([all[i], det]);
          i++;
        }}
      }}
    }}
    return pairs;
  }}

  function sortBy(col) {{
    if (sortState.col === col) {{
      sortState.asc = !sortState.asc;
    }} else {{
      sortState.col = col;
      sortState.asc = (col === 'symbol');
    }}
    document.querySelectorAll('.sort-arrow').forEach(a => a.textContent = '↕');
    const arr = document.getElementById('arr-' + col);
    if (arr) arr.textContent = sortState.asc ? '↑' : '↓';
    applySort();
  }}

  function applySort() {{
    const pairs = getPairs();
    const col = sortState.col;
    const isStr = (col === 'symbol');
    pairs.sort((a, b) => {{
      if (isStr) {{
        const av = (a[0].dataset[col] || '').toLowerCase();
        const bv = (b[0].dataset[col] || '').toLowerCase();
        return sortState.asc ? av.localeCompare(bv) : bv.localeCompare(av);
      }}
      const av = parseFloat(a[0].dataset[col]) || 0;
      const bv = parseFloat(b[0].dataset[col]) || 0;
      return sortState.asc ? av - bv : bv - av;
    }});
    const tb = document.getElementById('tbody');
    pairs.forEach(([m, d]) => {{ tb.appendChild(m); tb.appendChild(d); }});
    updateRanks();
  }}

  function applyFilters() {{
    filterState.sector = document.getElementById('sector-filter').value;
    applyFiltersToRows();
  }}

  function toggleQarp() {{
    filterState.qarp = !filterState.qarp;
    document.getElementById('qarp-btn').classList.toggle('active', filterState.qarp);
    applyFiltersToRows();
  }}

  function filterGrade(btn, grade) {{
    filterState.grade = grade;
    document.querySelectorAll('[id^="g-"]').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    applyFiltersToRows();
  }}

  function applyFiltersToRows() {{
    getPairs().forEach(([m, d]) => {{
      let show = true;
      if (filterState.sector && m.dataset.sector !== filterState.sector) show = false;
      if (filterState.qarp && m.dataset.qarp !== '1') show = false;
      if (filterState.grade) {{
        const ri = GRADE_ORDER.indexOf(m.dataset.grade);
        const mi = GRADE_ORDER.indexOf(filterState.grade);
        if (ri === -1 || ri > mi) show = false;
      }}
      m.style.display = show ? '' : 'none';
      if (!show) d.style.display = 'none';
    }});
    updateRanks();
  }}

  function resetFilters() {{
    filterState = {{ sector: '', qarp: false, grade: '' }};
    document.getElementById('sector-filter').value = '';
    document.getElementById('qarp-btn').classList.remove('active');
    document.querySelectorAll('[id^="g-"]').forEach(b => b.classList.remove('active'));
    document.getElementById('g-all').classList.add('active');
    getPairs().forEach(([m, d]) => {{ m.style.display = ''; }});
    updateRanks();
  }}

  function updateRanks() {{
    let rank = 1;
    getPairs().forEach(([m]) => {{
      if (m.style.display !== 'none') m.querySelector('.rank').textContent = rank++;
    }});
  }}

  function toggleDetail(idx) {{
    const all = document.querySelectorAll('.detail-row');
    const mains = document.querySelectorAll('.main-row');
    const btns  = document.querySelectorAll('.toggle-btn');
    const row  = document.getElementById('detail-' + idx);
    const main = mains[idx];
    const btn  = btns[idx];
    const isOpen = row && row.style.display !== 'none';
    all.forEach(r => r.style.display = 'none');
    mains.forEach(r => r.classList.remove('open'));
    btns.forEach(b => b.classList.remove('active'));
    if (!isOpen && row) {{
      row.style.display = 'table-row';
      if (main) main.classList.add('open');
      if (btn)  btn.classList.add('active');
    }}
  }}
</script>
</body>
</html>"""

    output.write_text(html, encoding="utf-8")
    print(f"  Rapport HTML v2 : {output.resolve()}")

# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    global MIN_ANALYSTS

    parser = argparse.ArgumentParser(
        description="PEA Screener v2 — Algorithme QARP (Quality at a Reasonable Price)")
    parser.add_argument("--refresh",       action="store_true",
                        help="Force mise à jour complète du cache")
    parser.add_argument("--enrich",        action="store_true",
                        help="Ajoute les recommandations par cabinet d'analyse")
    parser.add_argument("--top",           type=int,   default=TOP_N)
    parser.add_argument("--min-score",     type=float, default=0.0,
                        help="Score composite minimum 0-100 (défaut: 0 = tout afficher)")
    parser.add_argument("--min-analysts",  type=int,   default=MIN_ANALYSTS)
    args = parser.parse_args()

    MIN_ANALYSTS = args.min_analysts

    if args.enrich:
        enrich_with_firms()
    else:
        df = run_screener(force_refresh=args.refresh, min_analysts=args.min_analysts)
        display_results(df, top_n=args.top, min_composite=args.min_score)

    print("\nGénération du rapport HTML v2...")
    generate_html_report(Path("pea_report2.html"))


if __name__ == "__main__":
    main()
