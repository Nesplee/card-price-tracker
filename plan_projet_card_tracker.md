# Plan de projet — Card Price Tracker Pipeline

## Contexte pour Claude Code
Ce document est un plan de haut niveau. Le but est que tu (Claude Code) le lises et rédiges des specs techniques détaillées (architecture précise, schéma de base de données complet, structure de fichiers du repo, découpage en tâches) pour chaque étape, en posant des questions si des choix technique ne sont pas encore tranchés.

## Profil de l'auteur
Débutant en développement (reconversion depuis la restauration, étudiant à l'École 42), en stage Data/Digital chez un acteur financier (utilisation de n8n, bases SQL, automatisation). Objectif : décrocher un poste junior Data Engineer en Suisse romande d'ici 3 mois. Temps disponible limité (à côté du stage). Priorité : un projet fonctionnel et démontrable plutôt qu'exhaustif.

## Objectif du projet
Construire un pipeline de données de bout en bout qui suit l'évolution des prix de cartes à collectionner (Pokémon / One Piece) sur CardMarket, pour :
1. Apprendre et démontrer les compétences clés d'un Data Engineer junior
2. Servir de pièce centrale du portfolio GitHub pour candidater

## Stack technique retenue
- **Langage** : Python
- **Base de données** : PostgreSQL (local via Docker pour le développement)
- **Orchestration** : Apache Airflow (local via Docker au départ)
- **Automatisation continue** : GitHub Actions (cron scheduled workflow) — voir section Automatisation
- **Modélisation** : schéma en étoile (star schema)
- **Versioning** : Git / GitHub, avec fichiers de migration SQL numérotés
- **Visualisation (optionnel, mois 3)** : Metabase ou notebook Python avec graphiques

## Architecture des données (principe à respecter dans les specs)
Trois zones distinctes, jamais mélangées :
1. **Raw** : copie brute de la réponse de l'API CardMarket, aucune transformation, avec métadonnées (`source`, `loaded_at`)
2. **Staging** : données nettoyées et typées (prix en float, noms sans espaces superflus, etc.)
3. **Production (Data Warehouse)** : schéma en étoile avec :
   - Table de faits : `fact_price_history` (card_id, date_id, platform_id, price, quantity_available)
   - Dimensions : `dim_card`, `dim_date`, `dim_platform`

Principes non négociables à intégrer dans les specs :
- **Idempotence** : toute réexécution du pipeline ne doit pas dupliquer les données (UPSERT ou delete & reload)
- **Gestion des erreurs** : le pipeline doit échouer explicitement (logs + exception) si une source est indisponible, jamais insérer de données silencieusement fausses
- **Traçabilité** : chaque ligne raw/staging doit avoir `loaded_at` et `source`
- **Qualité des données** : validation avant insertion en staging (types, valeurs manquantes, valeurs aberrantes), avec table de quarantaine pour les lignes rejetées
- **Migrations SQL versionnées** : chaque changement de structure de table dans un fichier numéroté (`migrations/001_...sql`, etc.)

## Stratégie d'automatisation
- Le pipeline (extraction → nettoyage → chargement) doit pouvoir tourner automatiquement chaque jour, sans intervention manuelle.
- Choix retenu : **GitHub Actions** avec un `schedule` cron, plutôt qu'un serveur externe à gérer (pas de VM à maintenir, gratuit, simple).
- Le rôle d'Airflow dans le projet est pédagogique et local (comprendre les DAGs et l'orchestration), pas nécessairement l'outil qui tourne en prod pour ce projet précis — à trancher : est-ce qu'on garde Airflow en local uniquement pour la démo, ou est-ce qu'on essaie de le faire tourner aussi via GitHub Actions / conteneur programmé ? (Question à soulever dans les specs.)

## Plan en 3 mois — jalons

### Mois 1 — Fondations Python + SQL
Livrable de fin de mois : un script Python qui appelle l'API CardMarket, récupère des données de prix, et les insère dans une table PostgreSQL locale (Docker).
Sous-étapes attendues dans les specs :
- Setup environnement (Python, venv, Docker, PostgreSQL local)
- Script d'extraction API avec gestion des erreurs de base
- Script d'insertion SQL (table unique, pas encore de schéma étoile)

### Mois 2 — Pipeline complet avec architecture data
Livrable de fin de mois : pipeline structuré raw → staging → production, avec schéma en étoile, orchestré par Airflow en local, idempotent et loggé.
Sous-étapes attendues dans les specs :
- Modélisation complète du schéma étoile (tables de faits/dimensions)
- Scripts de migration SQL
- Logique de nettoyage/validation avec table de quarantaine
- DAG Airflow avec dépendances entre tâches (extract >> clean >> load)
- Tests d'idempotence (rejouer le pipeline, vérifier absence de doublons)

### Mois 3 — Automatisation + portfolio
Livrable de fin de mois : pipeline qui tourne automatiquement chaque jour via GitHub Actions, dashboard simple sur les données, repo GitHub présentable.
Sous-étapes attendues dans les specs :
- Configuration du workflow GitHub Actions (cron, secrets pour les credentials DB)
- Décision d'hébergement de la base de données pour la version automatisée (ex: rester en local n'est pas possible si GitHub Actions doit écrire quelque part accessible — à trancher : DB cloud gratuite type Supabase/Neon, ou autre option)
- Dashboard (Metabase ou notebook) branché sur les données accumulées
- README avec schéma d'architecture, structure de repo propre, code commenté

## Ce qui est volontairement exclu du scope (pour l'instant)
- dbt
- BigQuery / Snowflake
- Certification AWS Cloud Practitioner
- Kubernetes ou infrastructure cloud complexe

## Demande à Claude Code
Merci de :
1. Poser les questions nécessaires pour lever les zones d'ambiguïté marquées ci-dessus (notamment le choix d'hébergement de la base pour la version automatisée du mois 3)
2. Proposer une structure de repo GitHub complète
3. Rédiger des specs détaillées mois par mois, avec des tâches suffisamment petites pour être réalisées en sessions courtes (le temps disponible de l'auteur est limité chaque semaine)
