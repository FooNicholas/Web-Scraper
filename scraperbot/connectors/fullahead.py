"""FullAhead connector using its public exact serial search."""

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


class FullAheadConnector(StoreConnector):
    """Read exact Japanese Vanguard prints from FullAhead's public search."""

    store_id = "fullahead"
    store_name = "FullAhead"
    base_url = "https://fullahead-vg.com"
    search_url = f"{base_url}/shop/shopbrand.html"
    _reference_pattern = re.compile(r"([A-Za-z0-9-]+/[A-Za-z0-9-]+)")
    _price_pattern = re.compile(r"([0-9][0-9,]*)\s*円")
    _stock_pattern = re.compile(r"(?:残り|在庫).*?(\d+)\s*(?:点|個|枚)?")

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client

    async def search(self, card: CardPrint) -> list[StoreOffer]:
        response = await self._post(self.search_url, data={"search": retailer_print_reference(card)})
        offers = self.parse_html(card, self._decode(response))
        async def enrich(offer: StoreOffer) -> StoreOffer:
            if not offer.listing_url:
                return offer
            try:
                detail = await self._get(offer.listing_url)
            except (httpx.HTTPError, StoreUnavailableError):
                return offer
            return self._with_detail_stock(offer, self._decode(detail))

        enriched = await bounded_map(offers[:8], enrich, semaphore=self.bounded_request_limiter())
        offers[: len(enriched)] = enriched
        return offers

    async def _post(self, url: str, *, data: dict[str, str]) -> httpx.Response:
        return await self._request("post", url, data=data)

    async def _get(self, url: str) -> httpx.Response:
        return await self._request("get", url)

    async def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        headers = {"User-Agent": "JP-Price-Checker/0.1 (+approved personal price comparison)"}
        async with request_client(self.client) as client:
            response = await getattr(client, method)(url, headers=headers, **kwargs)
        if response.status_code in (403, 429):
            raise StoreUnavailableError("FullAhead did not permit this price request.")
        if response.status_code == 404:
            raise StoreUnavailableError("FullAhead no longer exposes this card page.")
        response.raise_for_status()
        return response

    @staticmethod
    def _decode(response: httpx.Response) -> str:
        """FullAhead's storefront declares EUC-JP but omits a useful HTTP charset."""
        return response.content.decode("euc_jp", errors="replace")

    @classmethod
    def parse_html(cls, card: CardPrint, page_content: str) -> list[StoreOffer]:
        soup = BeautifulSoup(page_content, "lxml")
        offers: list[StoreOffer] = []
        seen: set[str] = set()
        for item in soup.select(".indexItemBox > div"):
            product_link = item.select_one('a[href*="/shop/shopdetail.html"]')
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
        soup = BeautifulSoup(page_content, "lxml")
        stock_text = soup.get_text(" ", strip=True)
        stock_match = cls._stock_pattern.search(stock_text)
        stock_count = int(stock_match.group(1)) if stock_match else None
        sold_out = "売り切れ" in stock_text or "SOLD OUT" in stock_text.upper() or "在庫なし" in stock_text
        availability = (
            Availability.SOLD_OUT
            if sold_out or stock_count == 0
            else Availability.IN_STOCK
            if stock_count is not None or bool(soup.select_one(".add_cart"))
            else Availability.UNKNOWN
        )
        return replace(offer, availability=availability, stock_count=stock_count)

    @staticmethod
    def _name_text(link: object, fallback: str) -> str:
        image = getattr(link, "select_one", lambda _: None)("img[alt]")
        if image and image.get("alt"):
            return str(image["alt"]).strip()
        name = getattr(link, "select_one", lambda _: None)(".itemName")
        return name.get_text(" ", strip=True) if name else fallback
