#!/usr/bin/env python3
"""
World Screener - les ~190 plus grandes capitalisations mondiales classées par
score composite pondéré multi-facteurs.

Univers : US S&P 500 top 100+, Europe (UE, UK, Suisse), Asie-Pacifique.

Facteurs intégrés :
  1. Qualité fondamentale : ROE, FCF yield, croissance CA, croissance bénéfices
  2. Valorisation         : PEG ratio, EV/EBITDA, upside prix cible
  3. Consensus analyste   : % achat, score moyen
  4. Momentum             : révisions EPS 90j, performance 52 semaines
  5. Bilan                : dette/capitaux

Usage:
    python world_screener.py                   # cache si dispo, sinon refresh
    python world_screener.py --refresh         # force mise à jour complète (~8 min)
    python world_screener.py --enrich          # ajoute les cabinets d'analyse
    python world_screener.py --top 30          # top 30
    python world_screener.py --min-score 60    # score minimum (0-100)
    python world_screener.py --detail          # toutes les métriques

Prérequis:
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

# ── Configuration ─────────────────────────────────────────────────────────────

CACHE_FILE       = Path("world_cache.json")
CACHE_TTL_H      = 20
MIN_ANALYSTS     = 5
TOP_N            = 50
DELAY_S          = 1.5
RETRY_MAX        = 3
BASE_QUERY       = "https://query2.finance.yahoo.com"
FIRMS_LOOKBACK_D = 180

# ── Pondérations du score composite (total = 1.0) ─────────────────────────────
SCORE_WEIGHTS: list[tuple[str, float, bool]] = [
    # Qualité fondamentale (35%)
    ("roe",           0.12, True),
    ("fcf_yield",     0.10, True),
    ("rev_growth",    0.07, True),
    ("earn_growth",   0.06, True),
    # Valorisation (25%)
    ("peg_ratio",     0.12, False),
    ("ev_ebitda",     0.08, False),
    ("upside_pct",    0.05, True),
    # Consensus analyste (20%)
    ("buy_pct",       0.12, True),
    ("inv_score",     0.08, True),
    # Momentum (15%)
    ("eps_revision",  0.09, True),
    ("mom_52w",       0.06, True),
    # Bilan (5%)
    ("debt_equity",   0.05, False),
]

assert abs(sum(w for _, w, _ in SCORE_WEIGHTS) - 1.0) < 1e-9, "Poids invalides"

SCORE_GRADE = [(90,"S"),(80,"A+"),(70,"A"),(60,"B+"),(50,"B"),(40,"C"),(30,"D"),(0,"F")]

def score_to_grade(s: float) -> str:
    for threshold, grade in SCORE_GRADE:
        if s >= threshold:
            return grade
    return "F"

# ── Normalisation grades analyste ─────────────────────────────────────────────

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

# ── Univers mondial ───────────────────────────────────────────────────────────

STOCKS_WORLD: dict[str, list[str]] = {
    # ── États-Unis : top S&P 500 par capitalisation ──────────────────────────
    "US_MEGA": [
        # Magnificent 7 + méga-caps
        "AAPL","MSFT","NVDA","AMZN","GOOGL","META","BRK-B","LLY",
        "TSLA","JPM","V","WMT","MA","AVGO","XOM","UNH",
        # Large caps tech / santé / conso
        "JNJ","COST","HD","PG","NFLX","BAC","CRM","ORCL","ABBV",
        "CVX","MRK","KO","AMD","QCOM","GS","IBM","T","WFC",
        "MS","AMGN","INTU","AMAT","TXN","PEP","ABT","DHR",
        "LIN","NOW","ISRG","HON","RTX","UNP","TMO","SPGI",
        "ETN","BKNG","CAT","COP","CMCSA","VZ","BLK","AXP",
        "DE","LOW","NEE","SCHW","MU","GILD","REGN","VRTX",
        "ZTS","SYK","ELV","ADI","TJX","ADP","CB","NKE",
        "PANW","LRCX","SBUX","KLAC","MCO","ICE","SHW",
        "SNPS","CDNS","CME","WELL","ORLY","ITW","ADSK",
        "GE","MMC","AON","FTNT","PSA","MPC","TGT","PNC",
        "PSX","COF","FI","MSI","RSG","MELI","TMUS","USB",
        "EQIX","AMT","UBER","PYPL","EMR","PH","DLR","CCI",
        "DXCM","IDXX","SPOT","ABNB","SQ","PLTR",
    ],
    # ── Royaume-Uni ───────────────────────────────────────────────────────────
    "UK": [
        "AZN.L","SHEL.L","ULVR.L","GSK.L","BP.L","RIO.L",
        "HSBA.L","BATS.L","DGE.L","REL.L","BA.L","CPG.L",
    ],
    # ── Suisse ───────────────────────────────────────────────────────────────
    "SWISS": [
        "NESN.SW","ROG.SW","NOVN.SW","ABBN.SW","ZURN.SW","CFR.SW","UHR.SW",
    ],
    # ── Europe continentale (UE) ──────────────────────────────────────────────
    "EU_NL": [
        "ASML.AS","ADYEN.AS","INGA.AS","PRX.AS","WKL.AS","NN.AS",
    ],
    "EU_DE": [
        "SAP.DE","SIE.DE","ALV.DE","MUV2.DE","DTE.DE",
        "BMW.DE","MBG.DE","RHM.DE","ADS.DE","BAS.DE","BAYN.DE",
        "HEI.DE","DB1.DE","IFX.DE",
    ],
    "EU_FR": [
        "MC.PA","RMS.PA","OR.PA","TTE.PA","SU.PA",
        "AIR.PA","SAN.PA","EL.PA","AI.PA","BNP.PA",
        "KER.PA","RI.PA","CS.PA","VIV.PA","CAP.PA",
    ],
    "EU_NORD": [
        "NOVO-B.CO","DSV.CO","CARL-B.CO","ORSTED.CO",
        "ERIC-B.ST","VOLV-B.ST","SAND.ST","NESTE.HE",
    ],
    "EU_IT_ES_BE": [
        "RACE.MI","UCG.MI","ISP.MI","ENEL.MI","ENI.MI","LDO.MI",
        "ITX.MC","SAN.MC","BBVA.MC","IBE.MC","TEF.MC",
        "ABI.BR","ARGX.BR","UCB.BR",
    ],
    # ── Asie-Pacifique ────────────────────────────────────────────────────────
    "JAPAN": [
        "7203.T",   # Toyota
        "6758.T",   # Sony
        "6861.T",   # Keyence
        "9984.T",   # SoftBank
        "8035.T",   # Tokyo Electron
        "7974.T",   # Nintendo
        "9432.T",   # NTT
        "9433.T",   # KDDI
        "8306.T",   # Mitsubishi UFJ
        "4519.T",   # Chugai Pharma
    ],
    "KOREA_TAIWAN": [
        "005930.KS",   # Samsung Electronics
        "000660.KS",   # SK Hynix
        "TSM",         # TSMC (US ADR — plus fiable qu'en TWD)
    ],
    "CHINA_HK": [
        "700.HK",      # Tencent
        "9988.HK",     # Alibaba HK
        "3690.HK",     # Meituan
        "1211.HK",     # BYD
        "9618.HK",     # JD.com HK
        "BIDU",        # Baidu US ADR
        "PDD",         # Pinduoduo (US)
        "JD",          # JD.com US ADR
    ],
    "INDIA_OTHER": [
        "INFY",        # Infosys US ADR
        "HDB",         # HDFC Bank US ADR
        "SHOP",        # Shopify (Canada, US-listed)
        "RY",          # Royal Bank of Canada (US-listed)
        "BHP",         # BHP Group (US-listed)
        "NVO",         # Novo Nordisk US ADR
    ],
}

ALL_TICKERS: list[str] = []
_seen: set[str] = set()
for _lst in STOCKS_WORLD.values():
    for _t in _lst:
        if _t not in _seen:
            _seen.add(_t)
            ALL_TICKERS.append(_t)


# ── Cache ─────────────────────────────────────────────────────────────────────

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
                  "Utilisez --refresh pour mettre a jour.")
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
                "agree": ["agree", "agree"], "consentUUID": "default",
                "sessionId": ms.group(1) if ms else "",
                "csrfToken": mc.group(1) if mc else "",
                "originalDoneUrl": "https://finance.yahoo.com/", "namespace": "yahoo",
            }, timeout=20)
            self._session.get("https://finance.yahoo.com/", timeout=20)

    def _get_crumb(self, seed: str = "AAPL") -> str:
        r = self._session.get(f"https://finance.yahoo.com/quote/{seed}/",
                              timeout=20, headers={"Referer": "https://finance.yahoo.com/"})
        r.raise_for_status()
        m = re.search(r'"crumb"\s*:\s*"([^"]{5,30})"', r.text)
        if not m:
            raise RuntimeError("Crumb non trouve dans la page")
        return m.group(1)

    def setup(self) -> None:
        print("Initialisation session Yahoo Finance...")
        self._gdpr_consent()
        self._crumb = self._get_crumb()
        print("  Session prete.")

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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _v(obj) -> float | int | str | None:
    return obj.get("raw") if isinstance(obj, dict) else obj

def _pct(x) -> float | None:
    v = _v(x)
    return round(float(v) * 100, 2) if v is not None else None


# ── Recommandations par cabinet ───────────────────────────────────────────────

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
                "action":    item.get("action", ""),
                "action_fr": ACTION_FR.get(item.get("action", ""), item.get("action", "")),
                "from_grade":(item.get("fromGrade") or "").strip(),
                "date_str":  datetime.fromtimestamp(epoch).strftime("%d/%m/%Y"),
                "epoch":     epoch,
            }
    return sorted(firm_latest.values(), key=lambda x: x["epoch"], reverse=True)


# ── Récupération données par action ───────────────────────────────────────────

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

            # Consensus analyste
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

            # Qualité fondamentale
            roe         = _pct(fd.get("returnOnEquity"))
            rev_growth  = _pct(fd.get("revenueGrowth"))
            earn_growth = _pct(fd.get("earningsGrowth"))
            market_cap  = _v(sd.get("marketCap"))
            fcf_abs     = _v(fd.get("freeCashflow"))
            fcf_yield   = round(fcf_abs / market_cap * 100, 2) if (fcf_abs and market_cap and market_cap > 0) else None

            # Valorisation
            peg_ratio = _v(ks.get("pegRatio"))
            ev_ebitda = _v(ks.get("enterpriseToEbitda"))
            fwd_pe    = _v(ks.get("forwardPE"))

            # Bilan
            debt_equity = _v(fd.get("debtToEquity"))

            # Momentum cours
            mom_52w = _pct(ks.get("52WeekChange"))

            # Révisions EPS (90j)
            eps_revision = None
            et_trend = et.get("trend", [])
            if et_trend:
                eps_rev = et_trend[0].get("epsRevisions", {})
                up30  = _v(eps_rev.get("upLast30days"))  or 0
                dn30  = _v(eps_rev.get("downLast30days")) or 0
                total_rev = (up30 or 0) + (dn30 or 0)
                if total_rev > 0:
                    eps_revision = round((up30 - dn30) / total_rev * 100, 1)

            return {
                "symbol":       symbol,
                "name":         qt.get("longName") or qt.get("shortName") or "",
                "sector":       sp.get("sector", ""),
                "country":      sp.get("country", ""),
                "currency":     fd.get("financialCurrency", ""),
                "mean_score":   round(float(mean), 2),
                "inv_score":    round(6.0 - float(mean), 2),
                "n_analysts":   int(count),
                "consensus":    fd.get("recommendationKey", ""),
                "strong_buy":   sb, "buy": b, "hold": h,
                "sell":         se, "strong_sell": ss,
                "total_trend":  total_trend,
                "buy_count":    buy_count,
                "buy_pct":      buy_pct,
                "price":        price,
                "target_price": target,
                "upside_pct":   upside,
                "roe":          roe,
                "rev_growth":   rev_growth,
                "earn_growth":  earn_growth,
                "fcf_yield":    fcf_yield,
                "peg_ratio":    peg_ratio,
                "ev_ebitda":    ev_ebitda,
                "fwd_pe":       fwd_pe,
                "debt_equity":  debt_equity,
                "mom_52w":      mom_52w,
                "eps_revision": eps_revision,
                "score":        int(count) * (6.0 - float(mean)),
                "composite":    None,
                "grade":        None,
                "firms":        parse_firm_recommendations(uh),
            }

        except Exception:
            if attempt < RETRY_MAX - 1:
                time.sleep(15 * (attempt + 1))
            else:
                return None
    return None


# ── Score composite pondéré ───────────────────────────────────────────────────

def _percentile_ranks(values: list[float | None], ascending: bool) -> list[float]:
    n = len(values)
    valid = [(i, v) for i, v in enumerate(values) if v is not None]
    valid_sorted = sorted(valid, key=lambda x: x[1])
    ranks = [50.0] * n
    m = len(valid_sorted)
    for pos, (orig_idx, _) in enumerate(valid_sorted):
        pct = pos / (m - 1) * 100 if m > 1 else 50.0
        ranks[orig_idx] = pct if ascending else (100.0 - pct)
    return ranks


def compute_composite_scores(records: list[dict]) -> list[dict]:
    n = len(records)
    if n == 0:
        return records

    def clean_peg(v):
        return v if (v is not None and 0 < v < 50) else None
    def clean_ev(v):
        return v if (v is not None and v > 0) else None
    def clean_de(v):
        if v is None: return None
        return v / 100 if v > 20 else v

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

    percentile_matrix: dict[str, list[float]] = {}
    for fname, _, ascending in SCORE_WEIGHTS:
        percentile_matrix[fname] = _percentile_ranks(field_vectors[fname], ascending)

    for i, rec in enumerate(records):
        composite = sum(
            w * percentile_matrix[fname][i]
            for fname, w, _ in SCORE_WEIGHTS
        )
        rec["composite"] = round(composite, 1)
        rec["grade"]     = score_to_grade(composite)
        rec["score_detail"] = {
            fname: round(percentile_matrix[fname][i], 1)
            for fname, _, _ in SCORE_WEIGHTS
        }

    return records


# ── Enrichissement cabinets ───────────────────────────────────────────────────

def enrich_with_firms() -> pd.DataFrame:
    records = load_cache()
    if records is None:
        print("Aucun cache. Lancez d'abord --refresh.")
        sys.exit(1)

    already = sum(1 for r in records if r.get("firms") is not None)
    if already == len(records):
        print(f"Cache deja enrichi ({already} actions).")
        records = compute_composite_scores(records)
        save_cache(records)
        return pd.DataFrame(records)

    session = YahooSession()
    session.setup()
    print(f"\nEnrichissement de {len(records)} actions...")
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

    print(f"\n\n  Enrichissement termine.")
    records = compute_composite_scores(records)
    save_cache(records)
    print(f"  Cache mis a jour -> {CACHE_FILE}\n")
    return pd.DataFrame(records)


# ── Screener principal ────────────────────────────────────────────────────────

def run_screener(force_refresh: bool = False,
                 min_analysts: int = MIN_ANALYSTS) -> pd.DataFrame:

    if not force_refresh:
        records = load_cache()
        if records is not None:
            if any(r.get("composite") is None for r in records):
                records = compute_composite_scores(records)
                save_cache(records)
            return pd.DataFrame(records)

    session = YahooSession()
    session.setup()

    print(f"\nRecuperation donnees pour {len(ALL_TICKERS)} actions mondiales...")
    est = len(ALL_TICKERS) * DELAY_S / 60
    print(f"Delai : {DELAY_S}s/requete  |  Duree estimee : ~{est:.0f} min\n")

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
        print(f"  {errors} sans donnees ou erreur.")

    records = compute_composite_scores(records)
    save_cache(records)
    print(f"  Cache -> {CACHE_FILE}\n")
    return pd.DataFrame(records)


# ── Affichage terminal ────────────────────────────────────────────────────────

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
        print("Aucun resultat.")
        return

    df = df.copy()
    if "composite" in df.columns:
        df = df[df["composite"].notna() & (df["composite"] >= min_composite)]
        df = df.sort_values("composite", ascending=False)
    else:
        df = df.sort_values("score", ascending=False)

    df = df.head(top_n).reset_index(drop=True)
    W = 150 if detail else 120

    print()
    print("=" * W)
    print(f"{'TOP ACTIONS MONDIALES — SCORE COMPOSITE PONDÉRÉ':^{W}}")
    print(f"{'Qualite(35%) · Valorisation(25%) · Consensus(20%) · Momentum(15%) · Bilan(5%)':^{W}}")
    print("=" * W)

    if detail:
        print(f"{'#':>3}  {'Sym':<12} {'Nom':<28}  {'Score':>6} {'Grd':>3}  "
              f"{'Buy%':>5} {'PEG':>6} {'ROE':>6} {'FCF%':>6} "
              f"{'Rev+':>6} {'EPS+':>6} {'Mom':>6}  {'Upside':>7}  Consensus")
    else:
        print(f"{'#':>3}  {'Sym':<12} {'Nom':<28} {'Pays':<14}  "
              f"{'Score':>6} {'Grd':>3}  {'Buy%':>5} {'Upside':>7}  Consensus")
    print("-" * W)

    for idx, row in df.iterrows():
        key   = str(row.get("consensus", "")).lower()
        label = LABEL.get(key, key.upper())
        grade = row.get("grade", "?")
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
            country = str(row.get("country", ""))[:13]
            print(
                f"{idx+1:>3}  {str(row['symbol']):<12} {str(row.get('name',''))[:27]:<28}"
                f" {country:<14}"
                f"  {comp:>6.1f} {grade:>3}"
                f"  {row.get('buy_pct',0):>4.0f}%"
                f" {fmt_pct(row.get('upside_pct')):>7}"
                f"  {label}"
            )

    print("-" * W)
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    print(f"\n  {len(df)} actions | score >= {min_composite} | Source: Yahoo Finance | {now}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    global MIN_ANALYSTS

    parser = argparse.ArgumentParser(
        description="World Screener — score composite multi-facteurs, top capitalisations mondiales")
    parser.add_argument("--refresh",       action="store_true",
                        help="Force mise a jour complete du cache")
    parser.add_argument("--enrich",        action="store_true",
                        help="Ajoute les recommandations par cabinet d'analyse")
    parser.add_argument("--top",           type=int,   default=TOP_N)
    parser.add_argument("--min-composite", type=float, default=0.0)
    parser.add_argument("--min-analysts",  type=int,   default=MIN_ANALYSTS)
    parser.add_argument("--detail",        action="store_true")
    args = parser.parse_args()

    MIN_ANALYSTS = args.min_analysts

    if args.enrich:
        enrich_with_firms()
    else:
        df = run_screener(force_refresh=args.refresh, min_analysts=args.min_analysts)
        display_results(df, top_n=args.top, min_composite=args.min_composite, detail=args.detail)

    print("\nGeneration du rapport HTML...")
    result = subprocess.run([sys.executable, "generate_world_report.py"], capture_output=True, text=True)
    if result.returncode == 0:
        print(result.stdout.strip())
    else:
        print(f"Erreur rapport : {result.stderr.strip()}")


if __name__ == "__main__":
    main()
