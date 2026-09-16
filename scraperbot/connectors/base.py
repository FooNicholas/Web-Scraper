"""Shared connector protocol and exact print-reference matching helpers."""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
import re
from typing import TypeVar

import httpx

from scraperbot.models import CardPrint, Finish, StoreOffer, finish_from_text


REQUEST_TIMEOUT_SECONDS = 20.0
# Product detail pages are needed for precise stock on a few stores. Two at a
# time cuts their serial wait without turning one user selection into an
# aggressive same-store request burst.
DETAIL_REQUEST_CONCURRENCY = 2

_shared_request_client: ContextVar[httpx.AsyncClient | None] = ContextVar(
    "scraperbot_shared_request_client", default=None
)
_Input = TypeVar("_Input")
_Output = TypeVar("_Output")


class StoreUnavailableError(RuntimeError):
    """The store cannot currently answer a lookup for this card."""


class StoreConnector(ABC):
    store_id: str
    store_name: str

    @abstractmethod
    async def search(self, card: CardPrint) -> list[StoreOffer]:
        """Return offers matched to exactly one selected card print."""

    def bounded_request_limiter(self) -> asyncio.Semaphore:
        """Return this store's session-wide limiter for bounded follow-up work.

        An aggregate comparison can check several printings at once. Keeping
        this semaphore on the connector rather than one `bounded_map` call
        means those printings still share the same polite per-store cap.
        Direct test calls on a different event loop receive a new semaphore.
        """
        loop = asyncio.get_running_loop()
        if getattr(self, "_bounded_request_loop", None) is not loop:
            self._bounded_request_loop = loop
            self._bounded_request_semaphore = asyncio.Semaphore(DETAIL_REQUEST_CONCURRENCY)
        return self._bounded_request_semaphore


@contextmanager
def use_shared_request_client(client: httpx.AsyncClient):
    """Make one comparison-scoped client available to cooperating connectors."""
    token = _shared_request_client.set(client)
    try:
        yield
    finally:
        _shared_request_client.reset(token)


def active_request_client() -> httpx.AsyncClient | None:
    """Return the client supplied by the active comparison or web session."""
    return _shared_request_client.get()


@asynccontextmanager
async def request_client(injected: httpx.AsyncClient | None = None):
    """Yield an injected, comparison-scoped, or one-off HTTP client.

    Connectors retain injected clients for fixture tests. During normal price
    checks, multi-request connectors reuse the comparison's client for DNS,
    TLS, and keep-alive connections. A direct connector call still works on
    its own with the historic timeout behaviour.
    """
    if injected is not None:
        yield injected
        return
    shared = _shared_request_client.get()
    if shared is not None:
        yield shared
        return
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True) as client:
        yield client


async def bounded_map(
    values: Sequence[_Input],
    operation: Callable[[_Input], Awaitable[_Output]],
    *,
    concurrency: int = DETAIL_REQUEST_CONCURRENCY,
    semaphore: asyncio.Semaphore | None = None,
) -> list[_Output]:
    """Run bounded requests without changing output order or dropping values."""
    if concurrency < 1:
        raise ValueError("Detail request concurrency must be at least one.")
    if not values:
        return []
    limiter = semaphore or asyncio.Semaphore(concurrency)

    async def run(value: _Input) -> _Output:
        async with limiter:
            return await operation(value)

    return list(await asyncio.gather(*(run(value) for value in values)))


def print_reference(card: CardPrint) -> str:
    return f"{card.set_code}/{card.collector_number}"


def retailer_print_reference(card: CardPrint) -> str:
    """Return the conventional hyphenated Japanese serial used by stores.

    Database keys intentionally remove punctuation so variants such as
    ``D-PR/953`` and ``DPR953`` resolve to the same print.  Store search boxes,
    however, commonly index the displayed Japanese form with its hyphens.
    """
    set_code = card.set_code
    if set_code == "DPR":
        displayed_set = "D-PR"
    else:
        matched = re.fullmatch(r"(DZ|D|V)(LBT|TTD|BT|SS|SD|TD|TB|PS|PV|VS)(\d+)", set_code)
        displayed_set = f"{matched.group(1)}-{matched.group(2)}{matched.group(3)}" if matched else set_code
    return f"{displayed_set}/{card.collector_number}"


def normalise_print_reference(value: str) -> str:
    """Make retailer variants such as ``DZ-BT16/FFR02`` comparable."""
    return re.sub(r"[^A-Za-z0-9]", "", value).upper()


def references_card(card: CardPrint, retailer_reference: str) -> bool:
    return normalise_print_reference(print_reference(card)) == normalise_print_reference(retailer_reference)


def matches_card_finish(card: CardPrint, listing_name: str) -> bool:
    """Reject a seller listing only when it explicitly names another finish.

    Unknown is deliberately permissive. A retailer may omit finish data even
    when the selected canonical print has one, and absence is not evidence of
    a standard finish.
    """
    listing_finish, _ = finish_from_text(listing_name)
    return (
        card.finish is Finish.UNKNOWN
        or listing_finish is Finish.UNKNOWN
        or card.finish is listing_finish
    )


def price_from_text(value: str) -> int | None:
    digits = re.sub(r"[^0-9]", "", value)
    return int(digits) if digits else None
