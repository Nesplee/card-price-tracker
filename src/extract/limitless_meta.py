# Méta compétitive (ajouté le 2026-11-06) : decklists du top 8 des tournois
# Standard de play.limitlesstcg.com (API publique, sans clé). Une carte qui
# monte dans les decks gagnants voit aussi monter le prix de ses versions
# illustrées ; une carte qui disparaît du méta, l'inverse. On stocke, par
# tournoi, combien de decklists du top 8 jouent chaque carte (par nom) dans
# raw.meta_card_usage ; src/signals/opportunities.py compare ensuite la
# semaine écoulée aux deux précédentes.
from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import UTC, date, datetime, timedelta

import requests
from psycopg import Connection

log = logging.getLogger(__name__)

API = "https://play.limitlesstcg.com/api"
_HEADERS = {"User-Agent": "CardPriceTracker/1.0 (personal price tracker)"}
MIN_PLAYERS = 64
TOP_CUT = 8
LOOKBACK = timedelta(days=21)
# Un tournoi encore en cours n'a pas de classement final.
MIN_AGE = timedelta(days=1)
_PAUSE_S = 1.0


def top_cut_usage(standings: list[dict], top_cut: int = TOP_CUT) -> tuple[int, Counter]:
    """(nombre de decklists du top cut, nombre de decklists jouant chaque
    carte). Une carte jouée en 4 exemplaires compte une fois par deck."""
    decks = [
        s["decklist"]
        for s in standings
        if s.get("placing") is not None and s["placing"] <= top_cut and s.get("decklist")
    ]
    usage: Counter = Counter()
    for deck in decks:
        names = {card["name"] for section in deck.values() for card in section if card.get("name")}
        usage.update(names)
    return len(decks), usage


class LimitlessClient:
    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()

    def _get(self, path: str, **params) -> list[dict]:
        response = self._session.get(f"{API}{path}", params=params, headers=_HEADERS, timeout=30)
        response.raise_for_status()
        return response.json()

    def recent_tournaments(self, now: datetime) -> list[dict]:
        """Tournois Standard terminés des LOOKBACK derniers jours, avec au
        moins MIN_PLAYERS joueurs. L'API renvoie les plus récents d'abord."""
        found = []
        for page in range(1, 11):
            batch = self._get("/tournaments", game="PTCG", format="STANDARD", limit=100, page=page)
            if not batch:
                break
            for t in batch:
                played = datetime.fromisoformat(t["date"].replace("Z", "+00:00"))
                if now - LOOKBACK <= played <= now - MIN_AGE and t["players"] >= MIN_PLAYERS:
                    found.append(t)
            oldest = datetime.fromisoformat(batch[-1]["date"].replace("Z", "+00:00"))
            if oldest < now - LOOKBACK:
                break
            time.sleep(_PAUSE_S)
        return found

    def standings(self, tournament_id: str) -> list[dict]:
        return self._get(f"/tournaments/{tournament_id}/standings")


def load_meta(conn: Connection, client: LimitlessClient, now: datetime | None = None) -> int:
    """Charge les tournois pas encore vus. Chaque tournoi est commité à part
    (un échec réseau au milieu ne fait pas refaire les précédents). Retourne
    le nombre de tournois chargés."""
    now = now or datetime.now(UTC)
    known = {r[0] for r in conn.execute("SELECT tournament_id FROM raw.meta_tournament")}
    loaded = 0
    for t in client.recent_tournaments(now):
        # known est aussi complété au fil de l'eau : un tournoi peut revenir
        # sur deux pages si la liste bouge pendant la pagination.
        if t["id"] in known:
            continue
        known.add(t["id"])
        time.sleep(_PAUSE_S)
        decklists, usage = top_cut_usage(client.standings(t["id"]))
        played_on: date = datetime.fromisoformat(t["date"].replace("Z", "+00:00")).date()
        with conn.cursor() as cur:
            # decklists = 0 (decklists masquées) : le tournoi est quand même
            # enregistré, pour ne pas relire ses classements à chaque run.
            cur.execute(
                "INSERT INTO raw.meta_tournament "
                "(tournament_id, name, played_on, format, players, decklists) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (t["id"], t["name"], played_on, t["format"], t["players"], decklists),
            )
            cur.executemany(
                "INSERT INTO raw.meta_card_usage (tournament_id, card_name, decks) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                [(t["id"], name, n) for name, n in usage.items()],
            )
        conn.commit()
        loaded += 1
    log.info("Méta Limitless : %d nouveaux tournois", loaded)
    return loaded
