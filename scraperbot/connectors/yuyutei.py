"""Yuyu-Tei connector with exact card-print matching and no live translation."""

from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import httpx

from scraperbot.connectors.base import (
    StoreConnector,
    StoreUnavailableError,
    matches_card_finish,
    price_from_text,
    references_card,
    request_client,
)
from scraperbot.models import Availability, CardPrint, MatchConfidence, StoreOffer, finish_from_text


class YuyuTeiConnector(StoreConnector):
    store_id = "yuyutei"
    store_name = "Yuyu-Tei"
    base_url = "https://yuyu-tei.jp"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        promo_page_url: Callable[[CardPrint], str | None] | None = None,
    ) -> None:
        self.client = client
        self.promo_page_url = promo_page_url

    async def search(self, card: CardPrint) -> list[StoreOffer]:
        known_promo_page = self.promo_page_url(card) if self.promo_page_url and card.set_code == "DPR" else None
        url = known_promo_page or f"{self.base_url}/sell/vg/s/{card.set_code.lower()}"
        headers = {"User-Agent": "ScraperBot/0.1 (+personal price comparison)"}
        async with request_client(self.client) as client:
            response = await client.get(url, headers=headers)
        if response.status_code == 404:
            raise StoreUnavailableError(f"Yuyu-Tei does not list the set {card.set_code}.")
        response.raise_for_status()
        return self.parse_html(card, response.text)

    @classmethod
    def parse_html(cls, card: CardPrint, page_content: str) -> list[StoreOffer]:
        soup = BeautifulSoup(page_content, "lxml")
        offers: list[StoreOffer] = []
        for item in soup.select("div.col-md"):
            name_heading = item.select_one("h4")
            price_element = item.select_one("strong")
            if not name_heading or not price_element:
                continue
            item_text = item.get_text(" ", strip=True)
            raw_name = name_heading.get_text(" ", strip=True)
            if not cls._contains_print_reference(card, item_text) or not matches_card_finish(card, raw_name):
                continue
            finish, finish_raw = finish_from_text(raw_name)
            card_link = item.select_one("a[href*='/sell/vg/card/']")
            stock_match = re.search(r"在庫\s*[:：]\s*(\d+)", item_text)
            stock_label = item.select_one(".cart_sell_zaiko")
            stock_label_text = stock_label.get_text(" ", strip=True) if stock_label else item_text
            price = price_from_text(price_element.get_text(" ", strip=True))
            if stock_match:
                stock_count = int(stock_match.group(1))
                availability = Availability.SOLD_OUT if stock_count == 0 else Availability.IN_STOCK
            elif "◯" in stock_label_text or "○" in stock_label_text:
                # Current sell pages use a circle rather than a numeric stock
                # count. It means the card can be added to the cart.
                stock_count = None
                availability = Availability.IN_STOCK
            elif (
                any(marker in stock_label_text for marker in ("×", "✕", "✖"))
                or item.select_one(".sold-out") is not None
                or any(marker in item_text for marker in ("在庫なし", "売り切れ", "SOLD OUT"))
            ):
                stock_count = None
                availability = Availability.SOLD_OUT
            else:
                stock_count = None
                availability = Availability.UNKNOWN
            if price is not None:
                price_display = f"¥{price:,}"
            else:
                price_display = "Price unavailable"
            offers.append(
                StoreOffer(
                    store_id=cls.store_id,
                    store_name=cls.store_name,
                    raw_name=raw_name,
                    price_yen=price,
                    price_display=price_display,
                    availability=availability,
                    listing_url=urljoin(cls.base_url, card_link["href"]) if card_link else None,
                    match_confidence=MatchConfidence.EXACT_PRINT,
                    stock_count=stock_count,
                    finish=finish,
                    finish_raw=finish_raw,
                )
            )
        return offers

    @staticmethod
    def _contains_print_reference(card: CardPrint, text: str) -> bool:
        candidates = re.findall(r"[A-Za-z0-9-]+/[A-Za-z0-9-]+", text)
        return any(references_card(card, candidate) for candidate in candidates)
