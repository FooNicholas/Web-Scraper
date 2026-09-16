"""Concurrent offer comparison with a small in-memory cache."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from threading import RLock
from time import monotonic
from typing import Iterable

import httpx

from scraperbot.connectors.base import (
    StoreConnector,
    StoreUnavailableError,
    active_request_client,
    use_shared_request_client,
)
from scraperbot.models import (
    Availability,
    CardFamilyComparisonResult,
    CardPrint,
    ComparisonResult,
    FamilyOffer,
    StoreOffer,
    StoreCheckTiming,
)


@dataclass(slots=True)
class _CachedResult:
    expires_at: float
    result: ComparisonResult


class ComparisonService:
    def __init__(self, connectors: Iterable[StoreConnector], *, cache_ttl_seconds: int = 120) -> None:
        self.connectors = tuple(connectors)
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cache: dict[str, _CachedResult] = {}
        self._cache_lock = RLock()

    async def compare(self, card: CardPrint, *, refresh: bool = False) -> ComparisonResult:
        cache_key = card.print_key
        with self._cache_lock:
            cached = self._cache.get(cache_key)
        if not refresh and cached and cached.expires_at > monotonic():
            return replace(cached.result, cached=True)

        started_at = monotonic()
        shared_client = active_request_client()
        if shared_client is not None:
            results = await self._check_all_connectors(card)
        else:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                with use_shared_request_client(client):
                    results = await self._check_all_connectors(card)
        offers: list[StoreOffer] = []
        no_active_listing: list[str] = []
        unavailable: list[str] = []
        failed: list[str] = []
        timings: list[StoreCheckTiming] = []
        for connector, outcome, elapsed_ms in results:
            if isinstance(outcome, StoreUnavailableError):
                unavailable.append(connector.store_name)
                outcome_name = "unavailable"
            elif isinstance(outcome, Exception):
                failed.append(connector.store_name)
                outcome_name = "failed"
            else:
                if outcome:
                    offers.extend(outcome)
                    outcome_name = "offers"
                else:
                    no_active_listing.append(connector.store_name)
                    outcome_name = "no_active_listing"
            timings.append(
                StoreCheckTiming(
                    connector.store_id,
                    connector.store_name,
                    outcome_name,
                    elapsed_ms,
                )
            )

        result = ComparisonResult(
            card=card,
            offers=tuple(sorted(offers, key=self._offer_sort_key)),
            no_active_listing_stores=tuple(no_active_listing),
            unavailable_stores=tuple(unavailable),
            failed_stores=tuple(failed),
            store_timings=tuple(timings),
            duration_ms=round((monotonic() - started_at) * 1000),
        )
        with self._cache_lock:
            self._cache[cache_key] = _CachedResult(monotonic() + self.cache_ttl_seconds, result)
        return result

    @staticmethod
    async def _check_connector(
        connector: StoreConnector, card: CardPrint
    ) -> tuple[StoreConnector, list[StoreOffer] | Exception, int]:
        started_at = monotonic()
        try:
            outcome: list[StoreOffer] | Exception = await connector.search(card)
        except Exception as error:  # Each store remains isolated from every other store.
            outcome = error
        return connector, outcome, round((monotonic() - started_at) * 1000)

    async def _check_all_connectors(
        self, card: CardPrint
    ) -> list[tuple[StoreConnector, list[StoreOffer] | Exception, int]]:
        """Start every store together; none is skipped to improve latency."""
        return await asyncio.gather(
            *(self._check_connector(connector, card) for connector in self.connectors),
        )

    async def compare_family(
        self, selected_card: CardPrint, printings: Iterable[CardPrint], *, refresh: bool = False
    ) -> CardFamilyComparisonResult:
        """Compare verified reprints concurrently and sort offers by live price.

        Every connector still receives one exact selected printing at a time;
        this method only aggregates those exact results after they return.
        """
        unique_printings = tuple(
            {
                card.print_key: card
                for card in printings
            }.values()
        )
        results = await asyncio.gather(
            *(self.compare(card, refresh=refresh) for card in unique_printings)
        )
        offers = [FamilyOffer(result.card, offer) for result in results for offer in result.offers]
        return CardFamilyComparisonResult(
            selected_card=selected_card,
            printings=unique_printings,
            offers=tuple(sorted(offers, key=self._family_offer_sort_key)),
        )

    def invalidate(self, card: CardPrint | None = None) -> None:
        with self._cache_lock:
            if card:
                self._cache.pop(card.print_key, None)
            else:
                self._cache.clear()

    @staticmethod
    def _offer_sort_key(offer: StoreOffer) -> tuple[int, int, str]:
        availability_rank = 0 if offer.availability == Availability.IN_STOCK else 1
        price_rank = offer.price_yen if offer.price_yen is not None else 10**12
        return availability_rank, price_rank, offer.store_name.casefold()

    @classmethod
    def _family_offer_sort_key(cls, family_offer: FamilyOffer) -> tuple[int, int, str, str]:
        offer = family_offer.offer
        availability_rank, price_rank, store_name = cls._offer_sort_key(offer)
        return availability_rank, price_rank, family_offer.card.display_code, store_name
