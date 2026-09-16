"""Card Shop Olta connector using its public Vanguard product search API."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

import httpx

from scraperbot.connectors.base import (
    StoreConnector,
    StoreUnavailableError,
    matches_card_finish,
    references_card,
    request_client,
    retailer_print_reference,
)
from scraperbot.models import Availability, CardPrint, MatchConfidence, StoreOffer, finish_from_text


class OltaConnector(StoreConnector):
    """Read exact Japanese Vanguard prints from Card Shop Olta.

    The normal public storefront calls its product-search endpoint for every
    keyword result. Querying it by the selected Japanese serial yields exact
    SKU price, condition and stock values without treating reward points as
    inventory.
    """

    store_id = "olta"
    store_name = "Card Shop Olta"
    base_url = "https://olta-tcg.com"
    api_url = f"{base_url}/api"
    _reference_pattern = re.compile(r"\[\s*([A-Za-z0-9-]+/[A-Za-z0-9-]+)\s*\]")
    _query = """
    query FindProductFaces($page: Int, $perPage: Int, $where: ProductFaceWhereInput) {
      productFaces(page: $page, perPage: $perPage, where: $where) {
        items {
          name
          code
          productSkus {
            skuCode
            price
            stock
            noStockPurchasable
            productVariationValues { variationValueName }
          }
        }
      }
    }
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client

    async def search(self, card: CardPrint) -> list[StoreOffer]:
        reference = retailer_print_reference(card)
        payload = await self._post(
            self.api_url,
            json={
                "query": self._query,
                "variables": {
                    "page": 1,
                    "perPage": 50,
                    "where": {
                        "cardTitle": {"is": {"code": {"equals": "VG"}}},
                        "name": {"contains": reference},
                    },
                },
            },
        )
        if payload.get("errors"):
            raise StoreUnavailableError("Card Shop Olta returned an unsuccessful product response.")
        items = ((payload.get("data") or {}).get("productFaces") or {}).get("items") or []
        return self.parse_items(card, items)

    async def _post(self, url: str, *, json: Mapping[str, Any]) -> Mapping[str, Any]:
        headers = {
            "User-Agent": "JP-Price-Checker/0.1 (+approved personal price comparison)",
            "Content-Type": "application/json",
        }
        async with request_client(self.client) as client:
            response = await client.post(url, json=json, headers=headers)
        if response.status_code in (403, 429):
            raise StoreUnavailableError("Card Shop Olta did not permit this price request.")
        if response.status_code == 404:
            raise StoreUnavailableError("Card Shop Olta no longer exposes its Vanguard product search.")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise StoreUnavailableError("Card Shop Olta returned an invalid product response.")
        return payload

    @classmethod
    def parse_items(cls, card: CardPrint, items: list[Mapping[str, Any]]) -> list[StoreOffer]:
        offers: list[StoreOffer] = []
        seen: set[str] = set()
        for item in items:
            raw_name = str(item.get("name") or "").strip()
            reference_match = cls._reference_pattern.search(raw_name)
            product_code = str(item.get("code") or "").strip()
            if (
                not reference_match
                or not product_code
                or not references_card(card, reference_match.group(1))
                or not matches_card_finish(card, raw_name)
            ):
                continue
            finish, finish_raw = finish_from_text(raw_name)
            for sku in item.get("productSkus") or []:
                if not isinstance(sku, Mapping) or sku.get("price") is None:
                    continue
                sku_code = str(sku.get("skuCode") or "")
                key = f"{product_code}:{sku_code}"
                if key in seen:
                    continue
                seen.add(key)
                price = int(sku["price"])
                stock_count = int(sku["stock"]) if sku.get("stock") is not None else None
                offers.append(
                    StoreOffer(
                        store_id=cls.store_id,
                        store_name=cls.store_name,
                        raw_name=raw_name,
                        price_yen=price,
                        price_display=f"¥{price:,}",
                        availability=(
                            Availability.SOLD_OUT
                            if stock_count == 0 and not sku.get("noStockPurchasable")
                            else Availability.IN_STOCK
                            if stock_count is not None
                            else Availability.UNKNOWN
                        ),
                        listing_url=f"{cls.base_url}/VG/product/detail/{product_code}",
                        match_confidence=MatchConfidence.EXACT_PRINT,
                        condition=cls._condition(sku),
                        stock_count=stock_count,
                        finish=finish,
                        finish_raw=finish_raw,
                    )
                )
        return offers

    @staticmethod
    def _condition(sku: Mapping[str, Any]) -> str | None:
        values = sku.get("productVariationValues") or []
        if not values or not isinstance(values[0], Mapping):
            return None
        condition = str(values[0].get("variationValueName") or "").strip()
        return condition or None
