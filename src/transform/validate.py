# Ce module contient la logique de VALIDATION et de NETTOYAGE d'une carte
# brute (le payload JSON tel que renvoyé par pokemontcg.io, stocké tel quel
# dans raw.card_prices.payload par le Mois 1). Son unique fonction publique,
# validate_and_clean(), est une fonction PURE : elle prend un dict en entrée
# et renvoie un ValidationResult, sans jamais ouvrir de connexion ni toucher
# la base de données. C'est un choix de conception délibéré, pas un oubli :
#   - Testabilité : on peut couvrir toutes les règles métier (set manquant,
#     prix absent, prix négatif...) par de simples tests unitaires en
#     mémoire (voir tests/test_transform.py), sans conteneur Postgres, sans
#     fixture de connexion, en quelques millisecondes.
#   - Séparation des responsabilités : "décider si une carte est valide" et
#     "écrire en base" sont deux problèmes différents. La couche DB
#     (src/load/staging_loader.py) et l'orchestration (src/transform/clean.py)
#     restent minces et ne contiennent aucune règle métier de validation.
from __future__ import annotations

from dataclasses import dataclass


# @dataclass(frozen=True) : même pattern que DatabaseConfig dans
# src/common/config.py. frozen=True rend l'instance immuable une fois créée :
# une fois qu'une carte a été nettoyée et validée, rien dans le pipeline ne
# doit pouvoir modifier ses champs par erreur en cours de route (ex: un bug
# dans load_staging() qui muterait accidentellement l'objet avant l'insertion
# SQL). __init__/__repr__/__eq__ sont générés automatiquement à partir des
# champs déclarés ci-dessous.
@dataclass(frozen=True)
class CleanedCard:
    """Représente une carte APRÈS validation et nettoyage : ses champs sont
    des types Postgres directement utilisables par staging_loader (str, float
    ou None), plus aucune trace de la structure JSON imbriquée d'origine
    (set.id devient set_id, cardmarket.prices.averageSellPrice devient
    average_sell_price, etc.). C'est la "forme staging" de la donnée."""

    card_id: str
    name: str
    set_id: str
    set_name: str
    # rarity : optionnel côté source (certaines cartes promo n'ont pas de
    # rareté renseignée), donc `str | None` plutôt que `str`. Contrairement
    # aux prix, l'absence de rarity n'est pas assez grave pour rejeter toute
    # la carte : elle reste utile en staging (nom, set, prix) même sans
    # rarity, donc ce champ ne fait PAS partie des règles de rejet ci-dessous.
    rarity: str | None
    # Les 3 prix sont `float | None` individuellement : la règle de rejet
    # porte sur le TRIO (voir plus bas, "aucun prix cardmarket disponible"),
    # pas sur chaque champ séparément. Une carte peut très bien n'avoir que
    # trendPrice sans averageSellPrice ni lowPrice et rester valide.
    average_sell_price: float | None
    trend_price: float | None
    low_price: float | None


@dataclass(frozen=True)
class ValidationResult:
    """Résultat de validate_and_clean() : soit `cleaned` est renseigné (carte
    acceptée) et `rejection_reason` vaut None, soit c'est l'inverse (carte
    rejetée). Les deux ne sont jamais renseignés (ni jamais vides) en même
    temps : c'est un "either/or" représenté par deux champs optionnels plutôt
    que par une exception, pour que l'appelant (clean_raw_to_staging) puisse
    router chaque carte sans bloc try/except, juste en testant is_valid."""

    cleaned: CleanedCard | None
    rejection_reason: str | None

    @property
    def is_valid(self) -> bool:
        # is_valid est dérivé de `cleaned` plutôt que stocké comme un champ
        # séparé : impossible d'avoir un état incohérent du type
        # (is_valid=True, cleaned=None) puisque la propriété EST la source de
        # vérité. @property permet d'écrire result.is_valid comme un simple
        # attribut (pas result.is_valid()), ce qui lit naturellement dans les
        # tests et dans clean_raw_to_staging.
        return self.cleaned is not None


def validate_and_clean(payload: dict) -> ValidationResult:
    """Valide puis nettoie un payload brut de carte (dict JSON tel que stocké
    dans raw.card_prices.payload). Applique les règles métier dans un ordre
    précis (identité -> set -> prix) et s'arrête à la première violation :
    inutile de vérifier les prix si la carte n'a même pas de nom, la raison
    de rejet la plus utile pour le debug est la première anomalie rencontrée."""
    # --- Règle 1 : identité minimale de la carte ---
    # card_id sert de clé métier partout en aval (UNIQUE constraint en
    # staging, jointures futures vers le star schema) : sans lui, la carte
    # est inexploitable. "not card_id" rejette aussi bien None qu'une chaîne
    # vide "", deux façons dont l'API pourrait signaler une absence.
    card_id = payload.get("id")
    name = payload.get("name")
    if not card_id or not name:
        return ValidationResult(cleaned=None, rejection_reason="card_id ou name manquant")

    # --- Règle 2 : informations de set ---
    # `payload.get("set") or {}` : gère à la fois le cas où la clé "set" est
    # absente du dict ET le cas où elle vaut explicitement None (voir le test
    # test_validate_and_clean_rejects_missing_set, qui passe set=None). Dans
    # les deux cas, set_info devient un dict vide, et .get("id")/.get("name")
    # renverront None au lieu de lever une AttributeError sur `None.get(...)`.
    set_info = payload.get("set") or {}
    set_id = set_info.get("id")
    set_name = set_info.get("name")
    if not set_id or not set_name:
        return ValidationResult(cleaned=None, rejection_reason="informations de set manquantes")

    # --- Règle 3 : au moins un prix cardmarket disponible ---
    # Même logique défensive en chaîne que pour `set` : cardmarket ou
    # cardmarket.prices peuvent être absents ou None selon les cartes (toutes
    # les cartes n'ont pas de cote sur cardmarket, notamment les cartes très
    # récentes ou très obscures).
    cardmarket_prices = (payload.get("cardmarket") or {}).get("prices") or {}
    average_sell_price = cardmarket_prices.get("averageSellPrice")
    trend_price = cardmarket_prices.get("trendPrice")
    low_price = cardmarket_prices.get("lowPrice")

    # Si les 3 prix sont absents, la carte n'apporte rien à un pipeline dont
    # le but est justement de SUIVRE DES PRIX : on la rejette explicitement
    # plutôt que de l'insérer en staging avec average_sell_price/trend_price/
    # low_price tous NULL. Insérer quand même produirait une ligne "muette"
    # en staging qui pollue silencieusement les agrégats du prod (moyennes,
    # tendances) sans qu'aucune alerte ne signale le problème de données
    # source. En la routant vers card_prices_quarantine avec une raison
    # explicite, le problème reste visible et traçable pour un audit manuel.
    if average_sell_price is None and trend_price is None and low_price is None:
        return ValidationResult(cleaned=None, rejection_reason="aucun prix cardmarket disponible")

    # --- Règle 4 : aucun prix ne doit être négatif ---
    # On boucle sur les 3 prix nommés (label utilisé dans le message
    # d'erreur pour savoir PRÉCISÉMENT lequel est fautif) plutôt que de
    # dupliquer le même `if` trois fois. `value is not None and value < 0` :
    # on ne teste la négativité QUE si le prix est renseigné (un prix absent
    # n'est pas "négatif", c'est juste absent ; ce cas est déjà couvert par
    # la règle 3 si les 3 sont absents, et toléré individuellement sinon).
    # Une valeur négative signale une anomalie de la source (bug API, donnée
    # corrompue) car une carte ne peut pas avoir de valeur marchande < 0.
    for label, value in [
        ("averageSellPrice", average_sell_price),
        ("trendPrice", trend_price),
        ("lowPrice", low_price),
    ]:
        if value is not None and value < 0:
            return ValidationResult(
                cleaned=None, rejection_reason=f"prix négatif ({label}={value})"
            )

    # --- Carte valide : construction du CleanedCard ---
    # .strip() sur name/set_name retire les espaces superflus en début/fin
    # (parfois présents dans des exports API) : le nettoyage inclut donc
    # aussi bien des règles de rejet (ci-dessus) que des normalisations
    # discrètes qui ne rejettent rien mais améliorent la qualité des données
    # stockées en staging.
    return ValidationResult(
        cleaned=CleanedCard(
            card_id=card_id,
            name=name.strip(),
            set_id=set_id,
            set_name=set_name.strip(),
            # rarity n'est soumis à aucune règle de validation (voir le
            # commentaire sur le champ dans CleanedCard) : on le transmet tel
            # quel, y compris s'il vaut None.
            rarity=payload.get("rarity"),
            average_sell_price=average_sell_price,
            trend_price=trend_price,
            low_price=low_price,
        ),
        rejection_reason=None,
    )
