#!/usr/bin/env bash
# Régénère le rapport PEA et le publie dans le webroot servi par Caddy.
# Lancé par le timer systemd (pea-report.timer). Aucun login Trade Republic requis.
set -euo pipefail

APP_DIR="/opt/trader"          # dossier du repo sur le LXC
WEB_DIR="/var/www/pea"         # webroot servi par Caddy
PY="$APP_DIR/.venv/bin/python" # python du venv

cd "$APP_DIR"

# --refresh = met à jour le cache marché puis régénère pea_report2.html
"$PY" pea_screener2.py --refresh

# Publie de façon atomique (évite qu'un client lise un fichier à moitié écrit)
install -D -m 644 "$APP_DIR/pea_report2.html" "$WEB_DIR/index.html.tmp"
mv -f "$WEB_DIR/index.html.tmp" "$WEB_DIR/index.html"

echo "Rapport publié: $WEB_DIR/index.html ($(date -Is))"
