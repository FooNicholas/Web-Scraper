"""Mana Source connector using its public exact serial search."""

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
    retailer_print_reference,
)
from scraperbot.models import Availability, CardPrint, MatchConfidence, StoreOffer, finish_from_text


class ManaSourceConnector(StoreConnector):
    """Read exact Japanese Vanguard prints from Mana Source's product search."""

    store_id = "manasource"
    store_name = "Mana Source"
    base_url = "https://www.manasource.net"
    search_url = f"{base_url}/product-list"
    _reference_pattern = re.compile(r"([A-Za-z0-9-]+/[A-Za-z0-9-]+)")
    _price_pattern = re.compile(r"([0-9][0-9,]*)\s*円")
    _stock_pattern = re.compile(r"在庫数\s*(\d+)\s*個")

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client

    async def search(self, card: CardPrint) -> list[StoreOffer]:
        response = await self._get(self.search_url, params={"keyword": retailer_print_reference(card), "num": "60"})
        return self.parse_html(card, response.text)

    async def _get(self, url: str, *, params: dict[str, str]) -> httpx.Response:
        headers = {"User-Agent": "JP-Price-Checker/0.1 (+approved personal price comparison)"}
        async with request_client(self.client) as client:
            response = await client.get(url, params=params, headers=headers)
        if response.status_code in (403, 429):
            raise StoreUnavailableError("Mana Source did not permit this price request.")
        if response.status_code == 404:
            raise StoreUnavailableError("Mana Source no longer exposes its product search page.")
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
            raw_name = cls._name_text(product_link, item_text)
            listing_text = f"{item_text} {raw_name}"
            reference_match = cls._reference_pattern.search(listing_text)
            if not reference_match or not references_card(card, reference_match.group(1)):
                continue
            if not matches_card_finish(card, raw_name):
                continue
            listing_url = urljoin(cls.base_url, product_link.get("href", ""))
            if listing_url in seen:
                continue
            seen.add(listing_url)
            price_match = cls._price_pattern.search(item_text)
            price = int(price_match.group(1).replace(",", "")) if price_match else None
            stock_match = cls._stock_pattern.search(item_text)
            stock_count = int(stock_match.group(1)) if stock_match else None
            sold_out = "在庫なし" in item_text
            availability = (
                Availability.SOLD_OUT
                if sold_out or stock_count == 0
                else Availability.IN_STOCK
                if stock_count is not None or "在庫わずか" in item_text
                else Availability.UNKNOWN
            )
            finish, finish_raw = finish_from_text(raw_name)
            offers.append(
                StoreOffer(
                    store_id=cls.store_id,
                    store_name=cls.store_name,
                    raw_name=raw_name,
                    price_yen=price,
                    price_display=f"¥{price:,}" if price is not None else "Price unavailable",
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
    def _name_text(link: Tag, fallback: str) -> str:
        name = link.select_one("p")
        return name.get_text(" ", strip=True) if name and name.get_text(strip=True) else fallback
