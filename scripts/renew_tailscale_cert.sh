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
KEYSTORE_PASSWORD="${METABASE_KEYSTORE_PASSWORD:?METABASE_KEYSTORE_PASSWORD doit être exporté avant d\'appeler ce script}"
export KEYSTORE_PASSWORD

OLD_HASH=$(sha256sum "$CERT_DIR/cert.pem" | awk '{print $1}')

tailscale cert --cert-file="$CERT_DIR/cert.pem" --key-file="$CERT_DIR/key.pem" "$DOMAIN"
chown ubuntu:ubuntu "$CERT_DIR"/cert.pem "$CERT_DIR"/key.pem
# group root (gid 0), 640 : airflow-webserver tourne en "AIRFLOW_UID:0"
# (convention de l'image officielle Airflow) et lit key.pem directement
# (comme dashboard-api, pas de keystore) -- sans ce chgrp/chmod il ne peut
# pas lire un fichier 600 appartenant à "ubuntu" (même défaut que celui
# corrigé pour keystore.p12 ci-dessous, mais ici on garde la clé privée
# fermée à "other" plutôt que 644, dashboard-api/metabase n'en ont pas besoin
# via cette permission -- dashboard-api tourne en root, metabase ne lit que
# keystore.p12).
chgrp 0 "$CERT_DIR/key.pem"
chmod 640 "$CERT_DIR/key.pem"

NEW_HASH=$(sha256sum "$CERT_DIR/cert.pem" | awk '{print $1}')

if [ "$OLD_HASH" != "$NEW_HASH" ]; then
  echo "$(date): certificat renouvelé, régénération du keystore Metabase"
  # Metabase (Jetty) n'accepte pas de PEM brut -- conversion en keystore
  # PKCS12, seule étape qui n'existe pas dans le script équivalent de
  # DE_ANNONCES (n8n consomme le PEM directement).
  openssl pkcs12 -export \
  -in "$CERT_DIR/cert.pem" -inkey "$CERT_DIR/key.pem" \
  -out "$CERT_DIR/keystore.p12" \
  -passout env:KEYSTORE_PASSWORD
  chown ubuntu:ubuntu "$CERT_DIR/keystore.p12"
  # 644, pas 600 (défaut d'openssl) : le process Java de Metabase tourne
  # dans le conteneur sous un uid non-root (2000) distinct du uid hôte
  # "ubuntu" propriétaire du fichier -- avec 600 il ne peut pas lire le
  # keystore et Metabase crash-loop en AccessDeniedException. Le mot de
  # passe du PKCS12 reste la protection réelle du contenu.
  chmod 644 "$CERT_DIR/keystore.p12"

  echo "$(date): redémarrage dashboard-frontend, dashboard-api, metabase, airflow-webserver"
  cd /home/ubuntu/card-price-tracker && docker compose -f docker-compose.prod.yml restart dashboard-frontend dashboard-api metabase airflow-webserver
fi
