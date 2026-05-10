#!/usr/bin/env python3
"""Generate HTML report from pea_cache.json."""
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

CACHE  = Path("pea_cache.json")
OUTPUT = Path("pea_report.html")

with open(CACHE, encoding="utf-8") as f:
    cache = json.load(f)

records   = cache["records"]
cached_at = datetime.fromisoformat(cache["cached_at"]).strftime("%d/%m/%Y %H:%M")
generated = datetime.now().strftime("%d/%m/%Y %H:%M")

has_composite = any(r.get("composite") is not None for r in records)

if has_composite:
    records = sorted(records, key=lambda r: r.get("composite") or 0, reverse=True)
else:
    records = sorted(records, key=lambda r: r.get("score") or 0, reverse=True)

# ── Helpers ────────────────────────────────────────────────────────────────────

GRADE_COLOR = {
    "strong_buy":  "#16a34a",
    "buy":         "#22c55e",
    "hold":        "#eab308",
    "sell":        "#f97316",
    "strong_sell": "#ef4444",
    "unknown":     "#64748b",
}
GRADE_LABEL = {
    "strong_buy":  "Achat Fort",
    "buy":         "Achat",
    "hold":        "Neutre",
    "sell":        "Vente",
    "strong_sell": "Vente Forte",
    "unknown":     "—",
}
COMP_GRADE_COLOR = {
    "S":  "#f59e0b",
    "A+": "#16a34a",
    "A":  "#22c55e",
    "B+": "#84cc16",
    "B":  "#a3e635",
    "C":  "#eab308",
    "D":  "#f97316",
    "F":  "#ef4444",
}
ACTION_ICON = {
    "up":   ("▲", "#22c55e"),
    "down": ("▼", "#ef4444"),
    "init": ("★", "#38bdf8"),
    "reit": ("=", "#94a3b8"),
    "main": ("=", "#94a3b8"),
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


def consensus_badge(key):
    key = (key or "").lower()
    color = GRADE_COLOR.get(key, "#64748b")
    label = GRADE_LABEL.get(key, key.upper())
    return f'<span class="badge" style="background:{color}">{label}</span>'


def firm_badge(normalized):
    color = GRADE_COLOR.get(normalized, "#64748b")
    label = GRADE_LABEL.get(normalized, normalized)
    return (f'<span class="firm-badge" '
            f'style="background:{color}20;color:{color};border:1px solid {color}40">'
            f'{label}</span>')


def upside_cell(val):
    if val is None:
        return '<td class="center gray">N/A</td>'
    color = "#16a34a" if val > 0 else "#ef4444"
    sign  = "+" if val > 0 else ""
    return f'<td class="center" style="color:{color};font-weight:600">{sign}{val:.1f}%</td>'


def composite_bar(score):
    if score is None:
        return '<div class="comp-na">N/A</div>'
    if score >= 80:   color = "#16a34a"
    elif score >= 60: color = "#22c55e"
    elif score >= 50: color = "#eab308"
    elif score >= 30: color = "#f97316"
    else:             color = "#ef4444"
    pct = f"{score:.0f}"
    return (f'<div class="comp-wrap">'
            f'<div class="comp-bar" style="width:{pct}%;background:{color}"></div>'
            f'<span class="comp-num" style="color:{color}">{pct}'
            f'<span class="comp-max">/100</span></span>'
            f'</div>')


def grade_badge(grade):
    if not grade:
        return '<span class="grade-na">—</span>'
    color = COMP_GRADE_COLOR.get(grade, "#64748b")
    return (f'<div class="grade-badge" '
            f'style="background:{color}20;color:{color};border:2px solid {color}60">'
            f'{grade}</div>')


def buy_bar(pct_val):
    pct_val = pct_val or 0
    color = "#16a34a" if pct_val >= 70 else ("#eab308" if pct_val >= 50 else "#ef4444")
    return (f'<div class="buy-bar-wrap">'
            f'<div class="buy-bar" style="width:{pct_val:.0f}%;background:{color}"></div>'
            f'<span class="buy-num">{pct_val:.0f}%</span></div>')


def analyst_breakdown(r):
    total = r.get("total_trend") or 0
    if total == 0:
        return '<div class="center gray">-</div>'
    sb = r.get("strong_buy", 0) or 0
    b  = r.get("buy",  0) or 0
    h  = r.get("hold", 0) or 0
    se = r.get("sell", 0) or 0
    ss = r.get("strong_sell", 0) or 0
    parts = []
    specs = [
        (sb, "#16a34a", "Achat Fort"), (b, "#22c55e", "Achat"),
        (h,  "#eab308", "Neutre"),     (se, "#f97316", "Vente"),
        (ss, "#ef4444", "Vente Forte"),
    ]
    for count, color, label in specs:
        if count > 0:
            w = count / total * 100
            parts.append(
                f'<div class="seg" style="width:{w:.1f}%;background:{color}" '
                f'title="{label}: {count}"></div>')
    joined = "".join(parts)
    detail = f"SB:{sb} B:{b} N:{h} V:{se} VF:{ss}"
    return f'<div class="breakdown" title="{detail}">{joined}</div>'


def score_detail_section(score_detail):
    if not score_detail:
        return ('<div class="sd-empty">Score composite non disponible — '
                'relancez <code>python pea_screener.py --refresh</code> '
                'pour calculer tous les facteurs fondamentaux.</div>')
    cells = ""
    for fname, label, weight, ascending in METRIC_LABELS:
        pct = score_detail.get(fname)
        if pct is None:
            cells += (f'<div class="sd-item">'
                      f'<div class="sd-label">{label} '
                      f'<span class="sd-weight">{weight}</span></div>'
                      f'<div class="sd-bar-wrap">'
                      f'<div class="sd-bar" style="width:50%;background:#334155"></div></div>'
                      f'<span class="sd-pct gray">N/A</span>'
                      f'</div>')
        else:
            color  = "#22c55e" if pct >= 70 else ("#eab308" if pct >= 40 else "#ef4444")
            arrow  = "&#x2191;" if ascending else "&#x2193;"
            pct_i  = f"{pct:.0f}"
            cells += (f'<div class="sd-item">'
                      f'<div class="sd-label">{label} '
                      f'<span class="sd-weight">{weight}</span> '
                      f'<span class="sd-arrow" style="color:{color}">{arrow}</span></div>'
                      f'<div class="sd-bar-wrap">'
                      f'<div class="sd-bar" style="width:{pct_i}%;background:{color}"></div>'
                      f'</div>'
                      f'<span class="sd-pct" style="color:{color}">{pct_i}</span>'
                      f'</div>')
    return (f'<div class="sd-title">Décomposition du score composite (centile 0-100)</div>'
            f'<div class="sd-grid">{cells}</div>')


def firms_detail_row(idx, firms, score_detail):
    score_section = score_detail_section(score_detail)

    if not firms:
        firms_html = ('<div class="no-firms">'
                      'Aucune recommandation de cabinet disponible sur 180 jours.'
                      '</div>')
    else:
        rows = ""
        for f in firms:
            norm     = f.get("normalized", "unknown")
            color    = GRADE_COLOR.get(norm, "#64748b")
            icon, ic = ACTION_ICON.get(f.get("action", ""), ("·", "#64748b"))
            from_g   = f.get("from_grade", "")
            from_html = (f' <span class="from-grade">depuis {from_g}</span>'
                         if from_g else "")
            rows += (f'<tr class="firm-row">'
                     f'<td class="firm-name">{f["firm"]}</td>'
                     f'<td>{firm_badge(norm)}{from_html}</td>'
                     f'<td class="center" style="color:{ic}">{icon} {f.get("action_fr","")}</td>'
                     f'<td class="center gray">{f.get("date_str","")}</td>'
                     f'</tr>')

        counts = Counter(f.get("normalized", "unknown") for f in firms)
        summary_parts = []
        for norm in ["strong_buy", "buy", "hold", "sell", "strong_sell"]:
            if counts.get(norm, 0):
                col = GRADE_COLOR[norm]
                summary_parts.append(
                    f'<span style="color:{col};font-weight:600">'
                    f'{GRADE_LABEL[norm]}: {counts[norm]}</span>')
        summary = " &nbsp;·&nbsp; ".join(summary_parts)

        firms_html = (f'<div class="firms-header">'
                      f'{len(firms)} cabinets &nbsp;·&nbsp; {summary}</div>'
                      f'<table class="firms-table">'
                      f'<thead><tr>'
                      f'<th>Cabinet</th><th>Recommandation</th>'
                      f'<th>Action</th><th>Date</th>'
                      f'</tr></thead>'
                      f'<tbody>{rows}</tbody>'
                      f'</table>')

    return (f'<tr id="detail-{idx}" class="detail-row" style="display:none">'
            f'<td colspan="12">'
            f'<div class="detail-container">'
            f'<div class="detail-score">{score_section}</div>'
            f'<div class="detail-firms">{firms_html}</div>'
            f'</div>'
            f'</td></tr>')


# ── KPI ────────────────────────────────────────────────────────────────────────

buy_strong  = sum(1 for r in records if r.get("consensus","").lower() == "strong_buy")
buy_only    = sum(1 for r in records if r.get("consensus","").lower() == "buy")
hold_       = sum(1 for r in records if r.get("consensus","").lower() == "hold")
sell_       = sum(1 for r in records
                  if r.get("consensus","").lower() in ("sell","strong_sell","underperform"))
enriched    = sum(1 for r in records if r.get("firms") is not None)
top_grades  = sum(1 for r in records if r.get("grade") in ("S","A+","A"))

kpi_gold = (f'<div class="kpi gold"><div class="val">{top_grades}</div>'
            f'<div class="lbl">Grades S / A+ / A</div></div>'
            if has_composite else "")

# ── Rows ───────────────────────────────────────────────────────────────────────

rows_html = ""
for i, r in enumerate(records):
    name     = (r.get("name") or r["symbol"])[:40]
    sector   = r.get("sector", "") or ""
    country  = r.get("country", "") or ""
    price    = r.get("price")
    target   = r.get("target_price")
    cur      = r.get("currency", "")
    comp     = r.get("composite")
    grade    = r.get("grade")
    sd       = r.get("score_detail")
    firms    = r.get("firms") or []
    n_ana    = r.get("n_analysts", 0) or 0
    buy_p    = r.get("buy_pct", 0) or 0

    price_s  = f"{price:.2f}"  if price  else "N/A"
    target_s = f"{target:.2f}" if target else "N/A"
    price_td = (f'<div class="price-cur">{price_s}</div>'
                f'<div class="price-tgt">{target_s} <span class="price-ccy">{cur}</span></div>')

    has_firms = len(firms) > 0
    toggle_btn = (f'<button class="toggle-btn" onclick="event.stopPropagation();toggleDetail({i})" '
                  f'title="{len(firms)} cabinets">&#9658; {len(firms)}</button>'
                  if has_firms else
                  '<span class="no-btn">—</span>')

    rows_html += f"""
    <tr class="main-row" onclick="toggleDetail({i})" style="cursor:pointer">
      <td class="center rank">{i+1}</td>
      <td class="comp-cell">{composite_bar(comp)}</td>
      <td class="center grade-cell">{grade_badge(grade)}</td>
      <td><span class="ticker">{r["symbol"]}</span></td>
      <td class="name-cell" title="{r.get('name','')}">
        <div class="name">{name}</div>
        <div class="meta">{sector} · {country}</div>
      </td>
      <td class="center">{n_ana}</td>
      <td>{analyst_breakdown(r)}</td>
      <td>{buy_bar(buy_p)}</td>
      {upside_cell(r.get("upside_pct"))}
      <td class="center small price-cell">{price_td}</td>
      <td class="center">{consensus_badge(r.get("consensus",""))}</td>
      <td class="center">{toggle_btn}</td>
    </tr>
    {firms_detail_row(i, firms, sd)}"""

# ── HTML ───────────────────────────────────────────────────────────────────────

sort_notice = ("Classé par score composite pondéré (0-100)"
               if has_composite else
               "Score composite non disponible — classé par score analyste brut. "
               "Lancez <code>python pea_screener.py --refresh</code> pour calculer "
               "les facteurs fondamentaux.")

enrich_notice_cls = "enrich-notice" if enriched > 0 else "enrich-notice warn"
enrich_notice_txt = (f"&#10003; {enriched}/{len(records)} actions enrichies avec les cabinets"
                     if enriched > 0 else
                     "Lancez <code>python pea_screener.py --enrich</code> "
                     "pour ajouter les recommandations par cabinet.")

html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PEA Screener — Trade Republic</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
          background: #0f172a; color: #e2e8f0; font-size: 13px; }}

  header {{ background: linear-gradient(135deg, #1e3a5f 0%, #0f172a 100%);
            padding: 32px 40px 24px; border-bottom: 1px solid #1e293b; }}
  header h1 {{ font-size: 26px; font-weight: 700; color: #f8fafc; letter-spacing: -0.5px; }}
  header h1 span {{ color: #38bdf8; }}
  header p  {{ color: #94a3b8; margin-top: 6px; font-size: 13px; }}

  .kpi-row {{ display: flex; gap: 16px; padding: 24px 40px; flex-wrap: wrap; }}
  .kpi {{ background: #1e293b; border: 1px solid #334155; border-radius: 10px;
          padding: 16px 24px; flex: 1; min-width: 130px; }}
  .kpi .val {{ font-size: 32px; font-weight: 700; }}
  .kpi .lbl {{ color: #64748b; font-size: 11px; margin-top: 2px;
               text-transform: uppercase; letter-spacing: .5px; }}
  .kpi.green  .val {{ color: #22c55e; }}
  .kpi.lime   .val {{ color: #84cc16; }}
  .kpi.yellow .val {{ color: #eab308; }}
  .kpi.red    .val {{ color: #f87171; }}
  .kpi.blue   .val {{ color: #38bdf8; }}
  .kpi.purple .val {{ color: #a78bfa; }}
  .kpi.gold   .val {{ color: #f59e0b; }}

  .notice {{ margin: 0 40px 12px; padding: 10px 16px; border-radius: 8px;
             font-size: 12px; }}
  .enrich-notice {{ background: #16a34a20; border: 1px solid #16a34a40; color: #86efac; }}
  .enrich-notice.warn {{ background: #eab30820; border-color: #eab30840; color: #fde68a; }}
  .sort-notice {{ background: #38bdf820; border: 1px solid #38bdf840; color: #7dd3fc; }}
  .notice code {{ background: #1e293b; padding: 1px 6px; border-radius: 4px; }}

  .table-wrap {{ padding: 0 40px 40px; overflow-x: auto; }}
  .legend {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 16px;
             color: #64748b; font-size: 11px; align-items: center; }}
  .leg-dot {{ width: 10px; height: 10px; border-radius: 50%;
              display: inline-block; margin-right: 4px; }}

  table {{ width: 100%; border-collapse: collapse; }}
  thead tr {{ background: #1e293b; }}
  thead th {{ padding: 10px 12px; text-align: left; color: #94a3b8;
              font-size: 11px; text-transform: uppercase; letter-spacing: .6px;
              position: sticky; top: 0; background: #1e293b;
              white-space: nowrap; z-index: 10; }}
  .main-row {{ border-bottom: 1px solid #1e293b; transition: background .12s; }}
  .main-row:hover {{ background: #1e293b; }}
  .main-row.open {{ background: #162032; border-bottom: none; }}
  td {{ padding: 9px 12px; vertical-align: middle; }}

  .rank   {{ color: #475569; font-weight: 600; width: 32px; }}
  .ticker {{ font-family: monospace; font-weight: 700;
             color: #38bdf8; font-size: 13px; }}
  .name-cell .name {{ font-weight: 500; color: #e2e8f0; white-space: nowrap;
                      overflow: hidden; text-overflow: ellipsis; max-width: 220px; }}
  .name-cell .meta {{ color: #64748b; font-size: 11px; margin-top: 2px; white-space: nowrap; }}
  .center {{ text-align: center; }}
  .gray   {{ color: #64748b; }}
  .small  {{ font-size: 12px; }}

  /* Composite score bar */
  .comp-cell {{ min-width: 130px; }}
  .comp-wrap {{ display: flex; align-items: center; gap: 7px; }}
  .comp-bar  {{ height: 8px; border-radius: 4px; flex-shrink: 0; min-width: 4px; }}
  .comp-num  {{ font-weight: 700; font-size: 13px; white-space: nowrap; }}
  .comp-max  {{ font-size: 10px; opacity: .5; font-weight: 400; }}
  .comp-na   {{ color: #475569; font-size: 12px; font-style: italic; }}

  /* Grade badge */
  .grade-cell {{ width: 52px; }}
  .grade-badge {{ display: inline-flex; align-items: center; justify-content: center;
                  width: 40px; height: 28px; border-radius: 8px;
                  font-size: 13px; font-weight: 800; letter-spacing: -.3px; }}
  .grade-na {{ color: #334155; }}

  /* Buy/analyst bars */
  .buy-bar-wrap {{ display: flex; align-items: center; gap: 8px; min-width: 80px; }}
  .buy-bar {{ height: 6px; border-radius: 3px; flex-shrink: 0; }}
  .buy-num {{ font-weight: 600; font-size: 12px; color: #cbd5e1; }}

  .breakdown {{ display: flex; height: 8px; border-radius: 4px; overflow: hidden;
                width: 100px; gap: 1px; cursor: default; }}
  .seg {{ height: 100%; flex-shrink: 0; }}

  /* Consensus badge */
  .badge {{ display: inline-block; padding: 3px 8px; border-radius: 20px;
            font-size: 11px; font-weight: 600; color: #fff; white-space: nowrap; }}

  /* Price cell */
  .price-cell {{ min-width: 100px; }}
  .price-cur {{ font-weight: 500; color: #e2e8f0; font-size: 12px; }}
  .price-tgt {{ color: #64748b; font-size: 11px; margin-top: 2px; }}
  .price-ccy {{ color: #475569; }}

  /* Toggle button */
  .toggle-btn {{ background: #1e293b; border: 1px solid #334155; color: #94a3b8;
                 padding: 3px 8px; border-radius: 6px; font-size: 11px; cursor: pointer;
                 white-space: nowrap; transition: all .15s; }}
  .toggle-btn:hover {{ background: #334155; color: #e2e8f0; }}
  .toggle-btn.active {{ background: #38bdf820; border-color: #38bdf8; color: #38bdf8; }}
  .no-btn {{ color: #334155; font-size: 12px; }}

  /* Detail row */
  .detail-row td {{ padding: 0; background: #0c1929; border-bottom: 2px solid #1e3a5f; }}
  .detail-container {{ display: flex; gap: 0; padding: 20px 24px 24px 50px; flex-wrap: wrap; }}
  .detail-score {{ flex: 0 0 auto; min-width: 340px; max-width: 420px;
                   padding-right: 32px; border-right: 1px solid #1e293b; }}
  .detail-firms {{ flex: 1 1 300px; padding-left: 28px; min-width: 280px; }}

  /* Score detail */
  .sd-title {{ font-size: 11px; color: #38bdf8; text-transform: uppercase;
               letter-spacing: .7px; margin-bottom: 12px; font-weight: 600; }}
  .sd-empty {{ color: #475569; font-style: italic; font-size: 12px; }}
  .sd-empty code {{ background: #1e293b; padding: 1px 6px; border-radius: 4px; }}
  .sd-grid {{ display: flex; flex-direction: column; gap: 7px; }}
  .sd-item {{ display: flex; align-items: center; gap: 8px; }}
  .sd-label {{ width: 110px; font-size: 12px; color: #94a3b8; flex-shrink: 0; }}
  .sd-weight {{ color: #475569; font-size: 10px; }}
  .sd-arrow {{ font-size: 10px; }}
  .sd-bar-wrap {{ flex: 1; height: 6px; background: #1e293b; border-radius: 3px; overflow: hidden; }}
  .sd-bar {{ height: 100%; border-radius: 3px; }}
  .sd-pct {{ width: 28px; text-align: right; font-size: 12px; font-weight: 600;
             flex-shrink: 0; }}

  /* Firms */
  .firms-header {{ font-size: 12px; color: #94a3b8; margin-bottom: 10px; }}
  .no-firms {{ color: #475569; font-style: italic; font-size: 12px; }}
  .firms-table {{ width: 100%; border-collapse: collapse; max-width: 640px; }}
  .firms-table thead th {{ background: #1e293b; color: #64748b; font-size: 11px;
                           text-transform: uppercase; letter-spacing: .5px;
                           padding: 6px 10px; text-align: left; position: relative; top: auto; }}
  .firm-row {{ border-bottom: 1px solid #1e293b30; }}
  .firm-row:hover {{ background: #1e293b30; }}
  .firm-row td {{ padding: 5px 10px; color: #cbd5e1; font-size: 12px; }}
  .firm-name {{ font-weight: 500; color: #e2e8f0; min-width: 160px; }}
  .firm-badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px;
                 font-size: 11px; font-weight: 600; }}
  .from-grade {{ color: #475569; font-size: 11px; }}

  footer {{ text-align: center; padding: 20px; color: #334155; font-size: 11px;
            border-top: 1px solid #1e293b; }}
</style>
</head>
<body>

<header>
  <h1>PEA Screener &mdash; <span>Trade Republic</span></h1>
  <p>Actions eligibles PEA &bull; Score composite multi-facteurs pondéré &bull;
     Source : Yahoo Finance &bull;
     Données du {cached_at} &bull;
     Rapport généré le {generated}</p>
</header>

<div class="kpi-row">
  <div class="kpi blue">  <div class="val">{len(records)}</div><div class="lbl">Actions analysées</div></div>
  {kpi_gold}
  <div class="kpi green"> <div class="val">{buy_strong}</div><div class="lbl">Achat Fort</div></div>
  <div class="kpi lime">  <div class="val">{buy_only}</div><div class="lbl">Achat</div></div>
  <div class="kpi yellow"><div class="val">{hold_}</div><div class="lbl">Neutre</div></div>
  <div class="kpi red">   <div class="val">{sell_}</div><div class="lbl">Vente / Sous-perf</div></div>
  <div class="kpi purple"><div class="val">{enriched}</div><div class="lbl">Avec cabinets</div></div>
</div>

<div class="notice sort-notice">{sort_notice}</div>
<div class="notice {enrich_notice_cls}">{enrich_notice_txt}</div>

<div class="table-wrap">
  <div class="legend">
    <span>Grades : </span>
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
    <thead>
      <tr>
        <th>#</th>
        <th>Score composite</th>
        <th>Note</th>
        <th>Symbole</th>
        <th>Société / Secteur</th>
        <th class="center">Analystes</th>
        <th>Répartition</th>
        <th>% Achat</th>
        <th class="center">Upside</th>
        <th class="center">Prix / Cible</th>
        <th class="center">Consensus</th>
        <th class="center">Cabinets</th>
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>
</div>

<footer>
  PEA Screener &bull; Score composite : Qualité(35%) · Valorisation(25%) ·
  Consensus(20%) · Momentum(15%) · Bilan(5%) &bull;
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

OUTPUT.write_text(html, encoding="utf-8")
print(f"Rapport genere : {OUTPUT.resolve()}")
print(f"{len(records)} actions  |  {enriched} avec detail cabinets  |  "
      f"{'score composite' if has_composite else 'score brut (--refresh pour composite)'}")
