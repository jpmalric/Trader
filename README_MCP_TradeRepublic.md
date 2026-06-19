# Connecteur MCP — Trade Republic (PEA)

Serveur MCP qui donne à Claude un accès **lecture seule** à ton compte Trade Republic
(positions, liquidités, transactions) et croise tes positions avec les scores QARP
du `pea_screener2.py`.

> ⚠️ Trade Republic n'a **pas d'API publique**. Ce connecteur passe par l'API mobile
> non-officielle via la lib [`pytr`](https://github.com/pytr-org/pytr). C'est ton compte,
> donc c'est légitime, mais c'est techniquement contre les CGU de TR et l'API peut casser
> sans préavis. **Aucun ordre n'est passé.**

## Installation

```bash
C:\Users\jeanp\AppData\Local\Python\pythoncore-3.14-64\python.exe -m pip install mcp pytr
```

(déjà fait sur cette machine)

## Activation dans Claude Code

Le fichier `.mcp.json` à la racine du repo déclare déjà le serveur. Relance Claude Code
dans ce dossier ; il proposera d'activer le serveur `trade-republic`.

> `.mcp.json`, `.tr_cookies.txt` et `.tr_credentials` sont **gitignorés** (session = sensible).

## Utilisation (dans Claude)

1. **`tr_login`** — démarre la connexion.
   - Passe `phone_no` (`+33...`) et `pin`, ou définis les env vars `TR_PHONE` / `TR_PIN`.
   - S'il existe une session valide (`.tr_cookies.txt`), elle est reprise sans 2FA.
   - Sinon, un **code 2FA** est envoyé (SMS / notification appli).
2. **`tr_complete_login`** — saisis le code 2FA reçu.
3. Ensuite, à volonté :
   - **`tr_accounts`** / **`tr_cash`** — liquidités ventilées par compte (**PEA** vs **CTO**)
   - **`tr_portfolio`** — positions, valorisation, +/- value, totaux (chaque ligne taguée PEA/CTO)
   - **`tr_pea`** — vue dédiée du PEA : cash PEA + positions rattachées au PEA
   - **`tr_search`** — résout l'ISIN d'une valeur et son éligibilité PEA (`pea_only=true`)
   - **`tr_transactions`** — historique (ordres, dividendes, versements)
   - **`tr_screener_cross`** — tes positions notées avec le grade/score QARP
   - **`tr_status`** — état de la session
   - **`tr_raw`** — debug : payload brut d'un type d'API (ex `compactPortfolioByType`, `accountPairs`)

> **Comptes** : TR sépare le **PEA** (`TAX_WRAPPER`) et le **CTO** (`DEFAULT`). L'API ne filtre
> pas les positions par compte → l'attribution PEA/CTO est reconstruite via le champ
> `cashAccountNumber` de l'historique des ordres. Les lignes détenues sur les deux comptes
> sont marquées `PEA+CTO`.

## Notes techniques

- **WAF** : par défaut `TR_WAF=awswaf` (solveur pur-python). Si l'init du login échoue,
  passe à `TR_WAF=playwright` dans `.mcp.json` puis `playwright install chromium`.
- **Croisement screener** : le cache `pea_cache2.json` est indexé par ticker Yahoo et nom
  (pas d'ISIN), donc le matching se fait par **nom de société normalisé**. Vérifie les
  correspondances douteuses listées par `tr_screener_cross`.
- **Valorisation** : prix courant pris sur l'exchange `LSX` (Lang & Schwarz). Les champs
  exacts des payloads TR peuvent varier ; en cas d'anomalie, inspecte avec `tr_raw`.
