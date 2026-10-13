"""CLI for importing the official Japanese print master."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from pathlib import Path

from scraperbot.catalogue.japanese_source import OfficialJapaneseCardSource
from scraperbot.catalogue.official_source import OfficialExpansion, OfficialSourceError
from scraperbot.catalogue.repository import CatalogueRepository
from scraperbot.models import JapaneseCardPrint, normalise_set_code


# A new product page may publish more printings after its first import. Revisit
# recent D/DZ products on each explicit rebuild while older products retain
# their resume checkpoints.
RECENT_PRODUCT_RECHECK_COUNT = 6


def current_standard_expansions(expansions: list[OfficialExpansion]) -> list[OfficialExpansion]:
    """Keep D/DZ-era product pages, excluding V-series and older formats.

    Recognised D/DZ set codes remain eligible even after product IDs pass the
    three-digit range. Older entries without a parsed code retain the known
    201–999 range; annual PR pages start with 2021, after the V-era PR page.
    """
    return [
        expansion
        for expansion in expansions
        if (
            expansion.set_code is not None
            and normalise_set_code(expansion.set_code).startswith("D")
            and expansion.id >= 201
        )
        or (expansion.set_code is None and (201 <= expansion.id < 1000 or expansion.id >= 2021))
    ]


def recent_product_expansion_ids(expansions: list[OfficialExpansion]) -> set[int]:
    """Choose current D/DZ products whose official rows may still grow."""
    product_ids = sorted(
        (
            expansion.id
            for expansion in expansions
            if expansion.set_code and normalise_set_code(expansion.set_code).startswith("D")
        ),
        reverse=True,
    )
    return set(product_ids[:RECENT_PRODUCT_RECHECK_COUNT])


def current_standard_prints(cards: list[JapaneseCardPrint]) -> list[JapaneseCardPrint]:
    """Keep D/DZ print codes and the D-era campaign code CP only."""
    return [card for card in cards if card.set_code.startswith("D") or card.set_code == "CP"]


async def import_japanese_sets(
    database: Path,
    set_codes: list[str],
    *,
    all_sets: bool = False,
    progress: Callable[[int, int, str, int | None], None] | None = None,
    source: OfficialJapaneseCardSource | None = None,
) -> int:
    source = source or OfficialJapaneseCardSource()
    expansions = (
        current_standard_expansions(await source.list_expansions())
        if all_sets
        else await source.expansions_for_sets(set_codes)
    )
    recheck_ids = recent_product_expansion_ids(expansions) if all_sets else set()
    with CatalogueRepository(database) as catalogue:
        imported = 0
        for index, expansion in enumerate(expansions, start=1):
            label = expansion.set_code or expansion.title
            if all_sets and expansion.id not in recheck_ids and catalogue.has_japanese_expansion(expansion.id):
                if progress:
                    progress(index, len(expansions), label, None)
                continue
            cards = await source.cards_for_expansion(expansion)
            if all_sets:
                cards = current_standard_prints(cards)
            before = catalogue.japanese_count
            catalogue.import_japanese_many(cards)
            imported += catalogue.japanese_count - before
            catalogue.mark_japanese_expansion_imported(expansion.id)
            if progress:
                progress(index, len(expansions), label, len(cards))
    return imported


def main() -> None:
    parser = argparse.ArgumentParser(description="Import official Japanese Vanguard print data into the local master.")
    parser.add_argument("--set", dest="sets", action="append", default=[], help="Japanese set code, e.g. DZ-BT16")
    parser.add_argument(
        "--all", dest="all_sets", action="store_true", help="Import all D/DZ-era Japanese Standard-format products"
    )
    parser.add_argument("--list-sets", action="store_true", help="List official Japanese products and exit")
    parser.add_argument("--database", type=Path, default=Path("data/catalogue.sqlite3"))
    args = parser.parse_args()
    if args.list_sets:
        try:
            expansions = asyncio.run(OfficialJapaneseCardSource().list_expansions())
        except OfficialSourceError as error:
            parser.exit(2, f"Japanese product listing failed: {error}\n")
        print("\n".join(f"{expansion.set_code or 'unrecognised'}\t{expansion.title}" for expansion in expansions))
        return
    if not args.sets and not args.all_sets:
        parser.error("supply at least one --set or choose --all")
    try:
        count = asyncio.run(
            import_japanese_sets(
                args.database,
                args.sets,
                all_sets=args.all_sets,
                progress=lambda index, total, label, cards: print(
                    f"[{index}/{total}] {label}: "
                    f"{'already imported' if cards is None else f'{cards} Japanese prints'}",
                    flush=True,
                ),
            )
        )
    except OfficialSourceError as error:
        parser.exit(2, f"Japanese import failed: {error}\n")
    print(f"Imported {count} official Japanese prints into {args.database}.")


if __name__ == "__main__":
    main()
