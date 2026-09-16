"""Square Bushiroad connector using its public exact Japanese-serial search."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import httpx

from scraperbot.connectors.base import (
    StoreConnector,
    StoreUnavailableError,
    matches_card_finish,
    normalise_print_reference,
    print_reference,
    request_client,
    retailer_print_reference,
)
from scraperbot.models import Availability, CardPrint, MatchConfidence, StoreOffer, finish_from_text


class SquareBushiroadConnector(StoreConnector):
    """Read exact Japanese Vanguard prints from Square Bushiroad's storefront."""

    store_id = "square_bushiroad"
    store_name = "Square Bushiroad"
    base_url = "https://www.square-bushiroad.com"
    search_url = f"{base_url}/product-list"
    _stock_pattern = re.compile(r"在庫数\s*(\d+)\s*[点枚個]?")

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client

    async def search(self, card: CardPrint) -> list[StoreOffer]:
        response = await self._get(self.search_url, params={"keyword": retailer_print_reference(card)})
        return self.parse_html(card, response.text)

    async def _get(self, url: str, *, params: dict[str, str]) -> httpx.Response:
        headers = {"User-Agent": "JP-Price-Checker/0.1 (+approved personal price comparison)"}
        async with request_client(self.client) as client:
            response = await client.get(url, params=params, headers=headers)
        if response.status_code in (403, 429):
            raise StoreUnavailableError("Square Bushiroad did not permit this price request.")
        if response.status_code == 404:
            raise StoreUnavailableError("Square Bushiroad no longer exposes its product search page.")
        response.raise_for_status()
        return response

    @classmethod
    def parse_html(cls, card: CardPrint, page_content: str) -> list[StoreOffer]:
        soup = BeautifulSoup(page_content, "lxml")
        offers: list[StoreOffer] = []
        seen: set[str] = set()
        for item in soup.select("li.list_item_cell"):
            product_link = item.select_one('a.item_data_link[href*="/product/"]')
            name_node = item.select_one(".goods_name")
            price_node = item.select_one(".selling_price .figure")
            if not product_link or not name_node or not price_node:
                continue
            raw_name = name_node.get_text(" ", strip=True)
            if not cls._references_card(card, raw_name) or not matches_card_finish(card, raw_name):
                continue
            price_digits = re.sub(r"[^0-9]", "", price_node.get_text(" ", strip=True))
            if not price_digits:
                continue
            listing_url = urljoin(cls.base_url, product_link.get("href", ""))
            if listing_url in seen:
                continue
            seen.add(listing_url)
            item_text = item.get_text(" ", strip=True)
            stock_match = cls._stock_pattern.search(item_text)
            stock_count = int(stock_match.group(1)) if stock_match else None
            sold_out = "SOLD OUT" in item_text.upper() or "売り切れ" in item_text or "在庫なし" in item_text
            availability = (
                Availability.SOLD_OUT
                if sold_out or stock_count == 0
                else Availability.IN_STOCK
                if stock_count is not None
                else Availability.UNKNOWN
            )
            price = int(price_digits)
            finish, finish_raw = finish_from_text(raw_name)
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
                    stock_count=stock_count,
                    finish=finish,
                    finish_raw=finish_raw,
                )
            )
        return offers

    @staticmethod
    def _references_card(card: CardPrint, listing_name: str) -> bool:
        """Match Square's printed serial before its appended rarity suffix."""
        expected = normalise_print_reference(print_reference(card))
        listed = normalise_print_reference(listing_name)
        return bool(re.search(re.escape(expected) + r"(?!\d)", listed))
