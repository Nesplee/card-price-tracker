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
