"""One-command, permission-respecting refresh of the local card catalogue."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from scraperbot.catalogue.fandom_importer import import_all_fandom_mappings
from scraperbot.catalogue.japanese_importer import import_japanese_sets
from scraperbot.catalogue.japanese_name_deriver import derive_japanese_names
from scraperbot.catalogue.official_importer import import_official_sets
from scraperbot.catalogue.official_japanese_linker import link_official_english_names
from scraperbot.catalogue.official_source import OfficialSourceError
from scraperbot.catalogue.official_promos import import_official_promos
from scraperbot.catalogue.promo_mapping_repair import repair_promo_mappings
from scraperbot.catalogue.promo_catalogue_importer import (
    PromoCatalogueImportResult,
    import_yuyutei_promo_pages,
)
from scraperbot.catalogue.regional_mapping_repair import (
    RegionalMappingRepairResult,
    repair_region_specific_mappings,
)
from scraperbot.catalogue.status import catalogue_status
from scraperbot.catalogue.yuyutei_promo_source import PromoCatalogueSourceError


@dataclass(frozen=True, slots=True)
class CatalogueRefreshResult:
    """Summary of a completed refresh from approved catalogue sources."""

    english_prints_imported: int
    english_source_error: str | None
    japanese_prints_imported: int
    official_promo_identities: int
    official_link_candidates: int
    official_links_applied: int
    fandom_mappings_applied: int
    fandom_failed_sets: tuple[str, ...]
    derived_name_mappings: int
    yuyutei_promo_result: PromoCatalogueImportResult
    regional_repair: RegionalMappingRepairResult | None


async def refresh_catalogue(
    database: Path,
    *,
    refresh_promos: bool = False,
    repair_regional: bool = False,
    progress: Callable[[str], None] | None = None,
    official_importer: Callable[..., object] = import_official_sets,
    japanese_importer: Callable[..., object] = import_japanese_sets,
    official_promo_importer: Callable[..., object] = import_official_promos,
    promo_mapping_repairer: Callable[..., object] = repair_promo_mappings,
    official_linker: Callable[[Path], tuple[int, int]] = link_official_english_names,
    fandom_importer: Callable[..., object] = import_all_fandom_mappings,
    name_deriver: Callable[[Path], object] = derive_japanese_names,
    promo_importer: Callable[..., object] = import_yuyutei_promo_pages,
    regional_repairer: Callable[..., object] = repair_region_specific_mappings,
) -> CatalogueRefreshResult:
    """Refresh approved catalogue sources without visiting protected stores.

    The workflow deliberately excludes Card Rush, VanHappy, BigWeb, and any
    other retailer connector. Their price checks remain user-triggered. A
    store that requires Cloudflare access stays out of this workflow unless a
    permitted integration route is added separately.
    """
    report = progress or (lambda _: None)
    report("Refreshing official English catalogue…")
    english_source_error = None
    try:
        english_prints = await official_importer(
            database,
            [],
            all_sets=True,
            progress=lambda index, total, label, count: report(
                f"English [{index}/{total}] {label}: "
                f"{'already imported' if count is None else f'{count} prints'}"
            ),
        )
    except OfficialSourceError as error:
        english_prints = 0
        english_source_error = str(error)
        report(f"Official English catalogue unavailable: {error}. Continuing with Japanese and Fandom sources…")
    report("Refreshing official Japanese catalogue…")
    japanese_prints = await japanese_importer(
        database,
        [],
        all_sets=True,
        progress=lambda index, total, label, count: report(
            f"Japanese [{index}/{total}] {label}: "
            f"{'already imported' if count is None else f'{count} prints'}"
        ),
    )
    report("Verifying Japanese promo identities against the official PR table…")
    official_promos = await official_promo_importer(database, progress=report)
    report("Keeping official English print data as reference-only…")
    link_candidates, links_applied = official_linker(database)
    report("Mapping still-unmapped Japanese sets from Fandom…")
    fandom_items = await fandom_importer(
        database,
        progress=lambda index, total, item: report(
            f"Fandom [{index}/{total}] {item.set_code}: "
            f"{item.error or f'{item.result.mapped} mapped'}"
        ),
    )
    typed_fandom_items = tuple(fandom_items)
    fandom_mapped = sum(item.result.mapped for item in typed_fandom_items if item.result)
    fandom_failures = tuple(item.set_code for item in typed_fandom_items if item.error)
    report("Deriving only unambiguous Japanese-name reprint mappings…")
    derived = name_deriver(database)
    report("Refreshing saved Yuyu-Tei D-Promo catalogue locations…")
    promos = await promo_importer(
        database,
        all_pages=True,
        refresh=refresh_promos,
        progress=lambda index, total, page, count: report(
            f"Yuyu-Tei [{index}/{total}] {page}: "
            f"{'already imported' if count is None else f'{count} entries'}"
        ),
    )
    regional_result = None
    if repair_regional:
        report("Rebuilding regional promo and Special Series mappings from Fandom…")
        regional_result = await regional_repairer(
            database,
            progress=lambda set_code, title, count, error: report(
                f"Regional {set_code}: {error or f'{title}: {count} mappings'}"
            ),
        )
    report("Rebuilding promo English mappings from verified Japanese identities…")
    promo_mapping_repairer(database)
    return CatalogueRefreshResult(
        english_prints_imported=int(english_prints),
        english_source_error=english_source_error,
        japanese_prints_imported=int(japanese_prints),
        official_promo_identities=int(official_promos),
        official_link_candidates=link_candidates,
        official_links_applied=links_applied,
        fandom_mappings_applied=fandom_mapped,
        fandom_failed_sets=fandom_failures,
        derived_name_mappings=derived.mapped,
        yuyutei_promo_result=promos,
        regional_repair=regional_result,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Refresh the local catalogue from approved official, Fandom, and Yuyu-Tei sources."
    )
    parser.add_argument("--database", type=Path, default=Path("data/catalogue.sqlite3"))
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Run the network refresh. Without this flag, print the current local status only.",
    )
    parser.add_argument(
        "--refresh-promos",
        action="store_true",
        help="Re-read every previously imported Yuyu-Tei D-Promo range instead of checking only new ranges.",
    )
    parser.add_argument(
        "--repair-regional",
        action="store_true",
        help="Rebuild all D-PR/CP and D/DZ Special Series mappings from Fandom Japanese-set pages.",
    )
    args = parser.parse_args()
    if not args.apply:
        status = catalogue_status(args.database)
        print(
            f"Catalogue status: {status.searchable_japanese_prints}/{status.japanese_prints} Japanese prints "
            f"are English-searchable; {status.unresolved_playable_promo_prints} playable promos need review."
        )
        print("Run again with --apply to contact the approved catalogue sources.")
        return
    try:
        result = asyncio.run(
            refresh_catalogue(
                args.database,
                refresh_promos=args.refresh_promos,
                repair_regional=args.repair_regional,
                progress=print,
            )
        )
    except (OfficialSourceError, PromoCatalogueSourceError, ValueError) as error:
        parser.exit(2, f"Catalogue refresh failed: {error}\n")
    print(
        f"Imported {result.english_prints_imported} English and {result.japanese_prints_imported} Japanese prints; "
        f"kept {result.official_link_candidates} official references without Japanese serial links; "
        f"applied {result.fandom_mappings_applied} Fandom mappings; derived {result.derived_name_mappings} mappings; "
        f"saved {result.yuyutei_promo_result.entries_imported} Yuyu-Tei promo entries."
    )
    if result.fandom_failed_sets:
        print("Fandom pages needing review: " + ", ".join(result.fandom_failed_sets))
    if result.english_source_error:
        print("Official English catalogue could not be refreshed: " + result.english_source_error)
    if result.regional_repair:
        print(
            f"Regional rebuild: {result.regional_repair.unresolved} playable print(s) still unmapped; "
            f"{result.regional_repair.held_utility_prints} utility print(s) held."
        )


if __name__ == "__main__":
    main()
