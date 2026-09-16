"""Amenity Dream connector using its public exact serial search."""

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


class AmenityDreamConnector(StoreConnector):
    """Read exact Japanese Vanguard prints from Amenity Dream's product list."""

    store_id = "amenitydream"
    store_name = "Amenity Dream"
    base_url = "https://www.amenitydream.com"
    search_url = f"{base_url}/product-list"
    _reference_pattern = re.compile(r"([A-Za-z0-9-]+/[A-Za-z0-9-]+)")
    _price_pattern = re.compile(r"([0-9][0-9,]*)\s*円")
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
            raise StoreUnavailableError("Amenity Dream did not permit this price request.")
        if response.status_code == 404:
            raise StoreUnavailableError("Amenity Dream no longer exposes its product search page.")
        response.raise_for_status()
        return response

    @classmethod
    def parse_html(cls, card: CardPrint, page_content: str) -> list[StoreOffer]:
        soup = BeautifulSoup(page_content, "lxml")
        offers: list[StoreOffer] = []
        seen: set[str] = set()
        for item in soup.select("li"):
            product_link = item.select_one('a[href*="/product/"]')
            if not product_link:
                continue
            item_text = item.get_text(" ", strip=True)
            reference_match = cls._reference_pattern.search(item_text)
            if not reference_match or not references_card(card, reference_match.group(1)):
                continue
            raw_name = cls._name_text(product_link, item_text)
            if not matches_card_finish(card, raw_name):
                continue
            price_match = cls._price_pattern.search(item_text)
            if not price_match:
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
                if stock_count is not None or item.select_one("button:not([disabled])")
                else Availability.UNKNOWN
            )
            price = int(price_match.group(1).replace(",", ""))
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
    def _name_text(link: object, fallback: str) -> str:
        image = getattr(link, "select_one", lambda _: None)("img[alt]")
        return str(image["alt"]).strip() if image and image.get("alt") else fallback
