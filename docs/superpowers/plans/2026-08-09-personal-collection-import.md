# Card Price Tracker — Import de la collection personnelle (CSV) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Importer, de façon réutilisable et idempotente, les cartes de la collection personnelle de l'utilisateur (export CSV d'une app tierce) dans la base de données, reliées au catalogue `prod.dim_card` déjà alimenté par le pipeline de prix — pose les fondations du futur dashboard (Task 4, Mois 3, stretch).

**Architecture:** Nouveau schéma `collection` (`raw_import`, `match_quarantine`) + nouvelle table `prod.dim_owned_card`. Le rapprochement avec le catalogue est déterministe (pas de fuzzy matching) : `card_id = "{set_id}-{numéro normalisé}"`, où `set_id` vient d'une correspondance nom-de-set -> id récupérée via `GET /v2/sets`. Script manuel réutilisable (`scripts/import_collection.py`), même pattern que `scripts/run_extract_load.py` — pas de DAG.

**Tech Stack:** Identique à l'existant (Python 3.11+, psycopg 3, PostgreSQL 16, pytest, module `csv` de la bibliothèque standard). Aucune nouvelle dépendance.

## Global Constraints

- `export.csv` ne doit JAMAIS être commité (déjà dans `.gitignore`) — aucun test ni script de ce plan ne doit en dépendre pour fonctionner (les tests utilisent des fixtures CSV minimales créées dans `tests/`, pas le vrai fichier).
- Filtres d'import, exacts : `Category == "Pokemon"`, `Portfolio Name == "Main"`, `Rarity NOT IN ("Common", "Uncommon", "")`.
- Aucun matching approximatif : un set ou un `card_id` construit non trouvé part en quarantaine avec une raison explicite, jamais un rapprochement "au mieux".
- Idempotence par UPSERT sur la clé naturelle, jamais delete-and-reload — même principe que tout le reste du pipeline.
- `variance` et `grade` sont `NOT NULL DEFAULT ''` (pas nullable) dans `collection.raw_import` et `prod.dim_owned_card` : Postgres ne considère jamais deux `NULL` comme égaux dans une contrainte `UNIQUE`, ce qui casserait l'idempotence si ces colonnes étaient nullable et vides sur un import futur.
- Migrations SQL numérotées, jamais modifiées après merge — cette migration est `008_...sql`.
- Lint (`ruff`) et formatage (`black`) avant chaque commit.
- **Résultat du rapprochement (comptes matchés/quarantaine, sets non reconnus) communiqué explicitement à l'utilisateur** — pas seulement "ça a marché".

**Référence :** `docs/superpowers/specs/2026-08-09-personal-collection-import-design.md` (spec approuvée).

---

### Task 1 : Schéma `collection` + table `prod.dim_owned_card`

**Files:**
- Create: `migrations/008_create_collection_schema.sql`

**Interfaces:**
- Consomme : `prod.dim_card` (déjà créée, migration 003), rôles `pipeline_app` et `dashboard_reader` (déjà créés, migrations 001 et 007).
- Produit : schéma `collection` avec `collection.raw_import` et `collection.match_quarantine`, table `prod.dim_owned_card` — consommées par la Task 4 (chargement).

- [ ] **Step 1 : Créer `migrations/008_create_collection_schema.sql`**

```sql
-- Migration 008 : schéma pour l'import de la collection personnelle de
-- l'utilisateur (CSV externe, jamais commité -- voir .gitignore), séparé de
-- raw/staging/prod du pipeline de prix : domaine différent ("ce que
-- l'utilisateur possède" vs "quels sont les prix du marché"). Voir
-- docs/superpowers/specs/2026-08-09-personal-collection-import-design.md.

BEGIN;

CREATE SCHEMA IF NOT EXISTS collection;

-- collection.raw_import : copie des lignes du CSV qui ont passé les filtres
-- d'import (Category=Pokemon, Portfolio Name=Main, Rarity hors
-- Common/Uncommon/vide) -- AVANT tentative de rapprochement avec le
-- catalogue. Permet de rejouer la logique de matching plus tard (ex: une
-- fois une table de correspondance de sets améliorée) sans avoir besoin de
-- ré-uploader le CSV original.
--
-- variance/grade en NOT NULL DEFAULT '' (pas nullable) : Postgres ne
-- considère JAMAIS deux NULL comme égaux dans une contrainte UNIQUE -- si
-- ces colonnes étaient nullables, deux imports successifs d'une carte sans
-- variante/grade renseignés créeraient deux lignes distinctes au lieu de
-- mettre à jour la même, cassant l'idempotence du réimport.
CREATE TABLE collection.raw_import (
    id                      bigserial PRIMARY KEY,
    set_name                text NOT NULL,
    card_number             text NOT NULL,
    product_name            text NOT NULL,
    variance                text NOT NULL DEFAULT '',
    grade                   text NOT NULL DEFAULT '',
    rarity                  text NOT NULL,
    quantity                integer NOT NULL,
    average_cost_paid       numeric(10, 2),
    market_price_at_export  numeric(10, 2),
    imported_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_collection_raw_import_natural_key
        UNIQUE (set_name, card_number, product_name, variance, grade)
);

-- collection.match_quarantine : lignes de collection.raw_import pour
-- lesquelles le rapprochement avec prod.dim_card a échoué (set non reconnu,
-- ou card_id construit absent du catalogue). raw_import_id référence la
-- ligne d'origine -- pas de duplication du contenu ici, juste la raison.
CREATE TABLE collection.match_quarantine (
    id              bigserial PRIMARY KEY,
    raw_import_id   bigint NOT NULL REFERENCES collection.raw_import(id),
    rejection_reason  text NOT NULL,
    imported_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_collection_match_quarantine_raw_import_id UNIQUE (raw_import_id)
);

-- prod.dim_owned_card : cartes de la collection personnelle RECONNUES dans
-- le catalogue -- prête à être jointe à prod.fact_price_history pour le
-- futur dashboard (valeur de la collection = quantity * prix courant).
-- card_id REFERENCES prod.dim_card : ne peut pas exister sans une carte
-- déjà connue du catalogue de prix.
CREATE TABLE prod.dim_owned_card (
    id                  bigserial PRIMARY KEY,
    card_id             text NOT NULL REFERENCES prod.dim_card(card_id),
    variance            text NOT NULL DEFAULT '',
    grade               text NOT NULL DEFAULT '',
    quantity            integer NOT NULL,
    average_cost_paid   numeric(10, 2),
    imported_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_dim_owned_card_natural_key UNIQUE (card_id, variance, grade)
);

-- Permissions pipeline_app : le script d'import (scripts/import_collection.py,
-- Task 5) se connecte avec les identifiants pipeline_app, comme
-- scripts/run_extract_load.py. Mêmes droits que sur le reste du pipeline
-- (lecture/écriture, jamais DELETE).
GRANT USAGE ON SCHEMA collection TO pipeline_app;
GRANT SELECT, INSERT, UPDATE ON collection.raw_import TO pipeline_app;
GRANT USAGE, SELECT ON SEQUENCE collection.raw_import_id_seq TO pipeline_app;
GRANT SELECT, INSERT, UPDATE ON collection.match_quarantine TO pipeline_app;
GRANT USAGE, SELECT ON SEQUENCE collection.match_quarantine_id_seq TO pipeline_app;
GRANT SELECT, INSERT, UPDATE ON prod.dim_owned_card TO pipeline_app;
GRANT USAGE, SELECT ON SEQUENCE prod.dim_owned_card_id_seq TO pipeline_app;

-- dashboard_reader : lecture seule sur la nouvelle table prod, même pattern
-- que le reste de prod (migration 007) -- pour que Metabase puisse
-- l'exploiter dans le futur dashboard.
GRANT SELECT ON prod.dim_owned_card TO dashboard_reader;

COMMIT;
```

- [ ] **Step 2 : Appliquer la migration en local**

```bash
docker compose up -d db
./scripts/apply_migrations.sh
```
Expected : `Applique : 008_create_collection_schema.sql`, sans erreur.

- [ ] **Step 3 : Vérifier les tables et les droits**

```bash
docker compose exec db psql -U postgres -d card_tracker -c "\dt collection.*"
docker compose exec db psql -U postgres -d card_tracker -c "\dt prod.dim_owned_card"
docker compose exec db psql -U pipeline_app -d card_tracker -c "INSERT INTO collection.raw_import (set_name, card_number, product_name, rarity, quantity) VALUES ('Test Set', '1/100', 'Test Card', 'Rare', 1);"
docker compose exec db psql -U pipeline_app -d card_tracker -c "DELETE FROM collection.raw_import WHERE set_name = 'Test Set';"
```
Expected : les deux premières commandes listent les tables créées. Le `INSERT` réussit (droits corrects). Le `DELETE` échoue avec `permission denied` (aucun droit DELETE accordé à `pipeline_app` sur cette table — cohérent avec le reste du schéma prod, qui interdit aussi tout DELETE).

- [ ] **Step 4 : Commit**

```bash
git add migrations/008_create_collection_schema.sql
git commit -m "feat: add collection schema and dim_owned_card table for personal collection import"
```

---

### Task 2 : `PokemonTcgClient.fetch_sets()` — récupérer la liste des sets

**Files:**
- Modify: `src/extract/pokemontcg_client.py`
- Modify: `tests/test_extract.py`

**Interfaces:**
- Consomme : `PokemonTcgClient` existant (retry/backoff déjà en place, voir `fetch_cards_page`).
- Produit : `PokemonTcgClient.fetch_sets() -> list[dict]`, chaque dict a au moins les clés `"id"` et `"name"`. Consommée par la Task 3 (construction de la correspondance nom -> id).

- [ ] **Step 1 : Ajouter le test (doit échouer)**

Dans `tests/test_extract.py`, ajouter à la fin du fichier :
```python
@responses.activate
def test_fetch_sets_returns_data(client: PokemonTcgClient) -> None:
    responses.add(
        responses.GET,
        "https://api.pokemontcg.io/v2/sets",
        json={"data": [{"id": "swsh6", "name": "Chilling Reign"}, {"id": "xy5", "name": "Primal Clash"}]},
        status=200,
    )

    sets = client.fetch_sets()

    assert sets == [{"id": "swsh6", "name": "Chilling Reign"}, {"id": "xy5", "name": "Primal Clash"}]
```
(Réutilise la fixture `client` déjà définie en tête de `tests/test_extract.py` — un `PokemonTcgClient` déjà construit avec `wait_min_seconds=0, wait_max_seconds=0` pour ne pas ralentir les tests de retry. Le décorateur `@responses.activate` est requis sur chaque test qui utilise `responses.add`, comme sur les 4 tests existants du fichier.)

- [ ] **Step 2 : Lancer le test pour vérifier qu'il échoue**

Run: `pytest tests/test_extract.py::test_fetch_sets_returns_data -v`
Expected: FAIL avec `AttributeError: 'PokemonTcgClient' object has no attribute 'fetch_sets'`.

- [ ] **Step 3 : Implémenter `fetch_sets()` dans `src/extract/pokemontcg_client.py`**

Ajouter cette méthode juste après `fetch_cards_page` (même classe `PokemonTcgClient`) :
```python
    def fetch_sets(self) -> list[dict]:
        """Récupère la liste complète des sets pokemontcg.io (id, name, ...).
        Lève PokemonTcgApiError si l'API reste indisponible après épuisement
        des tentatives -- même politique de retry que fetch_cards_page, voir
        son commentaire pour le détail (backoff exponentiel, 4 tentatives)."""
        try:
            response = self._retrying(self._do_get, "/sets", {})
        except requests.RequestException as exc:
            logger.error("Échec définitif de l'appel API après retries: %s", exc)
            raise PokemonTcgApiError("Impossible de récupérer la liste des sets") from exc
        return response.json()["data"]
```

- [ ] **Step 4 : Lancer le test pour vérifier qu'il passe**

Run: `pytest tests/test_extract.py::test_fetch_sets_returns_data -v`
Expected: PASS.

- [ ] **Step 5 : Lancer la suite complète du fichier pour vérifier l'absence de régression**

Run: `pytest tests/test_extract.py -v`
Expected: tous les tests passent (les 4 existants + le nouveau).

- [ ] **Step 6 : Lint, format, commit**

```bash
ruff check . && black --check .
git add src/extract/pokemontcg_client.py tests/test_extract.py
git commit -m "feat: add fetch_sets() to PokemonTcgClient"
```

---

### Task 3 : Lecture du CSV et rapprochement (logique pure, sans DB)

**Files:**
- Create: `src/transform/collection_match.py`
- Test: `tests/test_collection_match.py`

**Interfaces:**
- Consomme : rien (fonctions pures).
- Produit : `CollectionRow` (dataclass), `MatchResult` (dataclass), `read_collection_csv(path: str) -> list[CollectionRow]`, `normalize_card_number(raw_number: str) -> str`, `match_row(row: CollectionRow, set_name_to_id: dict[str, str], known_card_ids: set[str]) -> MatchResult`. Consommées par la Task 5 (script d'orchestration) et testées directement ici sans dépendance DB/API.

- [ ] **Step 1 : Créer `tests/test_collection_match.py` (tests qui doivent échouer)**

```python
# Tests unitaires de la lecture CSV et du rapprochement (logique pure, sans
# DB ni appel API) -- src/transform/collection_match.py. Même philosophie que
# tests/test_transform.py : fonctions pures, testables en mémoire.
from __future__ import annotations

import csv
import tempfile
from pathlib import Path

from src.transform.collection_match import (
    CollectionRow,
    match_row,
    normalize_card_number,
    read_collection_csv,
)

_CSV_HEADER = [
    "Portfolio Name", "Category", "Set", "Product Name", "Card Number",
    "Rarity", "Variance", "Grade", "Card Condition", "Average Cost Paid",
    "Quantity", "Market Price (As of 2026-08-07)", "Price Override",
    "Watchlist", "Date Added", "Notes",
]


def _write_csv(tmp_path: Path, rows: list[list[str]]) -> str:
    path = tmp_path / "export.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(_CSV_HEADER)
        writer.writerows(rows)
    return str(path)


def test_normalize_card_number_strips_total_and_leading_zeros() -> None:
    assert normalize_card_number("011/193") == "11"
    assert normalize_card_number("132/193") == "132"
    assert normalize_card_number("001/025") == "1"


def test_normalize_card_number_handles_no_total() -> None:
    # Certaines cartes promo n'ont pas de "/total" dans leur numéro.
    assert normalize_card_number("SWSH001") == "SWSH001"


def test_read_collection_csv_filters_category_portfolio_and_rarity(tmp_path: Path) -> None:
    csv_path = _write_csv(tmp_path, [
        # Gardée : Pokemon, Main, Rare.
        ["Main", "Pokemon", "Paldea Evolved", "Baxcalibur", "060/193", "Rare", "Holofoil", "Ungraded", "Near Mint", "0.50", "1", "0.09", "0", "false", "2025-10-01", ""],
        # Exclue : Category=One Piece.
        ["Main", "One Piece", "500 Years in the Future", "Ain", "OP07-002", "R", "Foil", "Ungraded", "Near Mint", "0", "1", "0.21", "0", "false", "2025-05-22", ""],
        # Exclue : Portfolio Name != Main.
        ["MS - Paldea Evolved", "Pokemon", "Paldea Evolved", "Baxcalibur", "060/193", "Rare", "Holofoil", "Ungraded", "Near Mint", "0.50", "1", "0.09", "0", "false", "2025-10-01", ""],
        # Exclue : Rarity=Common.
        ["Main", "Pokemon", "Jungle", "Caterpie", "45/64", "Common", "", "Ungraded", "Near Mint", "0.10", "2", "0.15", "0", "false", "2025-01-01", ""],
        # Exclue : Rarity=Uncommon.
        ["Main", "Pokemon", "Jungle", "Metapod", "46/64", "Uncommon", "", "Ungraded", "Near Mint", "0.10", "1", "0.20", "0", "false", "2025-01-01", ""],
    ])

    rows = read_collection_csv(csv_path)

    assert len(rows) == 1
    assert rows[0] == CollectionRow(
        set_name="Paldea Evolved",
        card_number="060/193",
        product_name="Baxcalibur",
        variance="Holofoil",
        grade="Ungraded",
        rarity="Rare",
        quantity=1,
        average_cost_paid=0.50,
        market_price_at_export=0.09,
    )


def test_read_collection_csv_finds_market_price_column_regardless_of_embedded_date(tmp_path: Path) -> None:
    # Le nom de la colonne "Market Price (As of ...)" change à chaque export
    # (la date est intégrée au nom de colonne) -- doit être retrouvée par
    # préfixe, pas par un nom de colonne figé.
    header = [h if not h.startswith("Market Price") else "Market Price (As of 2099-01-01)" for h in _CSV_HEADER]
    path = Path(tempfile.mkdtemp()) / "export.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerow(["Main", "Pokemon", "Jungle", "Clefable", "1/64", "Rare", "Holofoil", "Ungraded", "Near Mint", "1.00", "1", "42.00", "0", "false", "2025-01-01", ""])

    rows = read_collection_csv(str(path))

    assert rows[0].market_price_at_export == 42.00


def test_match_row_succeeds_when_set_and_card_id_known() -> None:
    row = CollectionRow(
        set_name="Chilling Reign", card_number="132/198", product_name="Caitlin",
        variance="Holofoil", grade="Ungraded", rarity="Ultra Rare",
        quantity=1, average_cost_paid=None, market_price_at_export=None,
    )

    result = match_row(row, set_name_to_id={"Chilling Reign": "swsh6"}, known_card_ids={"swsh6-132"})

    assert result.card_id == "swsh6-132"
    assert result.rejection_reason is None


def test_match_row_rejects_unknown_set() -> None:
    row = CollectionRow(
        set_name="SV: 151", card_number="1/165", product_name="Bulbasaur",
        variance="Holofoil", grade="Ungraded", rarity="Rare",
        quantity=1, average_cost_paid=None, market_price_at_export=None,
    )

    result = match_row(row, set_name_to_id={"Chilling Reign": "swsh6"}, known_card_ids={"swsh6-132"})

    assert result.card_id is None
    assert "SV: 151" in result.rejection_reason


def test_match_row_rejects_unknown_card_id_within_known_set() -> None:
    row = CollectionRow(
        set_name="Chilling Reign", card_number="999/198", product_name="Carte inexistante",
        variance="Holofoil", grade="Ungraded", rarity="Rare",
        quantity=1, average_cost_paid=None, market_price_at_export=None,
    )

    result = match_row(row, set_name_to_id={"Chilling Reign": "swsh6"}, known_card_ids={"swsh6-132"})

    assert result.card_id is None
    assert "swsh6-999" in result.rejection_reason
```

- [ ] **Step 2 : Lancer les tests pour vérifier qu'ils échouent**

Run: `pytest tests/test_collection_match.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.transform.collection_match'`.

- [ ] **Step 3 : Créer `src/transform/collection_match.py`**

```python
# Lecture et rapprochement de la collection personnelle de l'utilisateur
# (export CSV d'une app tierce) avec le catalogue déjà en base
# (prod.dim_card, alimenté par le pipeline de prix). Fonctions PURES (aucun
# accès DB ni réseau ici) : la lecture du fichier local est un effet de bord
# limité et déterministe, mais aucune de ces fonctions n'ouvre de connexion
# DB ni n'appelle une API -- exactement la même philosophie que
# src/transform/validate.py (testable en mémoire, sans fixture DB).
#
# Voir docs/superpowers/specs/2026-08-09-personal-collection-import-design.md
# pour le raisonnement complet derrière le format de rapprochement choisi.
from __future__ import annotations

import csv
from dataclasses import dataclass


@dataclass(frozen=True)
class CollectionRow:
    """Une ligne du CSV après filtrage (Category=Pokemon, Portfolio
    Name=Main, Rarity hors Common/Uncommon/vide), avant tentative de
    rapprochement avec le catalogue."""

    set_name: str
    card_number: str
    product_name: str
    variance: str
    grade: str
    rarity: str
    quantity: int
    average_cost_paid: float | None
    market_price_at_export: float | None


@dataclass(frozen=True)
class MatchResult:
    """Résultat du rapprochement d'une CollectionRow avec prod.dim_card :
    soit card_id est renseigné (rapprochement réussi) et rejection_reason
    vaut None, soit l'inverse -- même pattern either/or que ValidationResult
    dans src/transform/validate.py."""

    row: CollectionRow
    card_id: str | None
    rejection_reason: str | None


def normalize_card_number(raw_number: str) -> str:
    """Convertit un numéro de carte au format du CSV ("011/193", avec le
    total de la série) vers le format utilisé par pokemontcg.io dans ses
    card_id ("11", sans le total ni les zéros de tête). Vérifié directement
    contre les données de production : prod.dim_card.card_id suit toujours
    le format "{set_id}-{numéro}", où le numéro n'est jamais complété par
    des zéros -- lstrip("0") retire ces zéros, `or "0"` protège le cas
    limite d'un numéro qui ne serait QUE des zéros (ex: "000"), qui
    deviendrait une chaîne vide sans cette protection.

    Certaines cartes (essentiellement des promos) n'ont pas de "/total"
    dans leur numéro d'origine (ex: "SWSH001") : split("/")[0] renvoie alors
    la chaîne complète telle quelle, inchangée."""
    number = raw_number.split("/")[0]
    return number.lstrip("0") or "0"


def _find_market_price_column(fieldnames: list[str]) -> str | None:
    """Le nom de la colonne "Market Price (As of ...)" intègre la date de
    l'export (ex: "Market Price (As of 2026-08-07)") -- ce nom change à
    CHAQUE nouvel export CSV envoyé par l'utilisateur (import réutilisable,
    voir le spec). On la retrouve donc par préfixe plutôt que par un nom de
    colonne figé qui casserait au prochain export. Renvoie None si absente
    (champ optionnel, une valeur de référence historique seulement -- ne
    doit jamais faire échouer tout l'import si elle manque)."""
    for name in fieldnames:
        if name.startswith("Market Price"):
            return name
    return None


def read_collection_csv(path: str) -> list[CollectionRow]:
    """Lit un export CSV de collection et applique les filtres d'import
    (Category=Pokemon, Portfolio Name=Main, Rarity hors Common/Uncommon/
    vide) -- voir Global Constraints du plan pour la justification de
    chaque filtre. Ne fait AUCUN rapprochement avec le catalogue (voir
    match_row ci-dessous, appelée séparément par l'appelant)."""
    rows: list[CollectionRow] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        market_price_column = _find_market_price_column(reader.fieldnames or [])
        for record in reader:
            if record["Category"] != "Pokemon":
                continue
            if record["Portfolio Name"] != "Main":
                continue
            if record["Rarity"] in ("Common", "Uncommon", ""):
                continue
            market_price_raw = record.get(market_price_column, "") if market_price_column else ""
            rows.append(
                CollectionRow(
                    set_name=record["Set"],
                    card_number=record["Card Number"],
                    product_name=record["Product Name"],
                    variance=record["Variance"] or "",
                    grade=record["Grade"] or "",
                    rarity=record["Rarity"],
                    quantity=int(record["Quantity"]),
                    average_cost_paid=float(record["Average Cost Paid"])
                    if record["Average Cost Paid"]
                    else None,
                    market_price_at_export=float(market_price_raw) if market_price_raw else None,
                )
            )
    return rows


def match_row(
    row: CollectionRow, set_name_to_id: dict[str, str], known_card_ids: set[str]
) -> MatchResult:
    """Tente de relier une CollectionRow à une carte connue de
    prod.dim_card. Aucun matching approximatif (voir le spec) : un nom de
    set absent de set_name_to_id, ou un card_id construit absent de
    known_card_ids, part en quarantaine avec une raison explicite plutôt
    que de risquer un rapprochement silencieusement erroné."""
    set_id = set_name_to_id.get(row.set_name)
    if set_id is None:
        return MatchResult(
            row=row, card_id=None, rejection_reason=f"set inconnu : {row.set_name!r}"
        )
    number = normalize_card_number(row.card_number)
    card_id = f"{set_id}-{number}"
    if card_id not in known_card_ids:
        return MatchResult(
            row=row,
            card_id=None,
            rejection_reason=f"card_id introuvable dans le catalogue : {card_id}",
        )
    return MatchResult(row=row, card_id=card_id, rejection_reason=None)
```

- [ ] **Step 4 : Lancer les tests pour vérifier qu'ils passent**

Run: `pytest tests/test_collection_match.py -v`
Expected: 6 PASS.

- [ ] **Step 5 : Lint, format, commit**

```bash
ruff check . && black --check .
git add src/transform/collection_match.py tests/test_collection_match.py
git commit -m "feat: add CSV reading and deterministic card matching for collection import"
```

---

### Task 4 : Chargement en base (`collection.raw_import`, `prod.dim_owned_card`, `collection.match_quarantine`)

**Files:**
- Create: `src/load/collection_loader.py`
- Test: `tests/test_collection_loader.py`

**Interfaces:**
- Consomme : `CollectionRow`, `MatchResult` (Task 3).
- Produit : `load_raw_import(conn, rows: list[CollectionRow]) -> list[int]` (renvoie les `id` insérés/mis à jour, dans l'ordre de `rows`, pour être réutilisés comme `raw_import_id`), `load_owned_cards(conn, matched: list[tuple[int, MatchResult]]) -> int` (le `int` de chaque tuple est le `raw_import_id` correspondant, non utilisé dans l'UPSERT lui-même mais gardé pour cohérence d'appel avec `load_quarantine`), `load_match_quarantine(conn, unmatched: list[tuple[int, MatchResult]]) -> int`. Consommées par la Task 5 (script d'orchestration).

- [ ] **Step 1 : Créer `tests/test_collection_loader.py` (tests qui doivent échouer)**

```python
# Tests d'intégration du chargement de la collection -- comme
# tests/test_staging_loader.py, ces tests touchent une vraie base Postgres
# (nécessite `docker compose up -d db`), car ils vérifient un comportement
# SQL réel (UPSERT, contraintes UNIQUE) qu'un mock ne peut pas garantir.
from __future__ import annotations

import os

import psycopg
import pytest

from src.common.config import load_db_config
from src.common.db import get_connection
from src.load.collection_loader import load_match_quarantine, load_owned_cards, load_raw_import
from src.transform.collection_match import CollectionRow, MatchResult


def _admin_dsn() -> str:
    # Même pattern que tests/test_idempotence.py : DSN admin (pas
    # pipeline_app) nécessaire pour TRUNCATE ... CASCADE ci-dessous, une
    # opération que le rôle applicatif least-privilege n'a pas le droit
    # d'exécuter.
    return (
        f"host={os.environ['POSTGRES_HOST']} port={os.environ['POSTGRES_PORT']} "
        f"dbname={os.environ['POSTGRES_DB']} user={os.environ['POSTGRES_ADMIN_USER']} "
        f"password={os.environ['POSTGRES_ADMIN_PASSWORD']}"
    )


@pytest.fixture
def db_connection():
    with psycopg.connect(_admin_dsn()) as admin_conn:
        admin_conn.execute(
            "TRUNCATE TABLE collection.raw_import, collection.match_quarantine, "
            "prod.dim_owned_card, prod.dim_card RESTART IDENTITY CASCADE;"
        )
        admin_conn.commit()
        # dim_card a besoin d'au moins une carte connue pour tester un
        # rapprochement réussi ci-dessous.
        admin_conn.execute(
            "INSERT INTO prod.dim_card (card_id, name, set_id, set_name, rarity) "
            "VALUES ('swsh6-132', 'Caitlin', 'swsh6', 'Chilling Reign', 'Ultra Rare');"
        )
        admin_conn.commit()

    with get_connection(load_db_config()) as conn:
        yield conn


def _row(**overrides) -> CollectionRow:
    defaults = dict(
        set_name="Chilling Reign",
        card_number="132/198",
        product_name="Caitlin",
        variance="Holofoil",
        grade="Ungraded",
        rarity="Ultra Rare",
        quantity=1,
        average_cost_paid=5.0,
        market_price_at_export=6.0,
    )
    defaults.update(overrides)
    return CollectionRow(**defaults)


def test_load_raw_import_is_idempotent(db_connection) -> None:
    row = _row()

    ids_first = load_raw_import(db_connection, [row])
    # Rejoue avec une quantité différente : même clé naturelle (set/numéro/
    # produit/variante/grade inchangés) -> doit mettre à jour la même ligne,
    # pas en créer une deuxième.
    ids_second = load_raw_import(db_connection, [_row(quantity=3)])

    with db_connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM collection.raw_import;")
        (count,) = cur.fetchone()
        cur.execute("SELECT quantity FROM collection.raw_import WHERE id = %s;", (ids_first[0],))
        (quantity,) = cur.fetchone()

    assert count == 1
    assert ids_first == ids_second
    assert quantity == 3


def test_load_owned_cards_inserts_matched_row(db_connection) -> None:
    row = _row()
    (raw_id,) = load_raw_import(db_connection, [row])
    match = MatchResult(row=row, card_id="swsh6-132", rejection_reason=None)

    loaded = load_owned_cards(db_connection, [(raw_id, match)])

    with db_connection.cursor() as cur:
        cur.execute(
            "SELECT card_id, quantity, average_cost_paid FROM prod.dim_owned_card WHERE card_id = %s;",
            ("swsh6-132",),
        )
        result = cur.fetchone()

    assert loaded == 1
    assert result == ("swsh6-132", 1, 5.0)


def test_load_owned_cards_is_idempotent(db_connection) -> None:
    row = _row()
    (raw_id,) = load_raw_import(db_connection, [row])
    match = MatchResult(row=row, card_id="swsh6-132", rejection_reason=None)

    load_owned_cards(db_connection, [(raw_id, match)])
    load_owned_cards(db_connection, [(raw_id, MatchResult(row=_row(quantity=4), card_id="swsh6-132", rejection_reason=None))])

    with db_connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM prod.dim_owned_card;")
        (count,) = cur.fetchone()
        cur.execute("SELECT quantity FROM prod.dim_owned_card WHERE card_id = %s;", ("swsh6-132",))
        (quantity,) = cur.fetchone()

    assert count == 1
    assert quantity == 4


def test_load_match_quarantine_records_unmatched_row(db_connection) -> None:
    row = _row(set_name="Set Inconnu")
    (raw_id,) = load_raw_import(db_connection, [row])
    match = MatchResult(row=row, card_id=None, rejection_reason="set inconnu : 'Set Inconnu'")

    loaded = load_match_quarantine(db_connection, [(raw_id, match)])

    with db_connection.cursor() as cur:
        cur.execute(
            "SELECT rejection_reason FROM collection.match_quarantine WHERE raw_import_id = %s;",
            (raw_id,),
        )
        (reason,) = cur.fetchone()

    assert loaded == 1
    assert reason == "set inconnu : 'Set Inconnu'"
```

- [ ] **Step 2 : Lancer les tests pour vérifier qu'ils échouent**

Run: `pytest tests/test_collection_loader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.load.collection_loader'`.

- [ ] **Step 3 : Créer `src/load/collection_loader.py`**

```python
# Chargement en base des données de collection personnelle déjà lues/validées
# par src/transform/collection_match.py -- aucune règle métier ici, seulement
# des INSERT/UPSERT SQL, exactement le même partage de responsabilité que
# src/load/staging_loader.py vis-à-vis de src/transform/validate.py.
from __future__ import annotations

import logging

from psycopg import Connection

from src.transform.collection_match import CollectionRow, MatchResult

logger = logging.getLogger(__name__)

# RETURNING id : renvoie l'id de la ligne insérée OU mise à jour (ON
# CONFLICT DO UPDATE renvoie aussi la ligne via RETURNING, contrairement à
# DO NOTHING qui ne renverrait rien pour une ligne déjà existante inchangée)
# -- nécessaire pour que l'appelant (Task 5) puisse ensuite référencer cette
# ligne comme raw_import_id dans collection.match_quarantine ou pour
# corréler avec prod.dim_owned_card.
_UPSERT_RAW_IMPORT_SQL = """
    INSERT INTO collection.raw_import
        (set_name, card_number, product_name, variance, grade, rarity,
         quantity, average_cost_paid, market_price_at_export)
    VALUES
        (%(set_name)s, %(card_number)s, %(product_name)s, %(variance)s, %(grade)s,
         %(rarity)s, %(quantity)s, %(average_cost_paid)s, %(market_price_at_export)s)
    ON CONFLICT (set_name, card_number, product_name, variance, grade)
    DO UPDATE SET
        rarity = EXCLUDED.rarity,
        quantity = EXCLUDED.quantity,
        average_cost_paid = EXCLUDED.average_cost_paid,
        market_price_at_export = EXCLUDED.market_price_at_export,
        imported_at = now()
    RETURNING id
"""

_UPSERT_OWNED_CARD_SQL = """
    INSERT INTO prod.dim_owned_card
        (card_id, variance, grade, quantity, average_cost_paid)
    VALUES
        (%(card_id)s, %(variance)s, %(grade)s, %(quantity)s, %(average_cost_paid)s)
    ON CONFLICT (card_id, variance, grade)
    DO UPDATE SET
        quantity = EXCLUDED.quantity,
        average_cost_paid = EXCLUDED.average_cost_paid,
        imported_at = now()
"""

_UPSERT_MATCH_QUARANTINE_SQL = """
    INSERT INTO collection.match_quarantine (raw_import_id, rejection_reason)
    VALUES (%(raw_import_id)s, %(rejection_reason)s)
    ON CONFLICT (raw_import_id)
    DO UPDATE SET
        rejection_reason = EXCLUDED.rejection_reason,
        imported_at = now()
"""


def load_raw_import(conn: Connection, rows: list[CollectionRow]) -> list[int]:
    """Charge une liste de CollectionRow dans collection.raw_import.
    Idempotent sur (set_name, card_number, product_name, variance, grade) :
    rejouer met à jour la ligne existante (ex: quantité modifiée) au lieu de
    la dupliquer. Renvoie la liste des id (insérés ou mis à jour), dans le
    MÊME ORDRE que `rows` -- l'appelant peut donc faire
    zip(rows, load_raw_import(conn, rows)) pour retrouver l'id de chaque ligne."""
    ids: list[int] = []
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(
                _UPSERT_RAW_IMPORT_SQL,
                {
                    "set_name": row.set_name,
                    "card_number": row.card_number,
                    "product_name": row.product_name,
                    "variance": row.variance,
                    "grade": row.grade,
                    "rarity": row.rarity,
                    "quantity": row.quantity,
                    "average_cost_paid": row.average_cost_paid,
                    "market_price_at_export": row.market_price_at_export,
                },
            )
            (new_id,) = cur.fetchone()
            ids.append(new_id)
    logger.info("collection.raw_import : %d lignes chargées", len(ids))
    return ids


def load_owned_cards(conn: Connection, matched: list[tuple[int, MatchResult]]) -> int:
    """Charge les MatchResult RÉUSSIS (card_id renseigné) dans
    prod.dim_owned_card. `matched` est une liste de (raw_import_id,
    MatchResult) -- raw_import_id n'est pas utilisé dans cette table (elle
    référence directement card_id, pas raw_import), gardé uniquement pour
    une signature d'appel cohérente avec load_match_quarantine. Idempotent
    sur (card_id, variance, grade)."""
    rows = [
        {
            "card_id": match.card_id,
            "variance": match.row.variance,
            "grade": match.row.grade,
            "quantity": match.row.quantity,
            "average_cost_paid": match.row.average_cost_paid,
        }
        for _, match in matched
    ]
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(_UPSERT_OWNED_CARD_SQL, rows)
    logger.info("prod.dim_owned_card : %d cartes chargées", len(rows))
    return len(rows)


def load_match_quarantine(conn: Connection, unmatched: list[tuple[int, MatchResult]]) -> int:
    """Charge les MatchResult ÉCHOUÉS (card_id=None) dans
    collection.match_quarantine. `unmatched` est une liste de
    (raw_import_id, MatchResult). Idempotent sur raw_import_id."""
    rows = [
        {"raw_import_id": raw_id, "rejection_reason": match.rejection_reason}
        for raw_id, match in unmatched
    ]
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(_UPSERT_MATCH_QUARANTINE_SQL, rows)
    logger.warning("collection.match_quarantine : %d cartes non reliées", len(rows))
    return len(rows)
```

- [ ] **Step 4 : Lancer les tests pour vérifier qu'ils passent**

Run: `pytest tests/test_collection_loader.py -v`
Expected: 4 PASS. (Nécessite `docker compose up -d db` et migrations à jour, Task 1.)

- [ ] **Step 5 : Lancer la suite complète pour vérifier l'absence de régression**

Run: `pytest -v`
Expected: tous les tests passent (existants + les nouveaux des Tasks 2-4).

- [ ] **Step 6 : Lint, format, commit**

```bash
ruff check . && black --check .
git add src/load/collection_loader.py tests/test_collection_loader.py
git commit -m "feat: add idempotent loaders for collection raw_import, dim_owned_card, match_quarantine"
```

---

### Task 5 : Script d'orchestration + rapport de rapprochement

**Files:**
- Create: `scripts/import_collection.py`

**Interfaces:**
- Consomme : `PokemonTcgClient.fetch_sets()` (Task 2), `read_collection_csv`/`match_row` (Task 3), `load_raw_import`/`load_owned_cards`/`load_match_quarantine` (Task 4).
- Produit : point d'entrée exécutable `python -m scripts.import_collection <chemin-vers-csv>`, qui affiche un rapport (cartes reliées, cartes en quarantaine, détail des sets non reconnus).

- [ ] **Step 1 : Créer `scripts/import_collection.py`**

```python
# Point d'entrée exécutable pour importer un export CSV de la collection
# personnelle de l'utilisateur. Réutilisable : peut être relancé à chaque
# nouvel export sans dupliquer les données (voir l'idempotence de
# src/load/collection_loader.py). Pas de DAG Airflow pour cet import : aucune
# façon d'automatiser la réception d'un export personnel depuis une app
# tierce, ce sera toujours un geste manuel de l'utilisateur qui fournit le
# fichier -- voir docs/superpowers/specs/2026-08-09-personal-collection-import-design.md.
#
# Usage : python -m scripts.import_collection /chemin/vers/export.csv
from __future__ import annotations

import logging
import sys
from collections import Counter

import psycopg

from src.common.config import load_db_config, load_pokemontcg_config
from src.extract.pokemontcg_client import PokemonTcgClient
from src.load.collection_loader import load_match_quarantine, load_owned_cards, load_raw_import
from src.transform.collection_match import match_row, read_collection_csv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage : python -m scripts.import_collection <chemin-vers-export.csv>")
        sys.exit(1)
    csv_path = sys.argv[1]

    # --- Étape 1 : lire le CSV (déjà filtré par read_collection_csv) ---
    rows = read_collection_csv(csv_path)
    logger.info("%d lignes candidates lues depuis %s", len(rows), csv_path)

    # --- Étape 2 : récupérer la correspondance nom de set -> set_id ---
    client = PokemonTcgClient(load_pokemontcg_config())
    sets = client.fetch_sets()
    set_name_to_id = {s["name"]: s["id"] for s in sets}
    logger.info("%d sets connus de pokemontcg.io", len(set_name_to_id))

    # --- Étape 3 : rapprochement + chargement en base ---
    # Connexion ouverte manuellement (pas get_connection()) : plusieurs
    # opérations distinctes (raw_import, puis owned_card OU quarantine)
    # doivent toutes réussir ou toutes échouer ensemble -- un seul commit à
    # la fin, pas de commit intermédiaire par ligne (contrairement à
    # l'extraction de prix, qui a besoin d'un checkpoint par page ; ici le
    # volume est petit, ~1000 lignes max, une seule transaction est
    # largement assez rapide).
    conn = psycopg.connect(load_db_config().dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT card_id FROM prod.dim_card;")
            known_card_ids = {r[0] for r in cur.fetchall()}
        logger.info("%d cartes connues dans prod.dim_card", len(known_card_ids))

        raw_ids = load_raw_import(conn, rows)
        matches = [match_row(row, set_name_to_id, known_card_ids) for row in rows]

        matched = [(rid, m) for rid, m in zip(raw_ids, matches) if m.card_id is not None]
        unmatched = [(rid, m) for rid, m in zip(raw_ids, matches) if m.card_id is None]

        load_owned_cards(conn, matched)
        load_match_quarantine(conn, unmatched)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # --- Étape 4 : rapport (décision utilisateur explicite -- voir le spec) ---
    print()
    print("=== Rapport d'import de la collection ===")
    print(f"Lignes candidates (Pokemon, Main, hors Common/Uncommon) : {len(rows)}")
    print(f"Reliées au catalogue (prod.dim_owned_card)             : {len(matched)}")
    print(f"En quarantaine (non reliées)                            : {len(unmatched)}")
    if unmatched:
        print()
        print("Détail des raisons de quarantaine :")
        reasons = Counter(m.rejection_reason.split(" : ")[0] for _, m in unmatched)
        for reason, count in reasons.most_common():
            print(f"  {count:4d}  {reason}")
        print()
        print("Sets non reconnus (nécessitent une correspondance manuelle) :")
        unknown_sets = Counter(
            m.row.set_name for _, m in unmatched if "set inconnu" in m.rejection_reason
        )
        for set_name, count in unknown_sets.most_common():
            print(f"  {count:4d}  {set_name!r}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2 : Vérifier avec un petit échantillon du vrai CSV (sans toucher aux vraies données de collection réelles au-delà de ce test)**

```bash
docker compose up -d db
head -1 export.csv > /tmp/collection_sample.csv
grep ",Pokemon,Paldea Evolved," export.csv | head -20 >> /tmp/collection_sample.csv
python -m scripts.import_collection /tmp/collection_sample.csv
```
Expected : le rapport s'affiche, avec un compte de lignes reliées et/ou en quarantaine cohérent avec l'échantillon (20 lignes ou moins, selon les doublons de clé naturelle). Aucune exception.

- [ ] **Step 3 : Lint, format, commit**

```bash
ruff check . && black --check .
git add scripts/import_collection.py
git commit -m "feat: add scripts/import_collection.py orchestration entry point"
```

---

### Task 6 : Import réel en production + rapport à l'utilisateur

**Files:**
- (Aucun nouveau fichier — déploiement des Tasks 1-5 sur le VPS déjà en production, puis import du vrai `export.csv` de l'utilisateur.)

**Interfaces:**
- Consomme : le code des Tasks 1-5, le vrai fichier `export.csv` (à transférer sur le VM, absent du repo git).
- Produit : `prod.dim_owned_card` peuplée en production avec la vraie collection de l'utilisateur ; un rapport précis (comptes + détail) présenté à l'utilisateur.

- [ ] **Step 1 : Pousser les commits des Tasks 1-5**

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
Expected : `Applique : 008_create_collection_schema.sql`, sans erreur.

- [ ] **Step 3 : Transférer le vrai `export.csv` sur le VM**

Depuis la machine locale (PAS sur la VM) :
```bash
scp export.csv card-tracker-vm:~/card-price-tracker/export.csv
```
Expected : transfert réussi. Ce fichier reste local à la VM, jamais commité (déjà dans `.gitignore`) ni repoussé nulle part ailleurs.

- [ ] **Step 4 : Lancer l'import réel en production**

```bash
ssh card-tracker-vm
cd ~/card-price-tracker
docker compose -f docker-compose.prod.yml exec -T airflow-scheduler python -m scripts.import_collection /opt/airflow/../export.csv
```
Si le chemin `/opt/airflow/../export.csv` ne fonctionne pas depuis l'intérieur du conteneur (le fichier est sur l'hôte VM, pas monté dans le conteneur par défaut), lancer plutôt directement sur l'hôte VM avec l'environnement Python du dépôt :
```bash
cd ~/card-price-tracker
python3 -m venv .venv && source .venv/bin/activate   # si pas déjà fait
pip install -e ".[dev]"
export $(grep -v '^#' .env | xargs)   # charge les variables du .env dans ce shell
python -m scripts.import_collection export.csv
```
Expected : le rapport s'affiche (comptes + détail des sets non reconnus), sans exception.

- [ ] **Step 5 : Vérifier les données chargées en base**

```bash
docker compose -f docker-compose.prod.yml exec db psql -U "$POSTGRES_ADMIN_USER" -d "$POSTGRES_DB" -c "
SELECT count(*) AS cartes_reliees, sum(quantity) AS quantite_totale FROM prod.dim_owned_card;
"
docker compose -f docker-compose.prod.yml exec db psql -U "$POSTGRES_ADMIN_USER" -d "$POSTGRES_DB" -c "
SELECT count(*) AS en_quarantaine FROM collection.match_quarantine;
"
```
Expected : des chiffres cohérents avec le rapport du Step 4.

- [ ] **Step 6 : Présenter le rapport complet à l'utilisateur**

Rapporter explicitement (décision utilisateur, voir Global Constraints) : le nombre total de cartes candidates, le nombre reliées avec succès, le nombre en quarantaine, et la liste des noms de sets non reconnus avec leur fréquence — pas seulement "l'import a réussi".

---

## Self-Review Notes

- **Couverture du spec** : schéma `collection` + `prod.dim_owned_card` ✓ (Task 1), rapprochement déterministe via `/v2/sets` + `card_id` construit ✓ (Tasks 2-3), aucun matching approximatif (quarantaine explicite) ✓ (Task 3, `match_row`), idempotence par UPSERT ✓ (Task 4), script manuel réutilisable sans DAG ✓ (Task 5), `dashboard_reader` avec `SELECT` sur `dim_owned_card` ✓ (Task 1, grant), confidentialité de `export.csv` ✓ (déjà fait hors plan, `.gitignore`), rapport explicite du rapprochement ✓ (Task 5 script + Task 6 présentation utilisateur).
- **Cohérence des types** : `CollectionRow`/`MatchResult` définis en Task 3, consommés tels quels en Task 4 (`load_owned_cards`/`load_match_quarantine` prennent `list[tuple[int, MatchResult]]`) et Task 5 (construction de ces tuples via `zip`). `card_id: str | None` sur `MatchResult` cohérent partout (jamais traité comme non-optionnel avant vérification).
- **Ordre des tasks** : Task 1 (schéma) doit précéder Task 4 (chargement, a besoin des tables). Task 2 (fetch_sets) et Task 3 (matching pur) sont indépendantes entre elles mais toutes deux nécessaires à Task 5. Task 6 dépend de toutes les précédentes (déploiement du tout + import réel).
- **Colonne "Market Price" au nom variable** : géré explicitement (Task 3, `_find_market_price_column`, testé) — piège concret identifié pendant le brainstorming (le nom de colonne intègre la date de l'export, donc change à chaque nouveau CSV envoyé par l'utilisateur).
