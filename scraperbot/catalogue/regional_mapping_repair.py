"""Repair Japanese promo and Special Series mappings from regional-safe evidence."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from scraperbot.catalogue.fandom_source import FandomMappingSource
from scraperbot.catalogue.official_source import OfficialSourceError
from scraperbot.catalogue.repository import (
    HELD_UTILITY_PROMO_NAMES,
    PROMO_PRINT_SET_CODES,
    CatalogueRepository,
    is_region_specific_print_set,
)
from scraperbot.models import EnglishNameMapping, normalise_set_code


@dataclass(frozen=True, slots=True)
class RegionalMappingRepairResult:
    """Summary of a regional mapping rebuild."""

    set_codes: tuple[str, ...]
    fandom_pages_read: int
    fandom_failures: tuple[str, ...]
    archived_english_references: int
    removed_user_search_records: int
    restored_fandom: int
    mapped_by_japanese_name: int
    held_utility_prints: int
    unresolved: int


async def repair_region_specific_mappings(
    database: Path,
    set_codes: tuple[str, ...] | None = None,
    *,
    source: FandomMappingSource | None = None,
    progress: Callable[[str, str | None, int, str | None], None] | None = None,
) -> RegionalMappingRepairResult:
    """Rebuild regional Japanese mappings without equal-serial assumptions.

    Fresh Fandom set-page evidence is collected before changing the local
    database.  Existing direct Fandom evidence is retained as a fallback.
    Any mapping not supported by one of those sources, or by an exact
    Japanese-name match to that evidence, is withheld instead of guessed.
    """
    with CatalogueRepository(database) as catalogue:
        wanted = (
            tuple(sorted({normalise_set_code(code) for code in set_codes if code.strip()}))
            if set_codes
            else tuple(catalogue.region_specific_japanese_set_codes())
        )
        if not wanted:
            raise ValueError("No imported regional promo or Special Series set is available to repair.")
        if not all(is_region_specific_print_set(set_code) for set_code in wanted):
            raise ValueError("Only promo and Special Series set codes may be repaired here.")
        retained_mappings = [
            mapping
            for mapping in catalogue.direct_fandom_mappings(wanted)
            if mapping.set_code not in PROMO_PRINT_SET_CODES
            or mapping.source in {"fandom-cross-print", "fandom-japanese-promo"}
        ]
        reviewed = catalogue.reviewed_promo_mappings(wanted)

    source = source or FandomMappingSource()
    fetched_mappings: list[EnglishNameMapping] = []
    failures: list[str] = []
    pages_read = 0
    for set_code in wanted:
        if set_code in PROMO_PRINT_SET_CODES:
            try:
                title, mappings = await source.japanese_promo_mappings(set_code)
            except OfficialSourceError as error:
                failures.append(set_code)
                if progress:
                    progress(set_code, None, 0, str(error))
                continue
            fetched_mappings.extend(mappings)
            pages_read += 1
            if progress:
                progress(set_code, title, len(mappings), None)
            continue
        try:
            title, mappings = await source.mappings_for_set(set_code)
        except OfficialSourceError as error:
            failures.append(set_code)
            if progress:
                progress(set_code, None, 0, str(error))
            continue
        # Non-promo special-set pages remain direct Fandom evidence. Japanese
        # promo pages are handled separately above so this generic path never
        # treats an English D-PR serial as Japanese evidence.
        fetched_mappings.extend(
            mapping
            for mapping in mappings
            if mapping.set_code not in PROMO_PRINT_SET_CODES or mapping.source == "fandom-cross-print"
        )
        pages_read += 1
        if progress:
            progress(set_code, title, len(mappings), None)

    # Fandom mappings are keyed by Japanese serial. Re-reading a page may
    # duplicate already retained evidence; apply each reference only once.
    mappings_by_reference = {
        (mapping.set_code, mapping.collector_number): mapping
        for mapping in fetched_mappings
    }
    for mapping in retained_mappings:
        key = (mapping.set_code, mapping.collector_number)
        # A reviewed card-page or Japanese-promo link outranks a later
        # provisional set-list row for the same physical printing.
        if key not in mappings_by_reference or (
            mapping.source in {"fandom-cross-print", "fandom-japanese-promo", "promo-review"}
            and mapping.status in {"verified", "reviewed"}
        ):
            mappings_by_reference[key] = mapping
    with CatalogueRepository(database) as catalogue:
        archived, removed = catalogue.clear_region_specific_mappings_for_rebuild(wanted)
        restored = catalogue.apply_name_mappings(mappings_by_reference.values()).mapped
        catalogue.apply_name_mappings(reviewed)
        inferred = catalogue.unambiguous_fandom_name_mappings(wanted)
        name_matched = catalogue.apply_name_mappings(inferred).mapped
        remaining = catalogue.unmapped_japanese_prints(wanted)
        held_utility = sum(
            card.set_code in {"DPR", "CP"} and card.japanese_name in HELD_UTILITY_PROMO_NAMES
            for card in remaining
        )
        return RegionalMappingRepairResult(
            set_codes=wanted,
            fandom_pages_read=pages_read,
            fandom_failures=tuple(failures),
            archived_english_references=archived,
            removed_user_search_records=removed,
            restored_fandom=restored,
            mapped_by_japanese_name=name_matched,
            held_utility_prints=held_utility,
            unresolved=len(remaining) - held_utility,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repair Japanese D-PR/CP and D/DZ Special Series mappings from Fandom evidence."
    )
    parser.add_argument(
        "--set",
        dest="sets",
        action="append",
        help="Regional Japanese set code to repair; defaults to every imported D-PR/CP/D-SS/DZ-SS set.",
    )
    parser.add_argument("--database", type=Path, default=Path("data/catalogue.sqlite3"))
    args = parser.parse_args()
    try:
        result = asyncio.run(
            repair_region_specific_mappings(
                args.database,
                tuple(args.sets) if args.sets else None,
                progress=lambda set_code, title, count, error: print(
                    f"{set_code}: {error or f'{title}: {count} Fandom mappings'}", flush=True
                ),
            )
        )
    except (OfficialSourceError, ValueError) as error:
        parser.exit(2, f"Regional mapping repair failed: {error}\n")
    failures = ", ".join(result.fandom_failures) or "none"
    print(
        f"Reviewed {len(result.set_codes)} regional set(s); read {result.fandom_pages_read} Fandom page(s); "
        f"archived {result.archived_english_references} English references; removed "
        f"{result.removed_user_search_records} stale search records; restored {result.restored_fandom} "
        f"Fandom mappings; mapped {result.mapped_by_japanese_name} by exact Japanese name; held "
        f"{result.held_utility_prints} utility prints; {result.unresolved} print(s) remain unmapped; "
        f"Fandom failures: {failures}."
    )


if __name__ == "__main__":
    main()
