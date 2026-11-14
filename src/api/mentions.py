# Mentions des Pokémon dans les posts Reddit et les articles de presse
# (prod.v_news_post) : combien de posts citent chaque Pokémon, jour par jour.
# Sert le « buzz » du board : ce dont la communauté parle, semaine contre
# semaine, et le rattachement de ce buzz aux cartes possédées et aux signaux.
#
# On compte par espèce (« Charizard »), pas par carte : un post parle de
# « Mega Charizard X ex » ou de « zard », rarement d'un numéro de set. Le nom
# d'espèce d'une carte s'obtient en retirant les suffixes (ex, GX, V, δ...) et
# les préfixes (Mega, Dark, Shining, « Ethan's »...) de son nom.
#
# La liste des Pokémon (data/pokemon_names.txt) vient des cartes de supertype
# « Pokémon » du dernier catalogue pokemontcg.io (raw.card_prices) : sans elle,
# des Dresseurs au nom courant (« Will », « Switch », « Penny ») seraient
# comptés à chaque phrase. Les cartes plus récentes s'y ajoutent quand leur nom
# porte un suffixe de Pokémon (ex, V...).
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

_NAMES_FILE = Path(__file__).with_name("data") / "pokemon_names.txt"

_SUFFIX = re.compile(
    r"\s+(ex|EX|GX|V|VMAX|VSTAR|V-UNION|δ|☆|★|LV\.X|BREAK|Prime|LEGEND|E4|C|G|GL|FB|4|X|Y"
    r"|Star|Lv\.\s*\d+)$"
)
_POSSESSIVE = re.compile(r"^.+?'s\s+")
_POKEMON_SUFFIX = re.compile(r"\s(ex|EX|GX|V|VMAX|VSTAR)$")
# Espèces qui sont aussi des mots anglais courants : comptées seulement quand
# le mot est écrit avec une majuscule.
_AMBIGUOUS = {"ditto", "gloom", "persian", "golem", "electrode", "klink", "mime"}
_TOKEN = re.compile(r"[^\w'.♀♂-]+")
MAX_WORDS = 3


def _strip(name: str) -> str:
    previous = None
    while previous != name:
        previous = name
        name = _SUFFIX.sub("", name).strip()
    return name


def _tokens(text: str) -> list[str]:
    out = []
    for raw in _TOKEN.split(text.replace("’", "'")):
        tok = raw.strip(".'-")
        if tok.lower().endswith("'s"):
            tok = tok[:-2]
        if tok:
            out.append(tok)
    return out


def _key(name: str) -> tuple[str, ...]:
    return tuple(t.lower() for t in _tokens(name))


class SpeciesIndex:
    """Associe un nom de carte à son espèce, et un texte aux espèces citées."""

    def __init__(self, pokemon_names: set[str]) -> None:
        stripped = {_strip(_POSSESSIVE.sub("", _strip(n))) for n in pokemon_names}
        self._known = stripped
        self.species: set[str] = set()
        for n in pokemon_names:
            core = self.core(n)
            if core and len(core) >= 3:
                self.species.add(core)
        self._by_key = {_key(s): s for s in self.species}

    def core(self, name: str) -> str:
        """« Mega Charizard X ex » -> « Charizard », « Ethan's Ho-Oh ex » -> « Ho-Oh »."""
        s = _strip(_POSSESSIVE.sub("", _strip(name)))
        words = s.split()
        # Retire les mots de tête tant que le reste est lui-même un Pokémon
        # connu : « Galarian Mr. Mime » -> « Mr. Mime », mais « Tapu Koko » reste.
        while len(words) > 1 and " ".join(words[1:]) in self._known:
            words = words[1:]
        return " ".join(words)

    def species_of(self, card_name: str) -> str | None:
        core = self.core(card_name)
        return core if core in self.species else None

    def find(self, text: str) -> set[str]:
        toks = _tokens(text)
        low = [t.lower() for t in toks]
        found = set()
        for i in range(len(toks)):
            for n in range(1, MAX_WORDS + 1):
                if i + n > len(toks):
                    break
                sp = self._by_key.get(tuple(low[i : i + n]))
                if sp is None:
                    continue
                if n == 1 and low[i] in _AMBIGUOUS and not toks[i][0].isupper():
                    continue
                found.add(sp)
        return found


@lru_cache(maxsize=1)
def _base_names() -> frozenset[str]:
    return frozenset(
        line.strip() for line in _NAMES_FILE.read_text(encoding="utf-8").splitlines() if line
    )


def build_index(catalog_names: list[str]) -> SpeciesIndex:
    extra = {n for n in catalog_names if _POKEMON_SUFFIX.search(n)}
    return SpeciesIndex(set(_base_names()) | extra)


def count_mentions(posts: list[dict], index: SpeciesIndex, today: date, days: int = 14) -> dict:
    """Par espèce : posts par jour sur `days` jours (le dernier = aujourd'hui),
    total des 7 derniers jours et des 7 précédents, sources, ton (hausse /
    baisse selon le LLM) et les trois posts les plus récents."""
    start = today - timedelta(days=days - 1)
    stats: dict[str, dict] = defaultdict(
        lambda: {"daily": [0] * days, "sources": Counter(), "tone": Counter(), "posts": []}
    )
    for p in sorted(posts, key=lambda p: p["published_on"], reverse=True):
        d = p["published_on"]
        if d < start or d > today:
            continue
        for sp in index.find(f"{p['title']} {p['summary'] or ''}"):
            s = stats[sp]
            s["daily"][(d - start).days] += 1
            if d > today - timedelta(days=7):
                s["sources"]["reddit" if p["source"] == "reddit" else "presse"] += 1
                if p.get("direction") in ("hausse", "baisse"):
                    s["tone"][p["direction"]] += 1
            if len(s["posts"]) < 3:
                s["posts"].append(
                    {
                        "title": p["title"],
                        "url": p["url"],
                        "source": p["source"],
                        "published_on": d,
                    }
                )
    out = {}
    for sp, s in stats.items():
        week = sum(s["daily"][-7:])
        out[sp] = {
            "species": sp,
            "daily": s["daily"],
            "week": week,
            "prev_week": sum(s["daily"][-14:-7]) if days >= 14 else None,
            "sources": dict(s["sources"]),
            "tone": dict(s["tone"]),
            "posts": s["posts"],
        }
    return out
