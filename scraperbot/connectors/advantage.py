"""TCG Advantage connector using its public exact product-code search."""

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
)
from scraperbot.models import Availability, CardPrint, MatchConfidence, StoreOffer, finish_from_text


class AdvantageConnector(StoreConnector):
    """Read exact Japanese Vanguard prints from TCG Advantage's public search.

    Advantage indexes Vanguard singles under a displayed inventory code such
    as ``VGDZBT16-FFR02``. That is a mechanical representation of the
    selected Japanese printing, rather than a card-name search.
    """

    store_id = "advantage"
    store_name = "TCG Advantage"
    base_url = "https://www.advantagetcg.jp"
    search_url = f"{base_url}/product-list"
    _price_pattern = re.compile(r"([0-9][0-9,]*)\s*円")
    _stock_pattern = re.compile(r"在庫数\s*(\d+)\s*[点枚個]?")

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client

    async def search(self, card: CardPrint) -> list[StoreOffer]:
        response = await self._get(self.search_url, params={"keyword": self.product_code(card)})
        return self.parse_html(card, response.text)

    async def _get(self, url: str, *, params: dict[str, str]) -> httpx.Response:
        headers = {"User-Agent": "JP-Price-Checker/0.1 (+approved personal price comparison)"}
        async with request_client(self.client) as client:
            response = await client.get(url, params=params, headers=headers)
        if response.status_code in (403, 429):
            raise StoreUnavailableError("TCG Advantage did not permit this price request.")
        if response.status_code == 404:
            raise StoreUnavailableError("TCG Advantage no longer exposes its product search page.")
        response.raise_for_status()
        return response

    @staticmethod
    def product_code(card: CardPrint) -> str:
        """Return Advantage's exact inventory-code query for one print."""
        return f"VG{card.set_code}-{card.collector_number}"

    @classmethod
    def parse_html(cls, card: CardPrint, page_content: str) -> list[StoreOffer]:
        soup = BeautifulSoup(page_content, "lxml")
        offers: list[StoreOffer] = []
        seen: set[str] = set()
        for product_link in soup.select('table.list_item_table h2 a[href*="/product/"]'):
            if not isinstance(product_link, Tag):
                continue
            model_number = product_link.select_one(".model_number")
            if not model_number:
                continue
            product_code = model_number.get_text(" ", strip=True).strip("[]")
            if not cls._references_card(card, product_code):
                continue
            raw_name = product_link.get_text(" ", strip=True).replace(model_number.get_text(" ", strip=True), "").strip()
            if not raw_name or not matches_card_finish(card, raw_name):
                continue
            item = product_link.find_parent("td")
            if not item:
                continue
            item_text = item.get_text(" ", strip=True)
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
                    listing_url=listing_url,
                    match_confidence=MatchConfidence.EXACT_PRINT,
                    stock_count=stock_count,
                    finish=finish,
                    finish_raw=finish_raw,
                )
            )
        return offers

    @staticmethod
    def _references_card(card: CardPrint, product_code: str) -> bool:
        """Compare the printed code after Advantage's ``VG`` game prefix."""
        return product_code.upper().startswith("VG") and references_card(card, product_code[2:])
