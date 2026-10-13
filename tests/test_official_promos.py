import asyncio

import httpx
import pytest

from scraperbot.catalogue.official_promos import OfficialPromoSource, import_official_promos
from scraperbot.catalogue.official_source import OfficialSourceError
from scraperbot.catalogue.promo_mapping_repair import repair_promo_mappings
from scraperbot.catalogue.repository import CatalogueRepository
from scraperbot.models import EnglishNameMapping, JapaneseCardPrint


def row(number='953', name='大地を駆ける守主 ルアン', date=None):
    date_cell = f'<td>{date}</td>' if date else ''
    return f'''<table><tr class="card_pr"><td>D -PR/{number}</td>
    <td><span class="new">NEW</span><a>{name}</a></td>{date_cell}
    <td>PRパック</td><td>ケテルサンクチュアリ</td></tr></table>'''


def source_for(pages):
    requests = []
    def handler(request):
        page = int(request.url.params.get('page', '1'))
        requests.append(page)
        content = pages[page]
        return httpx.Response(content if isinstance(content, int) else 200,
                              text=content if isinstance(content, str) else '')
    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), requests


def test_official_pr_parsing_pending_and_legacy_scope():
    html = row('1839', '新カード', '2026/ 9/15').replace('class="card_pr"', 'class="card_pr_new"') + row().replace('D -PR/', 'V-PR/')
    entries, _ = OfficialPromoSource.parse_page(html)
    assert len(entries) == 1
    assert entries[0].card.japanese_name == '新カード'
    assert entries[0].available_from == '2026/ 9/15'
    assert entries[0].distribution == 'PRパック'


def test_official_pr_follows_pagination_once_and_keeps_release_row():
    client, requests = source_for({
        1: row(date='2026/9/15') + '<a href="/cardlist/card-pr?page=2">2</a>',
        2: row() + '<a href="/cardlist/card-pr?page=2">2</a>',
    })
    async def run():
        async with client:
            return await OfficialPromoSource(client, page_delay=0).fetch()
    entries = asyncio.run(run())
    assert requests == [1, 2]
    assert len(entries) == 1
    assert entries[0].available_from is None


def test_failed_promo_snapshot_leaves_database_untouched(tmp_path):
    database = tmp_path / 'catalogue.sqlite3'
    with CatalogueRepository(database) as catalogue:
        catalogue.import_japanese_many([JapaneseCardPrint('DPR','953','PR','Old name','https://jp/old')])
    client, _ = source_for({1: row() + '<a href="/cardlist/card-pr?page=2">2</a>', 2: 429})
    async def run():
        async with client:
            await import_official_promos(database, source=OfficialPromoSource(client, page_delay=0))
    with pytest.raises(OfficialSourceError):
        asyncio.run(run())
    with CatalogueRepository(database) as catalogue:
        assert catalogue.japanese_prints(('DPR',))[0].japanese_name == 'Old name'
        assert catalogue.connection.execute('SELECT COUNT(*) FROM official_promo_identities').fetchone()[0] == 0


def test_official_identity_correction_remaps_leuhan_and_preserves_finish(tmp_path):
    database = tmp_path / 'catalogue.sqlite3'
    with CatalogueRepository(database) as catalogue:
        catalogue.import_japanese_many([
            JapaneseCardPrint('DPR','953','PR','Wrong identity','https://jp/old',finish_raw='H仕様'),
            JapaneseCardPrint('DSS11','108','R','大地を駆ける守主 ルアン','https://jp/source'),
        ])
        catalogue.apply_name_mappings([
            EnglishNameMapping('DPR','953','PR','Wrong English','promo-review','https://review',status='reviewed'),
            EnglishNameMapping('DSS11','108','R','Guard Running Through The Earth, Leuhan','fandom','https://fandom/source'),
        ])
    client, _ = source_for({1: row()})
    async def run():
        async with client:
            return await import_official_promos(database, source=OfficialPromoSource(client, page_delay=0))
    assert asyncio.run(run()) == 1
    repair_promo_mappings(database)
    with CatalogueRepository(database) as catalogue:
        card = catalogue.lookup_japanese_serial('DPR953')
        assert card.english_name == 'Guard Running Through The Earth, Leuhan'
        assert card.finish_raw == 'H仕様'
        assert card.rarity == 'PR'
        assert not catalogue.search('Wrong English', japanese_only=True)
        # Later general card-list imports cannot undo the dedicated PR identity.
        catalogue.import_japanese_many([JapaneseCardPrint('DPR','953','','Stale name','https://jp/stale')])
        assert catalogue.japanese_prints(('DPR',))[0].japanese_name == '大地を駆ける守主 ルアン'


def test_reviewed_english_name_survives_unchanged_official_identity_and_repair(tmp_path):
    database = tmp_path / 'catalogue.sqlite3'
    with CatalogueRepository(database) as catalogue:
        catalogue.import_japanese_many([JapaneseCardPrint('DPR','953','PR','大地を駆ける守主 ルアン','https://jp/953')])
        catalogue.apply_name_mappings([EnglishNameMapping('DPR','953','PR','Reviewed Leuhan','promo-review','https://review',status='reviewed')])
    client, _ = source_for({1: row()})
    async def run():
        async with client:
            await import_official_promos(database, source=OfficialPromoSource(client, page_delay=0))
    asyncio.run(run())
    repair_promo_mappings(database)
    with CatalogueRepository(database) as catalogue:
        assert catalogue.lookup_japanese_serial('DPR953').english_name == 'Reviewed Leuhan'


def test_missing_official_table_is_not_an_empty_success():
    with pytest.raises(OfficialSourceError):
        OfficialPromoSource.parse_page('<html>Temporarily unavailable</html>')


def test_current_official_pr_api_fallback_preserves_scheduled_and_released_rows():
    requests = []

    def respond(request):
        requests.append((request.url.path, request.url.params.get('page', '1')))
        if request.url.path == '/cardlist/card_pr':
            return httpx.Response(200, text='<html><div id="pwocg-card-pr"></div></html>')
        assert request.url.path == '/manage/card-pr-user/list'
        page = int(request.url.params['page'])
        return httpx.Response(200, json={
            'page': page, 'pages': 2,
            'scheduled': [{
                'card_number': 'D-PR/2000', 'card_name': '未来のカード',
                'card_use': '2026/10/17', 'dist_way': 'Event',
            }],
            'items': ([{'card_number': 'D-PR/953', 'card_name': '大地を駆ける守主 ルアン',
                       'dist_way': 'PR Pack'}] if page == 1 else
                      [{'card_number': 'D-PR/2000', 'card_name': '未来のカード',
                        'dist_way': 'PR Pack'}]),
        })

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await OfficialPromoSource(client, page_delay=0).fetch()

    entries = asyncio.run(run())
    assert requests == [('/cardlist/card_pr', '1'), ('/manage/card-pr-user/list', '1'),
                        ('/manage/card-pr-user/list', '2')]
    assert {entry.card.collector_number for entry in entries} == {'953', '2000'}
    assert next(entry for entry in entries if entry.card.collector_number == '2000').available_from is None


def test_invalid_official_pr_api_page_is_not_accepted():
    with pytest.raises(OfficialSourceError):
        OfficialPromoSource.parse_api_page({'items': [], 'page': 1, 'pages': 0})
