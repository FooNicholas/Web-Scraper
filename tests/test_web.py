import asyncio
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace

from scraperbot.catalogue.repository import CatalogueRepository
from scraperbot.connectors.base import StoreConnector
from scraperbot.models import Availability, CardPrint, Finish, MatchConfidence, StoreOffer
from scraperbot.services.comparison import ComparisonService
from scraperbot.web import INDEX_HTML, LocalPriceCheckWeb


class FixedConnector(StoreConnector):
    store_id = "example"
    store_name = "Example Store"

    async def search(self, _: CardPrint) -> list[StoreOffer]:
        return [
            StoreOffer(
                store_id=self.store_id,
                store_name=self.store_name,
                raw_name="Japanese listing",
                price_yen=1500,
                price_display="¥1,500",
                availability=Availability.IN_STOCK,
                listing_url="https://example.test/listing",
                match_confidence=MatchConfidence.EXACT_JAPANESE_NAME,
                stock_count=3,
            )
        ]


class SlowConnector(StoreConnector):
    store_id = "slow"
    store_name = "Slow Store"

    def __init__(self, started: Event) -> None:
        self.started = started

    async def search(self, _: CardPrint) -> list[StoreOffer]:
        self.started.set()
        await asyncio.sleep(0.4)
        return []


def test_local_web_search_and_comparison_share_the_catalogue(tmp_path: Path) -> None:
    with CatalogueRepository(tmp_path / "catalogue.sqlite3") as catalogue:
        card = catalogue.upsert(
            CardPrint(
                "DZ-BT16",
                "FFR02",
                "FFR",
                "Exacerbate Dragon",
                japanese_name="エグザサベイト・ドラゴン",
                source="test",
                finish=Finish.HOLO,
                finish_raw="H仕様",
            )
        )
        app = LocalPriceCheckWeb(catalogue, ComparisonService([FixedConnector()]))

        search = app.search("exacerbate rarity:FFR finish:holo")
        assert search["cards"] == [
            {
                "id": card.id,
                "english_name": "Exacerbate Dragon",
                "japanese_name": "エグザサベイト・ドラゴン",
                "set_code": "DZBT16",
                "collector_number": "FFR02",
                "rarity": "FFR",
                "finish": "holo",
                "finish_raw": "H仕様",
                "display_code": "DZBT16/FFR02 · FFR · H仕様",
                "source": "test",
                "family_print_count": 1,
            }
        ]
        assert search["rarities"] == ["FFR"]
        assert search["finishes"] == ["holo"]

        comparison = asyncio.run(app.compare(card.id or 0))
        assert comparison["offers"][0]["store_name"] == "Example Store"
        assert comparison["offers"][0]["match_confidence"] == "exact_japanese_name"
        assert comparison["offers"][0]["stock_count"] == 3
        assert comparison["family_print_count"] == 1
        assert comparison["cached"] is False
        assert comparison["duration_ms"] >= 0
        timing = comparison["store_timings"]
        assert len(timing) == 1
        assert timing[0]["store_id"] == "example"
        assert timing[0]["store_name"] == "Example Store"
        assert timing[0]["outcome"] == "offers"
        assert timing[0]["elapsed_ms"] >= 0

        lowest = asyncio.run(app.compare_family(card.id or 0))
        assert lowest["print_count"] == 1
        assert lowest["offers"][0]["card"]["id"] == card.id


def test_local_web_hides_english_only_prints(tmp_path: Path) -> None:
    with CatalogueRepository(tmp_path / "catalogue.sqlite3") as catalogue:
        english_only = catalogue.upsert(
            CardPrint("DZ-BT16", "010", "RRR", "English-only printing", source="test")
        )
        app = LocalPriceCheckWeb(catalogue, ComparisonService([]))

        assert app.search("english only")["cards"] == []
        try:
            asyncio.run(app.compare(english_only.id or 0))
        except LookupError as error:
            assert "Japanese-market" in str(error)
        else:
            raise AssertionError("English-only prints must not be comparable through the web API.")


def test_local_web_rejects_an_empty_search(tmp_path: Path) -> None:
    with CatalogueRepository(tmp_path / "catalogue.sqlite3") as catalogue:
        app = LocalPriceCheckWeb(catalogue, ComparisonService([]))
        try:
            app.search("   ")
        except ValueError as error:
            assert str(error) == "Enter a card name to search."
        else:
            raise AssertionError("An empty web search should fail clearly.")


def test_local_web_shows_more_than_twelve_shared_name_prints(tmp_path: Path) -> None:
    with CatalogueRepository(tmp_path / "catalogue.sqlite3") as catalogue:
        for number in range(13):
            catalogue.upsert(
                CardPrint(
                    "D-PR",
                    str(1200 + number),
                    "PR",
                    "Energy Generator",
                    japanese_name="エネルギージェネレーター",
                    source="test",
                )
            )
        app = LocalPriceCheckWeb(catalogue, ComparisonService([]))

        results = app.search("energy generator")["cards"]
        assert len(results) == 13
        assert {card["collector_number"] for card in results} == {
            str(1200 + number) for number in range(13)
        }


def test_local_web_compares_the_lowest_price_across_verified_reprints(tmp_path: Path) -> None:
    with CatalogueRepository(tmp_path / "catalogue.sqlite3") as catalogue:
        first = catalogue.upsert(
            CardPrint("DZ-BT01", "001", "RRR", "Example Card", japanese_name="日本語名", source="test")
        )
        catalogue.upsert(
            CardPrint("DZ-BT02", "002", "FFR", "Example Card", japanese_name="日本語名", source="test")
        )
        app = LocalPriceCheckWeb(catalogue, ComparisonService([FixedConnector()]))

        search = app.search("example card")
        family = asyncio.run(app.compare_family(first.id or 0))

    assert {card["family_print_count"] for card in search["cards"]} == {2}
    assert family["print_count"] == 2
    assert len(family["offers"]) == 2
    assert {offer["card"]["collector_number"] for offer in family["offers"]} == {"001", "002"}


def test_search_is_not_blocked_by_a_slow_store_comparison(tmp_path: Path) -> None:
    with CatalogueRepository(tmp_path / "catalogue.sqlite3") as catalogue:
        card = catalogue.upsert(
            CardPrint("DZ-BT16", "001", "RRR", "Example Card", japanese_name="日本語名", source="test")
        )
        started = Event()
        app = LocalPriceCheckWeb(catalogue, ComparisonService([SlowConnector(started)]))
        comparison_thread = Thread(target=lambda: asyncio.run(app.compare(card.id or 0)), daemon=True)
        try:
            comparison_thread.start()
            assert started.wait(timeout=1)
            started_at = monotonic()
            assert app.search("example")["cards"]
            assert monotonic() - started_at < 0.2
            comparison_thread.join(timeout=2)
            assert not comparison_thread.is_alive()
        finally:
            comparison_thread.join(timeout=2)


def test_catalogue_rebuild_keeps_the_old_catalogue_live_until_a_staged_refresh_completes(tmp_path: Path) -> None:
    database = tmp_path / "catalogue.sqlite3"
    catalogue = CatalogueRepository(database)
    catalogue.upsert(CardPrint("DZ-BT16", "001", "RRR", "Old Card", japanese_name="古いカード", source="test"))
    entered_refresh = Event()
    allow_refresh_to_finish = Event()

    async def refresh_copy(path: Path, **kwargs: object) -> object:
        assert kwargs["repair_regional"] is True
        kwargs["progress"]("Reading approved catalogue sources…")  # type: ignore[operator]
        entered_refresh.set()
        assert await asyncio.to_thread(allow_refresh_to_finish.wait, 2)
        with CatalogueRepository(path) as staged:
            staged.upsert(
                CardPrint("DZ-BT17", "001", "RRR", "New Card", japanese_name="新しいカード", source="test")
            )
        return SimpleNamespace(english_prints_imported=1, japanese_prints_imported=1, fandom_failed_sets=())

    app = LocalPriceCheckWeb(catalogue, ComparisonService([]), catalogue_refresher=refresh_copy)
    try:
        assert app.start_catalogue_rebuild()["status"] == "running"
        assert entered_refresh.wait(timeout=2)
        assert app.search("old")["cards"]
        assert not app.search("new")["cards"]

        allow_refresh_to_finish.set()
        deadline = monotonic() + 3
        while app.catalogue_rebuild_status()["status"] == "running" and monotonic() < deadline:
            sleep(0.01)

        status = app.catalogue_rebuild_status()
        assert status["status"] == "completed"
        assert status["english_prints_imported"] == 1
        assert app.search("new")["cards"]
        assert app.search("old")["cards"]
    finally:
        allow_refresh_to_finish.set()
        app.catalogue.close()


def test_browser_ui_separates_print_aggregation_from_selected_print_sorting() -> None:
    assert "Aggregate card prints" in INDEX_HTML
    assert "Compare prices across " in INDEX_HTML
    assert "Sorting displayed offers" in INDEX_HTML
    assert "Lowest price" in INDEX_HTML
    assert "Highest price" in INDEX_HTML
    assert "☰↑" in INDEX_HTML
    assert "☰↓" in INDEX_HTML
    assert "renderCurrentComparison" in INDEX_HTML
    assert "Activate to sort" in INDEX_HTML


def test_browser_ui_exposes_full_store_timing_details_after_a_comparison() -> None:
    assert "view timings" in INDEX_HTML
    assert "store_timings" in INDEX_HTML
    assert "comparisonStatus(data)" in INDEX_HTML


def test_browser_ui_only_checks_or_installs_catalogue_updates_after_a_user_action() -> None:
    assert "Check catalogue update" in INDEX_HTML
    assert "Install catalogue update" in INDEX_HTML
    assert "checkCatalogueUpdate" in INDEX_HTML
    assert "installCatalogueUpdate" in INDEX_HTML
    assert "fetch('/api/catalogue-update',{method:'POST',headers:{'X-JP-Price-Checker':'1'}})" in INDEX_HTML


def test_browser_ui_offers_an_explicit_safe_catalogue_rebuild() -> None:
    assert "Rebuild catalogue from sources" in INDEX_HTML
    assert "Your current catalogue stays usable" in INDEX_HTML
    assert "fetch('/api/catalogue-rebuild',{method:'POST',headers:{'X-JP-Price-Checker':'1'}})" in INDEX_HTML
    assert "pollCatalogueRebuild" in INDEX_HTML


def test_browser_ui_gives_each_desktop_panel_an_independent_scroll_container() -> None:
    assert "--pane-height:calc(100vh - 250px)" in INDEX_HTML
    assert ".result-list { min-height:0; overflow-y:auto; overscroll-behavior:contain;" in INDEX_HTML
    assert "#comparison { min-height:0; flex:1; overflow-y:auto; overscroll-behavior:contain;" in INDEX_HTML
    assert ".result-list,#comparison { overflow:visible;" in INDEX_HTML
