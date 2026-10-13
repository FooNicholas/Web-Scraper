import asyncio

import httpx

from scraperbot.catalogue.japanese_source import OfficialJapaneseCardSource


def test_parse_official_japanese_expansion_and_cards() -> None:
    expansions = OfficialJapaneseCardSource.parse_expansions(
        '''<div class="product-item"><a href="/cardlist/cardsearch/?expansion=300">
        <div class="title">【DZ-BT16】「幻真覚醒」</div></a></div>'''
    )
    assert expansions[0].id == 300
    assert expansions[0].set_code == "DZ-BT16"

    cards = OfficialJapaneseCardSource.parse_card_entries(
        '''<div id="cardlist-container"><ul>
        <li><a href="/cardlist/?cardno=DZ-BT16/002&expansion=300"><div class="number">DZ-BT16/002</div>
        <h5>エグザサベイト・ドラゴン<span>エグザサベイト・ドラゴン</span></h5></a></li>
        <li><a href="/cardlist/?cardno=DZ-BT16/FFR02&expansion=300"><div class="number">DZ-BT16/FFR02</div>
        <h5>エグザサベイト・ドラゴン<span>エグザサベイト・ドラゴン</span></h5></a></li>
        </ul></div>'''
    )
    assert [(card.collector_number, card.rarity) for card in cards] == [("002", ""), ("FFR02", "FFR")]
    assert cards[0].japanese_name == "エグザサベイト・ドラゴン"
    assert cards[1].source_url.endswith("expansion=300")


def test_current_official_api_discovers_and_paginates_new_set() -> None:
    requests: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, request.url.query.decode()))
        if request.url.path == "/cardlist/":
            return httpx.Response(200, text="<html><div id='app'></div></html>")
        if request.url.path.endswith("/products"):
            return httpx.Response(200, json={"items": [
                {"id": "1200", "name": "【DZ-BT18】新商品"},
                {"id": "1199", "name": "【DZ-BT17】運命星戦"},
            ]})
        if request.url.path.endswith("/list"):
            page = int(request.url.params["page"])
            rows = ([{"card_number": "DZ-BT18/001", "card_name": "新しいカード", "rare": "RRR"}]
                    if page == 1 else
                    [{"card_number": "DZ-BT18/FFR01", "card_name": "新しいカード", "rare": "FFR"}])
            return httpx.Response(200, json={"items": rows, "total": 2})
        raise AssertionError(request.url)

    async def run() -> tuple[list, list]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            source = OfficialJapaneseCardSource(client=client, page_delay=0)
            expansions = await source.list_expansions()
            cards = await source.cards_for_expansion(expansions[0])
            return expansions, cards

    expansions, cards = asyncio.run(run())
    assert [(item.id, item.set_code) for item in expansions] == [(1200, "DZ-BT18"), (1199, "DZ-BT17")]
    assert [(card.set_code, card.collector_number, card.japanese_name, card.rarity) for card in cards] == [
        ("DZBT18", "001", "新しいカード", "RRR"),
        ("DZBT18", "FFR01", "新しいカード", "FFR"),
    ]
    assert cards[0].source_url.endswith("cardno=DZ-BT18%2F001&expansion=1200")
    assert len([path for path, _ in requests if path.endswith("/list")]) == 2
