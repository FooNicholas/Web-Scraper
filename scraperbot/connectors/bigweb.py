"""BigWeb connector using the store's public product JSON endpoint."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import httpx

from scraperbot.connectors.base import (
    StoreConnector,
    StoreUnavailableError,
    matches_card_finish,
    references_card,
    request_client,
)
from scraperbot.models import Availability, CardPrint, MatchConfidence, StoreOffer, finish_from_text, normalise_set_code


class BigWebConnector(StoreConnector):
    store_id = "bigweb"
    store_name = "BigWeb"
    cardsets_url = "https://www.bigweb.co.jp/ja/assets/data/cardsets.json"
    api_base_url = "https://api.bigweb.co.jp"
    game_id = 144
    max_promo_pages = 3

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client
        self._cardsets: dict[str, int] | None = None
        self._rarities: dict[int, dict[str, int]] = {}
        self._cache_lock = asyncio.Lock()

    async def search(self, card: CardPrint) -> list[StoreOffer]:
        cardset_id = await self._cardset_id(card.set_code)
        params: dict[str, object] = {"game_id": self.game_id, "cardsets": cardset_id, "is_box": 0}
        is_promo = card.set_code == "DPR"
        if is_promo:
            # The public frontend's `name` filter searches printed serials too.
            # No name translation or broad D-PR/rarity scan is necessary.
            params["name"] = f"D-PR/{card.collector_number}"
        elif card.rarity:
            params["rarity"] = await self._rarity_id(cardset_id, card.rarity)
        payloads = await self._product_pages(params, max_pages=self.max_promo_pages if is_promo else None)
        if any(not payload.get("success") for payload in payloads):
            raise StoreUnavailableError("BigWeb returned an unsuccessful product response.")
        return self.parse_items(card, [item for payload in payloads for item in payload.get("items", [])])

    async def _product_pages(
        self, params: Mapping[str, object], *, max_pages: int | None = None
    ) -> list[Mapping[str, Any]]:
        """Fetch pages for a set/rarity or a narrowly filtered promo serial.

        The public endpoint accepts a card set without a rarity and returns
        every variant in paginated batches. This is markedly cheaper than one
        request per rarity and keeps a name lookup responsive.
        """
        first = await self._get_json(f"{self.api_base_url}/products", params=params)
        if not first.get("success"):
            raise StoreUnavailableError("BigWeb returned an unsuccessful product response.")
        page_count = int((first.get("pagenate") or {}).get("pageCount") or 1)
        if max_pages is not None and page_count > max_pages:
            raise StoreUnavailableError(
                "BigWeb's promo search returned too many pages; broad catalogue fetching was stopped."
            )
        if page_count <= 1:
            return [first]
        remaining = await asyncio.gather(
            *(
                self._get_json(f"{self.api_base_url}/products", params={**params, "page": page})
                for page in range(2, page_count + 1)
            )
        )
        return [first, *remaining]

    async def _cardset_id(self, set_code: str) -> int:
        if self._cardsets is None:
            async with self._cache_lock:
                if self._cardsets is None:
                    payload = await self._get_json(self.cardsets_url)
                    game = next((entry for entry in payload if entry.get("id") == self.game_id), None)
                    if not game:
                        raise StoreUnavailableError("BigWeb no longer exposes its Vanguard card-set catalogue.")
                    self._cardsets = {
                        normalise_set_code(entry.get("code", "")): int(entry["id"])
                        for entry in game.get("cardsets", [])
                        if entry.get("code") and not entry.get("is_separation")
                    }
        cardset_id = self._cardsets.get(normalise_set_code(set_code))
        if cardset_id is None:
            raise StoreUnavailableError(f"BigWeb does not currently list the set {set_code}.")
        return cardset_id

    async def _rarity_id(self, cardset_id: int, rarity: str) -> int:
        rarities = await self._rarities_for_set(cardset_id)
        rarity_id = rarities.get(rarity.strip().upper())
        if rarity_id is None:
            raise StoreUnavailableError(f"BigWeb does not list rarity {rarity} for this set.")
        return rarity_id

    async def _rarities_for_set(self, cardset_id: int) -> dict[str, int]:
        if cardset_id not in self._rarities:
            async with self._cache_lock:
                if cardset_id not in self._rarities:
                    payload = await self._get_json(f"{self.api_base_url}/Cardsets/loadRarities/{cardset_id}")
                    self._rarities[cardset_id] = {
                        entry["name"].strip().upper(): int(entry["id"])
                        for entry in payload.get("rarities", [])
                        if entry.get("name")
                    }
        return self._rarities[cardset_id]

    async def _get_json(self, url: str, *, params: Mapping[str, Any] | None = None) -> Any:
        headers = {"User-Agent": "ScraperBot/0.1 (+personal price comparison)"}
        async with request_client(self.client) as client:
            response = await client.get(url, params=params, headers=headers)
        response.raise_for_status()
        return response.json()

    @classmethod
    def parse_items(cls, card: CardPrint, items: list[Mapping[str, Any]]) -> list[StoreOffer]:
        offers: list[StoreOffer] = []
        for item in items:
            reference_parts = (item.get("comment") or "").strip().split(maxsplit=1)
            # A set and rarity alone cannot establish a printed serial.
            if not reference_parts:
                continue
            reference = reference_parts[0]
            if not references_card(card, reference):
                continue
            raw_name = str(item.get("name", ""))
            if not matches_card_finish(card, raw_name):
                continue
            finish, finish_raw = finish_from_text(raw_name)
            raw_stock_count = item.get("stock_count")
            stock_count = int(raw_stock_count) if raw_stock_count is not None else None
            sold_out = bool(item.get("is_sold_out")) or stock_count == 0
            price = int(item["price"]) if item.get("price") is not None else None
            condition = ((item.get("condition") or {}).get("name") or "").strip() or None
            offers.append(
                StoreOffer(
                    store_id=cls.store_id,
                    store_name=cls.store_name,
                    raw_name=raw_name,
                    price_yen=price,
                    price_display=f"¥{price:,}" if price is not None else "Price unavailable",
                    availability=Availability.SOLD_OUT if sold_out else Availability.IN_STOCK,
                    listing_url=f"https://www.bigweb.co.jp/ja/products/vg/cardViewer/{item['id']}",
                    match_confidence=MatchConfidence.EXACT_PRINT,
                    condition=condition,
                    stock_count=stock_count,
                    finish=finish,
                    finish_raw=finish_raw,
                )
            )
        return offers
