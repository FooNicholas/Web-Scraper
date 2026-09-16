"""VanHappy connector using its public name/serial search page."""

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


class VanHappyConnector(StoreConnector):
    """Read exact Vanguard print offers from VanHappy search results.

    VanHappy shows each product's printed reference in square brackets, plus
    its displayed yen price and numeric stock count. The reference is the
    match key; D-PR candidates are retrieved by serial, other cards by name.
    """

    store_id = "vanhappy"
    store_name = "VanHappy"
    base_url = "https://www.van-happy.com"
    search_url = f"{base_url}/view/search"
    _reference_pattern = re.compile(r"\[([A-Za-z0-9-]+/[A-Za-z0-9-]+)\]")
    _stock_pattern = re.compile(r"在庫\s*(\d+)\s*個")

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client

    async def search(self, card: CardPrint) -> list[StoreOffer]:
        keyword = f"D-PR/{card.collector_number}" if card.set_code == "DPR" else card.japanese_name
        if not keyword:
            return []
        response = await self._get(self.search_url, params={"search_keyword": keyword})
        return self.parse_html(card, response.text)

    async def _get(self, url: str, *, params: dict[str, str]) -> httpx.Response:
        headers = {"User-Agent": "JP-Price-Checker/0.1 (+approved personal price comparison)"}
        async with request_client(self.client) as client:
            response = await client.get(url, params=params, headers=headers)
        if response.status_code in (403, 429):
            raise StoreUnavailableError("VanHappy did not permit this price request.")
        if response.status_code == 404:
            raise StoreUnavailableError("VanHappy no longer exposes its card search page.")
        response.raise_for_status()
        return response

    @classmethod
    def parse_html(cls, card: CardPrint, page_content: str) -> list[StoreOffer]:
        soup = BeautifulSoup(page_content, "lxml")
        offers: list[StoreOffer] = []
        seen: set[str] = set()
        for item in soup.select(".product-card"):
            name_node = item.select_one(".product-card__name")
            price_node = item.select_one(".product-card__price")
            if not name_node or not price_node:
                continue
            raw_name = name_node.get_text(" ", strip=True)
            reference_match = cls._reference_pattern.search(raw_name)
            if (
                not reference_match
                or not references_card(card, reference_match.group(1))
                or not matches_card_finish(card, raw_name)
            ):
                continue
            finish, finish_raw = finish_from_text(raw_name)
            price_match = re.search(r"[¥￥]\s*([0-9][0-9,]*)", price_node.get_text(" ", strip=True))
            if not price_match:
                continue
            stock_text = cls._node_text(item.select_one(".product-card__stock"))
            stock_match = cls._stock_pattern.search(stock_text)
            stock_count = int(stock_match.group(1)) if stock_match else None
            sold_out = bool(item.select_one(".product-card__badge--soldout"))
            availability = (
                Availability.SOLD_OUT
                if sold_out or stock_count == 0
                else Availability.IN_STOCK
                if stock_count is not None or item.select_one("button:not([disabled])")
                else Availability.UNKNOWN
            )
            product_link = name_node.select_one('a[href*="/view/item/"]')
            listing_url = urljoin(cls.base_url, product_link["href"]) if product_link else None
            key = listing_url or raw_name
            if key in seen:
                continue
            seen.add(key)
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

    @staticmethod
    def _node_text(node: Tag | None) -> str:
        return node.get_text(" ", strip=True) if node else ""
