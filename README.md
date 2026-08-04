# Signal Bot — Cassure/Retest vers Telegram (100% gratuit)

Ce bot reproduit la logique du script Pine "MA25/MA55 Cross Strategy"
(EMA10 sortant de MA35/EMA55 pour la Cassure, retour sur EMA10 pour le
Retest, filtre R:R >= 2.7 base sur le plus haut/bas des 5 dernieres
bougies 4H) et envoie un message Telegram des qu'un signal confirme
apparait. Il tourne gratuitement via GitHub Actions, sans TradingView
payant ni serveur a louer.

## Etape 1 — Creer le bot Telegram

1. Ouvre Telegram, cherche **@BotFather**.
2. Envoie `/newbot`, choisis un nom et un identifiant (doit finir par `bot`).
3. BotFather te donne un **token** (garde-le secret) — ex. `123456:ABC-DEF...`.
4. Envoie n'importe quel message a ton nouveau bot (pour l'"activer").
5. Recupere ton **chat_id** : ouvre dans un navigateur
   `https://api.telegram.org/bot<TON_TOKEN>/getUpdates`
   apres avoir envoye un message au bot — le `chat_id` apparait dans la reponse JSON (`message.chat.id`).

## Etape 2 — Creer le depot GitHub

1. Cree un nouveau depot GitHub (public ou prive, peu importe).
2. Mets-y ces 4 fichiers en conservant la structure :
   ```
   signal_bot.py
   requirements.txt
   README.md
   .github/workflows/signals.yml
   ```

## Etape 3 — Ajouter les secrets

Dans le depot GitHub : **Settings > Secrets and variables > Actions > New repository secret**

- `TELEGRAM_BOT_TOKEN` = le token recupere a l'etape 1
- `TELEGRAM_CHAT_ID` = le chat_id recupere a l'etape 1

## Etape 4 — Activer et tester

1. Va dans l'onglet **Actions** du depot, active les workflows si demande.
2. Clique sur **Signal Bot > Run workflow** pour un test manuel immediat.
3. Verifie les logs : tu dois voir `[OK] Alerte envoyee : ...` si un
   signal etait present sur la derniere bougie cloturee, sinon rien
   n'est envoye (normal, pas de signal a ce moment-la).
4. Une fois valide, le workflow tourne automatiquement toutes les 30
   minutes (modifiable dans `signals.yml`, ligne `cron`).

## Parametres a adapter (dans signal_bot.py)

| Variable | Role | Valeur actuelle |
|---|---|---|
| `SYMBOLS` | Paires Binance surveillees | BTCUSDT, BNBUSDT, SUIUSDT, ADAUSDT, XRPUSDT |
| `SIGNAL_TIMEFRAME` | Timeframe des signaux | 30m |
| `LEN_FAST` / `LEN_TREND` / `LEN_SLOW` | Longueurs des MA | 10 / 35 / 55 |
| `RETEST_WINDOW` | Fenetre max pour le retest (en bougies) | 20 |
| `MIN_RR` | R:R minimum | 2.7 |
| `SIGNAL_SOURCE` | "cassure", "retest" ou "both" | both |

## Limitation connue

Le DXY (indice dollar) n'est pas disponible sur Binance — il n'est
donc pas inclus dans `SYMBOLS`. Seules les paires cotees sur Binance
peuvent etre surveillees par ce script.

## Frequence d'execution (cron) vs timeframe

Le cron doit tourner au moins aussi souvent que ton `SIGNAL_TIMEFRAME`
pour ne rater aucune bougie cloturee. Avec un timeframe 30m, un cron
`*/30 * * * *` (deja configure) suffit. Si tu passes en 5m, ajuste le
cron sur `*/5 * * * *`.
