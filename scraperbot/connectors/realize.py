"""REALiZE connector using its public exact serial search."""

from __future__ import annotations

from dataclasses import replace
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import httpx

from scraperbot.connectors.base import (
    StoreConnector,
    StoreUnavailableError,
    bounded_map,
    matches_card_finish,
    request_client,
    references_card,
    retailer_print_reference,
)
from scraperbot.models import Availability, CardPrint, MatchConfidence, StoreOffer, finish_from_text


class RealizeConnector(StoreConnector):
    """Read exact Japanese Vanguard prints from REALiZE's public storefront."""

    store_id = "realize"
    store_name = "REALiZE"
    base_url = "https://realize-tcg.com"
    search_url = f"{base_url}/"
    _reference_pattern = re.compile(r"([A-Za-z0-9-]+/[A-Za-z0-9-]+)")
    _price_pattern = re.compile(r"([0-9][0-9,]*)\s*円")
    _stock_pattern = re.compile(r'"stock_num"\s*:\s*(\d+)')

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client

    async def search(self, card: CardPrint) -> list[StoreOffer]:
        response = await self._get(
            self.search_url,
            params={"mode": "srh", "cid": "", "keyword": retailer_print_reference(card), "encode": "EUC-JP"},
        )
        offers = self.parse_html(card, self._decode(response))
        async def enrich(offer: StoreOffer) -> StoreOffer:
            if not offer.listing_url:
                return offer
            try:
                detail = await self._get(offer.listing_url)
            except (httpx.HTTPError, StoreUnavailableError):
                return offer
            return self._with_detail_stock(offer, self._decode(detail))

        enriched = await bounded_map(offers[:8], enrich)
        offers[: len(enriched)] = enriched
        return offers

    async def _get(self, url: str, *, params: dict[str, str] | None = None) -> httpx.Response:
        headers = {"User-Agent": "JP-Price-Checker/0.1 (+approved personal price comparison)"}
        request_kwargs = {"headers": headers}
        if params is not None:
            request_kwargs["params"] = params
        async with request_client(self.client) as client:
            response = await client.get(url, **request_kwargs)
        if response.status_code in (403, 429):
            raise StoreUnavailableError("REALiZE did not permit this price request.")
        if response.status_code == 404:
            raise StoreUnavailableError("REALiZE no longer exposes this card page.")
        response.raise_for_status()
        return response

    @staticmethod
    def _decode(response: httpx.Response) -> str:
        return response.content.decode("euc_jp", errors="replace")

    @classmethod
    def parse_html(cls, card: CardPrint, page_content: str) -> list[StoreOffer]:
        soup = BeautifulSoup(page_content, "lxml")
        offers: list[StoreOffer] = []
        seen: set[str] = set()
        for item in soup.select("li.productlist-unit"):
            product_link = item.select_one('a[href*="pid="]')
            if not product_link:
                continue
            raw_name = cls._name_text(product_link, item.get_text(" ", strip=True))
            reference_match = cls._reference_pattern.search(raw_name)
            if not reference_match or not references_card(card, reference_match.group(1)):
                continue
            if not matches_card_finish(card, raw_name):
                continue
            price_match = cls._price_pattern.search(item.get_text(" ", strip=True))
            if not price_match:
                continue
            listing_url = urljoin(cls.base_url, product_link.get("href", ""))
            if listing_url in seen:
                continue
            seen.add(listing_url)
            price = int(price_match.group(1).replace(",", ""))
            finish, finish_raw = finish_from_text(raw_name)
            offers.append(
                StoreOffer(
                    store_id=cls.store_id,
                    store_name=cls.store_name,
                    raw_name=raw_name,
                    price_yen=price,
                    price_display=f"¥{price:,}",
                    availability=Availability.UNKNOWN,
                    listing_url=listing_url,
                    match_confidence=MatchConfidence.EXACT_PRINT,
                    finish=finish,
                    finish_raw=finish_raw,
                )
            )
        return offers

    @classmethod
    def _with_detail_stock(cls, offer: StoreOffer, page_content: str) -> StoreOffer:
        stock_match = cls._stock_pattern.search(page_content)
        stock_count = int(stock_match.group(1)) if stock_match else None
        sold_out = "SOLD OUT" in page_content.upper() or '"availability": "https://schema.org/OutOfStock"' in page_content
        availability = (
            Availability.SOLD_OUT
            if sold_out or stock_count == 0
            else Availability.IN_STOCK
            if stock_count is not None
            else Availability.UNKNOWN
        )
        return replace(offer, availability=availability, stock_count=stock_count)

    @staticmethod
    def _name_text(link: object, fallback: str) -> str:
        image = getattr(link, "select_one", lambda _: None)("img[alt]")
        return str(image["alt"]).strip() if image and image.get("alt") else fallback
