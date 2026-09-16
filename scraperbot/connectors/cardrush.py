"""Card Rush Vanguard connector using exact serials for Japanese promos."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
import httpx

from scraperbot.connectors.base import (
    StoreConnector,
    StoreUnavailableError,
    matches_card_finish,
    references_card,
    request_client,
)
from scraperbot.models import Availability, CardPrint, MatchConfidence, StoreOffer, finish_from_text


class CardRushConnector(StoreConnector):
    """Read exact Vanguard print offers from Card Rush search results.

    Product names embed the printed reference in braces, for example
    ``{DZ-BT14/001}``. That reference—not the Japanese name—is used as the
    match key, so parallel rarities and similarly named cards stay separate.
    Japanese D-Promos use their printed ``D-PR/number`` as the Card Rush
    search keyword. This keeps promo lookup independent of an English mapping
    and avoids a broad shared-name search.
    """

    store_id = "cardrush"
    store_name = "Card Rush"
    base_url = "https://www.cardrush-vanguard.jp"
    search_url = f"{base_url}/product-list"
    # Search-result highlighting can wrap the serial in nested tags. The
    # text extractor then yields ``{ D-PR/953 }`` instead of ``{D-PR/953}``.
    _reference_pattern = re.compile(r"\{\s*([A-Za-z0-9-]+/[A-Za-z0-9-]+)\s*\}")
    _price_pattern = re.compile(r"(?<!\d)([0-9][0-9,]*)\s*円")
    _stock_pattern = re.compile(r"在庫数\s*(\d+)\s*[個枚]")
    _condition_pattern = re.compile(r"〔状態\s*([^〕]+)〕")

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client

    async def search(self, card: CardPrint) -> list[StoreOffer]:
        keyword = self._search_keyword(card)
        if not keyword:
            return []
        response = await self._get(self.search_url, params={"keyword": keyword, "Submit": "検索"})
        return self.parse_html(card, response.text)

    @staticmethod
    def _search_keyword(card: CardPrint) -> str | None:
        if card.set_code == "DPR":
            return f"D-PR/{card.collector_number}"
        return card.japanese_name

    async def _get(self, url: str, *, params: dict[str, str]) -> httpx.Response:
        headers = {"User-Agent": "JP-Price-Checker/0.1 (+approved personal price comparison)"}
        async with request_client(self.client) as client:
            response = await client.get(url, params=params, headers=headers)
        if response.status_code in (403, 429):
            raise StoreUnavailableError("Card Rush did not permit this price request.")
        if response.status_code == 404:
            raise StoreUnavailableError("Card Rush no longer exposes its Vanguard search page.")
        response.raise_for_status()
        return response

    @classmethod
    def parse_html(cls, card: CardPrint, page_content: str) -> list[StoreOffer]:
        soup = BeautifulSoup(page_content, "lxml")
        offers: list[StoreOffer] = []
        seen: set[tuple[str, str, int | None, str | None]] = set()
        for item in soup.select("li"):
            item_text = item.get_text(" ", strip=True)
            reference_match = cls._reference_pattern.search(item_text)
            price_match = cls._price_pattern.search(item_text)
            if not reference_match or not price_match or not references_card(card, reference_match.group(1)):
                continue
            product_link = item.select_one('a[href*="/product/"]')
            stock_match = cls._stock_pattern.search(item_text)
            stock_count = int(stock_match.group(1)) if stock_match else None
            sold_out = "×" in item_text
            availability = (
                Availability.SOLD_OUT
                if sold_out or stock_count == 0
                else Availability.IN_STOCK
                if stock_count is not None or "カートに入れる" in item_text
                else Availability.UNKNOWN
            )
            price = int(price_match.group(1).replace(",", ""))
            condition_match = cls._condition_pattern.search(item_text)
            condition = condition_match.group(1).strip() if condition_match else None
            raw_name = cls._name_text(item, item_text)
            if not matches_card_finish(card, raw_name):
                continue
            finish, finish_raw = finish_from_text(raw_name)
            listing_url = urljoin(cls.base_url, product_link["href"]) if product_link else None
            key = (listing_url or raw_name, item_text, price, condition)
            if key in seen:
                continue
            seen.add(key)
            offers.append(
                StoreOffer(
                    store_id=cls.store_id,
                    store_name=cls.store_name,
                    raw_name=raw_name,
                    price_yen=price,
                    price_display=f"¥{price:,}",
                    availability=availability,
                    listing_url=listing_url,
                    match_confidence=MatchConfidence.EXACT_PRINT,
                    condition=condition,
                    stock_count=stock_count,
                    finish=finish,
                    finish_raw=finish_raw,
                )
            )
        return offers

    @staticmethod
    def _name_text(item: Tag, fallback: str) -> str:
        name = item.select_one("img[alt], h1, h2, h3, h4, p")
        return name.get_text(" ", strip=True) if name and name.get_text(strip=True) else fallback
