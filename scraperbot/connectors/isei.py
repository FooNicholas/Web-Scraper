"""Cardshop Isei connector using its public exact serial search."""

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


class IseiConnector(StoreConnector):
    """Read exact Japanese Vanguard prints from Cardshop Isei's public storefront."""

    store_id = "isei"
    store_name = "Cardshop Isei"
    base_url = "https://cardshopisei.com"
    search_url = f"{base_url}/search"
    _reference_pattern = re.compile(r"([A-Za-z0-9-]+/[A-Za-z0-9-]+)")
    _price_pattern = re.compile(r"[￥¥]\s*([0-9][0-9,]*)")

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client

    async def search(self, card: CardPrint) -> list[StoreOffer]:
        response = await self._get(self.search_url, params={"q": retailer_print_reference(card)})
        return self.parse_html(card, response.text)

    async def _get(self, url: str, *, params: dict[str, str]) -> httpx.Response:
        headers = {"User-Agent": "JP-Price-Checker/0.1 (+approved personal price comparison)"}
        async with request_client(self.client) as client:
            response = await client.get(url, params=params, headers=headers)
        if response.status_code in (403, 429):
            raise StoreUnavailableError("Cardshop Isei did not permit this price request.")
        if response.status_code == 404:
            raise StoreUnavailableError("Cardshop Isei no longer exposes its product search page.")
        response.raise_for_status()
        return response

    @classmethod
    def parse_html(cls, card: CardPrint, page_content: str) -> list[StoreOffer]:
        soup = BeautifulSoup(page_content, "lxml")
        offers: list[StoreOffer] = []
        seen: set[str] = set()
        for item in soup.select(".card-wrapper.product-card-wrapper"):
            product_link = item.select_one('a[href*="/products/"]')
            if not product_link:
                continue
            raw_name = product_link.get_text(" ", strip=True)
            reference_match = cls._reference_pattern.search(raw_name)
            if not reference_match or not references_card(card, reference_match.group(1)):
                continue
            if not matches_card_finish(card, raw_name):
                continue
            price_match = cls._price_pattern.search(item.get_text(" ", strip=True))
            if not price_match:
                continue
            listing_url = urljoin(cls.base_url, product_link.get("href", ""))
            listing_url = listing_url.split("?")[0]
            if listing_url in seen:
                continue
            seen.add(listing_url)
            item_text = item.get_text(" ", strip=True)
            sold_out = "SOLD OUT" in item_text.upper() or "売り切れ" in item_text or "在庫切れ" in item_text or "×" in item_text
            in_stock = "product-stock--in" in str(item) or "在庫あり" in item_text or "○" in item_text
            availability = Availability.SOLD_OUT if sold_out else Availability.IN_STOCK if in_stock else Availability.UNKNOWN
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
                    finish=finish,
                    finish_raw=finish_raw,
                )
            )
        return offers
