# Card Price Tracker — Accès permanent via Tailscale (Metabase + dashboard) — Design

**Statut :** design validé, pas encore implémenté.

## Contexte et problème

Aujourd'hui, `dashboard-frontend`, `dashboard-api` et `metabase` (voir `docker-compose.prod.yml`) sont liés à `127.0.0.1` sur le VPS — accès uniquement via tunnel SSH manuel depuis la machine de l'utilisateur. L'utilisateur veut y accéder en permanence, y compris depuis son mobile, sans ouvrir de tunnel SSH à chaque fois, et sans exposer ces services sur l'interface publique du VPS.

Le VPS (`164.132.243.29`, hostname `annonces-vps`) est déjà rejoint à un tailnet Tailscale (IP `100.116.232.89`, MagicDNS `annonces-vps.tail094416.ts.net`), partagé avec le projet `DE_ANNONCES` qui héberge sur la même machine. Ce même changement a déjà été fait pour le service `n8n` de `DE_ANNONCES` : `network_mode: host` + `N8N_LISTEN_ADDRESS` + HTTPS via `tailscale cert` (n8n refuse de poser son cookie d'auth sur une origine non-localhost sans TLS). Un document de préparation (`DE_ANNONCES/docs/tailscale-cardtracker-instructions.md`) esquissait déjà la même démarche pour ce repo, mais seulement en HTTP brut — ce design l'étend à HTTPS.

**Décisions actées avec l'utilisateur** :
- HTTPS via certificat Tailscale sur les 3 services (cohérence avec n8n, pas seulement parce que WireGuard chiffre déjà le transport — chaque service applicatif termine lui-même le TLS).
- Certificat et cron de renouvellement **dédiés à ce repo** (pas de partage avec le certificat déjà en place pour `DE_ANNONCES`), pour garder les deux projets isolés — même s'ils sont émis pour le même nom de domaine.
- `dashboard-frontend` garde son port publié `5173` (pas de bascule sur `443`), cohérent avec `metabase` (`3000`) et `dashboard-api` (`8000`) qui restent inchangés.
- `airflow-webserver` (port 8080) reste hors scope, inchangé.

## Architecture

### Adressage

Les ports publiés des 3 services passent de `127.0.0.1:<port>` à `${TAILSCALE_IP:-127.0.0.1}:<port>` (adresse d'écoute, port externe inchangé) dans `docker-compose.prod.yml` — la valeur par défaut `127.0.0.1` préserve un déploiement local/dev inchangé. Nouvelle variable `TAILSCALE_IP=100.116.232.89` dans le `.env` du VPS (IP littérale, pas le nom MagicDNS — une variable d'environnement Compose pour un binding d'adresse doit être une IP résolue à l'avance, pas un nom résolu au runtime).

Pour `dashboard-api` et `metabase`, le port interne du conteneur ne change pas (HTTPS remplace HTTP sur le même port) : mapping symétrique `${TAILSCALE_IP:-127.0.0.1}:8000:8000` et `${TAILSCALE_IP:-127.0.0.1}:3000:3000`. Pour `dashboard-frontend`, nginx passe d'un `listen 80` à un `listen 443 ssl` interne (voir section TLS) alors que le port externe reste `5173` (décision actée plus haut) : mapping asymétrique `${TAILSCALE_IP:-127.0.0.1}:5173:443`.

### TLS — un mécanisme différent par service

Les trois services ne parlent pas le même TLS, donc le certificat Tailscale (PEM `cert.pem`/`key.pem`) est consommé différemment par chacun :

- **`dashboard-api` (uvicorn/FastAPI)** — consomme le PEM directement. Le `command` du conteneur ajoute `--ssl-keyfile /certs/key.pem --ssl-certfile /certs/cert.pem`, service à l'écoute sur le même port `8000` mais en HTTPS.
- **`dashboard-frontend` (nginx)** — `nginx.conf` ajoute `listen 443 ssl;` (le conteneur nginx écoute du HTTPS à l'intérieur ; c'est le mapping Compose `${TAILSCALE_IP}:5173:443` côté hôte qui garde le port externe à `5173`) avec `ssl_certificate`/`ssl_certificate_key` pointant sur le PEM monté. Le `location /api/` existant (proxy vers `dashboard-api`) reste inchangé.
- **`metabase` (Jetty)** — n'accepte **pas** de PEM brut : Jetty veut un keystore Java (PKCS12). Le script de renouvellement du certificat (voir plus bas) convertit `cert.pem`+`key.pem` en `keystore.p12` via `openssl pkcs12 -export`. Variables d'environnement Metabase : `MB_JETTY_SSL=true`, `MB_JETTY_SSL_PORT=3000`, `MB_JETTY_SSL_KEYSTORE=/metabase-certs/keystore.p12`, `MB_JETTY_SSL_KEYSTORE_PASSWORD=${METABASE_KEYSTORE_PASSWORD}`. Nouvelle variable `METABASE_KEYSTORE_PASSWORD` dans `.env` (mot de passe du keystore, généré une fois, jamais commité — même traitement que `DASHBOARD_READER_PASSWORD`).

Chaque service monte le dossier de certificats en lecture seule (`./tailscale-certs:/certs:ro` ou équivalent selon le chemin attendu par le service).

### Healthchecks

Les healthchecks actuels (`curl http://localhost:<port>/...`) passent en `curl -k https://localhost:<port>/...` : le certificat Tailscale est émis pour `annonces-vps.tail094416.ts.net`, pas pour `localhost`, donc la vérification du nom d'hôte échouerait sur une requête interne au conteneur. `-k` désactive uniquement la vérification d'identité de CE check interne — la vérification réelle du certificat se fait côté client (navigateur/mobile) quand il se connecte via le vrai nom Tailscale, qui lui n'a pas cette désactivation.

### Certificat & renouvellement

Nouveau `scripts/renew_tailscale_cert.sh`, copie du pattern déjà en place et éprouvé pour `DE_ANNONCES` (`DE_ANNONCES/scripts/renew_tailscale_cert.sh`), adapté à ce repo :

```
CERT_DIR=/home/ubuntu/card-price-tracker/tailscale-certs
DOMAIN=annonces-vps.tail094416.ts.net
```

Étapes du script :
1. Hash du `cert.pem` actuel (pour détecter un changement).
2. `tailscale cert --cert-file=... --key-file=... "$DOMAIN"` (idempotent : no-op si le certificat en cours est encore valide, ~90 jours de durée de vie).
3. Reconvertit `cert.pem`+`key.pem` en `keystore.p12` (étape supplémentaire par rapport à la version `DE_ANNONCES`, propre au besoin Metabase).
4. Si le hash a changé : `docker compose -f docker-compose.prod.yml restart dashboard-frontend dashboard-api metabase`.

Programmé via `sudo crontab -e` sur le VPS, même cadence que `DE_ANNONCES` (`0 4 * * 1`, chaque lundi 4h) — nécessite `root` pour `tailscale cert`.

## Sécurité / vérification

- Le firewall `ufw` du VPS ne gouverne pas les ports publiés par Docker (deux systèmes séparés) — l'adresse d'écoute (`127.0.0.1` vs IP Tailscale) reste le seul mécanisme de contrôle d'accès, pas une règle de firewall. Rappel déjà établi pour `n8n`, vrai ici aussi.
- Vérification explicite après déploiement : depuis un appareil du tailnet, `curl -s -o /dev/null -w "%{http_code}\n" https://100.116.232.89:<port>/` doit renvoyer un code HTTP (pas de timeout) pour les 3 ports (3000, 8000, 5173) ; depuis l'IP publique du VPS (`164.132.243.29`) ou hors tailnet, la même requête doit timeout — confirme l'absence d'exposition publique.
- Vérification TLS : le navigateur/client ne doit afficher aucun avertissement de certificat en se connectant via `https://annonces-vps.tail094416.ts.net:<port>` (certificat Tailscale = CA publiquement reconnue, contrairement à un certificat auto-signé).

## Déploiement (manuel, sur le VPS)

1. `sudo tailscale cert --cert-file=/home/ubuntu/card-price-tracker/tailscale-certs/cert.pem --key-file=/home/ubuntu/card-price-tracker/tailscale-certs/key.pem annonces-vps.tail094416.ts.net` (première émission).
2. Générer le keystore Metabase : `openssl pkcs12 -export -in cert.pem -inkey key.pem -out keystore.p12 -passout pass:${METABASE_KEYSTORE_PASSWORD}`.
3. Ajouter `TAILSCALE_IP` et `METABASE_KEYSTORE_PASSWORD` au `.env` du VPS.
4. `docker compose -f docker-compose.prod.yml up -d dashboard-frontend dashboard-api metabase` (seulement ces 3 services, pas toute la stack).
5. Installer `scripts/renew_tailscale_cert.sh` dans le cron root (`0 4 * * 1`).
6. Exécuter la checklist de vérification ci-dessus.

## Hors scope (rappel)

- `airflow-webserver` (port 8080) — inchangé, pas dans le périmètre de cette demande.
- Partage du certificat avec `DE_ANNONCES` — décision explicite de garder les deux projets isolés, quitte à dupliquer le script de renouvellement.
- Authentification applicative supplémentaire (Tailscale ACLs par device, etc.) — le tailnet actuel a 3 appareils de confiance (`annonces-vps`, `fedora`, `iphone173`), aucune restriction supplémentaire demandée.
