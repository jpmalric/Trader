#!/usr/bin/env python3
"""
Envoie les rapports PEA et World par email chaque matin via Gmail SMTP.

Configuration initiale :
    1. Activez la validation en 2 étapes sur votre compte Google
       https://myaccount.google.com/security
    2. Créez un mot de passe d'application (App Password) :
       https://myaccount.google.com/apppasswords
       → Sélectionnez "Autre (nom personnalisé)" → "PEA Screener"
       → Copiez le mot de passe à 16 caractères
    3. Renseignez email_config.json avec vos identifiants

Usage:
    python send_reports.py              # cache si valide, sinon refresh
    python send_reports.py --refresh    # force la mise à jour des données
    python send_reports.py --test       # envoie un email de test sans refresh
"""

import argparse
import json
import smtplib
import subprocess
import sys
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────────

CONFIG_FILE  = Path("email_config.json")
PEA_CACHE    = Path("pea_cache.json")
WORLD_CACHE  = Path("world_cache.json")
PEA_REPORT   = Path("pea_report.html")
WORLD_REPORT = Path("world_report.html")

DEFAULT_CONFIG = {
    "gmail_address":  "jeanphilippe.malric@gmail.com",
    "app_password":   "VOTRE_MOT_DE_PASSE_APP_16_CHARS",
    "recipient":      "jeanphilippe.malric@gmail.com",
    "send_pea":       True,
    "send_world":     True,
    "refresh_data":   True,
}


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(
            json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False),
            encoding="utf-8")
        print(f"Fichier de config cree : {CONFIG_FILE.resolve()}")
        print("=> Renseignez votre App Password Gmail dans ce fichier, puis relancez.")
        sys.exit(0)
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


# ── Lecture des stats depuis le cache ──────────────────────────────────────────

def get_top_stocks(cache_file: Path, n: int = 5) -> list[dict]:
    if not cache_file.exists():
        return []
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        records = data.get("records", [])
        if any(r.get("composite") is not None for r in records):
            records = sorted(records, key=lambda r: r.get("composite") or 0, reverse=True)
        else:
            records = sorted(records, key=lambda r: r.get("score") or 0, reverse=True)
        return records[:n]
    except Exception:
        return []


def get_cache_stats(cache_file: Path) -> dict:
    if not cache_file.exists():
        return {}
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        records = data.get("records", [])
        cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01"))
        buy_strong = sum(1 for r in records if r.get("consensus","").lower() == "strong_buy")
        buy_only   = sum(1 for r in records if r.get("consensus","").lower() == "buy")
        top_grade  = sum(1 for r in records if r.get("grade") in ("S","A+","A"))
        return {
            "total": len(records),
            "cached_at": cached_at.strftime("%d/%m/%Y %H:%M"),
            "strong_buy": buy_strong,
            "buy": buy_only,
            "top_grade": top_grade,
        }
    except Exception:
        return {}


# ── Génération des rapports ────────────────────────────────────────────────────

def run_screener(script: str, refresh: bool) -> bool:
    cmd = [sys.executable, script]
    if refresh:
        cmd.append("--refresh")
    print(f"  Lancement {script}{'  (--refresh)' if refresh else ''}...")
    result = subprocess.run(cmd, capture_output=True, text=True,
                            cwd=str(Path(__file__).parent))
    if result.returncode != 0:
        print(f"  ERREUR {script} :\n{result.stderr[-500:]}")
        return False
    print(f"  OK — {script}")
    return True


# ── Construction du corps de l'email ──────────────────────────────────────────

GRADE_EMOJI = {"S":"🥇","A+":"🟢","A":"✅","B+":"🔵","B":"🔵","C":"🟡","D":"🟠","F":"🔴"}
CONS_EMOJI  = {"strong_buy":"🟢","buy":"✅","hold":"🟡","sell":"🟠","strong_sell":"🔴"}

def stock_row(r: dict) -> str:
    symbol  = r.get("symbol","")
    name    = (r.get("name") or "")[:35]
    grade   = r.get("grade","?")
    comp    = r.get("composite")
    buy_p   = r.get("buy_pct", 0) or 0
    upside  = r.get("upside_pct")
    cons    = r.get("consensus","").lower()
    country = r.get("country","")

    comp_s   = f"{comp:.0f}/100" if comp is not None else "N/A"
    upside_s = (f"+{upside:.1f}%" if upside and upside > 0 else
                f"{upside:.1f}%" if upside else "N/A")
    g_emoji  = GRADE_EMOJI.get(grade, "")
    c_emoji  = CONS_EMOJI.get(cons, "")

    return f"""
      <tr style="border-bottom:1px solid #1e293b">
        <td style="padding:8px 12px;font-family:monospace;color:#a78bfa;font-weight:700">{symbol}</td>
        <td style="padding:8px 12px;color:#e2e8f0">{name}</td>
        <td style="padding:8px 4px;color:#94a3b8;font-size:12px">{country}</td>
        <td style="padding:8px 12px;text-align:center">{g_emoji} <b style="color:#f8fafc">{grade}</b> <span style="color:#64748b;font-size:12px">{comp_s}</span></td>
        <td style="padding:8px 12px;text-align:center;color:#22c55e;font-weight:600">{buy_p:.0f}%</td>
        <td style="padding:8px 12px;text-align:center;color:{'#16a34a' if (upside or 0)>0 else '#ef4444'};font-weight:600">{upside_s}</td>
        <td style="padding:8px 12px;text-align:center">{c_emoji}</td>
      </tr>"""


def build_section(title: str, icon: str, cache_file: Path, color: str) -> str:
    stats  = get_cache_stats(cache_file)
    tops   = get_top_stocks(cache_file, n=10)
    if not stats:
        return f'<p style="color:#ef4444">Données {title} non disponibles.</p>'

    rows = "".join(stock_row(r) for r in tops)

    return f"""
    <div style="margin-bottom:32px">
      <h2 style="color:{color};font-size:18px;margin:0 0 12px;border-bottom:1px solid #1e293b;padding-bottom:8px">
        {icon} {title}
      </h2>
      <div style="display:flex;gap:24px;flex-wrap:wrap;margin-bottom:16px">
        <div style="background:#1e293b;border-radius:8px;padding:12px 20px;min-width:100px">
          <div style="font-size:24px;font-weight:700;color:{color}">{stats.get('total',0)}</div>
          <div style="font-size:11px;color:#64748b;text-transform:uppercase">Actions</div>
        </div>
        <div style="background:#1e293b;border-radius:8px;padding:12px 20px;min-width:100px">
          <div style="font-size:24px;font-weight:700;color:#f59e0b">{stats.get('top_grade',0)}</div>
          <div style="font-size:11px;color:#64748b;text-transform:uppercase">Grades S/A+/A</div>
        </div>
        <div style="background:#1e293b;border-radius:8px;padding:12px 20px;min-width:100px">
          <div style="font-size:24px;font-weight:700;color:#16a34a">{stats.get('strong_buy',0)}</div>
          <div style="font-size:11px;color:#64748b;text-transform:uppercase">Achat Fort</div>
        </div>
        <div style="background:#1e293b;border-radius:8px;padding:12px 20px;min-width:100px">
          <div style="font-size:24px;font-weight:700;color:#22c55e">{stats.get('buy',0)}</div>
          <div style="font-size:11px;color:#64748b;text-transform:uppercase">Achat</div>
        </div>
      </div>
      <p style="color:#64748b;font-size:12px;margin:0 0 12px">Top 10 — données du {stats.get('cached_at','')}</p>
      <table style="width:100%;border-collapse:collapse;background:#1e293b;border-radius:8px;overflow:hidden">
        <thead>
          <tr style="background:#0f172a">
            <th style="padding:8px 12px;text-align:left;color:#64748b;font-size:11px;text-transform:uppercase">Symbole</th>
            <th style="padding:8px 12px;text-align:left;color:#64748b;font-size:11px;text-transform:uppercase">Société</th>
            <th style="padding:8px 4px;text-align:left;color:#64748b;font-size:11px;text-transform:uppercase">Pays</th>
            <th style="padding:8px 12px;text-align:center;color:#64748b;font-size:11px;text-transform:uppercase">Score</th>
            <th style="padding:8px 12px;text-align:center;color:#64748b;font-size:11px;text-transform:uppercase">% Achat</th>
            <th style="padding:8px 12px;text-align:center;color:#64748b;font-size:11px;text-transform:uppercase">Upside</th>
            <th style="padding:8px 12px;text-align:center;color:#64748b;font-size:11px;text-transform:uppercase">Avis</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      <p style="font-size:11px;color:#334155;margin:8px 0 0">Rapport complet en pièce jointe.</p>
    </div>"""


def build_email_html(date_str: str, send_pea: bool, send_world: bool) -> str:
    pea_section   = build_section("PEA Screener — Trade Republic", "🇪🇺",
                                  PEA_CACHE, "#38bdf8") if send_pea else ""
    world_section = build_section("World Screener — Top Capitalisations", "🌍",
                                  WORLD_CACHE, "#a78bfa") if send_world else ""

    return f"""<!DOCTYPE html>
<html lang="fr">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0f172a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
  <div style="max-width:760px;margin:0 auto;padding:32px 24px">

    <div style="margin-bottom:28px">
      <h1 style="color:#f8fafc;font-size:22px;margin:0 0 6px;font-weight:700">
        Screener quotidien &mdash; {date_str}
      </h1>
      <p style="color:#64748b;margin:0;font-size:13px">
        Score composite pondéré : Qualité(35%) · Valorisation(25%) · Consensus(20%) · Momentum(15%) · Bilan(5%)
      </p>
    </div>

    {pea_section}
    {world_section}

    <div style="border-top:1px solid #1e293b;padding-top:16px;margin-top:8px">
      <p style="color:#334155;font-size:11px;margin:0">
        Généré automatiquement · Source : Yahoo Finance ·
        Pas un conseil en investissement — à titre informatif uniquement.
      </p>
    </div>
  </div>
</body>
</html>"""


# ── Envoi de l'email ───────────────────────────────────────────────────────────

def send_email(config: dict, html_body: str, attachments: list[Path]) -> None:
    sender    = config["gmail_address"]
    password  = config["app_password"]
    recipient = config["recipient"]
    date_str  = datetime.now().strftime("%d/%m/%Y")

    msg = MIMEMultipart("mixed")
    msg["From"]    = f"PEA Screener <{sender}>"
    msg["To"]      = recipient
    msg["Subject"] = f"Screener quotidien — {date_str}"

    msg.attach(MIMEText(html_body, "html", "utf-8"))

    for path in attachments:
        if not path.exists():
            print(f"  Avertissement : pièce jointe manquante : {path}")
            continue
        part = MIMEBase("application", "octet-stream")
        part.set_payload(path.read_bytes())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment",
                        filename=f"{path.stem}_{date_str.replace('/','')}.html")
        msg.attach(part)

    print(f"  Connexion Gmail SMTP...")
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.ehlo()
        server.starttls()
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_bytes())
    print(f"  Email envoye a {recipient}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Envoi quotidien des rapports PEA et World")
    parser.add_argument("--refresh", action="store_true",
                        help="Force la mise a jour des donnees avant envoi")
    parser.add_argument("--test",    action="store_true",
                        help="Envoie un email de test sans relancer les screeners")
    args = parser.parse_args()

    config = load_config()

    if config.get("app_password") == "VOTRE_MOT_DE_PASSE_APP_16_CHARS":
        print("ERREUR : Renseignez votre App Password Gmail dans email_config.json")
        print("  => https://myaccount.google.com/apppasswords")
        sys.exit(1)

    send_pea   = config.get("send_pea",   True)
    send_world = config.get("send_world", True)
    do_refresh = args.refresh or (config.get("refresh_data", True) and not args.test)

    # Mise à jour des données
    if do_refresh and not args.test:
        print("\nMise a jour des donnees...")
        if send_pea:
            run_screener("pea_screener.py",   refresh=True)
        if send_world:
            run_screener("world_screener.py", refresh=True)

    # Construction et envoi de l'email
    date_str   = datetime.now().strftime("%d/%m/%Y")
    html_body  = build_email_html(date_str, send_pea, send_world)
    attachments = []
    if send_pea   and PEA_REPORT.exists():
        attachments.append(PEA_REPORT)
    if send_world and WORLD_REPORT.exists():
        attachments.append(WORLD_REPORT)

    print(f"\nEnvoi de l'email ({len(attachments)} piece(s) jointe(s))...")
    try:
        send_email(config, html_body, attachments)
        print("\nOK — email envoye avec succes.")
    except smtplib.SMTPAuthenticationError:
        print("\nERREUR d'authentification Gmail.")
        print("  Verifiez votre App Password dans email_config.json")
        print("  => https://myaccount.google.com/apppasswords")
        sys.exit(1)
    except Exception as e:
        print(f"\nERREUR envoi email : {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
