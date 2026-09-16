"""G-Project connector using its public Japanese-name search and categories."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import httpx

from scraperbot.connectors.base import (
    StoreConnector,
    StoreUnavailableError,
    bounded_map,
    matches_card_finish,
    price_from_text,
    request_client,
)
from scraperbot.models import (
    Availability,
    CardPrint,
    MatchConfidence,
    StoreOffer,
    finish_from_text,
    normalise_set_code,
    normalise_text,
    split_finish_annotation,
)


class GProjectConnector(StoreConnector):
    """Read a bounded set/rarity-validated Japanese-name match from G-Project.

    G-Project's public Vanguard search and product pages omit the printed card
    serial.  The store does publish an exact Japanese product name plus its
    set and rarity-category path, so an offer is accepted only when all three
    match the selected canonical print.  The result remains explicitly marked
    ``EXACT_JAPANESE_NAME`` rather than pretending it has serial evidence.
    """

    store_id = "gproject"
    store_name = "G-Project TCG"
    base_url = "https://gprojecttcg.xsrv.jp"
    search_url = f"{base_url}/products/list"
    vanguard_category_id = "1050"
    _maximum_product_checks = 8

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client

    async def search(self, card: CardPrint) -> list[StoreOffer]:
        if not card.japanese_name:
            return []
        response = await self._get(
            self.search_url,
            params={"category_id": self.vanguard_category_id, "name": card.japanese_name},
        )
        listing_urls = self.parse_search_html(card, response.text)
        # The initial query is one exact Japanese name.  G-Project does not
        # expose a serial in that result, so inspect only this bounded set of
        # exact-name products to validate the published set/rarity categories.
        async def inspect(listing_url: str) -> StoreOffer | None:
            try:
                detail = await self._get(listing_url)
            except (httpx.HTTPError, StoreUnavailableError):
                return None
            return self.parse_product_html(card, listing_url, detail.text)

        return [
            offer
            for offer in await bounded_map(listing_urls[: self._maximum_product_checks], inspect)
            if offer
        ]

    async def _get(self, url: str, *, params: dict[str, str] | None = None) -> httpx.Response:
        headers = {"User-Agent": "JP-Price-Checker/0.1 (+approved personal price comparison)"}
        request_kwargs: dict[str, object] = {"headers": headers}
        if params is not None:
            request_kwargs["params"] = params
        async with request_client(self.client) as client:
            response = await client.get(url, **request_kwargs)
        if response.status_code in (403, 429):
            raise StoreUnavailableError("G-Project did not permit this price request.")
        if response.status_code == 404:
            raise StoreUnavailableError("G-Project no longer exposes this Vanguard product page.")
        response.raise_for_status()
        return response

    @classmethod
    def parse_search_html(cls, card: CardPrint, page_content: str) -> list[str]:
        soup = BeautifulSoup(page_content, "lxml")
        listing_urls: list[str] = []
        seen: set[str] = set()
        for item in soup.select("li.ec-shelfGrid__item"):
            product_link = item.select_one('a[href*="/products/detail/"]')
            if not product_link:
                continue
            raw_name = cls._listing_name(product_link)
            if not raw_name or not cls._matches_japanese_name(card, raw_name):
                continue
            listing_url = urljoin(cls.base_url, product_link.get("href", ""))
            if listing_url in seen:
                continue
            seen.add(listing_url)
            listing_urls.append(listing_url)
        return listing_urls

    @classmethod
    def parse_product_html(cls, card: CardPrint, listing_url: str, page_content: str) -> StoreOffer | None:
        soup = BeautifulSoup(page_content, "lxml")
        title = soup.select_one(".ec-productRole__title")
        raw_name = title.get_text(" ", strip=True) if title else ""
        if not raw_name or not cls._matches_japanese_name(card, raw_name):
            return None
        if not matches_card_finish(card, raw_name):
            return None
        categories = [category.get_text(" ", strip=True) for category in soup.select(".ec-productRole__category a")]
        if not cls._matches_selected_category(card, categories):
            return None
        price_node = soup.select_one(".ec-price__price") or soup.select_one(".ec-productRole__price")
        price = price_from_text(price_node.get_text(" ", strip=True) if price_node else "")
        if price is None:
            return None
        button = soup.select_one(".ec-productRole__btn button")
        button_text = button.get_text(" ", strip=True) if button else ""
        sold_out = bool(button and button.has_attr("disabled")) or "品切" in button_text or "SOLD OUT" in button_text.upper()
        availability = Availability.SOLD_OUT if sold_out else Availability.IN_STOCK if button else Availability.UNKNOWN
        finish, finish_raw = finish_from_text(raw_name)
        return StoreOffer(
            store_id=cls.store_id,
            store_name=cls.store_name,
            raw_name=raw_name,
            price_yen=price,
            price_display=f"¥{price:,}",
            availability=availability,
            listing_url=listing_url,
            match_confidence=MatchConfidence.EXACT_JAPANESE_NAME,
            stock_count=None,
            finish=finish,
            finish_raw=finish_raw,
        )

    @staticmethod
    def _listing_name(product_link: object) -> str:
        paragraphs = getattr(product_link, "find_all", lambda *_args, **_kwargs: [])("p", recursive=False)
        for paragraph in paragraphs:
            classes = paragraph.get("class", [])
            text = paragraph.get_text(" ", strip=True)
            if text and "price02-default" not in classes:
                return text
        return ""

    @staticmethod
    def _matches_japanese_name(card: CardPrint, listing_name: str) -> bool:
        if not card.japanese_name:
            return False
        bare_name, _, _ = split_finish_annotation(listing_name)
        return normalise_text(bare_name) == normalise_text(card.japanese_name)

    @classmethod
    def _matches_selected_category(cls, card: CardPrint, categories: list[str]) -> bool:
        has_set = any(normalise_set_code(category).startswith(card.set_code) for category in categories)
        if not has_set or not card.rarity:
            return False
        category_rarities = {
            token
            for category in categories
            for token in re.findall(r"[A-Z0-9+]+", category.upper())
        }
        return card.rarity in category_rarities
