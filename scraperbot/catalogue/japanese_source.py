"""Official Japanese Cardfight!! Vanguard print-master importer."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import re
from typing import Iterable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
import httpx

from scraperbot.catalogue.official_source import OfficialExpansion, OfficialSourceError
from scraperbot.models import JapaneseCardPrint, normalise_set_code, split_finish_annotation


class OfficialJapaneseCardSource:
    """Read Japanese names and print references from Bushiroad's public list.

    This is deliberately separate from the English catalogue. It preserves a
    complete Japanese source of truth even when a card has no English name yet.
    """

    base_url = "https://cf-vanguard.com"
    card_list_url = f"{base_url}/cardlist/"
    card_search_url = f"{base_url}/cardlist/cardsearch/"
    card_search_extra_url = f"{base_url}/cardlist/cardsearch_ex/"
    product_api_url = f"{base_url}/manage/card-list-user/products"
    card_api_url = f"{base_url}/manage/card-list-user/list"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        page_delay: float = 0.15,
        request_attempts: int = 3,
    ) -> None:
        self.client = client
        self.page_delay = page_delay
        self.request_attempts = request_attempts
        self._api_mode = False

    async def list_expansions(self) -> list[OfficialExpansion]:
        expansions = self.parse_expansions(await self._get_text(self.card_list_url))
        if not expansions:
            expansions = self.parse_api_expansions(await self._get_json(self.product_api_url))
            self._api_mode = bool(expansions)
        if not expansions:
            raise OfficialSourceError("The official Japanese card list did not contain product expansions.")
        return expansions

    async def expansions_for_sets(self, set_codes: Iterable[str]) -> list[OfficialExpansion]:
        wanted = {normalise_set_code(code) for code in set_codes}
        selected = [
            expansion
            for expansion in await self.list_expansions()
            if expansion.set_code and normalise_set_code(expansion.set_code) in wanted
        ]
        found = {normalise_set_code(expansion.set_code or "") for expansion in selected}
        missing = sorted(wanted - found)
        if missing:
            raise OfficialSourceError("The official Japanese database does not list: " + ", ".join(missing))
        return selected

    async def cards_for_expansion(self, expansion: OfficialExpansion) -> list[JapaneseCardPrint]:
        if self._api_mode:
            return await self._api_cards_for_expansion(expansion)
        parameters = {"expansion": expansion.id, "view": "text"}
        html = await self._get_text(self.card_search_url, params=parameters)
        cards = self.parse_card_entries(html)
        for page in range(2, self.parse_page_count(html) + 1):
            if self.page_delay:
                await asyncio.sleep(self.page_delay)
            cards.extend(
                self.parse_card_entries(
                    await self._get_text(self.card_search_extra_url, params={**parameters, "page": page})
                )
            )
        return cards

    async def _api_cards_for_expansion(self, expansion: OfficialExpansion) -> list[JapaneseCardPrint]:
        cards: list[JapaneseCardPrint] = []
        seen_rows = 0
        for page in range(1, 101):
            if page > 1 and self.page_delay:
                await asyncio.sleep(self.page_delay)
            payload = await self._get_json(
                self.card_api_url,
                params={"expansion": expansion.id, "title": expansion.id,
                        "page": page, "per_page": 100, "sort": "new"},
            )
            rows = payload.get("items")
            if not isinstance(rows, list):
                raise OfficialSourceError("The official Japanese card API returned no card list.")
            cards.extend(self.parse_api_card_entries(rows, expansion.id))
            seen_rows += len(rows)
            total = payload.get("total")
            if not rows or (isinstance(total, int) and seen_rows >= total):
                break
        else:
            raise OfficialSourceError("The official Japanese card API exceeded its page limit.")
        return cards

    @classmethod
    def parse_api_expansions(cls, payload: dict[str, object]) -> list[OfficialExpansion]:
        rows = payload.get("items")
        if not isinstance(rows, list):
            return []
        expansions: list[OfficialExpansion] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                expansion_id = int(row.get("id", ""))
            except (TypeError, ValueError):
                continue
            title = str(row.get("name") or "").strip()
            if not title:
                continue
            match = re.search(r"[【〖]([A-Z]+-[A-Z]+\d+(?:-[A-Z]+)?)[】〗]", title)
            expansions.append(OfficialExpansion(expansion_id, match.group(1) if match else None, title))
        return expansions

    @classmethod
    def parse_api_card_entries(
        cls, rows: list[object], expansion_id: int
    ) -> list[JapaneseCardPrint]:
        cards: list[JapaneseCardPrint] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            number = str(row.get("card_number") or "").strip()
            parsed = cls._parse_card_number(number)
            name = str(row.get("card_name") or "").strip()
            if not parsed or not name:
                continue
            name, finish, finish_raw = split_finish_annotation(name)
            set_code, collector_number = parsed
            cards.append(
                JapaneseCardPrint(
                    set_code=set_code,
                    collector_number=collector_number,
                    rarity=str(row.get("rare") or cls._rarity_from_collector_number(collector_number)),
                    japanese_name=name,
                    source_url=f"{cls.card_list_url}?{urlencode({'cardno': number, 'expansion': expansion_id})}",
                    finish=finish,
                    finish_raw=finish_raw,
                )
            )
        return cards

    async def _get_json(self, url: str, *, params: dict[str, object] | None = None) -> dict[str, object]:
        try:
            payload = json.loads(await self._get_text(url, params=params))
        except (TypeError, ValueError) as error:
            raise OfficialSourceError("The official Japanese card API did not return JSON.") from error
        if not isinstance(payload, dict):
            raise OfficialSourceError("The official Japanese card API did not return an object.")
        return payload

    @classmethod
    def parse_expansions(cls, html: str) -> list[OfficialExpansion]:
        soup = BeautifulSoup(html, "lxml")
        result: list[OfficialExpansion] = []
        seen: set[int] = set()
        for product in soup.select(".product-item"):
            link = product.select_one("a[href*='expansion=']")
            title_node = product.select_one(".title")
            if not link or not title_node:
                continue
            query = parse_qs(urlparse(link.get("href", "")).query)
            try:
                expansion_id = int(query["expansion"][0])
            except (KeyError, ValueError, IndexError):
                continue
            if expansion_id in seen:
                continue
            seen.add(expansion_id)
            title = title_node.get_text(" ", strip=True)
            match = re.search(r"[【〖]([A-Z]+-[A-Z]+\d+(?:-[A-Z]+)?)[】〗]", title)
            result.append(OfficialExpansion(expansion_id, match.group(1) if match else None, title))
        return result

    @classmethod
    def parse_card_entries(cls, html: str) -> list[JapaneseCardPrint]:
        soup = BeautifulSoup(html, "lxml")
        items = soup.select("#cardlist-container li") or soup.select("li.ex-item")
        cards: list[JapaneseCardPrint] = []
        for item in items:
            number_node = item.select_one(".number")
            name_node = item.select_one("h5")
            link = item.select_one("a[href]")
            if not number_node or not name_node or not link:
                continue
            parsed = cls._parse_card_number(number_node.get_text(" ", strip=True))
            if not parsed:
                continue
            direct_name = "".join(name_node.find_all(string=True, recursive=False)).strip()
            name = direct_name or name_node.get_text(" ", strip=True)
            if not name:
                continue
            name, finish, finish_raw = split_finish_annotation(name)
            set_code, collector_number = parsed
            cards.append(
                JapaneseCardPrint(
                    set_code=set_code,
                    collector_number=collector_number,
                    rarity=cls._rarity_from_collector_number(collector_number),
                    japanese_name=name,
                    source_url=urljoin(cls.base_url, link["href"]),
                    finish=finish,
                    finish_raw=finish_raw,
                )
            )
        return cards

    @staticmethod
    def parse_page_count(html: str) -> int:
        matched = re.search(r"var\s+max_page\s*=\s*(\d+)", html)
        return int(matched.group(1)) if matched else 1

    @staticmethod
    def _parse_card_number(value: str) -> tuple[str, str] | None:
        compact = "".join(value.split())
        if "/" not in compact:
            return None
        set_code, collector = compact.split("/", maxsplit=1)
        return (set_code, collector) if set_code and collector else None

    @staticmethod
    def _rarity_from_collector_number(collector_number: str) -> str:
        matched = re.match(r"([A-Z]+)\d", collector_number.upper())
        return matched.group(1) if matched else ""

    async def _get_text(self, url: str, *, params: dict[str, object] | None = None) -> str:
        headers = {"User-Agent": "ScraperBot/0.1 (+local Japanese print import; polite sequential requests)"}
        response: httpx.Response | None = None
        for attempt in range(1, self.request_attempts + 1):
            try:
                if self.client:
                    response = await self.client.get(url, params=params, headers=headers)
                else:
                    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                        response = await client.get(url, params=params, headers=headers)
                response.raise_for_status()
                break
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as error:
                if attempt == self.request_attempts:
                    raise OfficialSourceError(
                        f"The official Japanese endpoint failed after {self.request_attempts} attempts."
                    ) from error
                await asyncio.sleep(attempt)
        assert response is not None
        if "ページが存在しません" in response.text or "<title>404" in response.text.casefold():
            raise OfficialSourceError("The official Japanese endpoint returned a not-found page.")
        return response.text
