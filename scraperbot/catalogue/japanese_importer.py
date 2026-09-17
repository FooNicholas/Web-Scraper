"""CLI for importing the official Japanese print master."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from pathlib import Path

from scraperbot.catalogue.japanese_source import OfficialJapaneseCardSource
from scraperbot.catalogue.official_source import OfficialExpansion, OfficialSourceError
from scraperbot.catalogue.repository import CatalogueRepository
from scraperbot.models import JapaneseCardPrint


def current_standard_expansions(expansions: list[OfficialExpansion]) -> list[OfficialExpansion]:
    """Keep D/DZ-era product pages, excluding V-series and older formats.

    Bushiroad's public catalogue gives D-SD01 the product id 201 and current
    D/DZ product IDs occupy the three-digit range starting at 201 and continue
    to grow as new products are published. Annual PR pages use a four-digit
    year, so 2021 onward remains part of the current Standard-format catalogue
    while the 2020 V-era PR page stays excluded.
    """
    return [
        expansion
        for expansion in expansions
        if 201 <= expansion.id < 1000 or expansion.id >= 2021
    ]


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
    with CatalogueRepository(database) as catalogue:
        imported = 0
        for index, expansion in enumerate(expansions, start=1):
            label = expansion.set_code or expansion.title
            if all_sets and catalogue.has_japanese_expansion(expansion.id):
                if progress:
                    progress(index, len(expansions), label, None)
                continue
            cards = await source.cards_for_expansion(expansion)
            if all_sets:
                cards = current_standard_prints(cards)
            imported += catalogue.import_japanese_many(cards)
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
