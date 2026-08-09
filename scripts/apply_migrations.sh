#!/usr/bin/env bash
# set -e : arrête le script à la première commande qui échoue (pas d'exécution
#          en cascade sur un état déjà cassé).
# set -u : erreur si on utilise une variable non définie (évite les typos
#          silencieuses comme $POSTGRES_ADIN_USER).
# set -o pipefail : dans un pipeline "cmd1 | cmd2", fait échouer le script si
#          cmd1 échoue, même si cmd2 réussit (sinon l'erreur serait masquée).
set -euo pipefail

# Applique migrations/*.sql non encore appliquées, dans l'ordre, chacune dans
# sa propre transaction. Garde une trace dans public.schema_migrations pour ne
# jamais rejouer un fichier déjà appliqué. Le mot de passe applicatif est fixé
# ici (hors fichier versionné) pour respecter la règle "aucun secret en dur".

# Charge les variables du fichier .env (POSTGRES_ADMIN_USER, POSTGRES_DB, ...)
# dans l'environnement de ce script, comme si on les avait tapées à la main.
source .env

# Vérification anticipée des mots de passe requis (bug réel trouvé en review,
# 2026-08-09) : sans ce bloc, un .env incomplet (ex: DASHBOARD_READER_PASSWORD
# absent sur un déploiement existant qui met à jour son .env après-coup) fait
# planter le script sur `set -u` SEULEMENT à la toute dernière ligne -- après
# que TOUTES les migrations SQL (dont la création du rôle SANS mot de passe)
# aient déjà été appliquées et enregistrées dans schema_migrations. Résultat :
# un rôle créé mais sans mot de passe, et un rejeu du script qui saute la
# migration déjà marquée "appliquée" au lieu de la retenter -- un état bancal
# qui ne se corrige pas tout seul. `${VAR:?message}` fait échouer le script
# IMMÉDIATEMENT si la variable est vide/absente, avant la moindre migration.
: "${POSTGRES_APP_PASSWORD:?POSTGRES_APP_PASSWORD manquant dans .env}"
: "${DASHBOARD_READER_PASSWORD:?DASHBOARD_READER_PASSWORD manquant dans .env}"

# Fichier compose à utiliser : premier argument du script si fourni, sinon
# "docker-compose.yml" par défaut (usage local inchangé). Permet de réutiliser
# ce même script en prod avec "./scripts/apply_migrations.sh docker-compose.prod.yml".
COMPOSE_FILE="${1:-docker-compose.yml}"

# Commande psql réutilisée pour toutes les requêtes admin ci-dessous :
# - exec -T : exécute une commande dans le conteneur "db" déjà démarré,
#   -T désactive l'allocation d'un pseudo-terminal (nécessaire en script,
#   sans terminal interactif).
# - ON_ERROR_STOP=1 : psql s'arrête dès qu'une requête SQL échoue, au lieu de
#   continuer sur les suivantes.
ADMIN_PSQL="docker compose -f ${COMPOSE_FILE} exec -T db psql -v ON_ERROR_STOP=1 -U ${POSTGRES_ADMIN_USER} -d ${POSTGRES_DB}"

# Table de suivi des migrations déjà appliquées (dans le schéma "public" par
# défaut). IF NOT EXISTS : ne fait rien si elle existe déjà (idempotent).
$ADMIN_PSQL -c "CREATE TABLE IF NOT EXISTS public.schema_migrations (filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());"

# Boucle sur tous les fichiers .sql du dossier migrations/, triés par ordre
# alphabétique (donc par leur préfixe numérique 001_, 002_, ...).
for filepath in migrations/*.sql; do
    # basename retire le chemin du dossier, ne garde que le nom de fichier
    # (ex: "migrations/001_x.sql" -> "001_x.sql").
    filename=$(basename "$filepath")
    # -tAc : "tuples only" + "unaligned" + "command" -> renvoie uniquement la
    # valeur brute de la requête (pas d'en-tête ni de mise en forme), pratique
    # pour la capturer dans une variable bash.
    already_applied=$($ADMIN_PSQL -tAc "SELECT 1 FROM public.schema_migrations WHERE filename = '${filename}';")
    if [ "$already_applied" = "1" ]; then
        echo "Skip (déjà appliquée) : ${filename}"
        continue
    fi
    echo "Applique : ${filename}"
    # -f /migrations/... exécute le fichier SQL monté en lecture seule dans le
    # conteneur (voir docker-compose.yml) plutôt que de le streamer depuis
    # l'hôte : évite les soucis d'encodage/chemin entre hôte et conteneur.
    docker compose -f ${COMPOSE_FILE} exec -T db psql -v ON_ERROR_STOP=1 -U "${POSTGRES_ADMIN_USER}" -d "${POSTGRES_DB}" -f "/migrations/${filename}"
    # Une fois la migration appliquée avec succès, on l'enregistre pour ne
    # jamais la rejouer lors d'un prochain appel de ce script.
    $ADMIN_PSQL -c "INSERT INTO public.schema_migrations (filename) VALUES ('${filename}');"
done

# Synchronise le mot de passe du rôle applicatif pipeline_app (créé sans mot
# de passe dans la migration SQL) avec la valeur actuelle de .env. Ainsi le
# secret ne transite jamais par un fichier versionné (les migrations SQL).
$ADMIN_PSQL -c "ALTER ROLE pipeline_app WITH PASSWORD '${POSTGRES_APP_PASSWORD}';"
echo "Mot de passe de pipeline_app synchronisé avec .env"

# Synchronise le mot de passe du rôle de lecture dashboard_reader (créé sans
# mot de passe dans la migration SQL) avec la valeur actuelle de .env.
$ADMIN_PSQL -c "ALTER ROLE dashboard_reader WITH PASSWORD '${DASHBOARD_READER_PASSWORD}';"
echo "Mot de passe de dashboard_reader synchronisé avec .env"
