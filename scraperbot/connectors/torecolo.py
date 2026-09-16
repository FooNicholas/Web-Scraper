"""Torecolo connector using its public Vanguard serial search."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import httpx

from scraperbot.connectors.base import (
    StoreConnector,
    StoreUnavailableError,
    matches_card_finish,
    request_client,
    retailer_print_reference,
)
from scraperbot.models import Availability, CardPrint, MatchConfidence, StoreOffer, finish_from_text


class TorecoloConnector(StoreConnector):
    """Read exact Japanese Vanguard prints from Torecolo's public search page."""

    store_id = "torecolo"
    store_name = "Torecolo"
    base_url = "https://www.torecolo.jp"
    search_url = f"{base_url}/shop/goods/search.aspx"
    _price_pattern = re.compile(r"([0-9][0-9,]*)\s*円")
    _stock_pattern = re.compile(r"在庫\s*(\d+)")

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client

    async def search(self, card: CardPrint) -> list[StoreOffer]:
        serial = retailer_print_reference(card).replace("/", "-")
        response = await self._get(
            self.search_url,
            params={"ct2": "1050", "search": "x", "keyword": serial},
        )
        return self.parse_html(card, response.text)

    async def _get(self, url: str, *, params: dict[str, str]) -> httpx.Response:
        headers = {"User-Agent": "JP-Price-Checker/0.1 (+approved personal price comparison)"}
        async with request_client(self.client) as client:
            response = await client.get(url, params=params, headers=headers)
        if response.status_code in (403, 429):
            raise StoreUnavailableError("Torecolo did not permit this price request.")
        if response.status_code == 404:
            raise StoreUnavailableError("Torecolo no longer exposes its Vanguard search page.")
        response.raise_for_status()
        return response

    @classmethod
    def parse_html(cls, card: CardPrint, page_content: str) -> list[StoreOffer]:
        soup = BeautifulSoup(page_content, "lxml")
        offers: list[StoreOffer] = []
        expected_code = retailer_print_reference(card).replace("/", "-").upper()
        for item in soup.select(".block-thumbnail-t--goods"):
            product_link = item.select_one('a[href*="/shop/g/g"]')
            if not product_link:
                continue
            matched = re.search(r"/shop/g/g([^/]+)/", product_link.get("href", ""), re.IGNORECASE)
            if not matched or not cls._matches_product_code(expected_code, matched.group(1)):
                continue
            raw_name = product_link.get("title") or product_link.get_text(" ", strip=True)
            if not matches_card_finish(card, raw_name):
                continue
            item_text = item.get_text(" ", strip=True)
            price_match = cls._price_pattern.search(item_text)
            if not price_match:
                continue
            stock_match = cls._stock_pattern.search(item_text)
            stock_count = int(stock_match.group(1)) if stock_match else None
            sold_out = "SOLDOUT" in item_text.upper() or "売切れ" in item_text or "売り切れ" in item_text
            availability = (
                Availability.SOLD_OUT
                if sold_out or stock_count == 0
                else Availability.IN_STOCK
                if stock_count is not None
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
                    listing_url=urljoin(cls.base_url, product_link.get("href", "")),
                    match_confidence=MatchConfidence.EXACT_PRINT,
                    stock_count=stock_count,
                    finish=finish,
                    finish_raw=finish_raw,
                )
            )
        return offers

    @staticmethod
    def _matches_product_code(expected_code: str, product_code: str) -> bool:
        """Torecolo appends its rarity to a serial in its product code."""
        return bool(re.fullmatch(re.escape(expected_code) + r"(?:[A-Z+]+(?:-\d+)?)?", product_code.upper()))
