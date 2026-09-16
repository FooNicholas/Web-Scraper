"""Manzokuya connector using its public Vanguard serial search."""

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


class ManzokuyaConnector(StoreConnector):
    """Read exact Japanese Vanguard prints from Manzokuya's product list."""

    store_id = "manzokuya"
    store_name = "Manzokuya"
    base_url = "https://shopmanzokuya.com"
    search_url = f"{base_url}/products/list"
    vanguard_category_id = "1005"
    _reference_pattern = re.compile(r"([A-Za-z0-9-]+/[A-Za-z0-9-]+)")
    _price_pattern = re.compile(r"[¥￥]\s*([0-9][0-9,]*)")
    _stock_pattern = re.compile(r"在庫\s*[:：]?\s*(\d+|[〇○◯×✕X])", re.IGNORECASE)

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client

    async def search(self, card: CardPrint) -> list[StoreOffer]:
        response = await self._get(
            self.search_url,
            params={"category_id": self.vanguard_category_id, "name": retailer_print_reference(card)},
        )
        return self.parse_html(card, response.text)

    async def _get(self, url: str, *, params: dict[str, str]) -> httpx.Response:
        headers = {"User-Agent": "JP-Price-Checker/0.1 (+approved personal price comparison)"}
        async with request_client(self.client) as client:
            response = await client.get(url, params=params, headers=headers)
        if response.status_code in (403, 429):
            raise StoreUnavailableError("Manzokuya did not permit this price request.")
        if response.status_code == 404:
            raise StoreUnavailableError("Manzokuya no longer exposes its Vanguard search page.")
        response.raise_for_status()
        return response

    @classmethod
    def parse_html(cls, card: CardPrint, page_content: str) -> list[StoreOffer]:
        soup = BeautifulSoup(page_content, "lxml")
        offers: list[StoreOffer] = []
        seen: set[str] = set()
        for item in soup.select("li"):
            product_link = item.select_one('a[href*="/products/detail/"]')
            if not product_link:
                continue
            item_text = item.get_text(" ", strip=True)
            raw_name = cls._name_text(product_link, item_text)
            listing_text = f"{item_text} {raw_name}"
            reference_match = cls._reference_pattern.search(listing_text)
            price_match = cls._price_pattern.search(listing_text)
            if not reference_match or not price_match or not references_card(card, reference_match.group(1)):
                continue
            if not matches_card_finish(card, raw_name):
                continue
            listing_url = urljoin(cls.base_url, product_link.get("href", ""))
            if listing_url in seen:
                continue
            seen.add(listing_url)
            stock_count, availability = cls._availability(item_text)
            finish, finish_raw = finish_from_text(raw_name)
            price = int(price_match.group(1).replace(",", ""))
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

    @classmethod
    def _availability(cls, item_text: str) -> tuple[int | None, Availability]:
        if "sold out" in item_text.casefold() or "品切れ" in item_text:
            return 0, Availability.SOLD_OUT
        matched = cls._stock_pattern.search(item_text)
        if not matched:
            return None, Availability.UNKNOWN
        value = matched.group(1).upper()
        if value.isdigit():
            stock_count = int(value)
            return stock_count, Availability.SOLD_OUT if stock_count == 0 else Availability.IN_STOCK
        if value in {"×", "✕", "X"}:
            return None, Availability.SOLD_OUT
        return None, Availability.IN_STOCK

    @staticmethod
    def _name_text(link: Tag, fallback: str) -> str:
        image = link.select_one("img[alt]")
        return str(image["alt"]).strip() if image and image.get("alt") else fallback
