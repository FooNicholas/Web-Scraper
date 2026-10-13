"""Hobby Station connector using browser-rendered Japanese-name searches."""

from __future__ import annotations

import re
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup
from bs4.element import Tag

from scraperbot.connectors.base import (
    StoreConnector,
    matches_card_finish,
    normalise_print_reference,
    print_reference,
    retailer_print_reference,
)
from scraperbot.connectors.browser import fetch_rendered_html
from scraperbot.models import Availability, CardPrint, MatchConfidence, StoreOffer, finish_from_text


class HobbyStationConnector(StoreConnector):
    """Read exact Japanese Vanguard prints from Hobby Station's browser search."""

    store_id = "hobbystation"
    store_name = "Hobby Station"
    base_url = "https://www.hobbystation-single.jp"
    search_url = f"{base_url}/vg/product/list"
    title_fragment = "ホビーステーション"
    _retailer_code_pattern = re.compile(r"(?<![A-Za-z0-9])VG-([A-Za-z0-9]+(?:-[A-Za-z0-9]+)+)", re.IGNORECASE)
    _price_pattern = re.compile(r"(?<!\d)([0-9][0-9,]*)\s*円")
    _stock_pattern = re.compile(r"在庫数\s*[:：]?\s*(\d+)")

    async def search(self, card: CardPrint) -> list[StoreOffer]:
        # The shop indexes its displayed VG- card code. Searching a Japanese
        # name after stripping punctuation can erase the match boundary and
        # return no results even when the exact printing is listed.
        reference = retailer_print_reference(card).replace("/", "-")
        if card.set_code == "DPR" and card.collector_number.isdigit():
            reference = f"D-PR-{int(card.collector_number):04d}"
        offers = await self._search_keyword(card, f"VG-{reference}")
        if offers or not card.japanese_name:
            return offers
        fragments = re.findall(r"[^\W_]+", card.japanese_name, flags=re.UNICODE)
        keyword = max(fragments, key=len, default="")
        if len(keyword) < 3:
            return []
        return await self._search_keyword(card, keyword)

    async def _search_keyword(self, card: CardPrint, keyword: str) -> list[StoreOffer]:
        params = {
            "search_word": keyword,
            "match_type": "1",
            "HbstSearchOptions[0][id]": "73",
            "HbstSearchOptions[0][search_keyword]": "",
            "HbstSearchOptions[0][Type]": "2",
            "HbstSearchOptions[0][search_keyword_ids][ids]": "",
            "HbstSearchOptions[1][id]": "74",
            "HbstSearchOptions[1][search_keyword_ids][ids]": "",
            "HbstSearchOptions[2][id]": "75",
            "HbstSearchOptions[2][search_keyword_ids][ids]": "",
            "HbstSearchOptions[3][id]": "76",
            "HbstSearchOptions[3][search_keyword_ids][ids]": "",
        }
        page = await fetch_rendered_html(
            f"{self.search_url}?{urlencode(params)}",
            store_name=self.store_name,
            title_fragment=self.title_fragment,
        )
        return self.parse_html(card, page)

    @classmethod
    def parse_html(cls, card: CardPrint, page_content: str) -> list[StoreOffer]:
        soup = BeautifulSoup(page_content, "lxml")
        expected_reference = normalise_print_reference(print_reference(card))
        offers: list[StoreOffer] = []
        seen: set[str] = set()
        for item in soup.select("li"):
            product_link = item.select_one('a[href*="/vg/product/detail/"]')
            if not product_link:
                continue
            item_text = item.get_text(" ", strip=True)
            code_match = cls._retailer_code_pattern.search(item_text)
            if not code_match:
                continue
            # Hobby Station prefixes all Vanguard product codes with "VG-";
            # its remaining hyphenated code maps to the canonical set/number.
            found_reference = normalise_print_reference(code_match.group(1))
            if found_reference != expected_reference and not (
                card.set_code == "DPR"
                and card.collector_number.isdigit()
                and (promo := re.fullmatch(r"D-PR-0*(\d+)", code_match.group(1), re.IGNORECASE))
                and int(promo.group(1)) == int(card.collector_number)
            ):
                continue
            raw_name = cls._name_text(item, product_link)
            if not raw_name or not matches_card_finish(card, raw_name):
                continue
            price_match = cls._price_pattern.search(item_text)
            if not price_match:
                continue
            listing_url = urljoin(cls.base_url, product_link.get("href", ""))
            if listing_url in seen:
                continue
            seen.add(listing_url)
            stock_match = cls._stock_pattern.search(item_text)
            stock_count = int(stock_match.group(1)) if stock_match else None
            image_labels = " ".join(image.get("alt", "") for image in item.select("img[alt]"))
            sold_out = any(
                marker in f"{item_text} {image_labels}".casefold()
                for marker in ("sold out", "soldout", "売り切れ", "在庫なし")
            )
            availability = (
                Availability.SOLD_OUT
                if sold_out or stock_count == 0
                else Availability.IN_STOCK
                if stock_count is not None or "◎" in item_text or "◯" in item_text
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
    def _name_text(item: Tag, product_link: Tag) -> str:
        name = item.select_one(".list_product_Name_pc")
        if name:
            return name.get_text(" ", strip=True)
        return product_link.get_text(" ", strip=True)
