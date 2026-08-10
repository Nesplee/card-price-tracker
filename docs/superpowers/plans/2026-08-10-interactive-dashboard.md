# Card Price Tracker — Dashboard interactif (colonne `series` + panneaux Metabase) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Propager le champ `series` (le "bloc" Pokémon TCG) de bout en bout dans le pipeline existant jusqu'à `prod.dim_card`, avec backfill immédiat des cartes déjà en base — pose les fondations de données pour le dashboard Metabase (construit manuellement dans l'UI Metabase après ce plan, hors scope du code versionné, voir le spec).

**Architecture:** `series` est déjà présent dans les payloads bruts stockés depuis le Mois 1 (`raw.card_prices.payload->'set'->'series'`) : aucun nouvel appel API. Propagation à travers les couches déjà en place : `CleanedCard` (validation) -> `staging.card_prices` -> `prod.dim_card`, suivant exactement le même chemin que `rarity` aujourd'hui.

**Tech Stack:** Identique à l'existant (Python 3.11, psycopg 3, PostgreSQL 16, pytest). Aucune nouvelle dépendance.

## Global Constraints

- Migrations SQL numérotées, jamais modifiées après merge — cette migration est `009_...sql`.
- Idempotence par UPSERT (`ON CONFLICT DO UPDATE`), jamais delete-and-reload — même principe que tout le reste du pipeline.
- `series` est nullable (`str | None`), comme `rarity` : son absence n'est pas assez grave pour rejeter une carte en quarantaine.
- Tout scopé `platform_name = 'tcgplayer'` pour le dashboard final (hors scope de ce plan, rappel pour cohérence).
- Lint (`ruff`) et formatage (`black`) avant chaque commit.
- La construction du dashboard Metabase lui-même (questions, filtres) n'est PAS une tâche de ce plan — guide manuel donné directement après, voir le spec.

**Référence :** `docs/superpowers/specs/2026-08-10-interactive-dashboard-design.md` (spec approuvée).

---

### Task 1 : Migration `series` (schéma + backfill)

**Files:**
- Create: `migrations/009_add_series_to_schema.sql`

**Interfaces:**
- Consomme : `raw.card_prices` (déjà peuplée), `prod.dim_card` (migration 003).
- Produit : colonne `series` sur `staging.card_prices` et `prod.dim_card` — consommée par les Tasks 2-4.

- [ ] **Step 1 : Créer `migrations/009_add_series_to_schema.sql`**

```sql
-- Migration 009 : ajoute la colonne series (le "bloc" Pokémon TCG, ex:
-- "Scarlet & Violet" regroupe les sets "Paldea Evolved", "Obsidian Flames"...)
-- à staging.card_prices et prod.dim_card, avec backfill immédiat de
-- dim_card pour toutes les cartes déjà connues. Voir
-- docs/superpowers/specs/2026-08-10-interactive-dashboard-design.md.
--
-- Aucun nouvel appel API nécessaire : series est déjà présent dans
-- raw.card_prices.payload->'set'->'series' depuis le Mois 1 (vérifié
-- directement sur les données de production avant d'écrire cette
-- migration) -- le backfill relit simplement ce qui est déjà stocké.

BEGIN;

ALTER TABLE staging.card_prices ADD COLUMN IF NOT EXISTS series text;
ALTER TABLE prod.dim_card ADD COLUMN IF NOT EXISTS series text;

-- Backfill : pour chaque carte déjà connue de dim_card, retrouve son
-- payload le plus récent dans raw.card_prices et en extrait series.
-- DISTINCT ON (payload->>'id') ... ORDER BY payload->>'id', extracted_date
-- DESC : si plusieurs payloads existent pour la même carte (plusieurs
-- jours d'extraction), on ne garde que le plus récent -- pas besoin de
-- les agréger, series ne change normalement jamais pour une carte donnée.
UPDATE prod.dim_card dc
SET series = sub.series
FROM (
    SELECT DISTINCT ON (payload->>'id')
        payload->>'id' AS card_id,
        payload->'set'->>'series' AS series
    FROM raw.card_prices
    WHERE source = 'pokemontcg.io'
    ORDER BY payload->>'id', extracted_date DESC
) sub
WHERE dc.card_id = sub.card_id;

COMMIT;
```

- [ ] **Step 2 : Appliquer la migration en local**

```bash
docker compose up -d db
./scripts/apply_migrations.sh
```
Expected : `Applique : 009_add_series_to_schema.sql`, sans erreur.

- [ ] **Step 3 : Vérifier le backfill**

```bash
docker compose exec db psql -U postgres -d card_tracker -c "
SELECT count(*) AS total, count(series) AS avec_series FROM prod.dim_card;
"
```
Expected : `total` et `avec_series` égaux (toutes les cartes déjà en base ont un payload raw qui contient `series`, donc aucune ne devrait rester `NULL` après le backfill) — sauf si la base locale contient des données de test artificielles sans vrai payload (auquel cas `avec_series` peut être inférieur, ce n'est pas un défaut de la migration elle-même).

- [ ] **Step 4 : Commit**

```bash
git add migrations/009_add_series_to_schema.sql
git commit -m "feat: add series column to staging.card_prices and prod.dim_card, backfill from stored payloads"
```

---

### Task 2 : `CleanedCard.series` + extraction dans `validate_and_clean()`

**Files:**
- Modify: `src/transform/validate.py`
- Modify: `tests/test_transform.py`
- Modify: `tests/test_staging_loader.py`
- Modify: `tests/test_warehouse_loader.py`

**Interfaces:**
- Consomme : `payload["set"]["series"]` (déjà présent dans tout payload pokemontcg.io stocké).
- Produit : `CleanedCard.series: str | None` — nouveau champ, sans valeur par défaut (même convention que `rarity`). Consommé par la Task 3 (`staging_loader.py`). **Tout code qui construit `CleanedCard(...)` directement doit maintenant passer `series=...` explicitement** (pas de défaut) — d'où les modifications de `tests/test_staging_loader.py` et `tests/test_warehouse_loader.py` ci-dessous, qui construisent `CleanedCard` en dur pour leurs fixtures.

- [ ] **Step 1 : Ajouter le test dans `tests/test_transform.py` (doit échouer)**

Ajouter à la fin du fichier :
```python
def test_validate_and_clean_extracts_series() -> None:
    # series est le "bloc" Pokémon TCG (ex: "Scarlet & Violet" regroupe
    # plusieurs sets/séries individuelles). Présent dans payload["set"]
    # exactement comme set_id/set_name.
    result = validate_and_clean(_make_payload(set={"id": "sv2", "name": "Paldea Evolved", "series": "Scarlet & Violet"}))

    assert result.is_valid
    assert result.cleaned.series == "Scarlet & Violet"


def test_validate_and_clean_accepts_missing_series() -> None:
    # series absent du payload.set ne doit pas rejeter la carte (même
    # tolérance que rarity) -- juste series=None dans le résultat.
    result = validate_and_clean(_make_payload(set={"id": "base1", "name": "Base"}))

    assert result.is_valid
    assert result.cleaned.series is None
```

- [ ] **Step 2 : Lancer les tests pour vérifier qu'ils échouent**

Run: `pytest tests/test_transform.py::test_validate_and_clean_extracts_series tests/test_transform.py::test_validate_and_clean_accepts_missing_series -v`
Expected: FAIL — `TypeError: CleanedCard.__init__() got an unexpected keyword argument` ou `AttributeError` (le champ `series` n'existe pas encore sur `CleanedCard`, et `validate_and_clean` ne le lit pas encore).

- [ ] **Step 3 : Ajouter `series` à `CleanedCard`**

Dans `src/transform/validate.py`, modifier la classe `CleanedCard` : ajouter le champ juste après `set_name` (avant `rarity`) :
```python
    set_name: str
    # series : le "bloc" Pokémon TCG (ex: "Scarlet & Violet" regroupe
    # plusieurs sets individuels comme "Paldea Evolved", "Obsidian
    # Flames"...). Optionnel côté source comme rarity -- son absence
    # n'est pas assez grave pour rejeter toute la carte.
    series: str | None
    rarity: str | None
```

- [ ] **Step 4 : Extraire `series` dans `validate_and_clean()`**

Dans `src/transform/validate.py`, juste après la lecture de `set_id`/`set_name` (Règle 2), ajouter :
```python
    series = set_info.get("series")
```
Puis dans la construction finale de `CleanedCard` (à la toute fin de la fonction), ajouter `series=series,` juste après `set_name=set_name.strip(),` :
```python
    return ValidationResult(
        cleaned=CleanedCard(
            card_id=card_id,
            name=name.strip(),
            set_id=set_id,
            set_name=set_name.strip(),
            series=series,
            rarity=payload.get("rarity"),
            average_sell_price=average_sell_price,
            trend_price=trend_price,
            low_price=low_price,
        ),
        rejection_reason=None,
    )
```

- [ ] **Step 5 : Lancer les tests pour vérifier qu'ils passent**

Run: `pytest tests/test_transform.py -v`
Expected: tous les tests passent (les existants + les 2 nouveaux).

- [ ] **Step 6 : Corriger les fixtures `CleanedCard(...)` dans les tests des loaders**

Ces deux fichiers construisent `CleanedCard` directement (pas via `validate_and_clean()`) et vont échouer tant que `series=` n'est pas passé.

Dans `tests/test_staging_loader.py`, dans `test_load_staging_is_idempotent_for_same_day`, ajouter `series="Base"` juste après `set_name="Base",` :
```python
    card = CleanedCard(
        card_id="base1-1",
        name="Alakazam",
        set_id="base1",
        set_name="Base",
        series="Base",
        rarity="Rare Holo",
        average_sell_price=12.5,
        trend_price=13.0,
        low_price=8.0,
    )
```

Dans `tests/test_warehouse_loader.py`, dans `_seed_card()`, même ajout :
```python
    return CleanedCard(
        card_id="base1-1",
        name="Alakazam",
        set_id="base1",
        set_name="Base",
        series="Base",
        rarity="Rare Holo",
        average_sell_price=12.5,
        trend_price=13.0,
        low_price=8.0,
    )
```

- [ ] **Step 7 : Lancer la suite complète pour vérifier l'absence de régression**

Run: `pytest -v`
Expected : tous les tests passent (`test_staging_loader.py`/`test_warehouse_loader.py` ne testent pas encore le CONTENU de `series` en base — c'est le rôle des Tasks 3-4 — mais leurs fixtures doivent au moins compiler et s'exécuter sans erreur avec le nouveau champ).

- [ ] **Step 8 : Lint, format, commit**

```bash
ruff check . && black --check .
git add src/transform/validate.py tests/test_transform.py tests/test_staging_loader.py tests/test_warehouse_loader.py
git commit -m "feat: extract series from card payload into CleanedCard"
```

---

### Task 3 : Propager `series` dans `staging_loader.py`

**Files:**
- Modify: `src/load/staging_loader.py`
- Modify: `tests/test_staging_loader.py`

**Interfaces:**
- Consomme : `CleanedCard.series` (Task 2).
- Produit : `staging.card_prices.series` correctement peuplé par `load_staging()`. Consommé par la Task 4 (`warehouse_loader.py` lit `staging.card_prices`).

- [ ] **Step 1 : Étendre le test existant pour vérifier `series` (doit échouer)**

Dans `tests/test_staging_loader.py`, modifier `test_load_staging_is_idempotent_for_same_day` : après la deuxième ligne `load_staging(db_connection, [card], extracted_date=date(2026, 9, 1))`, ajouter une vérification du contenu :
```python
    with db_connection.cursor() as cur:
        cur.execute(
            "SELECT series FROM staging.card_prices WHERE card_id = %s;", ("base1-1",)
        )
        (series,) = cur.fetchone()
    assert series == "Base"
```
(Insérer ce bloc juste avant les assertions de fin de test déjà présentes sur le comptage de lignes, sans les retirer.)

- [ ] **Step 2 : Lancer le test pour vérifier qu'il échoue**

Run: `pytest tests/test_staging_loader.py::test_load_staging_is_idempotent_for_same_day -v`
Expected: FAIL — `psycopg.errors.UndefinedColumn: column "series" does not exist` (la colonne existe en base depuis la Task 1, mais `_UPSERT_STAGING_SQL` ne l'écrit pas encore, donc elle reste `NULL`, et en fait l'erreur attendue ici est plutôt `assert None == "Base"` qui échoue -- la colonne existe bien grâce à la Task 1, seule l'écriture manque).

- [ ] **Step 3 : Propager `series` dans `_UPSERT_STAGING_SQL`**

Dans `src/load/staging_loader.py`, modifier `_UPSERT_STAGING_SQL` :
```python
_UPSERT_STAGING_SQL = """
    INSERT INTO staging.card_prices
        (card_id, extracted_date, name, set_id, set_name, series, rarity,
         average_sell_price, trend_price, low_price, source)
    VALUES
        (%(card_id)s, %(extracted_date)s, %(name)s, %(set_id)s, %(set_name)s, %(series)s, %(rarity)s,
         %(average_sell_price)s, %(trend_price)s, %(low_price)s, %(source)s)
    ON CONFLICT (card_id, extracted_date, source)
    DO UPDATE SET
        name = EXCLUDED.name,
        set_id = EXCLUDED.set_id,
        set_name = EXCLUDED.set_name,
        series = EXCLUDED.series,
        rarity = EXCLUDED.rarity,
        average_sell_price = EXCLUDED.average_sell_price,
        trend_price = EXCLUDED.trend_price,
        low_price = EXCLUDED.low_price,
        loaded_at = now()
"""
```

Dans `load_staging()`, ajouter `"series": c.series,` au dict construit dans la liste `rows` (juste après `"set_name": c.set_name,`) :
```python
    rows = [
        {
            "card_id": c.card_id,
            "extracted_date": extracted_date,
            "name": c.name,
            "set_id": c.set_id,
            "set_name": c.set_name,
            "series": c.series,
            "rarity": c.rarity,
            "average_sell_price": c.average_sell_price,
            "trend_price": c.trend_price,
            "low_price": c.low_price,
            "source": source,
        }
        for c in cleaned_cards
    ]
```

- [ ] **Step 4 : Lancer le test pour vérifier qu'il passe**

Run: `pytest tests/test_staging_loader.py -v`
Expected: tous les tests de ce fichier passent.

- [ ] **Step 5 : Lint, format, commit**

```bash
ruff check . && black --check .
git add src/load/staging_loader.py tests/test_staging_loader.py
git commit -m "feat: propagate series into staging.card_prices"
```

---

### Task 4 : Propager `series` dans `warehouse_loader.py`

**Files:**
- Modify: `src/load/warehouse_loader.py`
- Modify: `tests/test_warehouse_loader.py`

**Interfaces:**
- Consomme : `staging.card_prices.series` (Task 3).
- Produit : `prod.dim_card.series` correctement peuplé/mis à jour par `load_staging_to_warehouse()` -- complète la propagation de bout en bout du champ.

- [ ] **Step 1 : Étendre le test existant pour vérifier `series` (doit échouer)**

Dans `tests/test_warehouse_loader.py`, dans `test_load_staging_to_warehouse_inserts_fact`, ajouter après les assertions existantes :
```python
    with db_connection.cursor() as cur:
        cur.execute("SELECT series FROM prod.dim_card WHERE card_id = %s;", ("base1-1",))
        (series,) = cur.fetchone()
    assert series == "Base"
```

- [ ] **Step 2 : Lancer le test pour vérifier qu'il échoue**

Run: `pytest tests/test_warehouse_loader.py::test_load_staging_to_warehouse_inserts_fact -v`
Expected: FAIL — `assert None == "Base"` (la colonne `prod.dim_card.series` existe depuis la Task 1, mais `load_staging_to_warehouse()` ne la lit/écrit pas encore).

- [ ] **Step 3 : Propager `series` dans `warehouse_loader.py`**

Dans `src/load/warehouse_loader.py`, modifier `_UPSERT_DIM_CARD_SQL` :
```python
_UPSERT_DIM_CARD_SQL = """
    INSERT INTO prod.dim_card (card_id, name, set_id, set_name, series, rarity)
    VALUES (%(card_id)s, %(name)s, %(set_id)s, %(set_name)s, %(series)s, %(rarity)s)
    ON CONFLICT (card_id) DO UPDATE SET
        name = EXCLUDED.name, set_id = EXCLUDED.set_id,
        set_name = EXCLUDED.set_name, series = EXCLUDED.series,
        rarity = EXCLUDED.rarity,
        updated_at = now()
"""
```

Dans `load_staging_to_warehouse()`, modifier la requête `SELECT` pour inclure `series` :
```python
        cur.execute(
            """
            SELECT card_id, name, set_id, set_name, series, rarity,
                   average_sell_price, trend_price, low_price
            FROM staging.card_prices
            WHERE extracted_date = %s AND source = %s
            """,
            (extracted_date, source),
        )
        rows = cur.fetchall()
```

Puis mettre à jour la boucle `for` juste en dessous pour déballer la nouvelle colonne, et l'ajout dans `_UPSERT_DIM_CARD_SQL` :
```python
        for (
            card_id,
            name,
            set_id,
            set_name,
            series,
            rarity,
            average_sell_price,
            trend_price,
            low_price,
        ) in rows:
            cur.execute(
                _UPSERT_DIM_CARD_SQL,
                {
                    "card_id": card_id,
                    "name": name,
                    "set_id": set_id,
                    "set_name": set_name,
                    "series": series,
                    "rarity": rarity,
                },
            )
```
(Le reste de la boucle, l'appel à `_UPSERT_FACT_SQL` et le contrôle `cur.rowcount == 0`, ne change pas.)

- [ ] **Step 4 : Lancer les tests pour vérifier qu'ils passent**

Run: `pytest tests/test_warehouse_loader.py -v`
Expected: tous les tests de ce fichier passent.

- [ ] **Step 5 : Lancer la suite complète pour vérifier l'absence de régression**

Run: `pytest -v`
Expected : tous les tests passent (y compris `tests/test_idempotence.py`, qui exerce ce chemin de bout en bout).

- [ ] **Step 6 : Lint, format, commit**

```bash
ruff check . && black --check .
git add src/load/warehouse_loader.py tests/test_warehouse_loader.py
git commit -m "feat: propagate series into prod.dim_card"
```

---

### Task 5 : Déploiement en production + vérification du backfill

**Files:**
- (Aucun nouveau fichier — déploiement des Tasks 1-4 sur le VPS déjà en production.)

**Interfaces:**
- Consomme : le code des Tasks 1-4.
- Produit : `prod.dim_card.series` peuplé pour toutes les cartes déjà en base de production, et correctement maintenu par les futurs runs du DAG.

- [ ] **Step 1 : Pousser les commits des Tasks 1-4**

```bash
git push
```

- [ ] **Step 2 : Récupérer le code sur la VM et appliquer la migration**

```bash
ssh card-tracker-vm
cd ~/card-price-tracker
git pull
./scripts/apply_migrations.sh docker-compose.prod.yml
```
Expected : `Applique : 009_add_series_to_schema.sql`, sans erreur.

- [ ] **Step 3 : Vérifier le backfill en production**

```bash
docker compose -f docker-compose.prod.yml exec db psql -U "$POSTGRES_ADMIN_USER" -d "$POSTGRES_DB" -c "
SELECT count(*) AS total, count(series) AS avec_series, count(DISTINCT series) AS blocs_distincts
FROM prod.dim_card;
"
```
Expected : `total` et `avec_series` égaux (~19700+ cartes, toutes ont un payload raw avec `series`), `blocs_distincts` de l'ordre de quelques dizaines (chaque bloc regroupe plusieurs sets).

- [ ] **Step 4 : Vérifier un échantillon de correspondances bloc/série connues**

```bash
docker compose -f docker-compose.prod.yml exec db psql -U "$POSTGRES_ADMIN_USER" -d "$POSTGRES_DB" -c "
SELECT DISTINCT set_name, series FROM prod.dim_card WHERE set_name IN ('Paldea Evolved', 'Chilling Reign') ORDER BY set_name;
"
```
Expected : `Paldea Evolved` -> `Scarlet & Violet`, `Chilling Reign` -> `Sword & Shield` (ou équivalent officiel) -- confirme que le backfill a extrait les bonnes valeurs, pas juste rempli une colonne avec du bruit.

---

## Self-Review Notes

- **Couverture du spec** : colonne `series` sur `staging.card_prices` + `prod.dim_card` avec backfill immédiat ✓ (Task 1), propagation de bout en bout `CleanedCard` -> staging -> prod ✓ (Tasks 2-4), aucun nouvel appel API ✓ (backfill lit `raw.card_prices` déjà stocké). La construction effective des panneaux/filtres Metabase est explicitement hors scope de ce plan (spec, section "Construction dans Metabase") -- fournie directement par le contrôleur à l'utilisateur après ce plan, pas une tâche ici.
- **Cohérence des types** : `CleanedCard.series: str | None` (Task 2) propagé sans transformation jusqu'à `prod.dim_card.series` (Task 4) -- aucune conversion de type sur le chemin, cohérent avec le traitement de `rarity` déjà en place.
- **Ordre des tasks** : Task 1 (schéma) précède toutes les autres (colonnes doivent exister avant d'être écrites/lues). Task 2 (CleanedCard) précède Task 3 (staging_loader consomme `CleanedCard.series`). Task 3 précède Task 4 (warehouse_loader lit `staging.card_prices.series`). Task 5 dépend de toutes les précédentes.
- **Rupture de compatibilité gérée explicitement** : l'ajout d'un champ sans défaut à `CleanedCard` casse mécaniquement les deux fixtures de test qui la construisent en dur -- corrigé dans la Task 2 elle-même (Step 6), pas laissé comme dette pour une task ultérieure.
