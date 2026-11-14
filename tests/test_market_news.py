# Tests de la partie 2 des signaux d'opportunité : collecte des news
# (src/extract/news_feeds.py), classement LLM (src/signals/news_llm.py, Ollama
# mocké) et méta Limitless (src/extract/limitless_meta.py, API mockée). Les
# chargements tournent contre une vraie base, comme tests/test_opportunities.py.
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest
import requests
import responses

from src.common.config import OllamaConfig, load_db_config
from src.common.db import get_connection
from src.extract import limitless_meta
from src.extract.limitless_meta import load_meta, top_cut_usage
from src.extract.news_feeds import NewsItem, parse_feed, passes_prefilter, store_news
from src.signals.news_llm import (
    NewsAnalysis,
    OllamaClient,
    OllamaUnavailable,
    analyze_pending_news,
)

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Evolving Skies reprint announced - PokeBeach</title>
<link>https://news.example/1</link><guid>g-1</guid>
<pubDate>Thu, 05 Nov 2026 08:00:00 GMT</pubDate>
<description>&lt;a href="x"&gt;Evolving Skies&lt;/a&gt; is coming back</description></item>
<item><title>No date</title><link>https://news.example/2</link></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>t3_abc</id><title>Look at my pulls!</title>
<link href="https://reddit.example/abc"/><published>2026-11-06T01:00:00+00:00</published>
<content type="html">&lt;p&gt;So happy&lt;/p&gt;</content></entry>
</feed>"""


def _item(**overrides) -> NewsItem:
    base = dict(
        source="google_news",
        external_id="g-1",
        url="https://news.example/1",
        title="Evolving Skies reprint announced",
        summary="",
        published_at=NOW - timedelta(days=1),
    )
    return NewsItem(**{**base, **overrides})


# --- Flux ---------------------------------------------------------------------


def test_parse_rss_keeps_complete_items_and_strips_html():
    (item,) = parse_feed("google_news", RSS)
    assert item.external_id == "g-1"
    assert item.summary == "Evolving Skies is coming back"
    assert item.published_at == datetime(2026, 11, 5, 8, 0, tzinfo=UTC)


def test_parse_atom():
    (item,) = parse_feed("reddit", ATOM)
    assert (item.external_id, item.url, item.summary) == (
        "t3_abc",
        "https://reddit.example/abc",
        "So happy",
    )


def test_prefilter_keeps_market_news_and_known_sets():
    assert passes_prefilter(_item(title="Charizard prices spike"), frozenset())
    assert passes_prefilter(_item(title="Back to evolving skies"), frozenset({"evolving skies"}))
    assert not passes_prefilter(_item(title="Look at my pulls!"), frozenset())


# --- LLM ----------------------------------------------------------------------

OLLAMA = OllamaConfig(url="http://ollama.test:11434", model="qwen3.5:4b")


def _llm_answer(**overrides) -> dict:
    answer = {
        "pertinent": True,
        "event_type": "reimpression",
        "direction": "baisse",
        "confidence": 0.8,
        "sets": ["Evolving Skies"],
        "cards": [],
        "resume": "Réimpression d'Evolving Skies.",
    }
    return {**answer, **overrides}


@responses.activate
def test_ollama_client_sends_schema_and_parses_response():
    responses.post(
        "http://ollama.test:11434/api/generate", json={"response": json.dumps(_llm_answer())}
    )
    assert OllamaClient(OLLAMA).analyze("google_news", "t", "s")["direction"] == "baisse"
    body = json.loads(responses.calls[0].request.body)
    assert body["think"] is False and body["format"]["required"]


@responses.activate
def test_ollama_unreachable_is_distinguished():
    responses.post(
        "http://ollama.test:11434/api/generate", body=requests.ConnectionError("refused")
    )
    with pytest.raises(OllamaUnavailable):
        OllamaClient(OLLAMA).analyze("s", "t", "")


def test_low_confidence_or_neutral_is_not_a_signal():
    assert NewsAnalysis.from_llm(_llm_answer()).is_signal
    assert not NewsAnalysis.from_llm(_llm_answer(confidence=0.2)).is_signal
    assert not NewsAnalysis.from_llm(_llm_answer(direction="neutre")).is_signal
    assert not NewsAnalysis.from_llm(_llm_answer(pertinent=False)).is_signal


# --- Méta ---------------------------------------------------------------------


def _standing(placing, names):
    deck = {"pokemon": [{"name": n, "count": 2} for n in names], "trainer": [], "energy": []}
    return {"placing": placing, "decklist": deck}


def test_top_cut_usage_counts_each_card_once_per_deck():
    standings = [
        _standing(1, ["Gardevoir ex", "Ralts", "Ralts"]),
        _standing(2, ["Gardevoir ex"]),
        _standing(9, ["Charizard ex"]),  # hors top 8
        {"placing": 3, "decklist": None},  # decklist masquée
    ]
    decklists, usage = top_cut_usage(standings)
    assert decklists == 2
    assert usage == Counter({"Gardevoir ex": 2, "Ralts": 1})


# --- Intégration --------------------------------------------------------------


def _admin_dsn() -> str:
    return (
        f"host={os.environ['POSTGRES_HOST']} port={os.environ['POSTGRES_PORT']} "
        f"dbname={os.environ['POSTGRES_DB']} user={os.environ['POSTGRES_ADMIN_USER']} "
        f"password={os.environ['POSTGRES_ADMIN_PASSWORD']}"
    )


@pytest.fixture
def db_connection():
    with psycopg.connect(_admin_dsn()) as admin_conn:
        admin_conn.execute(
            "TRUNCATE TABLE prod.fact_news_event, raw.news_item, raw.meta_card_usage, "
            "raw.meta_tournament, prod.fact_opportunity, prod.dim_owned_card, "
            "prod.fact_price_history, prod.dim_card RESTART IDENTITY CASCADE;"
        )
        admin_conn.cursor().executemany(
            "INSERT INTO prod.dim_card (card_id, name, set_id, set_name, series, rarity) "
            "VALUES (%s, %s, 'swsh7', 'Evolving Skies', 'Sword & Shield', 'Rare Secret')",
            [("swsh7-215", "Umbreon VMAX"), ("swsh7-218", "Rayquaza VMAX")],
        )
        admin_conn.commit()
    with get_connection(load_db_config()) as conn:
        yield conn


def test_store_news_is_idempotent_and_skips_old_or_unfiltered(db_connection):
    items = [
        _item(),
        _item(external_id="g-2", title="Look at my pulls!"),
        _item(external_id="g-3", title="Old price spike", published_at=NOW - timedelta(days=30)),
    ]
    assert store_news(db_connection, items, now=NOW) == 2
    assert store_news(db_connection, items, now=NOW) == 0
    statuses = db_connection.execute(
        "SELECT external_id, status FROM raw.news_item ORDER BY external_id"
    ).fetchall()
    assert statuses == [("g-1", "pending"), ("g-2", "skipped")]


class FakeLLM:
    model = "fake"

    def __init__(self, answers):
        self.answers = list(answers)

    def analyze(self, source, title, summary):
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_analyze_pending_news_records_events_and_retries_errors(db_connection):
    store_news(db_connection, [_item(), _item(external_id="g-2", title="Price crash")], now=NOW)
    db_connection.commit()
    # g-1 et g-2 publiées à la même heure : ordre d'analyse indifférent, les
    # deux réponses sont équivalentes sauf l'erreur.
    llm = FakeLLM([_llm_answer(), ValueError("not json")])
    counts = analyze_pending_news(db_connection, llm, now=NOW)
    assert counts == {"analyzed": 1, "errors": 1, "events": 2, "rematched": 2}
    events = db_connection.execute(
        "SELECT card_id, direction, weight FROM prod.fact_news_event ORDER BY card_id"
    ).fetchall()
    assert [(c, d, float(w)) for c, d, w in events] == [
        ("swsh7-215", "baisse", 0.3),
        ("swsh7-218", "baisse", 0.3),
    ]
    # La news en erreur est reprise au run suivant.
    counts = analyze_pending_news(db_connection, FakeLLM([_llm_answer(direction="neutre")]), NOW)
    assert counts == {"analyzed": 1, "errors": 0, "events": 0, "rematched": 2}


def test_news_about_a_set_not_yet_in_catalog_is_matched_later(db_connection):
    store_news(db_connection, [_item(title="Delta Reign reprint")], now=NOW)
    db_connection.commit()
    answer = _llm_answer(sets=["Delta Reign"])
    assert analyze_pending_news(db_connection, FakeLLM([answer]), now=NOW)["events"] == 0
    # Le rafraîchissement du catalogue ajoute le set...
    with psycopg.connect(_admin_dsn()) as admin_conn:
        admin_conn.execute(
            "INSERT INTO prod.dim_card (card_id, name, set_id, set_name, series, rarity) "
            "VALUES ('me4-200', 'Mega Gengar ex', 'me4', 'Delta Reign', 'Mega Evolution', "
            "'Special Illustration Rare')"
        )
    # ... et le run suivant rattache la news sans rappeler le LLM.
    counts = analyze_pending_news(db_connection, FakeLLM([]), now=NOW)
    assert counts["rematched"] == 1
    assert db_connection.execute("SELECT card_id FROM prod.fact_news_event").fetchall() == [
        ("me4-200",)
    ]


def test_analyze_stops_when_ollama_is_unreachable(db_connection):
    store_news(db_connection, [_item()], now=NOW)
    db_connection.commit()
    with pytest.raises(OllamaUnavailable):
        analyze_pending_news(db_connection, FakeLLM([OllamaUnavailable("down")]))
    status = db_connection.execute("SELECT status, attempts FROM raw.news_item").fetchone()
    assert status == ("pending", 0)


class FakeLimitless:
    def __init__(self):
        self.standings_calls = 0

    def recent_tournaments(self, now):
        return [
            {
                "id": "t1",
                "name": "Big Cup",
                "date": "2026-11-04T18:00:00.000Z",
                "format": "STANDARD",
                "players": 128,
            }
        ]

    def standings(self, tournament_id):
        self.standings_calls += 1
        return [_standing(1, ["Gardevoir ex"]), _standing(2, ["Gardevoir ex", "Ralts"])]


def test_load_meta_reads_each_tournament_once(db_connection, monkeypatch):
    monkeypatch.setattr(limitless_meta, "_PAUSE_S", 0)
    client = FakeLimitless()
    assert load_meta(db_connection, client, now=NOW) == 1
    assert load_meta(db_connection, client, now=NOW) == 0
    assert client.standings_calls == 1
    rows = db_connection.execute(
        "SELECT t.played_on, t.decklists, u.card_name, u.decks FROM raw.meta_card_usage u "
        "JOIN raw.meta_tournament t USING (tournament_id) ORDER BY u.card_name"
    ).fetchall()
    assert rows == [(date(2026, 11, 4), 2, "Gardevoir ex", 2), (date(2026, 11, 4), 2, "Ralts", 1)]
