# Collecte des news Pokémon TCG (ajouté le 2026-11-06) pour les signaux
# d'opportunité : flux RSS / Atom gratuits, lus deux fois par jour par le DAG
# card_market_news, et rangés dans raw.news_item. Le classement (hausse /
# baisse, cartes touchées) est fait ensuite par le LLM local, voir
# src/signals/news_llm.py.
#
# Sources : PokeBeach et Bulbanews refusent les accès automatisés (403) et
# pokemon.com n'a pas de flux : Google News les agrège. Reddit sert de
# baromètre de la communauté (r/PokemonTCG, r/PokeInvesting,
# r/pkmntcgcollections, en un seul flux).
from __future__ import annotations

import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import requests
from psycopg import Connection

log = logging.getLogger(__name__)

# (source, url). when:7d : sans lui, Google News trie par pertinence et renvoie
# des articles vieux de plusieurs semaines. Les deux recherches Google partagent
# la source "google_news" : un article trouvé par les deux a le même guid, la
# contrainte UNIQUE (source, external_id) le garde une seule fois (sinon le
# LLM le classe deux fois, parfois dans deux sens opposés).
FEEDS: list[tuple[str, str]] = [
    (
        "google_news",
        "https://news.google.com/rss/search?q=%22pokemon+tcg%22+when:7d"
        "&hl=en-US&gl=US&ceid=US:en",
    ),
    (
        "google_news",
        "https://news.google.com/rss/search?q=pokemon+cards+(price+OR+prices+OR+reprint+OR+value)"
        "+when:7d&hl=en-US&gl=US&ceid=US:en",
    ),
    # Un seul flux pour les trois subreddits : Reddit répond 429 dès la
    # deuxième requête non authentifiée, même espacée de 15 s.
    (
        "reddit",
        "https://www.reddit.com/r/PokemonTCG+PokeInvesting+pkmntcgcollections/.rss?limit=75",
    ),
]
_HEADERS = {"User-Agent": "CardPriceTracker/1.0 (personal price tracker)"}
_PAUSE_BETWEEN_FEEDS_S = 5
# Au-delà, une news n'influence plus les signaux (fenêtre de 7 j) : inutile
# de la stocker ni de la faire analyser.
MAX_AGE = timedelta(days=7)
_SUMMARY_MAX_CHARS = 1500

_ATOM = "{http://www.w3.org/2005/Atom}"

# Préfiltre avant le LLM (le CPU est partagé avec second_brain) : une news
# passe si elle parle de marché, de sortie, de compétition... ou cite un set
# connu. Ça écarte surtout les posts Reddit "regardez mes pulls".
MARKET_KEYWORDS = (
    "reprint", "restock", "re-release", "price", "spike", "surge", "skyrocket",
    "crash", "tank", "value", "worth", "invest", "market", "ban", "rotation",
    "regional", "worlds", "championship", "tournament", "meta", "set list",
    "card list", "reveal", "announce", "leak", "secret rare", "illustration rare",
    "alt art", "chase", "pull rate", "release", "sold out", "sell out", "shortage",
    "scalp", "print run",
)  # fmt: skip


@dataclass(frozen=True)
class NewsItem:
    source: str
    external_id: str
    url: str
    title: str
    summary: str
    published_at: datetime


def _text(html_fragment: str | None) -> str:
    # Les résumés RSS sont du HTML : on garde le texte, sans balises.
    without_tags = re.sub(r"<[^>]+>", " ", html_fragment or "")
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()[:_SUMMARY_MAX_CHARS]


def parse_feed(source: str, xml_text: str) -> list[NewsItem]:
    """RSS 2.0 (Google News) ou Atom (Reddit) -> NewsItem. Les entrées
    incomplètes (sans titre, lien ou date) sont ignorées."""
    root = ET.fromstring(xml_text)
    items = []
    for entry in root.iter("item"):
        title, link = entry.findtext("title"), entry.findtext("link")
        pub = entry.findtext("pubDate")
        if not (title and link and pub):
            continue
        items.append(
            NewsItem(
                source=source,
                external_id=entry.findtext("guid") or link,
                url=link,
                title=_text(title),
                summary=_text(entry.findtext("description")),
                published_at=parsedate_to_datetime(pub).astimezone(UTC),
            )
        )
    for entry in root.iter(f"{_ATOM}entry"):
        title = entry.findtext(f"{_ATOM}title")
        link = entry.find(f"{_ATOM}link")
        pub = entry.findtext(f"{_ATOM}published") or entry.findtext(f"{_ATOM}updated")
        if not (title and link is not None and pub):
            continue
        items.append(
            NewsItem(
                source=source,
                external_id=entry.findtext(f"{_ATOM}id") or link.get("href", ""),
                url=link.get("href", ""),
                title=_text(title),
                summary=_text(entry.findtext(f"{_ATOM}content")),
                published_at=datetime.fromisoformat(pub).astimezone(UTC),
            )
        )
    return items


def passes_prefilter(item: NewsItem, set_names: frozenset[str]) -> bool:
    text = f"{item.title} {item.summary}".lower()
    return any(k in text for k in MARKET_KEYWORDS) or any(s in text for s in set_names)


def fetch_all(session: requests.Session | None = None) -> list[NewsItem]:
    """Lit tous les flux. Un flux en erreur (429 Reddit, panne) est sauté
    avec un avertissement ; la tâche n'échoue que si TOUS échouent."""
    session = session or requests.Session()
    items, failures = [], []
    for i, (source, url) in enumerate(FEEDS):
        if i:
            time.sleep(_PAUSE_BETWEEN_FEEDS_S)
        try:
            response = session.get(url, headers=_HEADERS, timeout=30)
            response.raise_for_status()
            items.extend(parse_feed(source, response.text))
        except (requests.RequestException, ET.ParseError) as exc:
            log.warning("Flux %s ignoré : %s", url, exc)
            failures.append(url)
    if len(failures) == len(FEEDS):
        raise RuntimeError(f"Aucun flux de news lisible : {failures}")
    return items


def _known_set_names(conn: Connection) -> frozenset[str]:
    # Noms de sets assez longs pour ne pas matcher n'importe quoi ("Base",
    # "Jungle", "151"...) : le préfiltre n'est qu'une porte d'entrée, le LLM
    # puis src/transform/news_match.py font le vrai rattachement.
    rows = conn.execute(
        "SELECT DISTINCT lower(set_name) FROM prod.dim_card WHERE length(set_name) >= 10"
    ).fetchall()
    return frozenset(r[0] for r in rows)


def store_news(conn: Connection, items: list[NewsItem], now: datetime | None = None) -> int:
    """Insère les news nouvelles (les déjà vues sont ignorées : idempotent).
    Celles qui ne passent pas le préfiltre sont stockées en 'skipped' pour ne
    pas être relues ni analysées. Retourne le nombre de news insérées."""
    now = now or datetime.now(UTC)
    set_names = _known_set_names(conn)
    inserted = 0
    with conn.cursor() as cur:
        for item in items:
            if now - item.published_at > MAX_AGE:
                continue
            cur.execute(
                "INSERT INTO raw.news_item "
                "(source, external_id, url, title, summary, published_at, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (source, external_id) DO NOTHING",
                (
                    item.source,
                    item.external_id,
                    item.url,
                    item.title,
                    item.summary,
                    item.published_at,
                    "pending" if passes_prefilter(item, set_names) else "skipped",
                ),
            )
            inserted += cur.rowcount
    log.info("News : %d lues, %d nouvelles", len(items), inserted)
    return inserted
