"""Domain objects shared by the catalogue, connectors, and bot."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
import unicodedata


def normalise_text(value: str) -> str:
    """Return a punctuation-insensitive form suitable for user search."""
    value = unicodedata.normalize("NFKC", value).casefold()
    value = "".join(char if char.isalnum() else " " for char in value)
    return " ".join(value.split())


def normalise_set_code(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value).upper()


def normalise_collector_number(value: str) -> str:
    cleaned = re.sub(r"\s+", "", value).upper()
    return cleaned.zfill(3) if cleaned.isdigit() else cleaned


class Finish(StrEnum):
    """An explicitly reported card finish, independent from rarity."""

    UNKNOWN = "unknown"
    STANDARD = "standard"
    HOLO = "holo"


def normalise_finish(value: Finish | str | None) -> Finish:
    """Return a conservative normalised finish for retailer or official text.

    A missing annotation remains unknown. In particular, rarity alone is not
    evidence that a card is foil: some promos and rarities have both standard
    and holographic treatments.
    """
    if isinstance(value, Finish):
        return value
    text = unicodedata.normalize("NFKC", value or "").casefold()
    compact = re.sub(r"\s+", "", text)
    if compact in {"h仕様", "ホロ", "ホロ仕様", "箔押し"}:
        return Finish.HOLO
    if compact in {"ノーマル", "ノーマル仕様", "通常", "通常仕様"}:
        return Finish.STANDARD
    try:
        return Finish(compact)
    except ValueError:
        return Finish.UNKNOWN


_FINISH_SUFFIX = re.compile(
    r"\s*[（(]\s*(?P<annotation>[HＨ]\s*仕様|ホロ仕様|ホロ|箔押し|ノーマル仕様|通常仕様)\s*[）)]\s*$",
    re.IGNORECASE,
)
_FINISH_ANNOTATION = re.compile(
    r"(?P<annotation>[HＨ]\s*仕様|ホロ仕様|ホロ|箔押し|ノーマル仕様|通常仕様)",
    re.IGNORECASE,
)


def finish_from_text(value: str) -> tuple[Finish, str | None]:
    """Find a declared finish annotation in a seller's unstructured title."""
    matched = _FINISH_ANNOTATION.search(value)
    if not matched:
        return Finish.UNKNOWN, None
    raw_finish = matched.group("annotation").strip()
    return normalise_finish(raw_finish), raw_finish


def split_finish_annotation(name: str) -> tuple[str, Finish, str | None]:
    """Split a recognised trailing finish annotation from a card title.

    For example, ``焔の巫女 シンディ(H仕様)`` keeps the card name separate
    from the raw ``H仕様`` annotation while reporting :attr:`Finish.HOLO`.
    Other parenthesised parts of a title are left intact.
    """
    trimmed = name.strip()
    matched = _FINISH_SUFFIX.search(trimmed)
    if not matched:
        return trimmed, Finish.UNKNOWN, None
    raw_finish = matched.group("annotation").strip()
    finish = normalise_finish(raw_finish)
    if finish is Finish.UNKNOWN:
        return trimmed, finish, None
    return trimmed[: matched.start()].strip(), finish, raw_finish


@dataclass(frozen=True, slots=True)
class CardPrint:
    """A specific printed card version, identified independently of its name."""

    set_code: str
    collector_number: str
    rarity: str
    english_name: str
    japanese_name: str | None = None
    aliases: tuple[str, ...] = ()
    source: str = "manual"
    source_url: str | None = None
    id: int | None = None
    finish: Finish | str = Finish.UNKNOWN
    finish_raw: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "set_code", normalise_set_code(self.set_code))
        object.__setattr__(self, "collector_number", normalise_collector_number(self.collector_number))
        object.__setattr__(self, "rarity", self.rarity.strip().upper())
        object.__setattr__(self, "english_name", self.english_name.strip())
        object.__setattr__(self, "japanese_name", self.japanese_name.strip() if self.japanese_name else None)
        object.__setattr__(self, "aliases", tuple(alias.strip() for alias in self.aliases if alias.strip()))
        raw_finish = self.finish_raw.strip() if self.finish_raw else None
        finish = normalise_finish(self.finish)
        if finish is Finish.UNKNOWN and raw_finish:
            finish = normalise_finish(raw_finish)
        object.__setattr__(self, "finish", finish)
        object.__setattr__(self, "finish_raw", raw_finish)

    @property
    def print_key(self) -> str:
        suffix = f":{self.rarity}" if self.rarity else ""
        return f"{self.set_code}/{self.collector_number}{suffix}"

    @property
    def display_code(self) -> str:
        base = f"{self.set_code}/{self.collector_number}"
        labels = [label for label in (self.rarity, self.finish_label) if label]
        return f"{base} · " + " · ".join(labels) if labels else base

    @property
    def finish_label(self) -> str:
        if self.finish_raw:
            return self.finish_raw
        if self.finish is Finish.HOLO:
            return "Holo"
        if self.finish is Finish.STANDARD:
            return "Standard"
        return ""

    @property
    def search_terms(self) -> tuple[str, ...]:
        return (self.english_name, *self.aliases)


@dataclass(frozen=True, slots=True)
class JapaneseCardPrint:
    """An official Japanese printing, kept even before an English name exists."""

    set_code: str
    collector_number: str
    rarity: str
    japanese_name: str
    source_url: str
    id: int | None = None
    finish: Finish | str = Finish.UNKNOWN
    finish_raw: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "set_code", normalise_set_code(self.set_code))
        object.__setattr__(self, "collector_number", normalise_collector_number(self.collector_number))
        object.__setattr__(self, "rarity", self.rarity.strip().upper())
        object.__setattr__(self, "japanese_name", self.japanese_name.strip())
        raw_finish = self.finish_raw.strip() if self.finish_raw else None
        finish = normalise_finish(self.finish)
        if finish is Finish.UNKNOWN and raw_finish:
            finish = normalise_finish(raw_finish)
        object.__setattr__(self, "finish", finish)
        object.__setattr__(self, "finish_raw", raw_finish)


@dataclass(frozen=True, slots=True)
class PromoCatalogueEntry:
    """A retailer's verified location for one Japanese promo printing.

    It deliberately contains no English card name. Retailer discovery and
    canonical Japanese-card identity are separate from the later name-mapping
    review process.
    """

    store_id: str
    page_slug: str
    set_code: str
    collector_number: str
    japanese_name: str
    listing_url: str
    source_page_url: str
    product_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "store_id", self.store_id.strip().lower())
        object.__setattr__(self, "page_slug", self.page_slug.strip().lower())
        object.__setattr__(self, "set_code", normalise_set_code(self.set_code))
        object.__setattr__(self, "collector_number", normalise_collector_number(self.collector_number))
        object.__setattr__(self, "japanese_name", self.japanese_name.strip())
        object.__setattr__(self, "listing_url", self.listing_url.strip())
        object.__setattr__(self, "source_page_url", self.source_page_url.strip())
        object.__setattr__(self, "product_id", self.product_id.strip() if self.product_id else None)


@dataclass(frozen=True, slots=True)
class EnglishNameMapping:
    """A reviewed English name for an official Japanese card print."""

    set_code: str
    collector_number: str
    rarity: str
    english_name: str
    source: str
    source_url: str
    aliases: tuple[str, ...] = ()
    status: str = "provisional"

    def __post_init__(self) -> None:
        object.__setattr__(self, "set_code", normalise_set_code(self.set_code))
        object.__setattr__(self, "collector_number", normalise_collector_number(self.collector_number))
        object.__setattr__(self, "rarity", self.rarity.strip().upper())
        object.__setattr__(self, "english_name", self.english_name.strip())
        object.__setattr__(self, "aliases", tuple(alias.strip() for alias in self.aliases if alias.strip()))
        object.__setattr__(self, "status", self.status.strip().lower())


class Availability(StrEnum):
    IN_STOCK = "in_stock"
    SOLD_OUT = "sold_out"
    UNKNOWN = "unknown"


class MatchConfidence(StrEnum):
    EXACT_PRINT = "exact_print"
    EXACT_JAPANESE_NAME = "exact_japanese_name"
    EXACT_ENGLISH_NAME = "exact_english_name"
    UNMATCHED = "unmatched"


@dataclass(frozen=True, slots=True)
class StoreOffer:
    store_id: str
    store_name: str
    raw_name: str
    price_yen: int | None
    price_display: str
    availability: Availability
    listing_url: str | None
    match_confidence: MatchConfidence
    condition: str | None = None
    direction: str = "retail"
    stock_count: int | None = None
    finish: Finish | str = Finish.UNKNOWN
    finish_raw: str | None = None

    def __post_init__(self) -> None:
        raw_finish = self.finish_raw.strip() if self.finish_raw else None
        finish = normalise_finish(self.finish)
        if finish is Finish.UNKNOWN and raw_finish:
            finish = normalise_finish(raw_finish)
        object.__setattr__(self, "finish", finish)
        object.__setattr__(self, "finish_raw", raw_finish)


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    card: CardPrint
    offers: tuple[StoreOffer, ...]
    no_active_listing_stores: tuple[str, ...] = ()
    unavailable_stores: tuple[str, ...] = ()
    failed_stores: tuple[str, ...] = ()
    store_timings: tuple["StoreCheckTiming", ...] = ()
    duration_ms: int = 0
    cached: bool = False


@dataclass(frozen=True, slots=True)
class StoreCheckTiming:
    """One connector's outcome and wall-clock time in a comparison."""

    store_id: str
    store_name: str
    outcome: str
    elapsed_ms: int


@dataclass(frozen=True, slots=True)
class FamilyOffer:
    """One exact-print store offer in a verified card-family comparison."""

    card: CardPrint
    offer: StoreOffer


@dataclass(frozen=True, slots=True)
class CardFamilyComparisonResult:
    """Aggregated offers for Japanese reprints of one verified card identity."""

    selected_card: CardPrint
    printings: tuple[CardPrint, ...]
    offers: tuple[FamilyOffer, ...]
