#!/usr/bin/env bash
# Spécifique au déploiement VPS actuel (chemin en dur, nom MagicDNS lu
# depuis l'environnement -- voir TAILSCALE_HOSTNAME).
# Programmé via `sudo crontab -e` : 0 4 * * 1 (chaque lundi 4h). Nécessite
# root (tailscale cert). Le cert Tailscale expire tous les ~90 jours ;
# `tailscale cert` est un no-op si le cert en cours est encore valide.
#
# Dédié à ce repo -- ne partage pas de certificat avec un autre projet
# hébergé sur le même compte Tailscale, même si le tailnet est le même
# (chaque VPS/projet reste isolé, avec son propre certificat).
set -euo pipefail

CERT_DIR=/home/ubuntu/card-price-tracker/tailscale-certs
DOMAIN="${TAILSCALE_HOSTNAME:?TAILSCALE_HOSTNAME doit être exporté avant d\'appeler ce script}"

OLD_HASH=$(sha256sum "$CERT_DIR/cert.pem" | awk '{print $1}')

tailscale cert --cert-file="$CERT_DIR/cert.pem" --key-file="$CERT_DIR/key.pem" "$DOMAIN"
chown ubuntu:ubuntu "$CERT_DIR"/cert.pem "$CERT_DIR"/key.pem
# group root (gid 0), 640 : airflow-webserver tourne en "AIRFLOW_UID:0"
# (convention de l'image officielle Airflow) et lit key.pem directement --
# sans ce chgrp/chmod il ne peut pas lire un fichier 600 appartenant à
# "ubuntu". La clé privée reste fermée à "other" (640 plutôt que 644) :
# dashboard-api et dashboard-frontend (nginx) tournent en root et n'ont pas
# besoin de cette permission.
chgrp 0 "$CERT_DIR/key.pem"
chmod 640 "$CERT_DIR/key.pem"

NEW_HASH=$(sha256sum "$CERT_DIR/cert.pem" | awk '{print $1}')

if [ "$OLD_HASH" != "$NEW_HASH" ]; then
  echo "$(date): certificat renouvelé"
  echo "$(date): redémarrage dashboard-frontend, dashboard-api, airflow-webserver"
  cd /home/ubuntu/card-price-tracker && docker compose -f docker-compose.prod.yml restart dashboard-frontend dashboard-api airflow-webserver
fi
