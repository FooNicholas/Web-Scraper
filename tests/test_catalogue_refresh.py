import asyncio
from pathlib import Path

from scraperbot.catalogue.fandom_importer import FandomBatchItem, import_all_fandom_mappings
from scraperbot.catalogue.japanese_importer import import_japanese_sets
from scraperbot.catalogue.official_source import OfficialExpansion, OfficialSourceError
from scraperbot.catalogue.promo_catalogue_importer import PromoCatalogueImportResult
from scraperbot.catalogue.refresh import refresh_catalogue
from scraperbot.catalogue.repository import CatalogueRepository, MappingImportResult
from scraperbot.models import EnglishNameMapping, JapaneseCardPrint


def test_refresh_orchestrates_only_approved_catalogue_sources(tmp_path: Path) -> None:
    calls: list[str] = []
    progress: list[str] = []

    async def official_importer(_: Path, __: list[str], **kwargs: object) -> int:
        calls.append("official")
        assert kwargs["all_sets"] is True
        kwargs["progress"](1, 1, "DZBT99", 3)  # type: ignore[operator]
        return 3

    async def japanese_importer(_: Path, __: list[str], **kwargs: object) -> int:
        calls.append("japanese")
        assert kwargs["all_sets"] is True
        kwargs["progress"](1, 1, "DZBT99", 4)  # type: ignore[operator]
        return 4

    def official_linker(_: Path) -> tuple[int, int]:
        calls.append("link")
        return 5, 4

    async def official_promo_importer(_: Path, **kwargs: object) -> int:
        calls.append("official-promos")
        return 9

    def promo_mapping_repairer(_: Path):
        calls.append("promo-repair")

    async def fandom_importer(_: Path, **kwargs: object) -> list[FandomBatchItem]:
        calls.append("fandom")
        item = FandomBatchItem("DZBT99", "Example", MappingImportResult(6, 0, 0, 0))
        kwargs["progress"](1, 1, item)  # type: ignore[operator]
        return [item]

    def name_deriver(_: Path) -> MappingImportResult:
        calls.append("derive")
        return MappingImportResult(7, 2, 0, 0)

    async def promo_importer(_: Path, **kwargs: object) -> PromoCatalogueImportResult:
        calls.append("yuyutei")
        assert kwargs["all_pages"] is True
        assert kwargs["refresh"] is True
        kwargs["progress"](1, 1, "dpromo-100", 8)  # type: ignore[operator]
        return PromoCatalogueImportResult(1, 0, 0, 8)

    result = asyncio.run(
        refresh_catalogue(
            tmp_path / "catalogue.sqlite3",
            refresh_promos=True,
            progress=progress.append,
            official_importer=official_importer,
            japanese_importer=japanese_importer,
            official_promo_importer=official_promo_importer,
            promo_mapping_repairer=promo_mapping_repairer,
            official_linker=official_linker,
            fandom_importer=fandom_importer,
            name_deriver=name_deriver,
            promo_importer=promo_importer,
        )
    )

    assert calls == ["official", "japanese", "official-promos", "link", "fandom", "derive", "yuyutei", "promo-repair"]
    assert result.official_promo_identities == 9
    assert result.english_prints_imported == 3
    assert result.japanese_prints_imported == 4
    assert result.official_link_candidates == 5
    assert result.official_links_applied == 4
    assert result.fandom_mappings_applied == 6
    assert result.derived_name_mappings == 7
    assert result.yuyutei_promo_result.entries_imported == 8
    assert result.regional_repair is None
    assert any(message.startswith("Fandom") for message in progress)


def test_repeated_new_set_refresh_imports_later_japanese_print_and_english_search_name(
    tmp_path: Path,
) -> None:
    database = tmp_path / "catalogue.sqlite3"

    class JapaneseSource:
        include_parallel = False

        async def list_expansions(self) -> list[OfficialExpansion]:
            return [OfficialExpansion(1200, "DZ-BT99", "New set")]

        async def cards_for_expansion(self, _: OfficialExpansion) -> list[JapaneseCardPrint]:
            cards = [JapaneseCardPrint("DZ-BT99", "001", "RRR", "新しい竜", "https://jp/card/001")]
            if self.include_parallel:
                cards.append(JapaneseCardPrint("DZ-BT99", "FFR01", "FFR", "新しい竜", "https://jp/card/ffr01"))
            return cards

    class FandomSource:
        unavailable = True

        async def mappings_for_set(self, set_code: str) -> tuple[str, list[EnglishNameMapping]]:
            assert set_code == "DZBT99"
            if self.unavailable:
                raise OfficialSourceError("Fandom page is not available yet")
            return "New set list", [
                EnglishNameMapping(
                    "DZ-BT99", "001", "RRR", "New Dragon", "fandom", "https://wiki/new-set"
                )
            ]

    japanese_source = JapaneseSource()
    fandom_source = FandomSource()

    async def official_importer(_: Path, __: list[str], **___: object) -> int:
        return 0

    async def japanese_importer(path: Path, codes: list[str], **kwargs: object) -> int:
        return await import_japanese_sets(path, codes, source=japanese_source, **kwargs)  # type: ignore[arg-type]

    async def official_promo_importer(_: Path, **__: object) -> int:
        return 0

    async def fandom_importer(path: Path, **kwargs: object) -> list[FandomBatchItem]:
        return await import_all_fandom_mappings(path, source=fandom_source, **kwargs)  # type: ignore[arg-type]

    async def promo_importer(_: Path, **__: object) -> PromoCatalogueImportResult:
        return PromoCatalogueImportResult(0, 0, 0, 0)

    def refresh() -> object:
        return asyncio.run(
            refresh_catalogue(
                database,
                official_importer=official_importer,
                japanese_importer=japanese_importer,
                official_promo_importer=official_promo_importer,
                official_linker=lambda _: (0, 0),
                fandom_importer=fandom_importer,
                promo_importer=promo_importer,
            )
        )

    first = refresh()
    assert first.japanese_prints_imported == 1  # type: ignore[attr-defined]
    assert first.fandom_failed_sets == ("DZBT99",)  # type: ignore[attr-defined]
    with CatalogueRepository(database) as catalogue:
        assert catalogue.lookup_japanese_serial("DZ-BT99/001") is not None
        assert not catalogue.search("new dragon", japanese_only=True)

    fandom_source.unavailable = False
    second = refresh()
    assert second.japanese_prints_imported == 0  # type: ignore[attr-defined]
    assert second.fandom_failed_sets == ()  # type: ignore[attr-defined]
    with CatalogueRepository(database) as catalogue:
        assert [card.collector_number for card in catalogue.search("new dragon", japanese_only=True)] == ["001"]

    japanese_source.include_parallel = True
    third = refresh()
    assert third.japanese_prints_imported == 1  # type: ignore[attr-defined]
    with CatalogueRepository(database) as catalogue:
        assert {card.collector_number for card in catalogue.search("new dragon", japanese_only=True)} == {
            "001", "FFR01"
        }


def test_refresh_continues_to_japanese_mappings_when_english_site_is_unavailable(tmp_path: Path) -> None:
    calls = []

    async def unavailable_english(*_: object, **__: object) -> int:
        raise OfficialSourceError("English site timed out")

    async def japanese_importer(*_: object, **__: object) -> int:
        calls.append("japanese")
        return 1

    async def official_promo_importer(*_: object, **__: object) -> int:
        return 0

    async def fandom_importer(*_: object, **__: object) -> list[FandomBatchItem]:
        calls.append("fandom")
        return []

    async def promo_importer(*_: object, **__: object) -> PromoCatalogueImportResult:
        return PromoCatalogueImportResult(0, 0, 0, 0)

    result = asyncio.run(refresh_catalogue(
        tmp_path / "catalogue.sqlite3",
        official_importer=unavailable_english,
        japanese_importer=japanese_importer,
        official_promo_importer=official_promo_importer,
        official_linker=lambda _: (0, 0),
        fandom_importer=fandom_importer,
        promo_importer=promo_importer,
    ))
    assert calls == ["japanese", "fandom"]
    assert result.english_source_error == "English site timed out"
    assert result.japanese_prints_imported == 1
