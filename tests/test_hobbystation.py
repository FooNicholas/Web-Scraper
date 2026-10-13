import asyncio
from urllib.parse import parse_qs, urlparse

from scraperbot.connectors import hobbystation
from scraperbot.connectors.hobbystation import HobbyStationConnector
from scraperbot.models import CardPrint


YOUTHBERK = CardPrint(
    "DZ-BT14", "001", "RRR", "Youthberk", japanese_name="ユースベルク“龍吼燎騎・幻影”", source="test"
)


def listing(code: str, name: str = "ユースベルク") -> str:
    return f'''<li><a href="/vg/product/detail/123"><span>{code}</span>
    <span class="list_product_Name_pc">{name}</span><span>400円</span>
    <span>在庫数: 2</span></a></li>'''


def test_search_uses_exact_print_code_before_the_name(monkeypatch) -> None:
    keywords = []

    async def fetch(url: str, **_: object) -> str:
        keywords.append(parse_qs(urlparse(url).query)["search_word"][0])
        return listing("VG-DZ-BT14-001")

    monkeypatch.setattr(hobbystation, "fetch_rendered_html", fetch)
    offers = asyncio.run(HobbyStationConnector().search(YOUTHBERK))

    assert keywords == ["VG-DZ-BT14-001"]
    assert len(offers) == 1
    assert offers[0].listing_url == "https://www.hobbystation-single.jp/vg/product/detail/123"


def test_search_retries_a_name_fragment_and_still_checks_the_exact_print(monkeypatch) -> None:
    keywords = []

    async def fetch(url: str, **_: object) -> str:
        keyword = parse_qs(urlparse(url).query)["search_word"][0]
        keywords.append(keyword)
        if keyword.startswith("VG-"):
            return "<html>お探しの商品は見つかりませんでした</html>"
        return listing("VG-DZ-BT14-002") + listing("VG-DZ-BT14-001")

    monkeypatch.setattr(hobbystation, "fetch_rendered_html", fetch)
    offers = asyncio.run(HobbyStationConnector().search(YOUTHBERK))

    assert keywords == ["VG-DZ-BT14-001", "ユースベルク"]
    assert len(offers) == 1


def test_promo_search_accepts_the_shops_padded_collector_number(monkeypatch) -> None:
    promo = CardPrint("D-PR", "953", "PR", "Leuhan", japanese_name="大地を駆ける守主 ルアン", source="test")
    keywords = []

    async def fetch(url: str, **_: object) -> str:
        keywords.append(parse_qs(urlparse(url).query)["search_word"][0])
        return listing("VG-D-PR-0953", "大地を駆ける守主 ルアン")

    monkeypatch.setattr(hobbystation, "fetch_rendered_html", fetch)
    offers = asyncio.run(HobbyStationConnector().search(promo))

    assert keywords == ["VG-D-PR-0953"]
    assert len(offers) == 1
