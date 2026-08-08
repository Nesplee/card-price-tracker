# Card Price Tracker — Fiabilité du DAG (retries, extracted_date, horaire) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Faire en sorte que le DAG `card_price_pipeline` survive à une panne prolongée de `pokemontcg.io` (comme celle du 2026-08-07, qui a fait échouer le run automatique après épuisement des retries), en corrigeant au passage un bug latent qui aurait pu corrompre le checkpoint si une longue séquence de retries traversait minuit UTC — et décaler le déclenchement automatique à 07:00 UTC.

**Architecture:** Un seul fichier change, `dags/card_price_pipeline_dag.py` : le décorateur `@dag` (nouvel horaire cron), le décorateur `@task` de `extract_and_load_raw` (budget de retries), et le corps de cette même tâche (source de `extracted_date`, désormais figée pour tout le run via `dag_run.start_date` au lieu d'être recalculée à chaque tentative via `datetime.now()`). Aucun changement dans `src/` : le mécanisme de checkpoint/reprise (`src/extract/pipeline.py`) reste identique, il reçoit juste une valeur `extracted_date` plus stable.

**Tech Stack:** Identique à l'existant (Apache Airflow 2.9.3, TaskFlow API). Aucune nouvelle dépendance.

## Global Constraints

- Un seul fichier applicatif modifié : `dags/card_price_pipeline_dag.py`. Aucun changement dans `src/`.
- Le mécanisme de checkpoint par page (commit après chaque page, `src/extract/pipeline.py`) reste la seule source de vérité pour la reprise — ce plan ne le touche pas, il fiabilise seulement ce qui l'entoure (budget de retries, stabilité de `extracted_date`).
- `clean_to_staging` et `load_to_warehouse` gardent `retries=2` (opérations SQL locales déterministes, pas d'appel API externe instable) — seul `extract_and_load_raw` voit son budget de retries augmenté.
- Aucune alerte (email/Slack/Discord) — explicitement hors scope de cette itération (décision utilisateur).
- Lint (`ruff`) et formatage (`black`) appliqués avant chaque commit.

**Référence :** `docs/superpowers/specs/2026-08-08-dag-reliability-design.md` (spec approuvée, contient le diagnostic complet du run échoué et le raisonnement détaillé derrière chaque changement).

---

### Task 1 : Modifier le DAG (retries, extracted_date figé, horaire 07:00 UTC)

**Files:**
- Modify: `dags/card_price_pipeline_dag.py`

**Interfaces:**
- Consomme : `dag_run` — objet `airflow.models.DagRun` injecté automatiquement par TaskFlow (aucun changement d'appel nécessaire ailleurs dans le fichier).
- Produit : le DAG `card_price_pipeline` inchangé dans sa structure (toujours 3 tâches, mêmes dépendances, même signature de retour `extracted_date_iso: str` transmise via XCom à `clean_to_staging`) — seuls schedule, retries et la source de `extracted_date` changent.

- [ ] **Step 1 : Ajouter l'import de `DagRun`**

En haut du fichier, avec les autres imports Airflow :
```python
from airflow.decorators import dag, task
from airflow.models import DagRun
```

- [ ] **Step 2 : Remplacer `schedule="@daily"` par le cron 07:00 UTC**

Dans le décorateur `@dag(...)`, remplacer la ligne `schedule="@daily",` par :
```python
    # schedule="0 7 * * *" (cron : minute=0, heure=7, tous les jours) plutôt
    # que le raccourci "@daily" (équivalent à "0 0 * * *", minuit UTC) :
    # décision utilisateur de décaler le déclenchement automatique à 7h00
    # UTC. Valeur FIXE en UTC (pas de fuseau horaire local type
    # Europe/Zurich) : le DAG reste entièrement en UTC comme le reste de ce
    # fichier (voir start_date ci-dessous) -- ça évite toute complication
    # liée au changement d'heure été/hiver suisse, qui décalerait sinon
    # l'heure UTC réelle du déclenchement de 1h selon la saison.
    schedule="0 7 * * *",
```
(Le reste du décorateur `@dag` — `start_date`, `catchup=False` et leurs commentaires existants — ne change pas.)

- [ ] **Step 3 : Augmenter le budget de retries et documenter pourquoi**

Remplacer le commentaire précédant `@task(retries=20, retry_delay=timedelta(seconds=30))` (le long bloc qui commence par `# retries=20 SCOPÉ À CETTE SEULE TÂCHE...`) ainsi que la ligne du décorateur elle-même, par :
```python
    # retries=60 (porté de 20 à 60 le 2026-08-08, voir
    # docs/superpowers/specs/2026-08-08-dag-reliability-design.md) SCOPÉ À
    # CETTE SEULE TÂCHE (pas à default_args du DAG comme avant une review de
    # code) : valeur augmentée après un run réel (scheduled__2026-08-07) qui
    # a épuisé ses 20 retries (21 tentatives au total) sans finir
    # l'extraction, à cause d'une panne pokemontcg.io large et prolongée
    # cette nuit-là (erreurs 500/502 sur des pages différentes selon les
    # tentatives : page 1, 28, 49...). Preuve concrète tirée des logs de ce
    # run : chaque tentative progressait bien via le checkpoint (page 1 ->
    # 28 -> 49 sur 21 tentatives, ~2,3 pages gagnées par tentative en
    # moyenne), mais 21 tentatives n'ont pas suffi à couvrir les ~80 pages
    # du catalogue -- il en aurait fallu environ 35 à ce rythme. 60 laisse
    # une marge confortable pour une panne encore pire, tout en restant
    # borné. Coût dans le pire cas : ~30 minutes de délai cumulé
    # (60 x retry_delay=30s), sans aucun risque de donnée : le mécanisme de
    # checkpoint (commit par page, voir src/extract/pipeline.py) garantit
    # qu'aucune tentative ne repart de zéro ni ne duplique.
    #
    # retry_delay=30s (pas les 5 minutes par défaut d'Airflow) : la reprise
    # est immédiate et peu coûteuse grâce au checkpoint par page (aucun
    # travail perdu, on repart de la dernière page confirmée) -- attendre 5
    # minutes entre chaque tentative n'apporterait rien ici (l'API ne "guérit"
    # pas parce qu'on attend plus longtemps que 30s) et multiplierait juste
    # inutilement la durée totale d'un run qui doit déjà absorber jusqu'à 60
    # tentatives.
    #
    # Pourquoi retries=60 N'EST PAS mis sur les 3 tâches (via default_args du
    # DAG, comme c'était le cas avant) : clean_to_staging et load_to_warehouse
    # ci-dessous sont des opérations SQL locales, rapides et déterministes --
    # elles n'appellent aucune API externe instable. Si l'une d'elles échoue,
    # c'est très probablement un vrai bug (pas un aléa réseau), et le laisser
    # masqué derrière 60 tentatives x le délai entre essais retarderait sa
    # visibilité pour rien. Elles gardent donc retries=2 (une valeur modeste,
    # pour absorber un aléa transitoire de connexion à la DB locale, sans
    # cacher un vrai bug pendant longtemps).
    @task(retries=60, retry_delay=timedelta(seconds=30))
```

- [ ] **Step 4 : Figer `extracted_date` via `dag_run.start_date`**

Remplacer la signature de la tâche et sa première ligne :
```python
    def extract_and_load_raw() -> str:
```
```python
        extracted_date = datetime.now(UTC).date()
```
par :
```python
    def extract_and_load_raw(dag_run: DagRun) -> str:
```
```python
        # dag_run: DagRun -- paramètre injecté AUTOMATIQUEMENT par Airflow
        # (TaskFlow reconnaît "dag_run" comme nom de paramètre spécial et le
        # peuple avec l'objet DagRun courant, sans rien à configurer côté
        # appel, voir l'invocation extract_and_load_raw() en bas de fichier
        # -- inchangée). extracted_date = dag_run.start_date.date() (et NON
        # PLUS datetime.now(UTC).date(), voir
        # docs/superpowers/specs/2026-08-08-dag-reliability-design.md) :
        # dag_run.start_date est FIXÉ UNE SEULE FOIS à la création du
        # DagRun et ne change JAMAIS entre les tentatives d'une même tâche
        # -- contrairement à datetime.now(), qui se réévalue à CHAQUE
        # tentative. Avec retries=60 (voir ci-dessus), une séquence de
        # retries peut désormais durer assez longtemps pour traverser
        # minuit UTC ; recalculer "aujourd'hui" à chaque tentative aurait
        # alors changé extracted_date en cours de route, cassant le
        # checkpoint (_resume_page compte les lignes déjà chargées pour
        # L'ANCIENNE date, en trouve zéro pour la nouvelle, et repart de la
        # page 1 -- perdant toute la progression déjà faite pour la date
        # d'origine). dag_run.start_date élimine ce risque : la valeur
        # reste identique du début à la toute dernière tentative, quelle
        # que soit la durée totale du run.
        extracted_date = dag_run.start_date.date()
```
(Le reste du corps de la fonction — ouverture de connexion, appel à `run_extract_load`, `finally: conn.close()`, `return extracted_date.isoformat()` — ne change pas.)

- [ ] **Step 5 : Relire le fichier complet pour vérifier la cohérence**

Ouvrir `dags/card_price_pipeline_dag.py` et confirmer :
- L'import `from airflow.models import DagRun` est présent.
- `schedule="0 7 * * *"` remplace bien `schedule="@daily"`.
- `@task(retries=60, retry_delay=timedelta(seconds=30))` précède `def extract_and_load_raw(dag_run: DagRun) -> str:`.
- `extracted_date = dag_run.start_date.date()` remplace bien `datetime.now(UTC).date()`.
- L'appel `extracted_date_iso = extract_and_load_raw()` tout en bas du fichier n'a **pas** changé (Airflow injecte `dag_run` lui-même, il ne faut surtout pas le passer explicitement ici).

- [ ] **Step 6 : Vérifier que le DAG s'importe sans erreur (environnement local)**

```bash
docker compose run --rm airflow-webserver airflow dags list-import-errors
```
Expected : sortie vide (ou tableau sans ligne pour `card_price_pipeline_dag.py`) — aucune erreur d'import. Si une erreur mentionne `DagRun` ou `dag_run`, vérifier l'import du Step 1 et l'orthographe exacte du nom de paramètre (`dag_run`, pas `dagrun` ni `dag_run_`).

- [ ] **Step 7 : Arrêter la stack locale (pas besoin de la laisser tourner)**

```bash
docker compose down
```

- [ ] **Step 8 : Lint, format, commit**

```bash
ruff check . && black --check .
git add dags/card_price_pipeline_dag.py
git commit -m "fix: raise retry budget, pin extracted_date per DagRun, shift schedule to 07:00 UTC"
```

---

### Task 2 : Déployer, vérifier en prod, et terminer le run du 2026-08-07 resté bloqué

**Files:**
- (Aucun nouveau fichier — déploiement du changement de la Task 1 sur le VPS déjà en production.)

**Interfaces:**
- Consomme : le code de la Task 1, le DagRun `scheduled__2026-08-07T00:00:00+00:00` déjà présent en base de métadonnées Airflow (état `failed`, checkpoint arrêté à la page 49 dans `raw.card_prices` pour `extracted_date=2026-08-08`, voir le diagnostic dans la spec).
- Produit : ce DagRun passe à `success`, avec `raw.card_prices`/`staging.card_prices`/`prod.fact_price_history` complets pour cette date — comblant le trou de données découvert pendant l'investigation, en même temps que ça valide le correctif en conditions réelles.

- [ ] **Step 1 : Pousser le commit de la Task 1**

```bash
git push
```

- [ ] **Step 2 : Récupérer le code sur la VM**

```bash
ssh card-tracker-vm
cd ~/card-price-tracker
git pull
```

- [ ] **Step 3 : Vérifier que le DAG s'importe sans erreur en prod**

```bash
docker compose -f docker-compose.prod.yml exec airflow-webserver airflow dags list-import-errors
```
Expected : sortie vide. Le code est monté en volume dans les conteneurs déjà en cours d'exécution (`docker-compose.prod.yml`, pattern déjà établi Mois 3) — pas de rebuild ni de redémarrage de conteneur nécessaire pour que ce changement soit pris en compte.

- [ ] **Step 4 : Vérifier que le nouvel horaire est bien pris en compte**

```bash
docker compose -f docker-compose.prod.yml exec airflow-webserver airflow dags next-execution card_price_pipeline
```
Expected : une date/heure affichée avec `07:00:00` (UTC) comme heure, pas `00:00:00`.

- [ ] **Step 5 : Relancer le DagRun du 2026-08-07 resté en échec, avec le nouveau code**

```bash
docker compose -f docker-compose.prod.yml exec airflow-webserver airflow tasks clear -y -s 2026-08-07 -e 2026-08-07 card_price_pipeline
```
(`dag_id` est un argument positionnel pour cette sous-commande, pas un flag `-d` — `-d` signifierait `--downstream` ici. Vérifié directement contre l'aide de la version installée : `airflow tasks clear --help` sur la VM.)
Expected : confirmation que 3 task instances ont été remises à l'état `scheduled` (les 3 tâches du DagRun `scheduled__2026-08-07T00:00:00+00:00` — la date `2026-08-07` ici est la logical_date du run, PAS la valeur d'`extracted_date` qui sera effectivement utilisée par le nouveau code, voir Step 6). Le scheduler (déjà en cours d'exécution, healthcheck confirmé Mois 3 Task 2) reprend ce DagRun automatiquement, sans action manuelle supplémentaire.

- [ ] **Step 6 : Suivre la progression jusqu'à `success`**

```bash
watch -n 30 "docker compose -f docker-compose.prod.yml exec -T airflow-webserver airflow dags list-runs -d card_price_pipeline -o table 2>/dev/null | head -5"
```
Expected : la ligne `scheduled__2026-08-07T00:00:00+00:00` passe de `queued`/`running` à `success`. Peut prendre plusieurs minutes (checkpoint reprend à la page 49/~80, plus le temps des deux tâches suivantes) — laisser tourner, ne pas interrompre. Ctrl+C pour sortir du `watch` une fois `success` confirmé.

- [ ] **Step 7 : Vérifier que extracted_date utilisée est stable et cohérente**

```bash
docker compose -f docker-compose.prod.yml exec db psql -U "$POSTGRES_ADMIN_USER" -d "$POSTGRES_DB" -c "
SELECT extracted_date, count(*) FROM raw.card_prices
WHERE extracted_date IN ('2026-08-07', '2026-08-08')
GROUP BY extracted_date ORDER BY extracted_date;
"
```
Expected : une seule ligne avec un compte proche de ~19000-20000 (le catalogue complet), sur `extracted_date=2026-08-08` (la date réelle où ce run — repris aujourd'hui — s'est exécuté ; voir la spec pour pourquoi `dag_run.start_date` donne cette valeur et pas `2026-08-07`). Si les deux dates apparaissent avec des comptes partiels chacune, le fix de la Task 1 n'a pas fonctionné comme prévu — ne pas continuer, investiguer avant de passer à la Task 3 du Mois 3.

- [ ] **Step 8 : Vérifier que les données sont arrivées jusqu'en prod**

```bash
docker compose -f docker-compose.prod.yml exec db psql -U "$POSTGRES_ADMIN_USER" -d "$POSTGRES_DB" -c "
SELECT dp.platform_name, count(*)
FROM prod.fact_price_history fph
JOIN prod.dim_date dd ON dd.date_id = fph.date_id
JOIN prod.dim_platform dp ON dp.platform_id = fph.platform_id
WHERE dd.full_date = '2026-08-08'
GROUP BY dp.platform_name;
"
```
Expected : au moins une ligne pour `platform_name='tcgplayer'` avec un compte cohérent avec le Step 7 (à quelques rejets de validation près, normal).

---

## Self-Review Notes

- **Couverture du spec** : retries 20→60 ✓ (Task 1, Step 3), `extracted_date` figé via `dag_run.start_date` ✓ (Task 1, Step 4), horaire `0 7 * * *` ✓ (Task 1, Step 2), vérification en conditions réelles ✓ (Task 2, reprise du run réellement échoué plutôt qu'un simple trigger de test synthétique).
- **Cohérence des types** : `extract_and_load_raw(dag_run: DagRun) -> str` — seule signature touchée ; `clean_to_staging(extracted_date_iso: str) -> str` en aval n'a pas besoin de changer, il continue de recevoir une string ISO via XCom exactement comme avant.
- **Absence de tests unitaires dédiés** : confirmé pendant le brainstorming qu'aucun test existant n'exécute `dags/card_price_pipeline_dag.py` (finding différé du Mois 2, "CI qui ne parse jamais le DAG", toujours ouvert). Ce plan compense par une vérification d'import explicite (Task 1 Step 6, Task 2 Step 3) et une vérification comportementale réelle (Task 2 Steps 5-8, en rejouant le run précédemment échoué) plutôt que de laisser ce changement complètement non vérifié avant la prod.
- **Ordre des tasks** : Task 2 dépend entièrement de Task 1 (déploie son code). Task 1 seule est déjà un livrable cohérent et testable (Step 6 vérifie l'import localement) si jamais Task 2 devait être reportée.
