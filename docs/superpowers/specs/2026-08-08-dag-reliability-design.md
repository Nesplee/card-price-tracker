# Card Price Tracker — Fiabilité du DAG face aux pannes prolongées de pokemontcg.io — Design

**Statut :** approuvé par l'utilisateur (2026-08-08), en attente de plan d'implémentation.

## Contexte et problème

Le run automatique quotidien du 2026-08-07 (`scheduled__2026-08-07T00:00:00+00:00`, déclenché tout seul par le scheduler juste après minuit UTC) a échoué : la tâche `extract_and_load_raw` a épuisé ses 21 tentatives (`retries=20`) sans finir l'extraction. Diagnostic fait en lisant les logs de chaque tentative sur le VPS :

- L'API `pokemontcg.io` a subi une panne large et prolongée cette nuit-là (erreurs 500/502 sur des pages différentes selon les tentatives : page 1, 28, 49...), pas une seule page isolée.
- Le mécanisme de checkpoint/reprise (`src/extract/pipeline.py`, commit par page) a fonctionné exactement comme prévu : chaque tentative reprenait bien où la précédente s'était arrêtée, sans perte ni doublon. Progression cumulée mesurée : de la page 1 à la page 49 sur les 21 tentatives (~2,3 pages gagnées par tentative en moyenne, sur ~80 pages à couvrir).
- Résultat concret : épuisement du budget de retries avant la fin de l'extraction → **aucune donnée chargée pour le 2026-08-07 en production**.

Aucune alerte n'a signalé cet échec ; il n'a été découvert qu'en inspectant les logs à la demande de l'utilisateur. L'utilisateur a explicitement écarté la piste "alerting" pour l'instant : l'objectif choisi n'est pas d'être notifié en cas d'échec, mais de maximiser les chances que le DAG réussisse tout seul, quitte à absorber une panne prolongée via plus de tentatives.

**Second problème découvert pendant l'investigation** (`dags/card_price_pipeline_dag.py:107`) : la tâche calcule `extracted_date = datetime.now(UTC).date()` — recalculé à **chaque tentative**, pas une seule fois pour tout le run. Tant que toutes les tentatives d'un même run restent le même jour calendaire UTC, le comportement reste correct. Mais si une séquence de retries suffisamment longue traverse minuit UTC, la tentative suivante calculerait une `extracted_date` différente (le jour suivant) ; le checkpoint (`_resume_page`, qui compte les lignes déjà chargées pour `(extracted_date, source)`) ne verrait aucune ligne pour cette "nouvelle" date et repartirait de la page 1 — perdant silencieusement toute la progression déjà faite pour la date d'origine, qui ne recevrait alors jamais de données complètes. Cette nuit le run est resté sous ce seuil (21 tentatives en ~26 minutes), mais augmenter significativement le budget de retries sans corriger ce point augmente mécaniquement la probabilité de le déclencher.

## Décision

Deux changements, dans le même fichier (`dags/card_price_pipeline_dag.py`), aucun changement dans `src/` :

### 1. Augmenter le budget de retries

`retries=20` → `retries=60` sur la tâche `extract_and_load_raw` (`retry_delay=30s` inchangé).

**Justification du chiffre** : cette nuit, ~35 tentatives auraient probablement suffi à ce rythme de progression (49 pages / 21 tentatives ≈ 2,3 pages/tentative → 80 pages / 2,3 ≈ 35). 60 laisse une marge confortable pour une panne encore pire, tout en restant borné (pas de retry infini). Coût dans le pire cas : ~30 minutes de délai cumulé (60 × 30s), sans aucun risque de donnée — le checkpoint garantit qu'aucune tentative ne repart de zéro ni ne duplique.

`clean_to_staging` et `load_to_warehouse` gardent `retries=2` : ce sont des opérations SQL locales déterministes, pas des appels à une API externe instable (raisonnement déjà établi et documenté dans le DAG, inchangé).

### 2. Figer `extracted_date` pour toute la durée du run

Remplacer `datetime.now(UTC).date()` par une valeur calculée **une seule fois à la création du DagRun**, réutilisée telle quelle par toutes les tentatives de la tâche.

**Mécanisme** : injecter `dag_run` dans la signature de la tâche TaskFlow (`dag_run: DagRun`) — Airflow le peuple automatiquement via le contexte d'exécution, aucune configuration supplémentaire nécessaire. Calculer `extracted_date = dag_run.start_date.date()`.

**Pourquoi `dag_run.start_date` et pas `logical_date`/`data_interval_start`** : ce pipeline scrape l'état *actuel* des prix (pas une reconstruction historique) — la sémantique voulue pour `extracted_date` est "le jour calendaire où ce run a effectivement tourné", pas la convention de partitionnement par intervalle de données d'Airflow. Pour un DAG `@daily` déclenché juste après minuit, `logical_date` vaudrait la veille (le début de l'intervalle fermé), ce qui mal-étiquetterait un snapshot pris aujourd'hui comme appartenant à hier. `dag_run.start_date`, lui, correspond exactement au comportement actuel voulu (`datetime.now(UTC).date()` au moment du déclenchement) tout en étant **fixé une fois pour toutes** à la création du DagRun — il ne change jamais entre les tentatives d'une même tâche, contrairement à `datetime.now()` qui se réévalue à chaque appel.

**Effet** : le checkpoint (`_resume_page`) continue de recevoir exactement le même `extracted_date` à chaque tentative, quelle que soit la durée totale des retries ou le nombre de minuits UTC traversés entre-temps.

## Hors scope (rappel)

- Aucune alerte (email/Slack/Discord) — explicitement écarté par l'utilisateur pour cette itération. Reste une piste future si le besoin réapparaît.
- Aucun changement du budget de retries interne au client HTTP (`PokemonTcgClient`, `max_attempts=4` par page) — le mécanisme de checkpoint + retries Airflow au niveau tâche suffit à absorber une panne prolongée, quitte à être moins efficace qu'un réglage fin des deux niveaux ensemble. Optimisation possible plus tard si les runs continuent de consommer beaucoup de tentatives.
- Aucun changement de `clean_to_staging`/`load_to_warehouse` (`retries=2` conservé).

## Tests à adapter

Aucun test existant ne couvre directement le contenu de `dags/card_price_pipeline_dag.py` (le DAG n'est pas exécuté dans la suite de tests actuelle — confirmé par le finding différé du Mois 2 "CI qui ne parse jamais le DAG"). Ce changement ne casse donc aucun test existant. Vérification prévue en production : déclencher manuellement le DAG après déploiement et confirmer dans les logs que `extracted_date` correspond bien à la date attendue.
