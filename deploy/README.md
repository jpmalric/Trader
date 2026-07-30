# Déploiement du rapport PEA (LXC Proxmox + Caddy)

Sert `pea_report2.html` en HTTP, rafraîchi automatiquement plusieurs fois par jour.
Aucun login Trade Republic requis (le screener n'utilise que des données de marché publiques).

Architecture :

```
[timer systemd] --> refresh_report.sh --> pea_screener2.py --refresh
                                              |
                                              v  écrit pea_report2.html
                                     /var/www/pea/index.html
                                              ^
                              [Caddy :80] ----+----> LAN  http://<IP_LXC>/
                                              +----> Cloudflare Tunnel -> Internet
```

## 1. Préparer le LXC (Debian/Ubuntu)

```bash
sudo apt update && sudo apt install -y python3 python3-venv git
sudo mkdir -p /opt/trader /var/www/pea /var/log/caddy

# Copier le repo dans /opt/trader (git clone, scp, rsync…). Exemple :
sudo git clone <URL_DE_TON_REPO> /opt/trader
# (ou copie au moins pea_screener2.py + le dossier deploy/)

# venv + dépendances
cd /opt/trader
sudo python3 -m venv .venv
sudo .venv/bin/pip install --upgrade pip pandas curl_cffi

chmod +x deploy/refresh_report.sh
```

Vérifie que ça génère bien le rapport à la main une première fois :

```bash
cd /opt/trader && ./deploy/refresh_report.sh
ls -l /var/www/pea/index.html
```

## 2. Installer le timer systemd (le rafraîchissement auto)

```bash
sudo cp deploy/pea-report.service /etc/systemd/system/
sudo cp deploy/pea-report.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pea-report.timer

# Vérifs
systemctl list-timers pea-report.timer   # prochaine exécution
sudo systemctl start pea-report.service   # forcer un run maintenant
journalctl -u pea-report.service -n 30    # logs du dernier run
```

Fréquence : voir `OnCalendar` dans `pea-report.timer` (par défaut Lun–Ven à 8h/11h/14h/17h/20h, heure de Paris).

## 3. Caddy (intégration à un Caddyfile existant)

Ajoute le bloc de `deploy/Caddyfile` à ton `/etc/caddy/Caddyfile` (remplace
`pea.tondomaine.fr` par ton sous-domaine), puis :

```bash
sudo mkdir -p /var/log/caddy
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

- Accès LAN : `http://<IP_DU_LXC>/` (si tu as un bloc `:80` par défaut).
- Accès externe : `https://pea.tondomaine.fr/` via le tunnel.

## 4. Cloudflare Tunnel

Fais pointer le hostname public sur Caddy en HTTP local. Deux options :

**Dashboard** (Zero Trust → Networks → Tunnels → ton tunnel → Public Hostname) :
- Subdomain `pea`, Domain `tondomaine.fr`
- Service : `HTTP` → `127.0.0.1:80`

**Ou config.yml de `cloudflared`** :

```yaml
ingress:
  - hostname: pea.tondomaine.fr
    service: http://127.0.0.1:80
  - service: http_status:404
```

Cloudflare gère le TLS au bord, donc Caddy reste en HTTP simple. Active
**Cloudflare Access** sur ce hostname si tu veux restreindre l'accès externe.

## Notes

- La page se recharge seule toutes les 30 min côté navigateur
  (`<meta http-equiv="refresh" content="1800">` dans le rapport).
- Le rafraîchissement des *données* est piloté par le timer, pas par le navigateur.
- Pour changer les flags du screener (ex. `--pea-strict`, `--top`), édite la ligne
  `pea_screener2.py --refresh` dans `refresh_report.sh`.
