"""SQLite-backed card catalogue with fast name and alias lookups."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
import re
import sqlite3
from typing import Iterable

from rapidfuzz import fuzz

from scraperbot.models import (
    CardPrint,
    EnglishNameMapping,
    Finish,
    JapaneseCardPrint,
    PromoCatalogueEntry,
    finish_from_text,
    normalise_collector_number,
    normalise_finish,
    normalise_set_code,
    normalise_text,
)


PROMO_PRINT_SET_CODES = frozenset({"DPR", "CP"})
SPECIAL_SET_CODE_PATTERN = re.compile(r"^(?:D|DZ)SS\d+$")
ENGLISH_REFERENCE_MAPPING_SOURCES = frozenset(
    {"official-english", "official-english-derived", "official-name-match"}
)
SAFE_JAPANESE_NAME_MAPPING_SOURCES = frozenset(
    {"fandom", "fandom-derived", "fandom-name-match", "fandom-cross-print", "fandom-japanese-promo", "promo-review"}
)

# These shared-name utility prints remain in the Japanese master while their
# user-facing search and selection workflow is on hold. Do not add a generic
# English mapping during an ordinary Fandom or repair import.
HELD_UTILITY_PROMO_NAMES = frozenset(
    {
        "エネルギー",
        "エネルギージェネレーター",
        "四精織り成す清浄の盾",
        "ペルソナシールド",
    }
)


def is_region_specific_print_set(set_code: str) -> bool:
    """Whether matching Japanese and English serials are not identity evidence.

    D/DZ promo sequences and D/DZ Special Series products reuse identifiers
    across regions for different releases.  Their Japanese catalogue entry
    must therefore never inherit an English name merely because the printed
    set code and collector number happen to match.
    """
    normalised = normalise_set_code(set_code)
    return normalised in PROMO_PRINT_SET_CODES or bool(SPECIAL_SET_CODE_PATTERN.fullmatch(normalised))


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS card_prints (
    id INTEGER PRIMARY KEY,
    set_code TEXT NOT NULL,
    collector_number TEXT NOT NULL,
    rarity TEXT NOT NULL,
    finish TEXT NOT NULL DEFAULT 'unknown',
    finish_raw TEXT,
    english_name TEXT NOT NULL,
    japanese_name TEXT,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    normalised_name TEXT NOT NULL,
    normalised_aliases TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    source_url TEXT,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(set_code, collector_number, rarity)
);

CREATE INDEX IF NOT EXISTS idx_card_prints_name ON card_prints(normalised_name);
CREATE INDEX IF NOT EXISTS idx_card_prints_rarity ON card_prints(rarity);
CREATE INDEX IF NOT EXISTS idx_card_prints_source ON card_prints(source);

CREATE TABLE IF NOT EXISTS english_print_references (
    id INTEGER PRIMARY KEY,
    set_code TEXT NOT NULL,
    collector_number TEXT NOT NULL,
    rarity TEXT NOT NULL,
    english_name TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL,
    source_url TEXT,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(set_code, collector_number, rarity)
);

CREATE INDEX IF NOT EXISTS idx_english_print_references_name
ON english_print_references(english_name);

CREATE TABLE IF NOT EXISTS japanese_prints (
    id INTEGER PRIMARY KEY,
    set_code TEXT NOT NULL,
    collector_number TEXT NOT NULL,
    rarity TEXT NOT NULL DEFAULT '',
    finish TEXT NOT NULL DEFAULT 'unknown',
    finish_raw TEXT,
    japanese_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(set_code, collector_number)
);

CREATE INDEX IF NOT EXISTS idx_japanese_prints_set ON japanese_prints(set_code);

-- A card identity is deliberately separate from a physical printing.  The
-- Japanese name is the canonical local identity; English names and aliases
-- are search affordances attached to that identity, never alternate prints.
CREATE TABLE IF NOT EXISTS card_identities (
    id INTEGER PRIMARY KEY,
    japanese_name TEXT NOT NULL UNIQUE,
    english_name TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    normalised_name TEXT NOT NULL,
    normalised_aliases TEXT NOT NULL DEFAULT '',
    mapping_source TEXT NOT NULL,
    mapping_source_url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'provisional',
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_card_identities_name ON card_identities(normalised_name);

CREATE TABLE IF NOT EXISTS japanese_print_identity_links (
    japanese_print_id INTEGER PRIMARY KEY REFERENCES japanese_prints(id) ON DELETE CASCADE,
    card_identity_id INTEGER NOT NULL REFERENCES card_identities(id) ON DELETE CASCADE,
    linked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_japanese_print_identity_card
ON japanese_print_identity_links(card_identity_id);

CREATE VIRTUAL TABLE IF NOT EXISTS card_identity_search USING fts5(
    identity_id UNINDEXED,
    english_name,
    japanese_name,
    aliases,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS official_promo_identities (
    collector_number TEXT PRIMARY KEY,
    japanese_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    distribution TEXT NOT NULL,
    available_from TEXT,
    checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS promo_catalogue_entries (
    store_id TEXT NOT NULL,
    page_slug TEXT NOT NULL,
    set_code TEXT NOT NULL,
    collector_number TEXT NOT NULL,
    japanese_name TEXT NOT NULL,
    listing_url TEXT NOT NULL,
    source_page_url TEXT NOT NULL,
    product_id TEXT,
    first_imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (store_id, set_code, collector_number)
);

CREATE INDEX IF NOT EXISTS idx_promo_catalogue_entries_page
ON promo_catalogue_entries(store_id, page_slug);

CREATE TABLE IF NOT EXISTS promo_catalogue_imports (
    store_id TEXT NOT NULL,
    page_slug TEXT NOT NULL,
    source_page_url TEXT NOT NULL,
    entry_count INTEGER NOT NULL,
    completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (store_id, page_slug)
);

CREATE TABLE IF NOT EXISTS japanese_expansion_imports (
    expansion_id INTEGER PRIMARY KEY,
    completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS english_name_mappings (
    japanese_print_id INTEGER PRIMARY KEY REFERENCES japanese_prints(id) ON DELETE CASCADE,
    english_name TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    mapping_source TEXT NOT NULL,
    mapping_source_url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'provisional',
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE VIRTUAL TABLE IF NOT EXISTS card_search USING fts5(
    print_id UNINDEXED,
    english_name,
    japanese_name,
    aliases,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- Legacy equality claims made from matching Japanese/English printed numbers.
-- These records are retained for a future, explicitly reviewed cross-region
-- feature, but are never read by Japanese-card search or identity code.
CREATE TABLE IF NOT EXISTS archived_english_reference_mappings (
    id INTEGER PRIMARY KEY,
    japanese_print_id INTEGER NOT NULL REFERENCES japanese_prints(id) ON DELETE CASCADE,
    english_name TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    mapping_source TEXT NOT NULL,
    mapping_source_url TEXT NOT NULL,
    status TEXT NOT NULL,
    archived_reason TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(japanese_print_id, english_name, mapping_source, mapping_source_url)
);

CREATE INDEX IF NOT EXISTS idx_archived_english_reference_mappings_japanese
ON archived_english_reference_mappings(japanese_print_id);
"""


@dataclass(frozen=True, slots=True)
class MappingImportResult:
    mapped: int
    derived: int
    unmatched: int
    preserved_official: int


class CatalogueRepository:
    """Owns the local card catalogue database.

    The public lookup API accepts only user-facing names. Store connectors use
    the selected :class:`CardPrint` object's hidden print key afterwards.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The local browser serves requests on worker threads. Access remains
        # serialised by the web application, but SQLite must permit that one
        # local read connection to be used by those workers.
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self._migrate_schema()
        self.connection.execute("CREATE INDEX IF NOT EXISTS idx_card_prints_finish ON card_prints(finish)")
        self.connection.execute("CREATE INDEX IF NOT EXISTS idx_japanese_prints_finish ON japanese_prints(finish)")
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def _migrate_schema(self) -> None:
        """Add finish fields without rewriting an existing local catalogue."""
        self._add_column_if_missing("card_prints", "finish", "TEXT NOT NULL DEFAULT 'unknown'")
        self._add_column_if_missing("card_prints", "finish_raw", "TEXT")
        self._add_column_if_missing("japanese_prints", "finish", "TEXT NOT NULL DEFAULT 'unknown'")
        self._add_column_if_missing("japanese_prints", "finish_raw", "TEXT")
        # D-PR is the printed promo rarity. The official PR identity table
        # intentionally omits a rarity column, so retain this stable printed
        # fact in the Japanese master for user rarity filters.
        self.connection.execute(
            "UPDATE japanese_prints SET rarity='PR' WHERE set_code='DPR' AND rarity=''"
        )
        self._migrate_card_identities()

    def _migrate_card_identities(
        self, preferred: dict[str, sqlite3.Row] | None = None
    ) -> None:
        """Build shared identities from previously accepted one-print mappings.

        This is a one-way compatibility migration: legacy per-print mappings
        stay as provenance, while the new link table becomes the source of
        user-facing Japanese search coverage.  A conflicted Japanese name is
        intentionally not migrated until a review resolves it.
        """
        existing = self.connection.execute("SELECT COUNT(*) FROM card_identities").fetchone()[0]
        if existing:
            return
        rows = self.connection.execute(
            """
            SELECT j.set_code, j.collector_number, j.japanese_name, m.english_name, m.aliases_json,
                   m.mapping_source, m.mapping_source_url, m.status, m.imported_at
            FROM japanese_prints AS j
            JOIN english_name_mappings AS m ON m.japanese_print_id = j.id
            WHERE j.japanese_name NOT IN (?, ?, ?, ?)
            ORDER BY j.japanese_name, m.imported_at DESC
            """,
            tuple(HELD_UTILITY_PROMO_NAMES),
        ).fetchall()
        candidates: dict[str, dict[str, sqlite3.Row]] = {}
        for row in rows:
            if row['mapping_source'] not in SAFE_JAPANESE_NAME_MAPPING_SOURCES:
                continue
            candidates.setdefault(str(row['japanese_name']), {}).setdefault(str(row['english_name']), row)
        for japanese_name, names in candidates.items():
            if len(names) == 1:
                row = next(iter(names.values()))
            else:
                # A rebuild can encounter older per-print translations that
                # disagree with an identity already accepted for search. Keep
                # that linked identity; a fresh ambiguous name still needs
                # review before it becomes searchable.
                row = (preferred or {}).get(japanese_name)
                if row is None or row["english_name"] not in names:
                    continue
            identity_id = self._upsert_card_identity(
                japanese_name,
                str(row['english_name']),
                tuple(json.loads(row['aliases_json'])),
                str(row['mapping_source']),
                str(row['mapping_source_url']),
                str(row['status']),
            )
            self._link_identity_to_matching_prints(identity_id, japanese_name)

    def rebuild_card_identities(self) -> tuple[int, int]:
        """Recreate shared search identities from current safe mapping evidence."""
        preferred = {
            str(row["japanese_name"]): row
            for row in self.connection.execute(
                """
                SELECT i.* FROM card_identities AS i
                WHERE i.status != 'conflicted'
                  AND EXISTS (
                    SELECT 1 FROM japanese_print_identity_links AS l
                    WHERE l.card_identity_id = i.id
                  )
                """
            )
        }
        self.connection.execute("DELETE FROM card_identity_search")
        self.connection.execute("DELETE FROM japanese_print_identity_links")
        self.connection.execute("DELETE FROM card_identities")
        self._migrate_card_identities(preferred)
        identities = int(self.connection.execute("SELECT COUNT(*) FROM card_identities").fetchone()[0])
        links = int(self.connection.execute("SELECT COUNT(*) FROM japanese_print_identity_links").fetchone()[0])
        return identities, links

    def archive_english_reference_number_mappings(self) -> tuple[int, int]:
        """Archive every JP mapping inferred from an English printed reference.

        English print serials are retained in ``english_print_references`` and
        ``card_prints`` for research only. They may never decide the identity
        of a Japanese printing: regional release ordering can differ for any
        product family, not just promo or ``Re`` rows.
        """
        sources = tuple(sorted(ENGLISH_REFERENCE_MAPPING_SOURCES))
        placeholders = ', '.join('?' for _ in sources)
        try:
            self.connection.execute(
                f"""
                INSERT OR IGNORE INTO archived_english_reference_mappings (
                    japanese_print_id, english_name, aliases_json, mapping_source,
                    mapping_source_url, status, archived_reason, imported_at
                )
                SELECT japanese_print_id, english_name, aliases_json, mapping_source,
                       mapping_source_url, status, 'english_serial_not_identity', imported_at
                FROM english_name_mappings
                WHERE mapping_source IN ({placeholders})
                """,
                sources,
            )
            removed = self.connection.execute(
                f"DELETE FROM english_name_mappings WHERE mapping_source IN ({placeholders})", sources
            ).rowcount
            identities, links = self.rebuild_card_identities()
        except Exception:
            self.connection.rollback()
            raise
        self.connection.commit()
        return removed, links

    # Compatibility spelling for an unshipped migration command. It archives
    # mapping claims; English printed serials themselves are retained.
    def remove_english_reference_number_mappings(self) -> tuple[int, int]:
        return self.archive_english_reference_number_mappings()

    def _add_column_if_missing(self, table: str, column: str, definition: str) -> None:
        columns = {
            str(row["name"])
            for row in self.connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _identity_source_rank(source: str, status: str) -> int:
        if source in {"promo-review", "fandom-cross-print", "fandom-japanese-promo"} and status in {"reviewed", "verified"}:
            return 3
        if source == "fandom-name-match":
            return 2
        return 1

    def _upsert_card_identity(
        self,
        japanese_name: str,
        english_name: str,
        aliases: tuple[str, ...],
        source: str,
        source_url: str,
        status: str,
    ) -> int:
        """Create or safely improve a shared English-search identity."""
        existing = self.connection.execute(
            "SELECT * FROM card_identities WHERE japanese_name = ?", (japanese_name,)
        ).fetchone()
        if existing:
            current_rank = self._identity_source_rank(existing['mapping_source'], existing['status'])
            incoming_rank = self._identity_source_rank(source, status)
            if existing['english_name'] != english_name:
                if current_rank > incoming_rank:
                    return int(existing['id'])
                # Different English names for one canonical Japanese name are
                # evidence of an unresolved mapping conflict.  Do not let
                # arrival order decide which name users can search. A direct
                # reviewed/verified correction is the sole way to resolve it.
                if incoming_rank < 3:
                    self.connection.execute(
                        "UPDATE card_identities SET status='conflicted', imported_at=CURRENT_TIMESTAMP WHERE id=?",
                        (existing['id'],),
                    )
                    self.connection.execute(
                        "DELETE FROM japanese_print_identity_links WHERE card_identity_id=?", (existing['id'],)
                    )
                    self._refresh_identity_search_row(int(existing['id']))
                    return int(existing['id'])
            if existing['english_name'] == english_name and incoming_rank <= current_rank:
                aliases = tuple(dict.fromkeys((*json.loads(existing['aliases_json']), *aliases)))
                source, source_url, status = (
                    existing['mapping_source'], existing['mapping_source_url'], existing['status']
                )
            self.connection.execute(
                """
                UPDATE card_identities
                SET english_name=?, aliases_json=?, normalised_name=?, normalised_aliases=?,
                    mapping_source=?, mapping_source_url=?, status=?, imported_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    english_name, json.dumps(aliases, ensure_ascii=False), normalise_text(english_name),
                    " ".join(normalise_text(alias) for alias in aliases), source, source_url, status, existing['id'],
                ),
            )
            identity_id = int(existing['id'])
        else:
            cursor = self.connection.execute(
                """
                INSERT INTO card_identities (
                    japanese_name, english_name, aliases_json, normalised_name, normalised_aliases,
                    mapping_source, mapping_source_url, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    japanese_name, english_name, json.dumps(aliases, ensure_ascii=False),
                    normalise_text(english_name), " ".join(normalise_text(alias) for alias in aliases),
                    source, source_url, status,
                ),
            )
            identity_id = int(cursor.lastrowid)
        self._refresh_identity_search_row(identity_id)
        return identity_id

    def _link_identity_to_matching_prints(self, identity_id: int, japanese_name: str) -> int:
        """Attach every physical Japanese printing of one canonical card."""
        cursor = self.connection.execute(
            """
            INSERT INTO japanese_print_identity_links (japanese_print_id, card_identity_id)
            SELECT id, ? FROM japanese_prints
            WHERE japanese_name = ? AND japanese_name NOT IN (?, ?, ?, ?)
            ON CONFLICT(japanese_print_id) DO UPDATE SET
                card_identity_id=excluded.card_identity_id, linked_at=CURRENT_TIMESTAMP
            """,
            (identity_id, japanese_name, *HELD_UTILITY_PROMO_NAMES),
        )
        return cursor.rowcount

    def _assign_card_identity(self, mapping: EnglishNameMapping, japanese: sqlite3.Row) -> int | None:
        if japanese['japanese_name'] in HELD_UTILITY_PROMO_NAMES:
            return None
        identity_id = self._upsert_card_identity(
            str(japanese['japanese_name']), mapping.english_name, mapping.aliases,
            mapping.source, mapping.source_url, mapping.status,
        )
        identity = self.connection.execute("SELECT status FROM card_identities WHERE id=?", (identity_id,)).fetchone()
        if identity and identity['status'] == 'conflicted':
            return identity_id
        self._link_identity_to_matching_prints(identity_id, str(japanese['japanese_name']))
        return identity_id

    def _refresh_identity_search_row(self, identity_id: int) -> None:
        row = self.connection.execute("SELECT * FROM card_identities WHERE id=?", (identity_id,)).fetchone()
        assert row is not None
        self.connection.execute("DELETE FROM card_identity_search WHERE identity_id = ?", (str(identity_id),))
        self.connection.execute(
            "INSERT INTO card_identity_search(identity_id, english_name, japanese_name, aliases) VALUES (?, ?, ?, ?)",
            (str(identity_id), row['english_name'], row['japanese_name'], " ".join(json.loads(row['aliases_json']))),
        )

    def __enter__(self) -> "CatalogueRepository":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def count(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM card_prints").fetchone()[0])

    @property
    def japanese_count(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM japanese_prints").fetchone()[0])

    def upsert(self, card: CardPrint) -> CardPrint:
        stored = self._upsert(card)
        self.connection.commit()
        return stored

    def import_many(self, cards: Iterable[CardPrint]) -> int:
        """Import a batch in one SQLite transaction instead of one per card."""
        imported = 0
        try:
            for card in cards:
                self._upsert(card)
                imported += 1
        except Exception:
            self.connection.rollback()
            raise
        self.connection.commit()
        return imported

    def import_japanese_many(self, cards: Iterable[JapaneseCardPrint]) -> int:
        """Store official Japanese print data without requiring an English name."""
        imported = 0
        try:
            for card in cards:
                self._upsert_japanese(card)
                imported += 1
        except Exception:
            self.connection.rollback()
            raise
        self.connection.commit()
        return imported

    def apply_name_mappings(
        self, mappings: Iterable[EnglishNameMapping], *, derive_same_name: bool = True
    ) -> MappingImportResult:
        """Layer community English names over the Japanese print master.

        An English name can be attached only through a Japanese catalogue
        record and its exact canonical Japanese name. English print numbers
        are retained elsewhere as reference data, never as identity evidence.
        """
        mapped = derived = unmatched = preserved_official = 0
        mappings_by_japanese_name: dict[tuple[str, str], EnglishNameMapping] = {}
        try:
            for mapping in mappings:
                # Official English catalogue numbers are never Japanese
                # identity evidence. Their records remain available to an
                # explicit cross-print/Fandom importer, but are not applied.
                if mapping.source in ENGLISH_REFERENCE_MAPPING_SOURCES:
                    unmatched += 1
                    continue
                japanese = self._japanese_by_reference(mapping.set_code, mapping.collector_number)
                if not japanese:
                    unmatched += 1
                    continue
                if (
                    japanese["set_code"] in PROMO_PRINT_SET_CODES
                    and japanese["japanese_name"] in HELD_UTILITY_PROMO_NAMES
                ):
                    continue
                incoming_rank = self._identity_source_rank(mapping.source, mapping.status)
                if incoming_rank < 3:
                    prior = self.connection.execute(
                        """
                        SELECT english_name, mapping_source, status
                        FROM english_name_mappings WHERE japanese_print_id = ?
                        """,
                        (japanese["id"],),
                    ).fetchone()
                    identity = self.connection.execute(
                        """
                        SELECT english_name, mapping_source, status
                        FROM card_identities WHERE japanese_name = ?
                        """,
                        (japanese["japanese_name"],),
                    ).fetchone()
                    if any(
                        row is not None
                        and row["english_name"] != mapping.english_name
                        and self._identity_source_rank(row["mapping_source"], row["status"]) > incoming_rank
                        for row in (prior, identity)
                    ):
                        continue
                # Archive any legacy equal-reference claim before the trusted
                # Japanese-name mapping is written. This is preservation, not
                # a match: no English serial is read to determine equality.
                self._archive_legacy_english_records_for_reference(
                    mapping.set_code, mapping.collector_number
                )
                rarity = mapping.rarity or japanese["rarity"]
                if mapping.rarity and mapping.rarity != japanese["rarity"]:
                    self.connection.execute(
                        "UPDATE japanese_prints SET rarity = ?, imported_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (mapping.rarity, japanese["id"]),
                    )
                    japanese = self._japanese_by_reference(mapping.set_code, mapping.collector_number)
                    assert japanese is not None
                mappings_by_japanese_name.setdefault(
                    (mapping.set_code, japanese["japanese_name"]), mapping
                )
                self.connection.execute(
                    """
                    INSERT INTO english_name_mappings (
                        japanese_print_id, english_name, aliases_json, mapping_source,
                        mapping_source_url, status
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(japanese_print_id) DO UPDATE SET
                        english_name = excluded.english_name,
                        aliases_json = excluded.aliases_json,
                        mapping_source = excluded.mapping_source,
                        mapping_source_url = excluded.mapping_source_url,
                        status = excluded.status,
                        imported_at = CURRENT_TIMESTAMP
                    """,
                    (
                        japanese["id"],
                        mapping.english_name,
                        json.dumps(mapping.aliases, ensure_ascii=False),
                        mapping.source,
                        mapping.source_url,
                        mapping.status,
                    ),
                )
                # This is the user-facing association: one English search
                # identity reaches every Japanese printing with this canonical
                # Japanese name, regardless of whether it is a main set, PR,
                # or Special Series printing.
                self._assign_card_identity(mapping, japanese)
                self._upsert(
                    CardPrint(
                        set_code=mapping.set_code,
                        collector_number=mapping.collector_number,
                        rarity=rarity,
                        english_name=mapping.english_name,
                        japanese_name=japanese["japanese_name"],
                        finish=japanese["finish"],
                        finish_raw=japanese["finish_raw"],
                        aliases=mapping.aliases,
                        source=f"{mapping.source}-{mapping.status}",
                        source_url=mapping.source_url,
                    )
                )
                mapped += 1
            # Fandom's set page names the base printing of many cards but may
            # omit its FFR/SR/SEC parallel rows. Official Japanese data gives
            # those rows the same Japanese name, so they can safely inherit
            # that already-trusted English name without a machine translation.
            for (set_code, japanese_name), source_mapping in (
                mappings_by_japanese_name.items() if derive_same_name else ()
            ):
                rows = self.connection.execute(
                    """
                    SELECT j.* FROM japanese_prints AS j
                    LEFT JOIN english_name_mappings AS m ON m.japanese_print_id = j.id
                    WHERE j.set_code = ? AND j.japanese_name = ? AND m.japanese_print_id IS NULL
                    """,
                    (set_code, japanese_name),
                ).fetchall()
                for japanese in rows:
                    self.connection.execute(
                        """
                        INSERT INTO english_name_mappings (
                            japanese_print_id, english_name, aliases_json, mapping_source,
                            mapping_source_url, status
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            japanese["id"],
                            source_mapping.english_name,
                            json.dumps(source_mapping.aliases, ensure_ascii=False),
                            f"{source_mapping.source}-derived",
                            source_mapping.source_url,
                            source_mapping.status,
                        ),
                    )
                    self._upsert(
                        CardPrint(
                            set_code=set_code,
                            collector_number=japanese["collector_number"],
                            rarity=japanese["rarity"],
                            english_name=source_mapping.english_name,
                            japanese_name=japanese["japanese_name"],
                            finish=japanese["finish"],
                            finish_raw=japanese["finish_raw"],
                            aliases=source_mapping.aliases,
                            source=f"{source_mapping.source}-derived-{source_mapping.status}",
                            source_url=source_mapping.source_url,
                        )
                    )
                    mapped += 1
                    derived += 1
        except Exception:
            self.connection.rollback()
            raise
        self.connection.commit()
        return MappingImportResult(mapped, derived, unmatched, preserved_official)

    def official_english_name_mappings(self) -> list[EnglishNameMapping]:
        """Return no mappings: English serials cannot establish JP identity.

        The official English import remains stored as reference data for a
        future reviewed cross-region feature, but its set/serial values are
        intentionally never converted into Japanese-name mappings.
        """
        return []

    def direct_fandom_mappings(self, set_codes: Iterable[str]) -> list[EnglishNameMapping]:
        """Return direct Fandom evidence already attached to selected prints.

        Derived rows are deliberately excluded: a promo rebuild must preserve
        only evidence directly obtained from a Fandom set or card page.
        """
        wanted = self._normalised_set_codes(set_codes)
        if not wanted:
            return []
        placeholders = ", ".join("?" for _ in wanted)
        rows = self.connection.execute(
            f"""
            SELECT j.set_code, j.collector_number, j.rarity, m.english_name,
                   m.aliases_json, m.mapping_source, m.mapping_source_url, m.status
            FROM japanese_prints AS j
            JOIN english_name_mappings AS m ON m.japanese_print_id = j.id
            WHERE j.set_code IN ({placeholders})
              AND m.mapping_source IN ('fandom', 'fandom-cross-print', 'fandom-japanese-promo')
            ORDER BY j.set_code, j.collector_number
            """,
            wanted,
        ).fetchall()
        return [
            EnglishNameMapping(
                set_code=row["set_code"],
                collector_number=row["collector_number"],
                rarity=row["rarity"],
                english_name=row["english_name"],
                aliases=tuple(json.loads(row["aliases_json"])),
                source=row["mapping_source"],
                source_url=row["mapping_source_url"],
                status=row["status"],
            )
            for row in rows
        ]

    def reviewed_promo_mappings(self, set_codes: Iterable[str]) -> list[EnglishNameMapping]:
        wanted = set(self._normalised_set_codes(set_codes)) & PROMO_PRINT_SET_CODES
        rows = self.connection.execute(
            """SELECT j.set_code,j.collector_number,j.rarity,m.*
            FROM japanese_prints j JOIN english_name_mappings m ON m.japanese_print_id=j.id
            WHERE m.mapping_source='promo-review' AND m.status='reviewed'"""
        ).fetchall()
        return [EnglishNameMapping(
            row['set_code'], row['collector_number'], row['rarity'], row['english_name'],
            row['mapping_source'], row['mapping_source_url'],
            aliases=tuple(json.loads(row['aliases_json'])), status=row['status'],
        ) for row in rows if row['set_code'] in wanted]

    def clear_region_specific_mappings_for_rebuild(
        self, set_codes: Iterable[str]
    ) -> tuple[int, int]:
        """Archive English references and clear affected Japanese search rows.

        Promos and Special Series product codes are region-specific. English
        rows remain in ``english_print_references`` for mapping evidence but
        must not occupy a Japanese catalogue key or appear in Japanese-price
        searches.
        """
        wanted = self._normalised_set_codes(set_codes)
        if not wanted:
            return 0, 0
        if not all(is_region_specific_print_set(set_code) for set_code in wanted):
            raise ValueError("Only region-specific promo or Special Series print families may be rebuilt.")
        placeholders = ", ".join("?" for _ in wanted)
        try:
            official_rows = self.connection.execute(
                f"""
                SELECT * FROM card_prints
                WHERE set_code IN ({placeholders}) AND source = 'official-english'
                """,
                wanted,
            ).fetchall()
            for row in official_rows:
                self._archive_english_print_reference(row)
            card_ids = [
                int(row[0])
                for row in self.connection.execute(
                    f"SELECT id FROM card_prints WHERE set_code IN ({placeholders})", wanted
                ).fetchall()
            ]
            self.connection.execute(
                f"""
                DELETE FROM english_name_mappings
                WHERE japanese_print_id IN (
                    SELECT id FROM japanese_prints WHERE set_code IN ({placeholders})
                )
                """,
                wanted,
            )
            self.connection.execute(
                f"""
                DELETE FROM japanese_print_identity_links
                WHERE japanese_print_id IN (
                    SELECT id FROM japanese_prints WHERE set_code IN ({placeholders})
                )
                """,
                wanted,
            )
            if card_ids:
                self.connection.executemany(
                    "DELETE FROM card_search WHERE print_id = ?", ((str(card_id),) for card_id in card_ids)
                )
            self.connection.execute(f"DELETE FROM card_prints WHERE set_code IN ({placeholders})", wanted)
            self._prune_orphan_card_identities()
        except Exception:
            self.connection.rollback()
            raise
        self.connection.commit()
        return len(official_rows), len(card_ids)

    def clear_promo_mappings_for_rebuild(self, set_codes: Iterable[str]) -> tuple[int, int]:
        """Backward-compatible promo-only wrapper for the regional rebuild."""
        wanted = self._normalised_set_codes(set_codes)
        if not set(wanted).issubset(PROMO_PRINT_SET_CODES):
            raise ValueError("Only region-specific promo print families may be rebuilt this way.")
        return self.clear_region_specific_mappings_for_rebuild(wanted)

    def unmapped_japanese_prints(self, set_codes: Iterable[str]) -> list[JapaneseCardPrint]:
        """Return unmapped Japanese prints in the requested set families."""
        wanted = self._normalised_set_codes(set_codes)
        if not wanted:
            return []
        placeholders = ", ".join("?" for _ in wanted)
        rows = self.connection.execute(
            f"""
            SELECT j.* FROM japanese_prints AS j
            LEFT JOIN japanese_print_identity_links AS l ON l.japanese_print_id = j.id
            WHERE j.set_code IN ({placeholders}) AND l.japanese_print_id IS NULL
            ORDER BY j.set_code, j.collector_number
            """,
            wanted,
        ).fetchall()
        return [
            JapaneseCardPrint(
                set_code=row["set_code"],
                collector_number=row["collector_number"],
                rarity=row["rarity"],
                japanese_name=row["japanese_name"],
                source_url=row["source_url"],
                id=int(row["id"]),
                finish=row["finish"],
                finish_raw=row["finish_raw"],
            )
            for row in rows
        ]

    def japanese_prints(self, set_codes: Iterable[str]) -> list[JapaneseCardPrint]:
        """Return every Japanese print in the requested set families."""
        wanted = self._normalised_set_codes(set_codes)
        if not wanted:
            return []
        placeholders = ", ".join("?" for _ in wanted)
        rows = self.connection.execute(
            f"""
            SELECT * FROM japanese_prints
            WHERE set_code IN ({placeholders})
            ORDER BY set_code, collector_number
            """,
            wanted,
        ).fetchall()
        return [
            JapaneseCardPrint(
                set_code=row["set_code"],
                collector_number=row["collector_number"],
                rarity=row["rarity"],
                japanese_name=row["japanese_name"],
                source_url=row["source_url"],
                id=int(row["id"]),
                finish=row["finish"],
                finish_raw=row["finish_raw"],
            )
            for row in rows
        ]

    def upsert_promo_catalogue_entries(self, entries: Iterable[PromoCatalogueEntry]) -> int:
        """Persist retailer promo locations without creating card-name mappings."""
        imported = 0
        try:
            for entry in entries:
                self.connection.execute(
                    """
                    INSERT INTO promo_catalogue_entries (
                        store_id, page_slug, set_code, collector_number, japanese_name,
                        listing_url, source_page_url, product_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(store_id, set_code, collector_number) DO UPDATE SET
                        page_slug = excluded.page_slug,
                        japanese_name = excluded.japanese_name,
                        listing_url = excluded.listing_url,
                        source_page_url = excluded.source_page_url,
                        product_id = excluded.product_id,
                        last_seen_at = CURRENT_TIMESTAMP
                    """,
                    (
                        entry.store_id,
                        entry.page_slug,
                        entry.set_code,
                        entry.collector_number,
                        entry.japanese_name,
                        entry.listing_url,
                        entry.source_page_url,
                        entry.product_id,
                    ),
                )
                if entry.store_id == "yuyutei":
                    finish, finish_raw = finish_from_text(entry.japanese_name)
                    if finish is not Finish.UNKNOWN:
                        for table in ("japanese_prints", "card_prints"):
                            self.connection.execute(
                                f"""
                                UPDATE {table}
                                SET finish = ?, finish_raw = ?, imported_at = CURRENT_TIMESTAMP
                                WHERE set_code = ? AND collector_number = ?
                                """,
                                (
                                    finish.value,
                                    finish_raw,
                                    entry.set_code,
                                    entry.collector_number,
                                ),
                            )
                imported += 1
        except Exception:
            self.connection.rollback()
            raise
        self.connection.commit()
        return imported

    def has_promo_catalogue_page(self, store_id: str, page_slug: str) -> bool:
        return self.connection.execute(
            """
            SELECT 1 FROM promo_catalogue_imports
            WHERE store_id = ? AND page_slug = ?
            """,
            (store_id.strip().lower(), page_slug.strip().lower()),
        ).fetchone() is not None

    def mark_promo_catalogue_page_imported(
        self, store_id: str, page_slug: str, source_page_url: str, entry_count: int
    ) -> None:
        """Checkpoint a completed page only after all its entries are stored."""
        self.connection.execute(
            """
            INSERT INTO promo_catalogue_imports (store_id, page_slug, source_page_url, entry_count)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(store_id, page_slug) DO UPDATE SET
                source_page_url = excluded.source_page_url,
                entry_count = excluded.entry_count,
                completed_at = CURRENT_TIMESTAMP
            """,
            (store_id.strip().lower(), page_slug.strip().lower(), source_page_url, entry_count),
        )
        self.connection.commit()

    def promo_catalogue_entry(
        self, store_id: str, set_code: str, collector_number: str
    ) -> PromoCatalogueEntry | None:
        """Look up a retailer location for a selected Japanese promo print."""
        row = self.connection.execute(
            """
            SELECT * FROM promo_catalogue_entries
            WHERE store_id = ? AND set_code = ? AND collector_number = ?
            """,
            (
                store_id.strip().lower(),
                normalise_set_code(set_code),
                normalise_collector_number(collector_number),
            ),
        ).fetchone()
        if not row:
            return None
        return self._to_promo_entry(row)

    def promo_catalogue_page_url(
        self, store_id: str, set_code: str, collector_number: str
    ) -> str | None:
        """Return the retailer page that contains one exact promo print."""
        entry = self.promo_catalogue_entry(store_id, set_code, collector_number)
        return entry.source_page_url if entry else None

    def unmapped_yuyutei_promo_entries(self) -> list[PromoCatalogueEntry]:
        """Return actual Yuyu-Tei D-PR listings lacking an English mapping."""
        rows = self.connection.execute(
            """
            SELECT p.* FROM promo_catalogue_entries AS p
            JOIN japanese_prints AS j
              ON j.set_code = p.set_code AND j.collector_number = p.collector_number
            LEFT JOIN japanese_print_identity_links AS l ON l.japanese_print_id = j.id
            WHERE p.store_id = 'yuyutei' AND p.set_code = 'DPR'
              AND l.japanese_print_id IS NULL
            ORDER BY CAST(p.collector_number AS INTEGER), p.collector_number
            """
        ).fetchall()
        return [self._to_promo_entry(row) for row in rows]

    def eligible_name_mapping_evidence(self) -> list[sqlite3.Row]:
        """Share exact-Japanese-name Fandom evidence used by repair and review."""
        rows = self.connection.execute(
            """
            SELECT j.set_code, j.collector_number, j.japanese_name, m.english_name,
                   m.aliases_json, m.mapping_source, m.mapping_source_url
            FROM japanese_prints AS j
            JOIN english_name_mappings AS m ON m.japanese_print_id = j.id
            WHERE j.set_code NOT IN ('DPR', 'CP')
              AND m.mapping_source IN ('fandom', 'fandom-cross-print')
            ORDER BY m.imported_at DESC, j.set_code, j.collector_number
            """
        ).fetchall()
        return list(rows)

    def unambiguous_fandom_name_mappings(self, set_codes: Iterable[str]) -> list[EnglishNameMapping]:
        """Map prints from one exact Japanese-name Fandom candidate.

        The historical method name is retained for the repair commands. Direct
        English printed references are never Japanese identity evidence.
        """
        wanted = self._normalised_set_codes(set_codes)
        if not wanted:
            return []
        placeholders = ", ".join("?" for _ in wanted)
        target_rows = self.connection.execute(
            f"""
            SELECT j.* FROM japanese_prints AS j
            LEFT JOIN english_name_mappings AS m ON m.japanese_print_id = j.id
            WHERE j.set_code IN ({placeholders}) AND m.japanese_print_id IS NULL
            ORDER BY j.set_code, j.collector_number
            """,
            wanted,
        ).fetchall()
        candidates: dict[str, dict[str, sqlite3.Row]] = {}
        for row in self.eligible_name_mapping_evidence():
            candidates.setdefault(str(row["japanese_name"]), {}).setdefault(str(row["english_name"]), row)
        mappings: list[EnglishNameMapping] = []
        for target in target_rows:
            if target['japanese_name'] in HELD_UTILITY_PROMO_NAMES:
                continue
            names = candidates.get(str(target["japanese_name"]), {})
            if len(names) != 1:
                continue
            source = next(iter(names.values()))
            mappings.append(
                EnglishNameMapping(
                    set_code=target["set_code"],
                    collector_number=target["collector_number"],
                    rarity=target["rarity"],
                    english_name=source["english_name"],
                    aliases=tuple(json.loads(source["aliases_json"])),
                    source="fandom-name-match",
                    source_url=source["mapping_source_url"],
                    status="provisional",
                )
            )
        return mappings

    def unlinked_official_english_cards(self, set_codes: Iterable[str]) -> list[CardPrint]:
        """Return official English prints still lacking a Japanese counterpart.

        Callers provide a small, explicit set scope because resolving a
        cross-print relationship requires reading each card's Fandom page.
        """
        wanted = sorted({normalise_set_code(set_code) for set_code in set_codes if set_code.strip()})
        if not wanted:
            return []
        placeholders = ", ".join("?" for _ in wanted)
        rows = self.connection.execute(
            f"""
            SELECT * FROM card_prints
            WHERE source = 'official-english'
              AND (japanese_name IS NULL OR TRIM(japanese_name) = '')
              AND set_code IN ({placeholders})
            ORDER BY set_code, collector_number, rarity
            """,
            wanted,
        ).fetchall()
        return [self._to_card(row) for row in rows]

    def derive_name_mappings_from_known_japanese_names(self) -> MappingImportResult:
        """Map reprints when their Japanese name has one trusted English name.

        This never translates text or uses English printing numbers. A
        candidate is accepted only when exact-Japanese-name Fandom evidence
        agrees on one English name.
        """
        rows = self.connection.execute(
            """
            WITH known_names AS (
                SELECT j.japanese_name
                FROM japanese_prints AS j
                JOIN english_name_mappings AS m ON m.japanese_print_id = j.id
                WHERE m.mapping_source IN ('fandom', 'fandom-cross-print')
                GROUP BY j.japanese_name
                HAVING COUNT(DISTINCT m.english_name) = 1
            ), ranked_sources AS (
                SELECT j.japanese_name, m.english_name, m.mapping_source,
                       m.mapping_source_url,
                       ROW_NUMBER() OVER (
                           PARTITION BY j.japanese_name
                           ORDER BY m.imported_at DESC
                       ) AS source_rank
                FROM japanese_prints AS j
                JOIN english_name_mappings AS m ON m.japanese_print_id = j.id
                JOIN known_names AS known ON known.japanese_name = j.japanese_name
                WHERE m.mapping_source IN ('fandom', 'fandom-cross-print')
            )
            SELECT j.set_code, j.collector_number, j.rarity, source.english_name,
                   source.mapping_source, source.mapping_source_url
            FROM japanese_prints AS j
            LEFT JOIN english_name_mappings AS mapped ON mapped.japanese_print_id = j.id
            JOIN ranked_sources AS source
              ON source.japanese_name = j.japanese_name AND source.source_rank = 1
            WHERE mapped.japanese_print_id IS NULL
              AND j.set_code NOT IN ('DPR', 'CP')
            ORDER BY j.set_code, j.collector_number
            """
        ).fetchall()
        mappings = [
            EnglishNameMapping(
                set_code=row["set_code"],
                collector_number=row["collector_number"],
                rarity=row["rarity"],
                english_name=row["english_name"],
                source="fandom-cross-print" if row["mapping_source"] == "fandom-cross-print" else "fandom",
                source_url=row["mapping_source_url"],
                status="derived",
            )
            for row in rows
        ]
        return self.apply_name_mappings(mappings)

    def _upsert(self, card: CardPrint) -> CardPrint:
        if not card.english_name:
            raise ValueError("A card print requires an English name.")
        if card.source == "official-english" and is_region_specific_print_set(card.set_code):
            self._archive_english_card_print(card)
            return card
        aliases_json = json.dumps(card.aliases, ensure_ascii=False)
        normalised_aliases = " ".join(normalise_text(alias) for alias in card.aliases)
        self.connection.execute(
            """
            INSERT INTO card_prints (
                set_code, collector_number, rarity, finish, finish_raw, english_name, japanese_name,
                aliases_json, normalised_name, normalised_aliases, source, source_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(set_code, collector_number, rarity) DO UPDATE SET
                finish = CASE
                    WHEN excluded.finish != 'unknown' THEN excluded.finish
                    ELSE card_prints.finish
                END,
                finish_raw = COALESCE(excluded.finish_raw, card_prints.finish_raw),
                english_name = excluded.english_name,
                japanese_name = excluded.japanese_name,
                aliases_json = excluded.aliases_json,
                normalised_name = excluded.normalised_name,
                normalised_aliases = excluded.normalised_aliases,
                source = excluded.source,
                source_url = excluded.source_url,
                imported_at = CURRENT_TIMESTAMP
            """,
            (
                card.set_code,
                card.collector_number,
                card.rarity,
                card.finish.value,
                card.finish_raw,
                card.english_name,
                card.japanese_name,
                aliases_json,
                normalise_text(card.english_name),
                normalised_aliases,
                card.source,
                card.source_url,
            ),
        )
        row = self.connection.execute(
            """
            SELECT * FROM card_prints
            WHERE set_code = ? AND collector_number = ? AND rarity = ?
            """,
            (card.set_code, card.collector_number, card.rarity),
        ).fetchone()
        assert row is not None
        self._refresh_search_row(row)
        return self._to_card(row)

    def get(self, print_id: int, *, japanese_only: bool = False) -> CardPrint | None:
        """Return one print, optionally requiring a Japanese store-search name."""
        conditions = ["id = ?"]
        if japanese_only:
            conditions.append("japanese_name IS NOT NULL AND TRIM(japanese_name) != ''")
        row = self.connection.execute(
            f"SELECT * FROM card_prints WHERE {' AND '.join(conditions)}", (print_id,)
        ).fetchone()
        return self._to_card(row) if row else None

    def get_user_selection(self, selection_id: int) -> CardPrint | None:
        """Resolve a stored card or an ephemeral Japanese-serial selection.

        Negative identifiers refer to a Japanese master record selected by its
        serial. They deliberately avoid writing a placeholder English name to
        the catalogue while still allowing exact-print store comparisons.
        """
        if selection_id >= 0:
            return self.get(selection_id, japanese_only=True)
        japanese = self.connection.execute(
            "SELECT * FROM japanese_prints WHERE id = ?", (-selection_id,)
        ).fetchone()
        return self._serial_selection_from_japanese(japanese) if japanese else None

    def verified_reprint_family(self, card: CardPrint) -> list[CardPrint]:
        """Return reprints only when both canonical names agree exactly.

        This is deliberately stricter than the user-facing fuzzy English
        search: all family members must share the selected Japanese canonical
        name *and* its normalised stored English mapping. Similar translated
        names or aliases cannot create a price-comparison family.
        """
        if not card.japanese_name:
            return [card]
        japanese_id: int | None = -card.id if card.id is not None and card.id < 0 else None
        if japanese_id is None:
            japanese = self._japanese_by_reference(card.set_code, card.collector_number)
            japanese_id = int(japanese['id']) if japanese else None
        if japanese_id is not None:
            identity = self.connection.execute(
                "SELECT card_identity_id FROM japanese_print_identity_links WHERE japanese_print_id=?",
                (japanese_id,),
            ).fetchone()
            if identity:
                rows = self.connection.execute(
                    self._identity_select()
                    + " WHERE i.id=? ORDER BY j.set_code, j.collector_number, j.rarity, j.finish, j.id",
                    (identity['card_identity_id'],),
                ).fetchall()
                if rows:
                    return [self._to_card(row) for row in rows]
        # Compatibility for direct `CardPrint` imports that do not have a
        # Japanese master record yet.
        rows = self.connection.execute(
            """
            SELECT * FROM card_prints
            WHERE japanese_name = ? AND normalised_name = ?
              AND japanese_name IS NOT NULL AND TRIM(japanese_name) != ''
            ORDER BY set_code, collector_number, rarity, finish, id
            """,
            (card.japanese_name, normalise_text(card.english_name)),
        ).fetchall()
        family = [self._to_card(row) for row in rows]
        return family or [card]

    def lookup_japanese_serial(self, value: str) -> CardPrint | None:
        """Resolve one formatted Japanese serial without accepting bare numbers.

        This is Japanese-print only. It accepts punctuation and spacing
        variants such as ``D-PR/953``, ``DPR953``, and ``D-PR 953`` but does
        not use English promo references as a user-facing lookup key.
        """
        compact = re.sub(r"[^A-Za-z0-9]", "", value).upper()
        if not compact or not re.search(r"[A-Z]", compact) or not re.search(r"\d", compact):
            return None
        set_codes = [
            str(row["set_code"])
            for row in self.connection.execute(
                "SELECT DISTINCT set_code FROM japanese_prints ORDER BY LENGTH(set_code) DESC, set_code"
            )
        ]
        matches: list[sqlite3.Row] = []
        for set_code in set_codes:
            if not compact.startswith(set_code):
                continue
            supplied_collector = compact.removeprefix(set_code)
            if not supplied_collector:
                continue
            wanted_collector = normalise_collector_number(supplied_collector)
            rows = self.connection.execute(
                "SELECT * FROM japanese_prints WHERE set_code = ?", (set_code,)
            ).fetchall()
            matches.extend(
                row
                for row in rows
                if self._compact_reference_part(row["collector_number"])
                == self._compact_reference_part(wanted_collector)
            )
        if len(matches) != 1:
            return None
        return self._serial_selection_from_japanese(matches[0])

    def unmapped_japanese_count(self, set_code: str | None = None) -> int:
        conditions = ["l.japanese_print_id IS NULL"]
        values: list[object] = []
        if set_code:
            conditions.append("j.set_code = ?")
            values.append(set_code)
        return int(
            self.connection.execute(
                f"""
                SELECT COUNT(*) FROM japanese_prints AS j
                LEFT JOIN japanese_print_identity_links AS l ON l.japanese_print_id = j.id
                WHERE {' AND '.join(conditions)}
                """,
                values,
            ).fetchone()[0]
        )

    def unmapped_japanese_set_codes(self) -> list[str]:
        """Return Japanese set codes that still need an English-name source."""
        return [
            str(row[0])
            for row in self.connection.execute(
                """
                SELECT DISTINCT j.set_code
                FROM japanese_prints AS j
                LEFT JOIN japanese_print_identity_links AS l ON l.japanese_print_id = j.id
                WHERE l.japanese_print_id IS NULL
                ORDER BY j.set_code
                """
            )
        ]

    def region_specific_japanese_set_codes(self) -> list[str]:
        """List imported promo and Special Series families needing regional review."""
        return [
            str(row["set_code"])
            for row in self.connection.execute(
                "SELECT DISTINCT set_code FROM japanese_prints ORDER BY set_code"
            )
            if is_region_specific_print_set(row["set_code"])
        ]

    def has_official_expansion(self, expansion_id: int) -> bool:
        """Whether a completed official import already wrote this expansion.

        Official detail URLs include the stable product expansion identifier.
        It makes a lengthy one-time import resumable after a transient network
        failure without a second bookkeeping database.
        """
        row = self.connection.execute(
            """
            SELECT 1 FROM (
                SELECT source, source_url FROM card_prints
                UNION ALL
                SELECT source, source_url FROM english_print_references
            )
            WHERE source = 'official-english'
              AND (source_url LIKE ? OR source_url LIKE ?)
            LIMIT 1
            """,
            (f"%expansion={expansion_id}&%", f"%expansion={expansion_id}"),
        ).fetchone()
        return row is not None

    def has_japanese_expansion(self, expansion_id: int) -> bool:
        """Whether a Japanese product page has already been imported fully.

        The explicit checkpoint also covers legitimate empty product pages.
        The URL check makes the first full import resume cleanly from Japanese
        cards that were imported before checkpoints were introduced.
        """
        checkpoint = self.connection.execute(
            "SELECT 1 FROM japanese_expansion_imports WHERE expansion_id = ?", (expansion_id,)
        ).fetchone()
        if checkpoint:
            return True
        return self.connection.execute(
            """
            SELECT 1 FROM japanese_prints
            WHERE source_url LIKE ? OR source_url LIKE ?
            LIMIT 1
            """,
            (f"%expansion={expansion_id}&%", f"%expansion={expansion_id}"),
        ).fetchone() is not None

    def mark_japanese_expansion_imported(self, expansion_id: int) -> None:
        """Record a completed Japanese product import after its cards commit."""
        self.connection.execute(
            """
            INSERT INTO japanese_expansion_imports (expansion_id)
            VALUES (?)
            ON CONFLICT(expansion_id) DO UPDATE SET completed_at = CURRENT_TIMESTAMP
            """,
            (expansion_id,),
        )
        self.connection.commit()

    def search(
        self,
        query: str,
        *,
        rarity: str | None = None,
        rarities: Iterable[str] = (),
        finishes: Iterable[Finish | str] = (),
        limit: int = 8,
        japanese_only: bool = False,
    ) -> list[CardPrint]:
        """Search English names and aliases by prefix, substring, and typo score.

        ``japanese_only`` keeps user-facing store searches to prints that have
        an official Japanese name. English-only catalogue records remain
        available as a local mapping source, but Yuyu-Tei cannot list them.
        """
        normalised_query = normalise_text(query)
        if not normalised_query:
            return []
        rarity_values = self._normalise_rarities(rarity, rarities)
        finish_values = self._normalise_finishes(finishes)

        if japanese_only:
            # User searches are identity-first: match the English name once,
            # then return every linked Japanese physical printing.  The old
            # per-print search cache remains only for English-reference and
            # backwards-compatible import workflows.
            rows = self._identity_fts_rows(normalised_query, rarity_values, finish_values)
            if not rows:
                rows = self._identity_substring_rows(normalised_query, rarity_values, finish_values)
            if not rows:
                rows = self._identity_all_rows(rarity_values, finish_values)
            if not rows and not self._has_card_identities():
                rows = self._fts_rows(normalised_query, rarity_values, finish_values, japanese_only=True)
                if not rows:
                    rows = self._substring_rows(normalised_query, rarity_values, finish_values, japanese_only=True)
                if not rows:
                    rows = self._all_rows(rarity_values, finish_values, japanese_only=True)
        else:
            rows = self._fts_rows(normalised_query, rarity_values, finish_values, japanese_only=False)
            if not rows:
                rows = self._substring_rows(normalised_query, rarity_values, finish_values, japanese_only=False)
            if not rows:
                rows = self._all_rows(rarity_values, finish_values, japanese_only=False)

        ranked = sorted(
            ((self._score(normalised_query, row), self._to_card(row)) for row in rows),
            key=lambda item: (-item[0], item[1].english_name.casefold(), item[1].display_code),
        )
        return [card for score, card in ranked if score >= 55][:limit]

    def _has_card_identities(self) -> bool:
        return bool(self.connection.execute("SELECT 1 FROM card_identities LIMIT 1").fetchone())

    @staticmethod
    def _identity_select() -> str:
        return """
            SELECT -j.id AS id, j.set_code, j.collector_number, j.rarity, j.finish, j.finish_raw,
                   i.english_name, i.japanese_name, i.aliases_json, i.normalised_name,
                   i.normalised_aliases, i.mapping_source || '-' || i.status AS source,
                   i.mapping_source_url AS source_url
            FROM card_identities AS i
            JOIN japanese_print_identity_links AS l ON l.card_identity_id=i.id
            JOIN japanese_prints AS j ON j.id=l.japanese_print_id
        """

    def _identity_fts_rows(
        self, query: str, rarities: tuple[str, ...], finishes: tuple[Finish, ...]
    ) -> list[sqlite3.Row]:
        tokens = [token for token in query.split() if token]
        if not tokens:
            return []
        match_expression = " AND ".join(f'"{token}"*' for token in tokens)
        conditions = ["card_identity_search MATCH ?"]
        values: list[object] = [match_expression]
        filters, filter_values = self._filter_conditions("j.", rarities, finishes)
        conditions.extend(filters)
        values.extend(filter_values)
        try:
            return self.connection.execute(
                f"""
                SELECT -j.id AS id, j.set_code, j.collector_number, j.rarity, j.finish, j.finish_raw,
                       i.english_name, i.japanese_name, i.aliases_json, i.normalised_name,
                       i.normalised_aliases, i.mapping_source || '-' || i.status AS source,
                       i.mapping_source_url AS source_url
                FROM card_identity_search
                JOIN card_identities AS i ON i.id=CAST(card_identity_search.identity_id AS INTEGER)
                JOIN japanese_print_identity_links AS l ON l.card_identity_id=i.id
                JOIN japanese_prints AS j ON j.id=l.japanese_print_id
                WHERE {' AND '.join(conditions)}
                LIMIT 160
                """,
                values,
            ).fetchall()
        except sqlite3.OperationalError:
            return []

    def _identity_substring_rows(
        self, query: str, rarities: tuple[str, ...], finishes: tuple[Finish, ...]
    ) -> list[sqlite3.Row]:
        conditions = ["(i.normalised_name LIKE ? OR i.normalised_aliases LIKE ?)"]
        values: list[object] = [f"%{query}%", f"%{query}%"]
        filters, filter_values = self._filter_conditions("j.", rarities, finishes)
        conditions.extend(filters)
        values.extend(filter_values)
        return self.connection.execute(
            self._identity_select() + f" WHERE {' AND '.join(conditions)} LIMIT 160", values
        ).fetchall()

    def _identity_all_rows(
        self, rarities: tuple[str, ...], finishes: tuple[Finish, ...]
    ) -> list[sqlite3.Row]:
        conditions, values = self._filter_conditions("j.", rarities, finishes)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        return self.connection.execute(self._identity_select() + where, values).fetchall()

    @staticmethod
    def _normalise_rarities(rarity: str | None, rarities: Iterable[str]) -> tuple[str, ...]:
        values = [value.strip().upper() for value in rarities if value and value.strip()]
        if rarity and rarity.strip():
            values.append(rarity.strip().upper())
        return tuple(sorted(set(values)))

    @staticmethod
    def _normalise_finishes(finishes: Iterable[Finish | str]) -> tuple[Finish, ...]:
        return tuple(sorted({normalise_finish(value) for value in finishes}, key=lambda finish: finish.value))

    @staticmethod
    def _filter_conditions(
        prefix: str, rarities: tuple[str, ...], finishes: tuple[Finish, ...]
    ) -> tuple[list[str], list[object]]:
        conditions: list[str] = []
        values: list[object] = []
        if rarities:
            conditions.append(f"{prefix}rarity IN ({', '.join('?' for _ in rarities)})")
            values.extend(rarities)
        if finishes:
            placeholders = ", ".join("?" for _ in finishes)
            if Finish.UNKNOWN in finishes:
                conditions.append(f"{prefix}finish IN ({placeholders})")
                values.extend(finish.value for finish in finishes)
            else:
                # A store or official source omitting a finish is not evidence
                # that the printing is standard. Keep those candidates visible
                # for a holo/standard filter until the catalogue can classify
                # them explicitly.
                conditions.append(f"({prefix}finish IN ({placeholders}) OR {prefix}finish = 'unknown')")
                values.extend(finish.value for finish in finishes)
        return conditions, values

    def _fts_rows(
        self,
        query: str,
        rarities: tuple[str, ...],
        finishes: tuple[Finish, ...],
        japanese_only: bool,
    ) -> list[sqlite3.Row]:
        tokens = [token for token in query.split() if token]
        if not tokens:
            return []
        match_expression = " AND ".join(f'"{token}"*' for token in tokens)
        conditions = ["card_search MATCH ?"]
        values: list[object] = [match_expression]
        filter_conditions, filter_values = self._filter_conditions("p.", rarities, finishes)
        conditions.extend(filter_conditions)
        values.extend(filter_values)
        if japanese_only:
            conditions.append("p.japanese_name IS NOT NULL AND TRIM(p.japanese_name) != ''")
        try:
            return self.connection.execute(
                f"""
                SELECT p.* FROM card_search
                JOIN card_prints AS p ON p.id = CAST(card_search.print_id AS INTEGER)
                WHERE {' AND '.join(conditions)}
                LIMIT 80
                """,
                values,
            ).fetchall()
        except sqlite3.OperationalError:
            return []

    def _substring_rows(
        self,
        query: str,
        rarities: tuple[str, ...],
        finishes: tuple[Finish, ...],
        japanese_only: bool,
    ) -> list[sqlite3.Row]:
        conditions = ["(normalised_name LIKE ? OR normalised_aliases LIKE ?)"]
        values: list[object] = [f"%{query}%", f"%{query}%"]
        filter_conditions, filter_values = self._filter_conditions("", rarities, finishes)
        conditions.extend(filter_conditions)
        values.extend(filter_values)
        if japanese_only:
            conditions.append("japanese_name IS NOT NULL AND TRIM(japanese_name) != ''")
        return self.connection.execute(
            f"SELECT * FROM card_prints WHERE {' AND '.join(conditions)} LIMIT 120", values
        ).fetchall()

    def _all_rows(
        self, rarities: tuple[str, ...], finishes: tuple[Finish, ...], japanese_only: bool
    ) -> list[sqlite3.Row]:
        conditions, values = self._filter_conditions("", rarities, finishes)
        if japanese_only:
            conditions.append("japanese_name IS NOT NULL AND TRIM(japanese_name) != ''")
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        return self.connection.execute(f"SELECT * FROM card_prints{where}", values).fetchall()

    @staticmethod
    def _score(query: str, row: sqlite3.Row) -> float:
        names = [row["normalised_name"]]
        names.extend(normalise_text(alias) for alias in json.loads(row["aliases_json"]))
        best = 0.0
        query_tokens = set(query.split())
        for name in names:
            if query == name:
                best = max(best, 200.0)
            elif name.startswith(query):
                best = max(best, 160.0)
            elif query in name:
                best = max(best, 140.0)
            elif query_tokens.issubset(set(name.split())):
                best = max(best, 120.0)
            best = max(best, float(fuzz.WRatio(query, name)))
        return best

    def _refresh_search_row(self, row: sqlite3.Row) -> None:
        self.connection.execute("DELETE FROM card_search WHERE print_id = ?", (str(row["id"]),))
        self.connection.execute(
            "INSERT INTO card_search(print_id, english_name, japanese_name, aliases) VALUES (?, ?, ?, ?)",
            (
                str(row["id"]),
                row["english_name"],
                row["japanese_name"] or "",
                " ".join(json.loads(row["aliases_json"])),
            ),
        )

    def _archive_english_card_print(self, card: CardPrint) -> None:
        self.connection.execute(
            """
            INSERT INTO english_print_references (
                set_code, collector_number, rarity, english_name, aliases_json, source, source_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(set_code, collector_number, rarity) DO UPDATE SET
                english_name = excluded.english_name,
                aliases_json = excluded.aliases_json,
                source = excluded.source,
                source_url = excluded.source_url,
                imported_at = CURRENT_TIMESTAMP
            """,
            (
                card.set_code,
                card.collector_number,
                card.rarity,
                card.english_name,
                json.dumps(card.aliases, ensure_ascii=False),
                card.source,
                card.source_url,
            ),
        )

    def _archive_english_print_reference(self, row: sqlite3.Row) -> None:
        self.connection.execute(
            """
            INSERT INTO english_print_references (
                set_code, collector_number, rarity, english_name, aliases_json, source, source_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(set_code, collector_number, rarity) DO UPDATE SET
                english_name = excluded.english_name,
                aliases_json = excluded.aliases_json,
                source = excluded.source,
                source_url = excluded.source_url,
                imported_at = CURRENT_TIMESTAMP
            """,
            (
                row["set_code"],
                row["collector_number"],
                row["rarity"],
                row["english_name"],
                row["aliases_json"],
                row["source"],
                row["source_url"],
            ),
        )

    @staticmethod
    def _normalised_set_codes(set_codes: Iterable[str]) -> list[str]:
        return sorted({normalise_set_code(set_code) for set_code in set_codes if set_code.strip()})

    def _serial_selection_from_japanese(self, japanese: sqlite3.Row) -> CardPrint:
        identity_row = self.connection.execute(
            self._identity_select() + " WHERE j.id=?", (japanese['id'],)
        ).fetchone()
        if identity_row:
            # Keep an existing cache ID for legacy callers only when it agrees
            # with the shared identity.  It cannot revive a stale mapping.
            mapped = self.connection.execute(
                """
                SELECT * FROM card_prints
                WHERE set_code=? AND collector_number=? AND normalised_name=?
                ORDER BY id LIMIT 1
                """,
                (japanese['set_code'], japanese['collector_number'], identity_row['normalised_name']),
            ).fetchone()
            return self._to_card(mapped) if mapped else self._to_card(identity_row)
        mapped = self.connection.execute(
            """
            SELECT * FROM card_prints
            WHERE set_code = ? AND collector_number = ?
              AND japanese_name IS NOT NULL AND TRIM(japanese_name) != ''
            ORDER BY id
            LIMIT 1
            """,
            (japanese["set_code"], japanese["collector_number"]),
        ).fetchone()
        if mapped:
            return self._to_card(mapped)
        # No English mapping is created. The Japanese title is an explicit
        # serial-only display label, sufficient for exact retailer matching.
        return CardPrint(
            id=-int(japanese["id"]),
            set_code=japanese["set_code"],
            collector_number=japanese["collector_number"],
            rarity=japanese["rarity"],
            english_name=japanese["japanese_name"],
            japanese_name=japanese["japanese_name"],
            source="japanese-serial-only",
            source_url=japanese["source_url"],
            finish=japanese["finish"],
            finish_raw=japanese["finish_raw"],
        )

    @staticmethod
    def _compact_reference_part(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9]", "", value).upper()

    @staticmethod
    def _to_promo_entry(row: sqlite3.Row) -> PromoCatalogueEntry:
        return PromoCatalogueEntry(
            store_id=row["store_id"],
            page_slug=row["page_slug"],
            set_code=row["set_code"],
            collector_number=row["collector_number"],
            japanese_name=row["japanese_name"],
            listing_url=row["listing_url"],
            source_page_url=row["source_page_url"],
            product_id=row["product_id"],
        )

    def _upsert_japanese(self, card: JapaneseCardPrint) -> sqlite3.Row:
        if not card.japanese_name:
            raise ValueError("A Japanese print requires a Japanese name.")
        if card.set_code == "DPR":
            identity = self.connection.execute(
                "SELECT japanese_name,source_url FROM official_promo_identities WHERE collector_number=?",
                (card.collector_number,),
            ).fetchone()
            if identity:
                card = replace(card, japanese_name=identity['japanese_name'], source_url=identity['source_url'])
            if not card.rarity:
                card = replace(card, rarity="PR")
        previous = self._japanese_by_reference(card.set_code, card.collector_number)
        if previous and previous['japanese_name'] != card.japanese_name:
            # A corrected official Japanese identity invalidates its search
            # association.  It must be reviewed again rather than retaining a
            # name that belonged to the old Japanese card.
            self.connection.execute(
                "DELETE FROM japanese_print_identity_links WHERE japanese_print_id=?", (previous['id'],)
            )
            self.connection.execute(
                "DELETE FROM english_name_mappings WHERE japanese_print_id=?", (previous['id'],)
            )
            self._prune_orphan_card_identities()
        self.connection.execute(
            """
            INSERT INTO japanese_prints (
                set_code, collector_number, rarity, finish, finish_raw, japanese_name, source_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(set_code, collector_number) DO UPDATE SET
                rarity = CASE
                    WHEN excluded.rarity != '' THEN excluded.rarity
                    ELSE japanese_prints.rarity
                END,
                finish = CASE
                    WHEN excluded.finish != 'unknown' THEN excluded.finish
                    ELSE japanese_prints.finish
                END,
                finish_raw = COALESCE(excluded.finish_raw, japanese_prints.finish_raw),
                japanese_name = excluded.japanese_name,
                source_url = excluded.source_url,
                imported_at = CURRENT_TIMESTAMP
            """,
            (
                card.set_code,
                card.collector_number,
                card.rarity,
                card.finish.value,
                card.finish_raw,
                card.japanese_name,
                card.source_url,
            ),
        )
        row = self._japanese_by_reference(card.set_code, card.collector_number)
        assert row is not None
        return row

    def _prune_orphan_card_identities(self) -> None:
        rows = self.connection.execute(
            """
            SELECT i.id FROM card_identities AS i
            LEFT JOIN japanese_print_identity_links AS l ON l.card_identity_id=i.id
            WHERE l.card_identity_id IS NULL
            """
        ).fetchall()
        if not rows:
            return
        self.connection.executemany(
            "DELETE FROM card_identity_search WHERE identity_id=?", ((str(row['id']),) for row in rows)
        )
        self.connection.executemany("DELETE FROM card_identities WHERE id=?", ((row['id'],) for row in rows))

    def _official_english_by_reference_unchecked(
        self, set_code: str, collector_number: str
    ) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT * FROM card_prints
            WHERE set_code = ? AND collector_number = ? AND source = 'official-english'
            """,
            (set_code, collector_number),
        ).fetchall()

    def _archive_legacy_english_records_for_reference(
        self, set_code: str, collector_number: str
    ) -> int:
        """Move same-reference English records to reference-only storage.

        This function deliberately does not return an identity match. It is
        called only while installing already-validated Japanese-name evidence,
        so a legacy English row with a coincident printed number cannot leak
        into the active Japanese catalogue.
        """
        rows = self._official_english_by_reference_unchecked(set_code, collector_number)
        for row in rows:
            self._archive_english_print_reference(row)
            self.connection.execute("DELETE FROM card_search WHERE print_id=?", (str(row["id"]),))
            self.connection.execute("DELETE FROM card_prints WHERE id=?", (row["id"],))
        return len(rows)

    def _japanese_by_reference(self, set_code: str, collector_number: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM japanese_prints WHERE set_code = ? AND collector_number = ?",
            (set_code, collector_number),
        ).fetchone()

    @staticmethod
    def _to_card(row: sqlite3.Row) -> CardPrint:
        return CardPrint(
            id=int(row["id"]),
            set_code=row["set_code"],
            collector_number=row["collector_number"],
            rarity=row["rarity"],
            english_name=row["english_name"],
            japanese_name=row["japanese_name"],
            finish=row["finish"],
            finish_raw=row["finish_raw"],
            aliases=tuple(json.loads(row["aliases_json"])),
            source=row["source"],
            source_url=row["source_url"],
        )
