from pathlib import Path

from scraperbot.catalogue.repository import CatalogueRepository
from scraperbot.models import CardPrint, EnglishNameMapping, JapaneseCardPrint


def test_fandom_mapping_promotes_a_japanese_master_print_to_name_search(tmp_path: Path) -> None:
    with CatalogueRepository(tmp_path / "catalogue.sqlite3") as catalogue:
        catalogue.import_japanese_many(
            [
                JapaneseCardPrint(
                    set_code="DZ-BT16",
                    collector_number="002",
                    rarity="",
                    japanese_name="エグザサベイト・ドラゴン",
                    source_url="https://cf-vanguard.com/cardlist/?cardno=DZ-BT16/002",
                ),
                JapaneseCardPrint(
                    set_code="DZ-BT16",
                    collector_number="FFR02",
                    rarity="FFR",
                    japanese_name="エグザサベイト・ドラゴン",
                    source_url="https://cf-vanguard.com/cardlist/?cardno=DZ-BT16/FFR02",
                ),
            ]
        )
        result = catalogue.apply_name_mappings(
            [
                EnglishNameMapping(
                    set_code="DZ-BT16",
                    collector_number="002",
                    rarity="RRR",
                    english_name="Exacerbate Dragon",
                    source="fandom",
                    source_url="https://cardfight.fandom.com/wiki/DZ_Booster_Set_16",
                    aliases=("exzabite dragon",),
                ),
                EnglishNameMapping(
                    set_code="DZ-BT16",
                    collector_number="FFR02",
                    rarity="FFR",
                    english_name="Exacerbate Dragon",
                    source="fandom",
                    source_url="https://cardfight.fandom.com/wiki/DZ_Booster_Set_16",
                ),
            ]
        )
        assert result.mapped == 2
        assert catalogue.japanese_count == 2
        assert catalogue.unmapped_japanese_count("DZBT16") == 0
        card = catalogue.search("exzabite ffr", rarity="FFR")[0]
        assert card.english_name == "Exacerbate Dragon"
        assert card.japanese_name == "エグザサベイト・ドラゴン"


def test_fandom_name_wins_without_using_an_equal_english_serial(tmp_path: Path) -> None:
    with CatalogueRepository(tmp_path / "catalogue.sqlite3") as catalogue:
        catalogue.upsert(
            CardPrint(
                set_code="DZ-BT16",
                collector_number="002",
                rarity="RRR",
                english_name="Official Exacerbate Dragon",
                source="official-english",
            )
        )
        catalogue.import_japanese_many(
            [
                JapaneseCardPrint(
                    set_code="DZ-BT16",
                    collector_number="002",
                    rarity="RRR",
                    japanese_name="エグザサベイト・ドラゴン",
                    source_url="https://cf-vanguard.com/cardlist/?cardno=DZ-BT16/002",
                )
            ]
        )
        result = catalogue.apply_name_mappings(
            [
                EnglishNameMapping(
                    set_code="DZ-BT16",
                    collector_number="002",
                    rarity="RRR",
                    english_name="Community Exacerbate Dragon",
                    source="fandom",
                    source_url="https://cardfight.fandom.com/wiki/DZ_Booster_Set_16",
                )
            ]
        )
        assert result.preserved_official == 0
        card = catalogue.search("community exacerbate", japanese_only=True)[0]
        assert card.japanese_name == "エグザサベイト・ドラゴン"
        assert all(
            candidate.english_name != "Official Exacerbate Dragon"
            for candidate in catalogue.search("official exacerbate", japanese_only=True)
        )
        archived = catalogue.connection.execute(
            "SELECT english_name FROM english_print_references WHERE set_code='DZBT16' AND collector_number='002'"
        ).fetchone()
        assert archived["english_name"] == "Official Exacerbate Dragon"


def test_official_english_sync_never_uses_an_equal_printed_number(tmp_path: Path) -> None:
    with CatalogueRepository(tmp_path / "catalogue.sqlite3") as catalogue:
        catalogue.upsert(
            CardPrint(
                set_code="D-BT01",
                collector_number="001",
                rarity="RRR",
                english_name="Official Card Name",
                source="official-english",
                source_url="https://en.cf-vanguard.com/cardlist/?cardno=D-BT01/001EN",
            )
        )
        catalogue.import_japanese_many(
            [JapaneseCardPrint("D-BT01", "001", "RRR", "公式日本語名", "https://official/001")]
        )
        result = catalogue.apply_name_mappings(catalogue.official_english_name_mappings())
        assert result.mapped == 0
        assert catalogue.unmapped_japanese_count("DBT01") == 1

        catalogue.apply_name_mappings(
            [
                EnglishNameMapping(
                    "D-BT01", "001", "RRR", "Community Name", "fandom", "https://fandom/set"
                )
            ]
        )
        card = catalogue.search("community name", japanese_only=True)[0]
        assert card.japanese_name == "公式日本語名"
        assert not catalogue.search("official card name", japanese_only=True)


def test_global_repair_archives_all_english_serial_equality_claims(tmp_path: Path) -> None:
    """A shared number is reference-only even for ordinary booster sets."""
    with CatalogueRepository(tmp_path / "catalogue.sqlite3") as catalogue:
        catalogue.import_japanese_many(
            [JapaneseCardPrint("D-BT09", "Re06", "RE", "バーニングフレイル・ドラゴン", "https://jp/re06")]
        )
        catalogue.connection.execute(
            """
            INSERT INTO english_name_mappings (
                japanese_print_id, english_name, aliases_json, mapping_source, mapping_source_url, status
            ) SELECT id, 'Trickmoon', '[]', 'official-english', 'https://en/re06', 'official'
            FROM japanese_prints WHERE set_code='DBT09' AND collector_number='RE06'
            """
        )
        catalogue.connection.commit()
        identities, links = catalogue.rebuild_card_identities()
        assert identities == 0
        assert links == 0

        removed, links = catalogue.archive_english_reference_number_mappings()
        assert removed == 1
        assert links == 0
        assert not catalogue.search("trickmoon", japanese_only=True)
        archived = catalogue.connection.execute(
            "SELECT english_name, archived_reason FROM archived_english_reference_mappings"
        ).fetchone()
        assert dict(archived) == {"english_name": "Trickmoon", "archived_reason": "english_serial_not_identity"}


def test_repository_lists_only_japanese_sets_with_missing_name_mappings(tmp_path: Path) -> None:
    with CatalogueRepository(tmp_path / "catalogue.sqlite3") as catalogue:
        catalogue.import_japanese_many(
            [
                JapaneseCardPrint("D-BT01", "001", "RRR", "Mapped", "https://official/001"),
                JapaneseCardPrint("D-BT02", "001", "RRR", "Unmapped", "https://official/002"),
            ]
        )
        catalogue.apply_name_mappings(
            [EnglishNameMapping("D-BT01", "001", "RRR", "Mapped", "fandom", "https://fandom/1")]
        )
        assert catalogue.unmapped_japanese_set_codes() == ["DBT02"]


def test_reviewed_card_page_name_survives_a_weaker_set_list_conflict(tmp_path: Path) -> None:
    with CatalogueRepository(tmp_path / "catalogue.sqlite3") as catalogue:
        catalogue.import_japanese_many(
            [JapaneseCardPrint("DZ-BT17", "001", "RRR", "同じ日本語名", "https://jp/001")]
        )
        catalogue.apply_name_mappings(
            [EnglishNameMapping("DZ-BT17", "001", "RRR", "Verified Name",
                                "fandom-cross-print", "https://wiki/card", status="verified")]
        )
        result = catalogue.apply_name_mappings(
            [EnglishNameMapping("DZ-BT17", "001", "RRR", "Conflicting Name",
                                "fandom", "https://wiki/set")]
        )

        assert result.mapped == 0
        assert [card.collector_number for card in catalogue.search("verified name", japanese_only=True)] == ["001"]
        assert not catalogue.search("conflicting name", japanese_only=True)
        row = catalogue.connection.execute(
            "SELECT english_name FROM english_name_mappings"
        ).fetchone()
        assert row["english_name"] == "Verified Name"


def test_identity_rebuild_keeps_a_linked_review_when_old_print_claims_disagree(tmp_path: Path) -> None:
    with CatalogueRepository(tmp_path / "catalogue.sqlite3") as catalogue:
        catalogue.import_japanese_many(
            [
                JapaneseCardPrint("DZ-BT17", "001", "RRR", "同じ日本語名", "https://jp/001"),
                JapaneseCardPrint("DZ-BT17", "FFR01", "FFR", "同じ日本語名", "https://jp/ffr01"),
            ]
        )
        catalogue.apply_name_mappings(
            [EnglishNameMapping("DZ-BT17", "001", "RRR", "Accepted Name",
                                "fandom-cross-print", "https://wiki/card", status="verified")]
        )
        catalogue.connection.execute(
            "UPDATE english_name_mappings SET english_name='Older Translation' "
            "WHERE japanese_print_id=(SELECT id FROM japanese_prints WHERE collector_number='FFR01')"
        )
        catalogue.connection.commit()

        _, links = catalogue.rebuild_card_identities()

        assert links == 2
        assert {card.collector_number for card in catalogue.search("accepted name", japanese_only=True)} == {"001", "FFR01"}
        assert not catalogue.search("older translation", japanese_only=True)


def test_shared_identity_includes_region_specific_promo_without_serial_equality(tmp_path: Path) -> None:
    with CatalogueRepository(tmp_path / "catalogue.sqlite3") as catalogue:
        catalogue.import_japanese_many(
            [
                JapaneseCardPrint("D-BT01", "001", "RRR", "同一名", "https://official/base"),
                JapaneseCardPrint("D-PR", "1008", "", "同一名", "https://official/reprint"),
            ]
        )
        catalogue.apply_name_mappings(
            [EnglishNameMapping("D-BT01", "001", "RRR", "Known Name", "fandom", "https://fandom/base")]
        )
        result = catalogue.derive_name_mappings_from_known_japanese_names()
        assert result.mapped == 0
        assert catalogue.unmapped_japanese_count("DPR") == 0
        assert any(
            card.set_code == "DPR" for card in catalogue.search("known name", japanese_only=True)
        )


def test_mapping_propagates_a_trusted_name_to_parallel_prints(tmp_path: Path) -> None:
    with CatalogueRepository(tmp_path / "catalogue.sqlite3") as catalogue:
        catalogue.import_japanese_many(
            [
                JapaneseCardPrint("DZ-BT16", "002", "", "エグザサベイト・ドラゴン", "https://official/002"),
                JapaneseCardPrint("DZ-BT16", "FFR02", "FFR", "エグザサベイト・ドラゴン", "https://official/ffr02"),
            ]
        )
        result = catalogue.apply_name_mappings(
            [
                EnglishNameMapping(
                    "DZ-BT16", "002", "RRR", "Exacerbate Dragon", "fandom", "https://fandom/set"
                )
            ]
        )
        assert result.mapped == 2
        assert result.derived == 1
        assert catalogue.search("exacerbate", rarity="FFR")[0].collector_number == "FFR02"


def test_one_english_identity_exposes_main_set_and_promo_prints_by_japanese_name(tmp_path: Path) -> None:
    """English search is a card lookup, not a collection of English prints."""
    with CatalogueRepository(tmp_path / "catalogue.sqlite3") as catalogue:
        catalogue.import_japanese_many(
            [
                JapaneseCardPrint("DZ-BT01", "001", "RRR", "同じ日本語名", "https://jp/main"),
                JapaneseCardPrint("DZ-SS01", "015", "FFR", "同じ日本語名", "https://jp/special", finish="holo"),
                JapaneseCardPrint("D-PR", "953", "PR", "同じ日本語名", "https://jp/promo"),
                JapaneseCardPrint("D-PR", "954", "PR", "別の日本語名", "https://jp/other"),
            ]
        )
        catalogue.apply_name_mappings(
            [
                EnglishNameMapping(
                    "DZ-BT01", "001", "RRR", "Shared Card", "fandom", "https://fandom/card",
                    aliases=("shared alias",),
                )
            ],
            derive_same_name=False,
        )

        all_prints = catalogue.search("shared alias", japanese_only=True, limit=10)
        assert {(card.set_code, card.collector_number) for card in all_prints} == {
            ("DZBT01", "001"), ("DZSS01", "015"), ("DPR", "953"),
        }
        assert {(card.set_code, card.collector_number) for card in catalogue.search(
            "shared", rarities=("PR",), japanese_only=True
        )} == {("DPR", "953")}
        assert {(card.set_code, card.collector_number) for card in catalogue.search(
            "shared", finishes=("holo",), japanese_only=True
        )} == {("DZBT01", "001"), ("DZSS01", "015"), ("DPR", "953")}
        assert catalogue.lookup_japanese_serial("DPR953").english_name == "Shared Card"
        assert not catalogue.search("shared", japanese_only=True, rarities=("C",))
        assert catalogue.unmapped_japanese_count() == 1
        direct_promo_mappings = catalogue.connection.execute(
            "SELECT COUNT(*) FROM english_name_mappings AS m JOIN japanese_prints AS j ON j.id=m.japanese_print_id "
            "WHERE j.set_code='DPR'"
        ).fetchone()[0]
        assert direct_promo_mappings == 0


def test_blank_dpr_rarity_is_normalised_for_promo_filters(tmp_path: Path) -> None:
    with CatalogueRepository(tmp_path / "catalogue.sqlite3") as catalogue:
        catalogue.import_japanese_many([
            JapaneseCardPrint("D-PR", "465", "", "プロモカード", "https://jp/promo"),
            JapaneseCardPrint("D-BT01", "001", "RRR", "プロモカード", "https://jp/main"),
        ])
        catalogue.apply_name_mappings([
            EnglishNameMapping("D-BT01", "001", "RRR", "Promo Card", "fandom", "https://fandom/card")
        ])
        assert catalogue.lookup_japanese_serial("DPR465").rarity == "PR"
        assert [(card.set_code, card.rarity) for card in catalogue.search(
            "promo card", rarities=("PR",), japanese_only=True
        )] == [("DPR", "PR")]
