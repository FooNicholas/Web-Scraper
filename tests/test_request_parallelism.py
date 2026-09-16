import asyncio
from time import monotonic

from scraperbot.connectors.base import bounded_map, request_client
from scraperbot.web import AsyncOperationRunner


async def _measure_detail_checks(concurrency: int) -> tuple[float, list[int], int]:
    active = 0
    peak_active = 0

    async def inspect(value: int) -> int:
        nonlocal active, peak_active
        active += 1
        peak_active = max(peak_active, active)
        try:
            await asyncio.sleep(0.04)
            return value
        finally:
            active -= 1

    started_at = monotonic()
    values = await bounded_map(list(range(6)), inspect, concurrency=concurrency)
    return monotonic() - started_at, values, peak_active


def test_two_at_a_time_detail_checks_are_faster_than_serial_and_keep_order() -> None:
    serial_elapsed, serial_values, serial_peak = asyncio.run(_measure_detail_checks(1))
    parallel_elapsed, parallel_values, parallel_peak = asyncio.run(_measure_detail_checks(2))

    assert serial_values == parallel_values == list(range(6))
    assert serial_peak == 1
    assert parallel_peak == 2
    # Six checks take roughly 240 ms serially and 120 ms in three polite
    # waves. This guards against a future accidental return to serial detail
    # fetches without raising the same-store request cap.
    assert parallel_elapsed < serial_elapsed * 0.7


def test_browser_request_runner_reuses_one_http_client_for_a_shopping_session() -> None:
    async def client_identity() -> int:
        async with request_client() as client:
            return id(client)

    runner = AsyncOperationRunner()
    try:
        assert runner.run(client_identity()) == runner.run(client_identity())
    finally:
        runner.close()
