# Accès permanent via Tailscale (Metabase + dashboard) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rendre `dashboard-frontend`, `dashboard-api` et `metabase` accessibles en HTTPS depuis n'importe quel appareil du tailnet Tailscale du VPS, sans les exposer sur l'interface publique, sans tunnel SSH manuel.

**Architecture:** Paramétrer l'adresse d'écoute des 3 services (`127.0.0.1` → `${TAILSCALE_IP}`) dans `docker-compose.prod.yml`, terminer TLS dans chaque service avec un certificat Tailscale (PEM direct pour uvicorn/nginx, converti en keystore PKCS12 pour Metabase/Jetty), et automatiser le renouvellement via un script cron qui redémarre les conteneurs concernés en cas de changement de certificat.

**Tech Stack:** Docker Compose, Tailscale (`tailscale cert`), OpenSSL (conversion PKCS12), nginx, uvicorn/FastAPI, Metabase (Jetty).

**Spec:** `docs/superpowers/specs/2026-08-14-tailscale-remote-access-design.md`

## Global Constraints

- Aucun service ne doit jamais être lié à une interface publique — `${TAILSCALE_IP:-127.0.0.1}` avec défaut `127.0.0.1`, jamais `0.0.0.0`.
- `airflow-webserver` (port 8080) reste hors scope de ce plan, inchangé.
- Certificat et cron de renouvellement dédiés à ce repo — pas de partage avec le certificat déjà en place pour `DE_ANNONCES`, même si les deux sont émis pour le même nom de domaine `annonces-vps.tail094416.ts.net`.
- `dashboard-frontend` garde son port externe `5173` ; `dashboard-api` garde `8000` ; `metabase` garde `3000`. Seul le port interne de nginx change (`80` → `443`).
- Aucun mot de passe/secret en clair dans un fichier commité — toute nouvelle variable va dans `.env` (non commité) avec une entrée `changeme_...` correspondante dans `.env.example`.

---

## Task 1: Script de renouvellement du certificat + conversion keystore

**Files:**
- Create: `scripts/renew_tailscale_cert.sh`
- Modify: `.env.example` (ajoute `METABASE_KEYSTORE_PASSWORD`)

**Interfaces:**
- Produces: `tailscale-certs/cert.pem`, `tailscale-certs/key.pem`, `tailscale-certs/keystore.p12` (chemins consommés par Task 2 dans `docker-compose.prod.yml`).

Ce script ne peut pas être testé par une suite pytest (il appelle `tailscale cert`, une commande qui nécessite root et une session Tailscale authentifiée, indisponibles en CI/local). La vérification se fait par relecture + un test syntaxique bash (`bash -n`) + une exécution manuelle documentée dans la section Vérification du Task 1.

- [x] **Step 1: Écrire le script**

```bash
#!/usr/bin/env bash
# Spécifique au déploiement VPS actuel (chemin et nom MagicDNS en dur).
# Programmé via `sudo crontab -e` : 0 4 * * 1 (chaque lundi 4h). Nécessite
# root (tailscale cert). Le cert Tailscale expire tous les ~90 jours ;
# `tailscale cert` est un no-op si le cert en cours est encore valide.
#
# Dédié à ce repo -- ne partage pas de certificat avec DE_ANNONCES même si
# le nom de domaine est identique (les deux VPS/projets restent isolés,
# voir docs/superpowers/specs/2026-08-14-tailscale-remote-access-design.md).
set -euo pipefail

CERT_DIR=/home/ubuntu/card-price-tracker/tailscale-certs
DOMAIN=annonces-vps.tail094416.ts.net
KEYSTORE_PASSWORD="${METABASE_KEYSTORE_PASSWORD:?METABASE_KEYSTORE_PASSWORD doit être exporté avant d'appeler ce script}"

OLD_HASH=$(sha256sum "$CERT_DIR/cert.pem" | awk '{print $1}')

tailscale cert --cert-file="$CERT_DIR/cert.pem" --key-file="$CERT_DIR/key.pem" "$DOMAIN"
chown ubuntu:ubuntu "$CERT_DIR"/cert.pem "$CERT_DIR"/key.pem

NEW_HASH=$(sha256sum "$CERT_DIR/cert.pem" | awk '{print $1}')

if [ "$OLD_HASH" != "$NEW_HASH" ]; then
  echo "$(date): certificat renouvelé, régénération du keystore Metabase"
  # Metabase (Jetty) n'accepte pas de PEM brut -- conversion en keystore
  # PKCS12, seule étape qui n'existe pas dans le script équivalent de
  # DE_ANNONCES (n8n consomme le PEM directement).
  openssl pkcs12 -export \
    -in "$CERT_DIR/cert.pem" -inkey "$CERT_DIR/key.pem" \
    -out "$CERT_DIR/keystore.p12" \
    -passout "pass:$KEYSTORE_PASSWORD"
  chown ubuntu:ubuntu "$CERT_DIR/keystore.p12"

  echo "$(date): redémarrage dashboard-frontend, dashboard-api, metabase"
  cd /home/ubuntu/card-price-tracker && docker compose -f docker-compose.prod.yml restart dashboard-frontend dashboard-api metabase
fi
```

- [x] **Step 2: Vérifier la syntaxe bash**

Run: `bash -n scripts/renew_tailscale_cert.sh`
Expected: aucune sortie (pas d'erreur de syntaxe).

- [x] **Step 3: Rendre le script exécutable**

Run: `chmod +x scripts/renew_tailscale_cert.sh`

- [x] **Step 4: Ajouter la nouvelle variable à `.env.example`**

Ajouter après le bloc `DASHBOARD_READER_PASSWORD` existant :

```
# Certificat Tailscale (accès permanent Metabase/dashboard, voir
# docs/superpowers/specs/2026-08-14-tailscale-remote-access-design.md) :
# mot de passe du keystore PKCS12 généré à partir du certificat Tailscale
# pour Metabase (Jetty n'accepte pas de PEM brut). Générer une vraie valeur
# aléatoire pour .env, ex: python3 -c "import secrets; print(secrets.token_hex(30))"
METABASE_KEYSTORE_PASSWORD=changeme_metabase_keystore
TAILSCALE_IP=changeme_tailscale_ip
```

- [x] **Step 5: Commit**

```bash
git add scripts/renew_tailscale_cert.sh .env.example
git commit -m "feat: script de renouvellement du certificat Tailscale + keystore Metabase"
```

---

## Task 2: `docker-compose.prod.yml` — adresses d'écoute, TLS, montage des certificats

**Files:**
- Modify: `docker-compose.prod.yml:146-177` (service `metabase`)
- Modify: `docker-compose.prod.yml:179-218` (service `dashboard-api`)
- Modify: `docker-compose.prod.yml:220-233` (service `dashboard-frontend`)

**Interfaces:**
- Consumes: `tailscale-certs/cert.pem`, `tailscale-certs/key.pem`, `tailscale-certs/keystore.p12` (produits par Task 1 sur le VPS), `${TAILSCALE_IP}` et `${METABASE_KEYSTORE_PASSWORD}` (variables `.env`, Task 1).
- Produces: montages `/certs` (dashboard-api, dashboard-frontend) et `/metabase-certs` (metabase) — chemins consommés par Task 3 (nginx.conf) et Task 4 (Dockerfile.api / commande uvicorn).

Pas de suite pytest pour un fichier Compose — vérification par `docker compose config` (valide la syntaxe/interpolation) et par les vérifications opérationnelles du Task 5 (déploiement réel).

- [x] **Step 1: Modifier le service `metabase`**

Remplacer le bloc `metabase:` actuel (lignes 146-177) par :

```yaml
  # metabase : UI d'exploration de la base de données (Mois 3, voir
  # docs/superpowers/specs/2026-08-09-metabase-db-ui-design.md). Stockage
  # interne H2 (métadonnées Metabase -- comptes, dashboards sauvegardés --
  # PAS une copie des données du pipeline) sur un volume nommé persistant.
  # Aucun depends_on : Metabase démarre et devient "healthy" de façon
  # autonome (serveur Jetty + assistant de configuration), la connexion à
  # "db" se fait plus tard, via son assistant web, pas au démarrage du
  # conteneur.
  #
  # TLS via certificat Tailscale (voir
  # docs/superpowers/specs/2026-08-14-tailscale-remote-access-design.md) :
  # Jetty n'accepte pas de PEM brut, MB_JETTY_SSL_KEYSTORE pointe sur le
  # keystore PKCS12 généré par scripts/renew_tailscale_cert.sh.
  metabase:
    image: metabase/metabase:v0.50.32
    environment:
      # MB_DB_FILE : sans cette variable, Metabase stocke son fichier H2
      # dans le système de fichiers éphémère du conteneur (perdu à chaque
      # recréation). En la pointant vers un chemin sous le volume monté
      # ci-dessous, les comptes/dashboards Metabase survivent à un
      # `docker compose down`/`up` -- piège documenté de Metabase en Docker,
      # vérifié explicitement pour ne pas le reproduire ici.
      MB_DB_FILE: /metabase-data/metabase.db
      MB_JETTY_SSL: "true"
      MB_JETTY_SSL_PORT: "3000"
      MB_JETTY_SSL_KEYSTORE: /metabase-certs/keystore.p12
      MB_JETTY_SSL_KEYSTORE_PASSWORD: ${METABASE_KEYSTORE_PASSWORD}
    volumes:
      - metabase_data:/metabase-data
      - ./tailscale-certs:/metabase-certs:ro
    # ${TAILSCALE_IP:-127.0.0.1} : IP Tailscale du VPS en prod, 127.0.0.1 en
    # local/dev (défaut si TAILSCALE_IP n'est pas défini). Jamais exposé sur
    # l'interface publique -- seule l'adresse d'écoute contrôle l'accès (ufw
    # ne gouverne pas les ports publiés par Docker).
    ports:
      - "${TAILSCALE_IP:-127.0.0.1}:3000:3000"
    healthcheck:
      # -k : le healthcheck interne tape sur "localhost", pas sur le nom
      # Tailscale pour lequel le certificat est émis -- la vérification de
      # nom d'hôte échouerait sinon. La vérification réelle du certificat se
      # fait côté client (navigateur/mobile) via le vrai nom Tailscale, sans
      # -k. Voir la spec pour le détail.
      test: ["CMD", "curl", "--fail", "-k", "https://localhost:3000/api/health"]
      interval: 30s
      timeout: 10s
      retries: 5
      # start_period plus long que les autres services (60s pour Airflow) :
      # Metabase est une application JVM, son démarrage à froid est
      # nettement plus lent qu'un serveur Python/Airflow.
      start_period: 120s
    restart: unless-stopped
```

- [x] **Step 2: Modifier le service `dashboard-api`**

Remplacer le bloc `dashboard-api:` actuel (lignes 179-218) par :

```yaml
  # dashboard-api : API FastAPI en lecture seule pour le dashboard sur mesure
  # (voir docs/superpowers/specs/2026-08-13-custom-dashboard-design.md).
  # Se connecte via dashboard_reader (migration 007), jamais pipeline_app.
  #
  # TLS via certificat Tailscale (voir
  # docs/superpowers/specs/2026-08-14-tailscale-remote-access-design.md) :
  # uvicorn consomme le PEM Tailscale directement, pas de conversion
  # nécessaire (contrairement à Metabase).
  dashboard-api:
    build:
      context: .
      dockerfile: Dockerfile.api
    depends_on:
      db:
        condition: service_healthy
    environment:
      POSTGRES_HOST: db
      POSTGRES_PORT: 5432
      # POSTGRES_DB / DASHBOARD_READER_PASSWORD : injectees directement par
      # Compose (interpolation ${VAR} depuis le .env de l'hote, cote hote
      # uniquement) plutot que de monter le fichier .env entier dans le
      # conteneur. dashboard-api n'a besoin QUE de ces 4 variables (voir
      # load_dashboard_reader_config() dans src/common/config.py) -- monter
      # tout .env lui donnerait aussi acces au mot de passe superuser
      # Postgres, a la cle API PokemonTCG et aux secrets Airflow, alors que
      # ce conteneur est le seul du stack a parser des query strings fournies
      # par un utilisateur.
      POSTGRES_DB: ${POSTGRES_DB}
      DASHBOARD_READER_PASSWORD: ${DASHBOARD_READER_PASSWORD}
    volumes:
      - ./src:/app/src
      - ./tailscale-certs:/certs:ro
    # ${TAILSCALE_IP:-127.0.0.1} : voir commentaire équivalent sur metabase.
    ports:
      - "${TAILSCALE_IP:-127.0.0.1}:8000:8000"
    healthcheck:
      # -k : voir commentaire équivalent sur metabase (nom "localhost" vs
      # nom du certificat).
      test: ["CMD", "python3", "-c", "import ssl, urllib.request; ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE; urllib.request.urlopen('https://localhost:8000/api/health', context=ctx)"]
      interval: 15s
      timeout: 5s
      retries: 5
      start_period: 10s
    restart: unless-stopped
```

- [x] **Step 3: Modifier le service `dashboard-frontend`**

Remplacer le bloc `dashboard-frontend:` actuel (lignes 220-233) par :

```yaml
  # dashboard-frontend : build React statique servi par Nginx, proxy /api/
  # vers dashboard-api (voir frontend/nginx.conf). Port externe 5173
  # inchangé (décision actée dans la spec) -- seul le port interne du
  # conteneur change (80 -> 443, voir frontend/nginx.conf, Task 3).
  #
  # TLS via certificat Tailscale (voir
  # docs/superpowers/specs/2026-08-14-tailscale-remote-access-design.md) :
  # nginx consomme le PEM Tailscale directement.
  dashboard-frontend:
    build:
      context: ./frontend
      dockerfile: Dockerfile
    depends_on:
      - dashboard-api
    volumes:
      - ./tailscale-certs:/certs:ro
    # Mapping asymétrique : port externe 5173 (inchangé), port interne 443
    # (nginx écoute désormais en HTTPS, voir Task 3).
    ports:
      - "${TAILSCALE_IP:-127.0.0.1}:5173:443"
    restart: unless-stopped
```

- [x] **Step 4: Valider la syntaxe du fichier Compose**

Run: `TAILSCALE_IP=127.0.0.1 METABASE_KEYSTORE_PASSWORD=test POSTGRES_DB=x DASHBOARD_READER_PASSWORD=x POSTGRES_ADMIN_USER=x POSTGRES_ADMIN_PASSWORD=x AIRFLOW_DB_PASSWORD=x AIRFLOW_ADMIN_PASSWORD=x AIRFLOW_SECRET_KEY=x docker compose -f docker-compose.prod.yml config --quiet`
Expected: aucune sortie, code de sortie 0 (fichier syntaxiquement et sémantiquement valide).

- [x] **Step 5: Commit**

```bash
git add docker-compose.prod.yml
git commit -m "feat: TLS Tailscale + adresse d'écoute paramétrable pour metabase/dashboard-api/dashboard-frontend"
```

---

## Task 3: `frontend/nginx.conf` — HTTPS

**Files:**
- Modify: `frontend/nginx.conf`

**Interfaces:**
- Consumes: `/certs/cert.pem`, `/certs/key.pem` (montés par Task 2).

Pas de suite pytest (config nginx) — vérification par `nginx -t` à l'intérieur du conteneur buildé, et par un test HTTPS réel en Task 5.

- [x] **Step 1: Modifier `nginx.conf`**

```nginx
# Sert le build statique React (dist/) et proxifie /api/ vers le conteneur
# dashboard-api -- "dashboard-api" est résolu par le DNS interne de Docker
# Compose (nom du service, voir resolver ci-dessous pour la ré-résolution
# à chaque requête plutôt qu'une résolution figée au démarrage).
#
# HTTPS via certificat Tailscale (voir
# docs/superpowers/specs/2026-08-14-tailscale-remote-access-design.md) :
# écoute en 443 en interne, le port externe publié reste 5173 (mapping
# asymétrique dans docker-compose.prod.yml).
server {
    listen 443 ssl;
    ssl_certificate /certs/cert.pem;
    ssl_certificate_key /certs/key.pem;
    root /usr/share/nginx/html;
    index index.html;

    location /api/ {
        # resolver 127.0.0.11 = serveur DNS interne de Docker Compose (adresse
        # standard sur tout réseau Compose). Sans "resolver" + "set", nginx
        # résout "dashboard-api" une seule fois au démarrage du worker et
        # garde cette IP en cache indéfiniment -- si le conteneur dashboard-api
        # est recréé seul (ex: déploiement de l'API sans toucher au frontend),
        # Docker lui donne une nouvelle IP et nginx continue de proxifier vers
        # l'ancienne IP morte (502 silencieux) jusqu'à un restart manuel du
        # frontend. "set $api_upstream ..." force nginx à repasser par le
        # resolver à chaque requête (proxy_pass vers une variable, contrairement
        # à un proxy_pass vers un littéral, redéclenche la résolution DNS).
        # $request_uri (pas un chemin littéral après la variable) : quand
        # proxy_pass cible une variable, nginx n'applique plus son
        # remplacement habituel du préfixe de location -- tout texte littéral
        # écrit après la variable devient l'URI ENTIÈRE envoyée en amont, et
        # le chemin/la query string réels du client sont silencieusement
        # perdus (ex: "/api/cards?search=x" deviendrait juste "/api/").
        # $request_uri restaure le chemin+query string d'origine tels quels.
        #
        # dashboard-api sert lui-même en HTTPS désormais (Task 2) -- nginx
        # doit donc parler HTTPS à l'upstream, pas HTTP.
        resolver 127.0.0.11 valid=10s;
        set $api_upstream https://dashboard-api:8000;
        proxy_pass $api_upstream$request_uri;
        # dashboard-api présente un certificat émis pour le nom Tailscale du
        # VPS, pas pour "dashboard-api" (nom DNS interne Docker) -- SANS ces
        # deux directives, la vérification du nom d'hôte échouerait sur
        # cette connexion interne. Plutôt que désactiver la vérification
        # (proxy_ssl_verify off, qui accepterait n'importe quel certificat),
        # on vérifie explicitement CE certificat précis (même fichier que
        # celui monté sur dashboard-api, Task 2) et on force le nom attendu
        # dans le SNI/la vérification de nom à celui pour lequel il a
        # effectivement été émis.
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /certs/cert.pem;
        proxy_ssl_name annonces-vps.tail094416.ts.net;
    }

    # try_files ... /index.html : nécessaire pour un routeur côté client
    # (react-router-dom, Task 7) -- sans ça, un accès direct à /collection ou
    # /cartes/xyz renverrait un 404 Nginx (le fichier collection/xyz n'existe
    # pas sur le disque, seul index.html + JS sait router ces chemins).
    location / {
        try_files $uri /index.html;
    }
}
```

- [x] **Step 2: Builder l'image et valider la config nginx**

Run: `docker build -t dashboard-frontend-test -f frontend/Dockerfile frontend/ && docker run --rm dashboard-frontend-test nginx -t`
Expected: `nginx: configuration file /etc/nginx/nginx.conf test is successful` (l'absence des fichiers `/certs/*.pem` au moment du test est attendue et sans effet sur `nginx -t`, qui valide la syntaxe, pas l'existence des fichiers référencés à l'exécution).

- [x] **Step 3: Commit**

```bash
git add frontend/nginx.conf
git commit -m "feat: nginx écoute en HTTPS (certificat Tailscale), proxy HTTPS vers dashboard-api"
```

---

## Task 4: `Dockerfile.api` / commande uvicorn — HTTPS

**Files:**
- Modify: `Dockerfile.api:16`

**Interfaces:**
- Consumes: `/certs/cert.pem`, `/certs/key.pem` (montés par Task 2).

- [x] **Step 1: Modifier la commande `CMD`**

```dockerfile
# Image de l'API FastAPI du dashboard (src/api/). Même logique que
# Dockerfile.airflow : seules les dépendances tierces sont installées dans
# l'image, le code (src/) arrive par volume monté (docker-compose.prod.yml)
# pour éviter de reconstruire l'image à chaque modification.
FROM python:3.11-slim

COPY pyproject.toml /app/pyproject.toml
WORKDIR /app

# Guillemets obligatoires sur chaque contrainte de version -- même piège que
# Dockerfile.airflow (RUN passe par "sh -c", ">=" non quoté serait interprété
# comme une redirection shell plutôt qu'une comparaison pip).
RUN pip install --no-cache-dir \
    "fastapi>=0.111" "uvicorn[standard]>=0.30" "psycopg[binary]>=3.1" "python-dotenv>=1.0"

# --ssl-keyfile/--ssl-certfile : certificat Tailscale monté par
# docker-compose.prod.yml sur /certs (voir
# docs/superpowers/specs/2026-08-14-tailscale-remote-access-design.md).
# uvicorn consomme le PEM directement, pas de conversion nécessaire
# (contrairement à Metabase/Jetty).
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--ssl-keyfile", "/certs/key.pem", "--ssl-certfile", "/certs/cert.pem"]
```

- [x] **Step 2: Commit**

```bash
git add Dockerfile.api
git commit -m "feat: dashboard-api sert en HTTPS via le certificat Tailscale monté"
```

---

## Task 5: Déploiement VPS + vérification bout-en-bout

**Files:** aucun fichier repo modifié — ce task documente et exécute les commandes sur le VPS, cohérent avec le pattern déjà utilisé pour Metabase (`2026-08-09-metabase-db-ui-design.md`, section "Configuration initiale").

**Interfaces:**
- Consumes : tout ce qui précède (Tasks 1-4).

- [x] **Step 1: Créer le dossier de certificats et émettre le certificat initial**

Sur le VPS :
```bash
mkdir -p /home/ubuntu/card-price-tracker/tailscale-certs
sudo tailscale cert \
  --cert-file=/home/ubuntu/card-price-tracker/tailscale-certs/cert.pem \
  --key-file=/home/ubuntu/card-price-tracker/tailscale-certs/key.pem \
  annonces-vps.tail094416.ts.net
sudo chown ubuntu:ubuntu /home/ubuntu/card-price-tracker/tailscale-certs/*.pem
```
Expected: deux fichiers `cert.pem`/`key.pem` créés, propriétaire `ubuntu`.

- [x] **Step 2: Ajouter les nouvelles variables au `.env` du VPS**

Éditer `/home/ubuntu/card-price-tracker/.env`, ajouter :
```
TAILSCALE_IP=100.116.232.89
METABASE_KEYSTORE_PASSWORD=<générer via: python3 -c "import secrets; print(secrets.token_hex(30))">
```

- [x] **Step 3: Générer le keystore Metabase initial**

Sur le VPS (charge `METABASE_KEYSTORE_PASSWORD` depuis `.env`) :
```bash
cd /home/ubuntu/card-price-tracker
set -a && source .env && set +a
openssl pkcs12 -export \
  -in tailscale-certs/cert.pem -inkey tailscale-certs/key.pem \
  -out tailscale-certs/keystore.p12 \
  -passout "pass:$METABASE_KEYSTORE_PASSWORD"
chown ubuntu:ubuntu tailscale-certs/keystore.p12
# 644, pas 600 (défaut d'openssl) : le process Java de Metabase tourne dans
# le conteneur sous un uid non-root distinct du uid hôte "ubuntu"
# propriétaire du fichier -- avec 600 il ne peut pas lire le keystore et
# Metabase crash-loop en AccessDeniedException (observé en Task 5 réelle).
chmod 644 tailscale-certs/keystore.p12
```
Expected: `tailscale-certs/keystore.p12` créé, lisible par le process Metabase (644).

- [x] **Step 4: Pull + build + recréer uniquement les 3 conteneurs concernés**

```bash
cd /home/ubuntu/card-price-tracker
git pull
docker compose -f docker-compose.prod.yml up -d --build dashboard-frontend dashboard-api metabase
```
Expected: les 3 conteneurs redémarrent, `airflow-*` et `db` restent inchangés (pas dans la commande).

- [x] **Step 5: Vérifier que les 3 conteneurs sont `healthy`**

```bash
docker ps --format '{{.Names}}\t{{.Status}}'
```
Expected: `card-price-tracker-metabase-1`, `card-price-tracker-dashboard-api-1`, `card-price-tracker-dashboard-frontend-1` tous `Up ... (healthy)` (dashboard-frontend n'a pas de healthcheck défini, `Up` suffit pour lui).

- [x] **Step 6: Vérifier l'accessibilité HTTPS depuis un appareil du tailnet**

Depuis le PC (`fedora`) ou le mobile (`iphone173`) sur le tailnet. Utiliser le
nom MagicDNS, pas l'IP Tailscale : le certificat est émis pour
`annonces-vps.tail094416.ts.net`, pas pour l'IP -- interroger par IP ferait
échouer la vérification du nom d'hôte par construction, ce qui invaliderait
ce test.
```bash
curl -s -o /dev/null -w "%{http_code}\n" https://annonces-vps.tail094416.ts.net:3000/
curl -s -o /dev/null -w "%{http_code}\n" https://annonces-vps.tail094416.ts.net:8000/api/health
curl -s -o /dev/null -w "%{http_code}\n" https://annonces-vps.tail094416.ts.net:5173/
# Test de non-régression pour la vérification TLS entre nginx
# (dashboard-frontend) et dashboard-api (voir frontend/nginx.conf,
# proxy_ssl_trusted_certificate/proxy_ssl_verify_depth) : sans ce fix,
# nginx échoue à valider le certificat de dashboard-api en HTTPS interne et
# renvoie 502 sur toute requête /api/, même si dashboard-api lui-même est
# joignable directement sur le port 8000 ci-dessus.
curl -s -o /dev/null -w "%{http_code}\n" https://annonces-vps.tail094416.ts.net:5173/api/health
```
Expected: un code HTTP pour chaque commande (200/302/etc.), pas de timeout, pas d'erreur de certificat. En particulier, la dernière commande (`/api/health` via le frontend) doit renvoyer 200, pas 502.

- [x] **Step 7: Vérifier l'absence d'exposition publique**

Depuis une machine hors tailnet (ou en testant l'IP publique du VPS) :
```bash
curl -s -o /dev/null -w "%{http_code}\n" --max-time 5 https://164.132.243.29:3000/
curl -s -o /dev/null -w "%{http_code}\n" --max-time 5 https://164.132.243.29:8000/
curl -s -o /dev/null -w "%{http_code}\n" --max-time 5 https://164.132.243.29:5173/
```
Expected: timeout sur les 3 (pas de réponse) — confirme que ces ports ne sont pas exposés sur l'interface publique.

- [x] **Step 8: Vérifier qu'aucun avertissement de certificat n'apparaît**

Ouvrir `https://annonces-vps.tail094416.ts.net:3000`, `:8000/api/health`, `:5173` dans un navigateur (PC ou mobile).
Expected: cadenas HTTPS valide, aucun avertissement de certificat (le certificat Tailscale est signé par une CA publiquement reconnue).

- [x] **Step 9: Installer le script de renouvellement dans le cron root**

```bash
sudo crontab -e
```
Ajouter :
```
0 4 * * 1 METABASE_KEYSTORE_PASSWORD=<valeur> /home/ubuntu/card-price-tracker/scripts/renew_tailscale_cert.sh >> /var/log/tailscale-cert-renew.log 2>&1
```

- [x] **Step 10: Vérifier qu'un MTA local existe pour que cron puisse envoyer un mail d'échec**

```bash
which sendmail postfix 2>/dev/null || echo "AUCUN MTA LOCAL -- ajouter MAILTO=<adresse externe> en tête de la crontab root"
```
Si aucun MTA n'est trouvé, ajouter en première ligne de `sudo crontab -e` :
```
MAILTO=<adresse email de l'utilisateur>
```

- [x] **Step 11: Test à blanc du script de renouvellement (sans attendre le cron)**

```bash
cd /home/ubuntu/card-price-tracker
set -a && source .env && set +a
sudo -E scripts/renew_tailscale_cert.sh
echo "exit code: $?"
```
Expected: `exit code: 0`. Comme le certificat vient d'être émis (Step 1), `tailscale cert` est un no-op (hash inchangé) — donc pas de régénération de keystore ni de redémarrage de conteneurs à cette exécution ; confirme seulement que le script tourne sans erreur.
