# Rapports daily/weekly (gros mouvements de collection) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ajouter une page "Rapports" au dashboard qui liste les cartes possédées ayant bougé de ±10/20/30% ou plus sur 7 jours (daily) et 30 jours (weekly), avec pour la vue weekly un top 5 hausses / top 5 baisses accompagné de mini-graphiques.

**Architecture:** Un seul nouvel endpoint FastAPI `GET /api/reports/movers?window=7|30`, qui délègue à une nouvelle fonction SQL dans `src/api/queries.py` (jointure `dim_owned_card`/`dim_card`/`fact_price_history` avec `ROW_NUMBER()` pour retrouver le prix courant et le prix N observations en arrière), puis calcule `pct_change`/`threshold`/filtre/trie côté Python dans `src/api/main.py` — exactement le même partage de responsabilités (SQL fait les jointures, Python calcule les champs dérivés) que `/api/collection` pour `market_value`/`gain_loss`. Le frontend ajoute une page `Rapports.tsx` avec deux sections qui appellent ce même endpoint avec des paramètres différents ; la section weekly réutilise l'endpoint déjà existant `GET /api/cards/{card_id}/history` pour ses mini-graphiques.

**Tech Stack:** FastAPI + Pydantic + psycopg (backend, déjà en place), React + TypeScript + Recharts + react-router-dom (frontend, déjà en place). Aucune nouvelle dépendance.

**Spec:** `docs/superpowers/specs/2026-08-21-collection-movers-report-design.md`

## Global Constraints

- Toutes les requêtes filtrent `platform_name = 'tcgplayer'` (jamais de mélange avec cardmarket/EUR) — même règle que le reste de l'API (`src/api/queries.py:_PLATFORM`).
- `window` n'accepte que `7` ou `30` côté endpoint ; toute autre valeur → `422`.
- Seules les cartes possédées (`prod.dim_owned_card`) sont concernées — pas le catalogue complet.
- Une carte n'apparaît dans la réponse que si elle a une observation de prix à la fois à la date la plus récente et à la date de comparaison (sinon `current_price`/`past_price` est `None` et la carte est exclue en Python, jamais en SQL).
- Le filtre `cost_unknown` ne s'applique jamais ici (il ne concerne que le coût d'achat, pas le prix de marché comparé).
- Aucune nouvelle table, aucune tâche Airflow — calcul à la volée uniquement.

---

### Task 1: Requête SQL `get_collection_movers`

**Files:**
- Modify: `src/api/queries.py` (ajouter la fonction en fin de fichier, après `get_collection_value_history`)
- Test: `tests/test_api_queries.py` (ajouter après `test_collection_value_history_includes_unknown_cost_rows`)

**Interfaces:**
- Consumes: rien de nouveau — utilise `_PLATFORM` déjà défini en haut de `src/api/queries.py`, et la fixture `db_connection` déjà définie dans `tests/test_api_queries.py`.
- Produces: `get_collection_movers(conn, *, window: int) -> list[dict]`, chaque dict avec les clés `card_id`, `name`, `quantity`, `current_price`, `past_price` (les deux derniers peuvent être `None`). Utilisé par Task 3.

- [ ] **Step 1: Write the failing test**

Ajouter dans `tests/test_api_queries.py`, juste après `test_collection_value_history_includes_unknown_cost_rows` (et ajouter `get_collection_movers` à l'import existant en haut du fichier, à côté de `get_collection_value_history`) :

```python
def test_get_collection_movers_maps_current_and_past_price(db_connection):
    # window=1 : compare la dernière observation (rn=1) à celle d'avant (rn=2).
    # base1-1 a 2 observations (08-01=10.00, 08-02=12.00, quantity=2) -- les
    # deux rangs existent. base1-2 n'a qu'UNE observation (08-02=50.00) --
    # rn=2 n'existe pas, past_price doit être None (pas d'extrapolation).
    rows = get_collection_movers(db_connection, window=1)
    by_card = {row["card_id"]: row for row in rows}

    assert float(by_card["base1-1"]["current_price"]) == 12.00
    assert float(by_card["base1-1"]["past_price"]) == 10.00
    assert by_card["base1-1"]["quantity"] == 2

    assert float(by_card["base1-2"]["current_price"]) == 50.00
    assert by_card["base1-2"]["past_price"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_api_queries.py::test_get_collection_movers_maps_current_and_past_price -v`
Expected: FAIL avec `ImportError: cannot import name 'get_collection_movers'`

- [ ] **Step 3: Write minimal implementation**

Ajouter en fin de `src/api/queries.py` :

```python
def get_collection_movers(conn, *, window: int) -> list[dict]:
    # ranked : classe chaque observation de prix d'une carte par ancienneté
    # décroissante (rn=1 = la plus récente). cur = rn=1 (prix actuel), past
    # = rn=1+window (prix il y a "window" observations, PAS "window jours
    # calendaires" -- même convention que les moyennes mobiles déjà
    # implémentées côté frontend sur la fiche carte). LEFT JOIN : une carte
    # dont l'historique est trop court pour atteindre le rang demandé garde
    # past_price = NULL plutôt que d'être silencieusement exclue par un JOIN
    # classique -- l'exclusion se décide en Python (Task 3), pas ici.
    return conn.execute(
        """
        WITH ranked AS (
            SELECT
                fph.card_id, fph.average_sell_price,
                ROW_NUMBER() OVER (PARTITION BY fph.card_id ORDER BY fph.date_id DESC) AS rn
            FROM prod.fact_price_history fph
            JOIN prod.dim_platform p ON p.platform_id = fph.platform_id
            WHERE p.platform_name = %(platform)s
        )
        SELECT
            o.card_id, c.name, o.quantity,
            cur.average_sell_price AS current_price,
            past.average_sell_price AS past_price
        FROM prod.dim_owned_card o
        JOIN prod.dim_card c ON c.card_id = o.card_id
        LEFT JOIN ranked cur ON cur.card_id = o.card_id AND cur.rn = 1
        LEFT JOIN ranked past ON past.card_id = o.card_id AND past.rn = 1 + %(window)s
        """,
        {"platform": _PLATFORM, "window": window},
    ).fetchall()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_api_queries.py::test_get_collection_movers_maps_current_and_past_price -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/api/queries.py tests/test_api_queries.py
git commit -m "feat: ajoute get_collection_movers pour comparer le prix actuel au prix passé d'une carte possédée"
```

---

### Task 2: Schéma Pydantic `MoverCard`

**Files:**
- Modify: `src/api/schemas.py` (ajouter en fin de fichier)

**Interfaces:**
- Consumes: rien.
- Produces: `MoverCard` (Pydantic `BaseModel`) avec les champs `card_id: str`, `name: str`, `quantity: int`, `current_price: float`, `past_price: float`, `pct_change: float`, `threshold: int`. Utilisé par Task 3 (comme `response_model`) et implicitement par le frontend (Task 4, mêmes noms de champs en JSON).

- [ ] **Step 1: Write the model**

Ajouter en fin de `src/api/schemas.py` :

```python
class MoverCard(BaseModel):
    card_id: str
    name: str
    quantity: int
    current_price: float
    past_price: float
    pct_change: float
    threshold: int
```

- [ ] **Step 2: Vérifier que le module s'importe sans erreur**

Run: `.venv/bin/python -c "from src.api.schemas import MoverCard; print(MoverCard(card_id='x', name='y', quantity=1, current_price=1.0, past_price=1.0, pct_change=0.0, threshold=10))"`
Expected: affiche l'instance sans erreur (pas de test pytest dédié -- ce fichier ne contient que des définitions de modèles, déjà couvertes indirectement par les tests d'endpoint de Task 3).

- [ ] **Step 3: Commit**

```bash
git add src/api/schemas.py
git commit -m "feat: ajoute le schéma MoverCard pour l'endpoint reports/movers"
```

---

### Task 3: Endpoint `GET /api/reports/movers`

**Files:**
- Modify: `src/api/main.py` (ajouter l'import `MoverCard` à la liste d'imports existante depuis `src.api.schemas`, puis l'endpoint en fin de fichier)
- Test: `tests/test_api_main.py` (ajouter après `test_collection_value_history`)

**Interfaces:**
- Consumes: `queries.get_collection_movers(conn, *, window: int) -> list[dict]` (Task 1), `MoverCard` (Task 2).
- Produces: endpoint HTTP `GET /api/reports/movers?window=7|30` → `list[MoverCard]` en JSON. Consommé par le frontend (Task 4) via `fetchMovers`.

- [ ] **Step 1: Write the failing tests**

Ajouter dans `tests/test_api_main.py`, après `test_collection_value_history` :

```python
def test_reports_movers_rejects_invalid_window(client):
    response = client.get("/api/reports/movers", params={"window": 14})
    assert response.status_code == 422


def test_reports_movers_filters_sorts_and_flags_threshold(client):
    # Setup dédié : 3 cartes possédées sur 8 jours (2026-08-01 .. 2026-08-08).
    # - base1-1 : 10.00 -> 15.00 sur les 8 jours = +50% (franchit le palier 30)
    # - base1-2 : seulement 3 observations (06,07,08) -> pas de rang 8,
    #   donc past_price=None pour window=7 -- doit être EXCLUE de la réponse.
    # - base1-3 : 10.00 -> 10.30 = +3% -- sous le seuil de 10%, doit être EXCLUE.
    with psycopg.connect(_admin_dsn()) as admin_conn:
        admin_conn.execute(
            "INSERT INTO prod.dim_date (date_id, year, month, day, day_of_week) "
            "SELECT d, EXTRACT(YEAR FROM d)::int, EXTRACT(MONTH FROM d)::int, "
            "EXTRACT(DAY FROM d)::int, EXTRACT(DOW FROM d)::int "
            "FROM generate_series(date '2026-08-01', date '2026-08-08', interval '1 day') AS d "
            "ON CONFLICT (date_id) DO NOTHING"
        )
        admin_conn.execute(
            "INSERT INTO prod.dim_card (card_id, name, set_id, set_name, rarity, series) "
            "VALUES "
            "('base1-2', 'Blastoise', 'base1', 'Base Set', 'Rare Holo', 'Base'), "
            "('base1-3', 'Charizard', 'base1', 'Base Set', 'Rare Holo', 'Base')"
        )
        platform_id = admin_conn.execute(
            "SELECT platform_id FROM prod.dim_platform WHERE platform_name = 'tcgplayer'"
        ).fetchone()[0]
        admin_conn.execute(
            "INSERT INTO prod.fact_price_history "
            "(card_id, date_id, platform_id, average_sell_price, trend_price, low_price) "
            "SELECT 'base1-1', d, %(platform_id)s, "
            "  CASE WHEN d = date '2026-08-08' THEN 15.00 ELSE 10.00 END, NULL, NULL "
            "FROM generate_series(date '2026-08-01', date '2026-08-08', interval '1 day') AS d "
            "UNION ALL "
            "SELECT 'base1-2', d, %(platform_id)s, 20.00, NULL, NULL "
            "FROM generate_series(date '2026-08-06', date '2026-08-08', interval '1 day') AS d "
            "UNION ALL "
            "SELECT 'base1-3', d, %(platform_id)s, "
            "  CASE WHEN d = date '2026-08-08' THEN 10.30 ELSE 10.00 END, NULL, NULL "
            "FROM generate_series(date '2026-08-01', date '2026-08-08', interval '1 day') AS d",
            {"platform_id": platform_id},
        )
        admin_conn.execute(
            "INSERT INTO prod.dim_owned_card (card_id, variance, grade, quantity, average_cost_paid) "
            "VALUES "
            "('base1-2', 'Normal', '', 1, 5.00), "
            "('base1-3', 'Normal', '', 1, 10.00)"
        )
        admin_conn.commit()

    response = client.get("/api/reports/movers", params={"window": 7})
    assert response.status_code == 200
    movers = response.json()

    card_ids = [m["card_id"] for m in movers]
    assert card_ids == ["base1-1"]  # base1-2 (past manquant) et base1-3 (<10%) exclues

    mover = movers[0]
    assert mover["current_price"] == 15.00
    assert mover["past_price"] == 10.00
    assert mover["pct_change"] == 50.0
    assert mover["threshold"] == 30
    assert mover["quantity"] == 2
```

Le fixture `client` existant dans `tests/test_api_main.py` insère déjà `base1-1` (quantity=2) avec une seule observation de prix (08-01=10.00) -- ce test ajoute les 7 observations manquantes (02..08) pour que `base1-1` atteigne `10.00 -> 15.00` sur la fenêtre de 7. Vérifie avant d'écrire ce test que `psycopg` est bien importé en haut de `tests/test_api_main.py` (déjà le cas, ligne 11).

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_api_main.py::test_reports_movers_rejects_invalid_window tests/test_api_main.py::test_reports_movers_filters_sorts_and_flags_threshold -v`
Expected: FAIL avec `404 Not Found` (route inexistante)

- [ ] **Step 3: Write minimal implementation**

Dans `src/api/main.py`, ajouter `MoverCard` à l'import existant :

```python
from src.api.schemas import (
    CardHistoryResponse,
    CardListResponse,
    CardSummary,
    CollectionResponse,
    CollectionValuePoint,
    MoverCard,
    OwnedCard,
    PricePoint,
)
```

Puis ajouter l'endpoint en fin de fichier :

```python
@app.get("/api/reports/movers", response_model=list[MoverCard])
def reports_movers(window: int, conn=Depends(get_api_connection)) -> list[MoverCard]:
    if window not in (7, 30):
        raise HTTPException(status_code=422, detail="window doit être 7 ou 30")
    rows = queries.get_collection_movers(conn, window=window)
    movers = []
    for row in rows:
        current_price = row["current_price"]
        past_price = row["past_price"]
        # Pas d'extrapolation : une carte sans prix aux deux dates ne peut
        # pas avoir de variation calculable, donc exclue plutôt qu'affichée
        # avec une valeur trompeuse.
        if current_price is None or past_price is None or past_price == 0:
            continue
        pct_change = (current_price - past_price) / past_price * 100
        if abs(pct_change) < 10:
            continue
        threshold = 30 if abs(pct_change) >= 30 else 20 if abs(pct_change) >= 20 else 10
        movers.append(
            MoverCard(
                card_id=row["card_id"],
                name=row["name"],
                quantity=row["quantity"],
                current_price=current_price,
                past_price=past_price,
                pct_change=pct_change,
                threshold=threshold,
            )
        )
    movers.sort(key=lambda m: abs(m.pct_change), reverse=True)
    return movers
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_api_main.py -v`
Expected: PASS (tous les tests du fichier, y compris les 2 nouveaux)

- [ ] **Step 5: Run the full backend suite**

Run: `.venv/bin/python -m pytest tests/test_api_queries.py tests/test_api_main.py -q`
Expected: tous les tests passent (aucune régression sur les endpoints existants)

- [ ] **Step 6: Commit**

```bash
git add src/api/main.py tests/test_api_main.py
git commit -m "feat: ajoute l'endpoint GET /api/reports/movers (daily/weekly gros mouvements)"
```

---

### Task 4: Client HTTP frontend (`fetchMovers`)

**Files:**
- Modify: `frontend/src/api/client.ts` (ajouter l'interface et la fonction en fin de fichier)

**Interfaces:**
- Consumes: endpoint `/api/reports/movers` (Task 3).
- Produces: `interface MoverCard` (mêmes champs que le schéma Pydantic : `card_id`, `name`, `quantity`, `current_price`, `past_price`, `pct_change`, `threshold`) et `fetchMovers(window: 7 | 30): Promise<MoverCard[]>`. Utilisé par Task 5 (`Rapports.tsx`).

- [ ] **Step 1: Write the interface and function**

Ajouter en fin de `frontend/src/api/client.ts` :

```ts
export interface MoverCard {
  card_id: string
  name: string
  quantity: number
  current_price: number
  past_price: number
  pct_change: number
  threshold: number
}

export async function fetchMovers(window: 7 | 30): Promise<MoverCard[]> {
  const response = await fetch(`/api/reports/movers?window=${window}`)
  if (!response.ok) {
    throw new Error(`Erreur API /api/reports/movers : ${response.status}`)
  }
  return response.json()
}
```

- [ ] **Step 2: Vérifier la compilation TypeScript**

Run: `cd frontend && npm run build`
Expected: build réussi, aucune erreur `tsc`

- [ ] **Step 3: Commit**

```bash
git add frontend/src/api/client.ts
git commit -m "feat: ajoute fetchMovers au client API frontend"
```

---

### Task 5: Page `Rapports.tsx`

**Files:**
- Create: `frontend/src/pages/Rapports.tsx`
- Modify: `frontend/src/index.css` (ajouter les styles de badge de palier en fin de fichier)

**Interfaces:**
- Consumes: `fetchMovers(window: 7 | 30)` et `MoverCard` (Task 4), `fetchCardHistory(cardId)` et `CardHistory`/`PricePoint` (déjà existants dans `frontend/src/api/client.ts`, voir `DetailCarte.tsx` pour l'usage de référence).
- Produces: composant `Rapports` exporté, monté par Task 6 sur la route `/rapports`.

- [ ] **Step 1: Créer la page complète (tableaux daily/weekly + top 5 hausses/baisses weekly avec mini-graphiques)**

Créer `frontend/src/pages/Rapports.tsx` directement dans sa version finale (pas de version intermédiaire sans le top/flop : une version partielle laisserait des imports inutilisés et casserait le build, `noUnusedLocals: true` dans `frontend/tsconfig.app.json`) :

```tsx
import { useEffect, useState } from 'react'
import { Line, LineChart, ResponsiveContainer } from 'recharts'
import { Link } from 'react-router-dom'
import type { CardHistory, MoverCard } from '../api/client'
import { fetchCardHistory, fetchMovers } from '../api/client'

function formatPrice(value: number): string {
  return `$${value.toFixed(2)}`
}

function formatChange(value: number): string {
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(1)}%`
}

function MoversTable({ movers }: { movers: MoverCard[] }) {
  if (movers.length === 0) {
    return <p role="note">Aucun mouvement notable sur cette période.</p>
  }
  return (
    <table>
      <thead>
        <tr>
          <th>Nom</th>
          <th className="cell-price">Quantité</th>
          <th className="cell-price">Prix passé</th>
          <th className="cell-price">Prix actuel</th>
          <th className="cell-price">Variation</th>
        </tr>
      </thead>
      <tbody>
        {movers.map((mover) => (
          <tr key={mover.card_id}>
            <td>
              <Link to={`/cartes/${mover.card_id}`}>{mover.name}</Link>
            </td>
            <td className="cell-price">{mover.quantity}</td>
            <td className="cell-price">{formatPrice(mover.past_price)}</td>
            <td className="cell-price">{formatPrice(mover.current_price)}</td>
            <td className="cell-price">
              <span className={`mover-badge mover-badge-${mover.threshold} ${mover.pct_change > 0 ? 'mover-up' : 'mover-down'}`}>
                {formatChange(mover.pct_change)}
              </span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

// Mini-graphique d'aperçu (pas de sélecteur de plage ni de moyennes
// mobiles -- c'est un aperçu top/flop, pas la fiche carte complète de
// DetailCarte.tsx). `history` peut être `undefined` pendant le chargement
// -- affiche un espace réservé plutôt que de planter Recharts sur un
// tableau vide.
function MiniMoverCard({ mover, history }: { mover: MoverCard; history: CardHistory | undefined }) {
  return (
    <Link to={`/cartes/${mover.card_id}`} className="mini-mover-card">
      <div className="mini-mover-name">{mover.name}</div>
      <div className={`mini-mover-change ${mover.pct_change > 0 ? 'mover-up' : 'mover-down'}`}>
        {formatChange(mover.pct_change)}
      </div>
      <div className="mini-mover-chart">
        {history ? (
          <ResponsiveContainer width="100%" height={60}>
            <LineChart data={history.history}>
              <Line type="monotone" dataKey="average_sell_price" stroke="#2451ff" dot={false} strokeWidth={2} />
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <div className="mini-mover-chart-placeholder" />
        )}
      </div>
    </Link>
  )
}

function TopMoversPanel({
  title,
  movers,
  histories,
}: {
  title: string
  movers: MoverCard[]
  histories: Map<string, CardHistory>
}) {
  if (movers.length === 0) return null
  return (
    <div className="top-movers-panel">
      <h3>{title}</h3>
      <div className="top-movers-grid">
        {movers.map((mover) => (
          <MiniMoverCard key={mover.card_id} mover={mover} history={histories.get(mover.card_id)} />
        ))}
      </div>
    </div>
  )
}

export function Rapports() {
  const [daily, setDaily] = useState<MoverCard[]>([])
  const [weekly, setWeekly] = useState<MoverCard[]>([])
  const [histories, setHistories] = useState<Map<string, CardHistory>>(new Map())
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetchMovers(7)
      .then(setDaily)
      .catch((err) => setError(String(err)))
    fetchMovers(30)
      .then(setWeekly)
      .catch((err) => setError(String(err)))
  }, [])

  const topGains = weekly.filter((m) => m.pct_change > 0).slice(0, 5)
  const topLosses = [...weekly]
    .filter((m) => m.pct_change < 0)
    .sort((a, b) => a.pct_change - b.pct_change)
    .slice(0, 5)

  useEffect(() => {
    // Un fetch indépendant par carte du top/flop : l'échec d'une seule
    // (ex : historique incomplet) ne doit pas empêcher l'affichage des
    // autres mini-graphiques.
    const cardIds = [...topGains, ...topLosses].map((m) => m.card_id)
    cardIds.forEach((cardId) => {
      if (histories.has(cardId)) return
      fetchCardHistory(cardId)
        .then((history) => {
          setHistories((prev) => new Map(prev).set(cardId, history))
        })
        .catch(() => {
          // Silencieux : MiniMoverCard affiche un espace réservé si
          // l'historique n'arrive jamais pour cette carte.
        })
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [weekly])

  return (
    <div className="page">
      <h1>Rapports</h1>
      {error && <p role="alert">{error}</p>}

      <h2>Daily (7 jours)</h2>
      <MoversTable movers={daily} />

      <h2>Weekly (30 jours)</h2>
      <TopMoversPanel title="Top 5 hausses" movers={topGains} histories={histories} />
      <TopMoversPanel title="Top 5 baisses" movers={topLosses} histories={histories} />
      <MoversTable movers={weekly} />
    </div>
  )
}
```

- [ ] **Step 2: Vérifier la compilation**

Run: `cd frontend && npm run build`
Expected: build réussi

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/Rapports.tsx
git commit -m "feat: ajoute la page Rapports (daily/weekly + top 5 hausses/baisses avec mini-graphiques)"
```

- [ ] **Step 4: Ajouter les styles CSS**

Ajouter en fin de `frontend/src/index.css` :

```css
.mover-badge {
  font-family: var(--font-mono);
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 4px;
}

.mover-up {
  background: #dcfce7;
  color: #166534;
}

.mover-down {
  background: #fee2e2;
  color: #991b1b;
}

.mover-badge-30 {
  outline: 2px solid currentColor;
  outline-offset: 1px;
}

.top-movers-panel {
  margin-bottom: 20px;
}

.top-movers-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
}

.mini-mover-card {
  display: block;
  padding: 12px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  text-decoration: none;
  color: var(--ink);
}

.mini-mover-name {
  font-size: 13px;
  font-weight: 500;
  margin-bottom: 4px;
}

.mini-mover-change {
  font-family: var(--font-mono);
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 6px;
}

.mini-mover-chart-placeholder {
  height: 60px;
}
```

- [ ] **Step 5: Vérifier la compilation puis commit**

Run: `cd frontend && npm run build`
Expected: build réussi

```bash
git add frontend/src/index.css
git commit -m "style: ajoute les badges de palier et les cartes mini-mover à la page Rapports"
```

---

### Task 6: Routage et navigation

**Files:**
- Modify: `frontend/src/App.tsx`

**Interfaces:**
- Consumes: composant `Rapports` (Task 5).
- Produces: route `/rapports` accessible depuis la nav, dernière étape visible de la fonctionnalité.

- [ ] **Step 1: Ajouter la route et le lien de navigation**

Dans `frontend/src/App.tsx`, ajouter l'import :

```tsx
import { Rapports } from './pages/Rapports'
```

Ajouter le lien de nav, après celui de "Ma collection" :

```tsx
<NavLink to="/rapports" className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}>
  Rapports
</NavLink>
```

Ajouter la route, après celle de `/collection` :

```tsx
<Route path="/rapports" element={<Rapports />} />
```

- [ ] **Step 2: Vérifier la compilation**

Run: `cd frontend && npm run build`
Expected: build réussi

- [ ] **Step 3: Commit**

```bash
git add frontend/src/App.tsx
git commit -m "feat: ajoute la route et le lien de navigation vers /rapports"
```

---

### Task 7: Vérification manuelle et déploiement

**Files:** aucun (déploiement uniquement)

- [ ] **Step 1: Lancer la suite de tests backend complète**

Démarrer la base de test si nécessaire (`docker compose up -d db` depuis la racine du repo), puis :

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: tous les tests passent, aucune régression

Si le conteneur `db` n'était pas déjà démarré avant cette tâche, l'arrêter après (`docker compose stop db`) pour revenir à l'état initial.

- [ ] **Step 2: Vérifier manuellement l'endpoint via curl (contre la base de test ou de prod)**

Run: `curl -s "http://localhost:8000/api/reports/movers?window=30" | head -c 500` (adapter l'hôte/port à l'environnement disponible -- voir `annonces-vps` en production, section suivante)
Expected: JSON avec des cartes triées par variation absolue décroissante, chacune avec `threshold` cohérent avec `pct_change`

- [ ] **Step 3: Vérification visuelle si un navigateur est disponible**

Si les outils `mcp__claude-in-chrome__*` sont connectés : naviguer vers `/rapports`, vérifier que les deux sections s'affichent, que les badges de couleur/palier sont cohérents, que les mini-graphiques du top/flop weekly se chargent, et qu'un clic sur une carte renvoie bien vers sa fiche détail.

Si aucun navigateur n'est disponible dans l'environnement d'implémentation (déjà rencontré sur ce projet, voir `docs/superpowers/specs/2026-08-13-custom-dashboard-design.md`), le signaler explicitement plutôt que d'affirmer une vérification visuelle non faite -- la vérification API réelle (Step 2) et la relecture du code restent la preuve disponible.

- [ ] **Step 4: Déployer sur le VPS**

```bash
ssh annonces-vps "cd ~/card-price-tracker && git pull && docker compose -f docker-compose.prod.yml build dashboard-api dashboard-frontend && docker compose -f docker-compose.prod.yml up -d dashboard-api dashboard-frontend"
```

- [ ] **Step 5: Vérifier le déploiement**

Run: `ssh annonces-vps "docker compose -f card-price-tracker/docker-compose.prod.yml ps --format 'table {{.Name}}\t{{.Status}}'"`
Expected: `dashboard-api` et `dashboard-frontend` `Up` et `healthy`

Run: `ssh annonces-vps "curl -sk https://100.116.232.89:8000/api/reports/movers?window=7 | head -c 300"`
Expected: réponse JSON cohérente (liste de cartes ou `[]` si aucun mouvement notable actuellement)

- [ ] **Step 6: Mettre à jour le statut de la spec**

Modifier la ligne "Statut" en tête de `docs/superpowers/specs/2026-08-21-collection-movers-report-design.md` :

```markdown
**Statut :** implémenté et déployé en production (<date du jour>).
```

```bash
git add docs/superpowers/specs/2026-08-21-collection-movers-report-design.md
git commit -m "docs: marque la spec des rapports movers comme implémentée"
git push origin main
```
