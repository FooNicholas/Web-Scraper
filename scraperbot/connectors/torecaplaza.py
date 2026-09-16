"""Toreca Plaza 55 connector using its public exact Japanese-serial search."""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import httpx

from scraperbot.connectors.base import (
    StoreConnector,
    StoreUnavailableError,
    bounded_map,
    matches_card_finish,
    references_card,
    request_client,
    retailer_print_reference,
)
from scraperbot.models import Availability, CardPrint, MatchConfidence, StoreOffer, finish_from_text


class TorecaPlazaConnector(StoreConnector):
    """Read exact Japanese Vanguard prints from Toreca Plaza 55's storefront."""

    store_id = "torecaplaza"
    store_name = "Toreca Plaza 55"
    base_url = "https://torecaplaza55.com"
    search_url = f"{base_url}/"
    _reference_pattern = re.compile(r"([A-Za-z0-9-]+/[A-Za-z0-9-]+)")
    _colorme_pattern = re.compile(r"var Colorme = (\{.*?\});\s*\n", re.DOTALL)

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client

    async def search(self, card: CardPrint) -> list[StoreOffer]:
        response = await self._get(
            self.search_url,
            params={"mode": "srh", "cid": "2921527,2", "keyword": retailer_print_reference(card)},
        )
        listings = self.parse_search_html(card, response.text)
        # The public search is constrained to one serial. This cap protects
        # against a retailer regression broadening that search unexpectedly.
        async def inspect(listing_url: str) -> StoreOffer | None:
            try:
                detail = await self._get(listing_url)
            except (httpx.HTTPError, StoreUnavailableError):
                return None
            return self.parse_product_html(card, listing_url, detail.text)

        return [offer for offer in await bounded_map(listings[:8], inspect) if offer]

    async def _get(self, url: str, *, params: dict[str, str] | None = None) -> httpx.Response:
        headers = {"User-Agent": "JP-Price-Checker/0.1 (+approved personal price comparison)"}
        request_kwargs = {"headers": headers}
        if params is not None:
            request_kwargs["params"] = params
        async with request_client(self.client) as client:
            response = await client.get(url, **request_kwargs)
        if response.status_code in (403, 429):
            raise StoreUnavailableError("Toreca Plaza 55 did not permit this price request.")
        if response.status_code == 404:
            raise StoreUnavailableError("Toreca Plaza 55 no longer exposes this card page.")
        response.raise_for_status()
        return response

    @classmethod
    def parse_search_html(cls, card: CardPrint, page_content: str) -> list[str]:
        soup = BeautifulSoup(page_content, "lxml")
        listing_urls: list[str] = []
        seen: set[str] = set()
        for item in soup.select("li.c-item-list__item"):
            title = item.select_one(".c-item-list__ttl a[href]")
            if not title:
                continue
            raw_name = title.get_text(" ", strip=True)
            reference_match = cls._reference_pattern.search(raw_name)
            if not reference_match or not references_card(card, reference_match.group(1)):
                continue
            if not matches_card_finish(card, raw_name):
                continue
            listing_url = urljoin(cls.base_url, title.get("href", ""))
            if listing_url not in seen:
                seen.add(listing_url)
                listing_urls.append(listing_url)
        return listing_urls

    @classmethod
    def parse_product_html(cls, card: CardPrint, listing_url: str, page_content: str) -> StoreOffer | None:
        match = cls._colorme_pattern.search(page_content)
        if not match:
            return None
        try:
            product = json.loads(match.group(1)).get("product")
        except json.JSONDecodeError:
            return None
        if not isinstance(product, dict):
            return None
        raw_name = str(product.get("name") or "").strip()
        reference_match = cls._reference_pattern.search(raw_name)
        if not raw_name or not reference_match or not references_card(card, reference_match.group(1)):
            return None
        if not matches_card_finish(card, raw_name):
            return None
        raw_price = product.get("sales_price_including_tax", product.get("sales_price"))
        try:
            price = int(raw_price)
        except (TypeError, ValueError):
            return None
        raw_stock = product.get("stock_num")
        try:
            stock_count = int(raw_stock) if raw_stock is not None else None
        except (TypeError, ValueError):
            stock_count = None
        finish, finish_raw = finish_from_text(raw_name)
        return StoreOffer(
            store_id=cls.store_id,
            store_name=cls.store_name,
            raw_name=raw_name,
            price_yen=price,
            price_display=f"¥{price:,}",
            availability=(
                Availability.SOLD_OUT
                if stock_count == 0
                else Availability.IN_STOCK
                if stock_count is not None
                else Availability.UNKNOWN
            ),
            listing_url=listing_url,
            match_confidence=MatchConfidence.EXACT_PRINT,
            stock_count=stock_count,
            finish=finish,
            finish_raw=finish_raw,
        )
