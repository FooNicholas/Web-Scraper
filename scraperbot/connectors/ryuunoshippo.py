"""Ryuunoshippo connector using its public exact serial search."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import httpx

from scraperbot.connectors.base import (
    StoreConnector,
    StoreUnavailableError,
    matches_card_finish,
    references_card,
    request_client,
    retailer_print_reference,
)
from scraperbot.models import Availability, CardPrint, MatchConfidence, StoreOffer, finish_from_text


class RyuunoshippoConnector(StoreConnector):
    """Read exact Japanese Vanguard prints from Ryuunoshippo's public product list."""

    store_id = "ryuunoshippo"
    store_name = "Ryuunoshippo"
    base_url = "https://www.ryuunoshippo4.com"
    search_url = f"{base_url}/product-list/0/0/normal"
    _reference_pattern = re.compile(r"([A-Za-z0-9-]+/[A-Za-z0-9-]+)")
    _price_pattern = re.compile(r"([0-9][0-9,]*)\s*円")
    _stock_pattern = re.compile(r"在庫数\s*(\d+)\s*[点枚個]?")

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client

    async def search(self, card: CardPrint) -> list[StoreOffer]:
        response = await self._get(
            self.search_url,
            params={"keyword": retailer_print_reference(card), "order": "rank", "page": "1"},
        )
        return self.parse_html(card, response.text)

    async def _get(self, url: str, *, params: dict[str, str]) -> httpx.Response:
        headers = {"User-Agent": "JP-Price-Checker/0.1 (+approved personal price comparison)"}
        async with request_client(self.client) as client:
            response = await client.get(url, params=params, headers=headers)
        if response.status_code in (403, 429):
            raise StoreUnavailableError("Ryuunoshippo did not permit this price request.")
        if response.status_code == 404:
            raise StoreUnavailableError("Ryuunoshippo no longer exposes its product search page.")
        response.raise_for_status()
        return response

    @classmethod
    def parse_html(cls, card: CardPrint, page_content: str) -> list[StoreOffer]:
        soup = BeautifulSoup(page_content, "lxml")
        offers: list[StoreOffer] = []
        seen: set[str] = set()
        for item in soup.select(".list_item_cell"):
            raw_name = item.select_one(".goods_name")
            if not raw_name:
                continue
            raw_name_text = raw_name.get_text(" ", strip=True)
            reference_match = cls._reference_pattern.search(raw_name_text)
            if not reference_match or not references_card(card, reference_match.group(1)):
                continue
            if not matches_card_finish(card, raw_name_text):
                continue
            item_text = item.get_text(" ", strip=True)
            price_match = cls._price_pattern.search(item_text)
            if not price_match:
                continue
            product_link = item.select_one('a[href*="/product/"]')
            if not product_link:
                continue
            listing_url = urljoin(cls.base_url, product_link.get("href", ""))
            if listing_url in seen:
                continue
            seen.add(listing_url)
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
            price = int(price_match.group(1).replace(",", ""))
            finish, finish_raw = finish_from_text(raw_name_text)
            offers.append(
                StoreOffer(
                    store_id=cls.store_id,
                    store_name=cls.store_name,
                    raw_name=raw_name_text,
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
