"""Small local web interface sharing the Telegram bot's catalogue and stores."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
from threading import Event, RLock, Thread
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from scraperbot.catalogue.refresh import refresh_catalogue
from scraperbot.catalogue.repository import CatalogueRepository
from scraperbot.config import Settings
from scraperbot.distribution import (
    CatalogueSnapshotError,
    CatalogueUpdateClient,
    local_version_path,
    read_local_snapshot,
    remove_catalogue_backup,
    replace_catalogue_atomically,
    restore_previous_catalogue,
    seed_bundled_catalogue,
    validate_catalogue_database,
)
from scraperbot.connectors.advantage import AdvantageConnector
from scraperbot.connectors.bigweb import BigWebConnector
from scraperbot.connectors.cardmax import CardMaxConnector
from scraperbot.connectors.cardrush import CardRushConnector
from scraperbot.connectors.clabo import CLaboConnector
from scraperbot.connectors.avalon import AvalonConnector
from scraperbot.connectors.amenitydream import AmenityDreamConnector
from scraperbot.connectors.fullahead import FullAheadConnector
from scraperbot.connectors.gamers import GamersConnector
from scraperbot.connectors.gproject import GProjectConnector
from scraperbot.connectors.isei import IseiConnector
from scraperbot.connectors.manasource import ManaSourceConnector
from scraperbot.connectors.manzokuya import ManzokuyaConnector
from scraperbot.connectors.mastersguild import MastersGuildConnector
from scraperbot.connectors.net193 import Net193Connector
from scraperbot.connectors.noah import NoahConnector
from scraperbot.connectors.olta import OltaConnector
from scraperbot.connectors.pao import PAOConnector
from scraperbot.connectors.pachipachi import PachipachiConnector
from scraperbot.connectors.realize import RealizeConnector
from scraperbot.connectors.ryuunoshippo import RyuunoshippoConnector
from scraperbot.connectors.squarebushiroad import SquareBushiroadConnector
from scraperbot.connectors.torecolo import TorecoloConnector
from scraperbot.connectors.torecaplaza import TorecaPlazaConnector
from scraperbot.connectors.vanhappy import VanHappyConnector
from scraperbot.connectors.yuyutei import YuyuTeiConnector
from scraperbot.connectors.base import use_shared_request_client
from scraperbot.models import CardFamilyComparisonResult, CardPrint, ComparisonResult, StoreOffer, normalise_finish
from scraperbot.query import parse_name_search_query
from scraperbot.services.comparison import ComparisonService


MAX_QUERY_LENGTH = 120
# Shared-name promo cards may have dozens of distinct Japanese printings. Keep
# the browser list large enough for the user to choose the exact one.
MAX_CHOICES = 40


@dataclass(slots=True)
class _CatalogueRebuildState:
    """Small, local-only status record for an explicitly requested rebuild."""

    status: str = "idle"
    message: str = ""
    english_prints_imported: int | None = None
    japanese_prints_imported: int | None = None
    fandom_failed_sets: int | None = None

    def payload(self) -> dict[str, object | None]:
        return {
            "status": self.status,
            "message": self.message,
            "english_prints_imported": self.english_prints_imported,
            "japanese_prints_imported": self.japanese_prints_imported,
            "fandom_failed_sets": self.fandom_failed_sets,
        }


class LocalPriceCheckWeb:
    """Application-facing operations used by the local HTTP handler."""

    def __init__(
        self,
        catalogue: CatalogueRepository,
        comparison: ComparisonService,
        *,
        update_client: CatalogueUpdateClient | None = None,
        catalogue_refresher: Any = refresh_catalogue,
    ) -> None:
        self.catalogue = catalogue
        self.comparison = comparison
        self._catalogue_db = catalogue.path
        self._update_client = update_client
        self._catalogue_lock = RLock()
        self._catalogue_refresher = catalogue_refresher
        self._catalogue_rebuild_lock = RLock()
        self._catalogue_rebuild = _CatalogueRebuildState()
        self._catalogue_maintenance_lock = RLock()
        self._catalogue_snapshot_update_active = False

    def search(
        self, raw_query: str, *, rarities: tuple[str, ...] = (), finishes: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        with self._catalogue_lock:
            parsed_query = parse_name_search_query(raw_query[:MAX_QUERY_LENGTH])
            serial_card = self.catalogue.lookup_japanese_serial(raw_query[:MAX_QUERY_LENGTH])
            if serial_card and not (parsed_query.rarities or parsed_query.finishes or rarities or finishes):
                return {
                    "query": raw_query.strip(),
                    "rarity": None,
                    "rarities": [],
                    "finishes": [],
                    "mode": "japanese_serial",
                    "cards": [self._search_card_payload(serial_card)],
                }
            if not parsed_query.name:
                raise ValueError("Enter a card name to search.")
            selected_rarities = tuple(
                sorted({*parsed_query.rarities, *(value.strip().upper() for value in rarities if value.strip())})
            )
            selected_finishes = tuple(
                sorted(
                    {*parsed_query.finishes, *(normalise_finish(value) for value in finishes)},
                    key=lambda finish: finish.value,
                )
            )
            cards = self.catalogue.search(
                parsed_query.name,
                rarities=selected_rarities,
                finishes=selected_finishes,
                limit=MAX_CHOICES,
                japanese_only=True,
            )
            return {
                "query": parsed_query.name,
                "rarity": selected_rarities[0] if len(selected_rarities) == 1 else None,
                "rarities": list(selected_rarities),
                "finishes": [finish.value for finish in selected_finishes],
                "mode": "name",
                "cards": [self._search_card_payload(card) for card in cards],
            }

    async def compare(self, print_id: int, *, refresh: bool = False) -> dict[str, Any]:
        with self._catalogue_lock:
            card = self.catalogue.get_user_selection(print_id)
        if not card:
            raise LookupError("That Japanese-market card print is no longer available for comparison.")
        result = await self.comparison.compare(card, refresh=refresh)
        payload = self._comparison_payload(result)
        with self._catalogue_lock:
            payload["family_print_count"] = len(self.catalogue.verified_reprint_family(card))
        return payload

    async def compare_family(self, print_id: int, *, refresh: bool = False) -> dict[str, Any]:
        with self._catalogue_lock:
            card = self.catalogue.get_user_selection(print_id)
            family = self.catalogue.verified_reprint_family(card) if card else ()
        if not card:
            raise LookupError("That Japanese-market card print is no longer available for comparison.")
        return self._family_comparison_payload(
            await self.comparison.compare_family(card, family, refresh=refresh)
        )

    def promo_catalogue_page_url(self, card: CardPrint) -> str | None:
        """Resolve stored promo locations from whichever snapshot is active."""
        with self._catalogue_lock:
            return self.catalogue.promo_catalogue_page_url(
                "yuyutei", card.set_code, card.collector_number
            )

    def catalogue_update_status(self) -> dict[str, object | None]:
        if not self._update_client:
            return {"enabled": False, "status": "not_configured"}
        return self._update_client.status(self._catalogue_db).to_dict()

    def apply_catalogue_update(self) -> dict[str, object | None]:
        """Apply a user-approved release snapshot without interrupting data integrity.

        The file is fully downloaded and validated before the live connection is
        closed. On a reopen failure the previous SQLite file is restored.
        """
        if not self._update_client or not self._update_client.enabled:
            raise CatalogueSnapshotError("Catalogue updates are not configured for this app build.")
        self._begin_catalogue_snapshot_update()
        try:
            snapshot = self._update_client.fetch_snapshot()
            local = read_local_snapshot(self._catalogue_db)
            if local and local.catalogue_version == snapshot.catalogue_version and local.sha256 == snapshot.sha256:
                return {
                    "enabled": True,
                    "status": "up_to_date",
                    "current_version": local.catalogue_version,
                    "available_version": snapshot.catalogue_version,
                    "generated_at": snapshot.generated_at,
                }
            staged = self._update_client.stage_snapshot(snapshot, self._catalogue_db.parent)
            backup: Path | None = None
            with self._catalogue_lock:
                self.catalogue.close()
                try:
                    backup = replace_catalogue_atomically(staged, self._catalogue_db, snapshot)
                    replacement = CatalogueRepository(self._catalogue_db)
                except Exception:
                    restore_previous_catalogue(self._catalogue_db, backup)
                    # A restore might bring back an older database after its
                    # version sidecar was already written. Remove the sidecar so
                    # the next explicit check cannot mistake it for the new one.
                    local_version_path(self._catalogue_db).unlink(missing_ok=True)
                    self.catalogue = CatalogueRepository(self._catalogue_db)
                    raise
                else:
                    self.catalogue = replacement
                    self.comparison.invalidate()
                    remove_catalogue_backup(backup)
            return {
                "enabled": True,
                "status": "updated",
                "current_version": snapshot.catalogue_version,
                "available_version": snapshot.catalogue_version,
                "generated_at": snapshot.generated_at,
            }
        finally:
            self._end_catalogue_snapshot_update()

    def catalogue_rebuild_status(self) -> dict[str, object | None]:
        """Return local progress without contacting any source or store."""
        with self._catalogue_rebuild_lock:
            return self._catalogue_rebuild.payload()

    def start_catalogue_rebuild(self) -> dict[str, object | None]:
        """Start one user-approved source rebuild on a staged database copy."""
        with self._catalogue_maintenance_lock:
            if self._catalogue_snapshot_update_active:
                raise CatalogueSnapshotError("Wait for the published catalogue update to finish first.")
            with self._catalogue_rebuild_lock:
                if self._catalogue_rebuild.status == "running":
                    return self._catalogue_rebuild.payload()
                self._catalogue_rebuild = _CatalogueRebuildState(
                    status="running",
                    message="Preparing your current catalogue for a safe rebuild…",
                )
        Thread(target=self._run_catalogue_rebuild, name="jp-price-checker-catalogue-rebuild", daemon=True).start()
        return self.catalogue_rebuild_status()

    def _begin_catalogue_snapshot_update(self) -> None:
        """Reserve the catalogue for one explicit published snapshot install."""
        with self._catalogue_maintenance_lock:
            if self._catalogue_snapshot_update_active:
                raise CatalogueSnapshotError("A published catalogue update is already in progress.")
            if self._catalogue_rebuild_is_running():
                raise CatalogueSnapshotError("Wait for the catalogue rebuild from sources to finish first.")
            self._catalogue_snapshot_update_active = True

    def _end_catalogue_snapshot_update(self) -> None:
        with self._catalogue_maintenance_lock:
            self._catalogue_snapshot_update_active = False

    def _catalogue_rebuild_is_running(self) -> bool:
        with self._catalogue_rebuild_lock:
            return self._catalogue_rebuild.status == "running"

    def _set_catalogue_rebuild_message(self, message: str) -> None:
        with self._catalogue_rebuild_lock:
            if self._catalogue_rebuild.status == "running":
                self._catalogue_rebuild.message = message

    def _run_catalogue_rebuild(self) -> None:
        staged = self._catalogue_db.with_name(f".{self._catalogue_db.name}.rebuild-{uuid4().hex}.sqlite3")
        try:
            self._set_catalogue_rebuild_message("Copying your current catalogue before contacting sources…")
            with self._catalogue_lock:
                destination = sqlite3.connect(staged)
                try:
                    self.catalogue.connection.backup(destination)
                finally:
                    destination.close()
            result = asyncio.run(
                self._catalogue_refresher(
                    staged,
                    repair_regional=True,
                    progress=self._set_catalogue_rebuild_message,
                )
            )
            validate_catalogue_database(staged)
            self._set_catalogue_rebuild_message("Validating and installing the rebuilt catalogue…")
            self._install_rebuilt_catalogue(staged)
        except Exception as error:
            staged.unlink(missing_ok=True)
            with self._catalogue_rebuild_lock:
                self._catalogue_rebuild.status = "failed"
                self._catalogue_rebuild.message = f"Catalogue rebuild failed: {error}"
        else:
            with self._catalogue_rebuild_lock:
                self._catalogue_rebuild = _CatalogueRebuildState(
                    status="completed",
                    message="Catalogue rebuilt from approved sources. Search again to use the updated card list.",
                    english_prints_imported=result.english_prints_imported,
                    japanese_prints_imported=result.japanese_prints_imported,
                    fandom_failed_sets=len(result.fandom_failed_sets),
                )

    def _install_rebuilt_catalogue(self, staged: Path) -> None:
        """Replace the live database only after a full staged rebuild succeeds."""
        backup = self._catalogue_db.with_name(f".{self._catalogue_db.name}.rebuild-previous")
        with self._catalogue_lock:
            backup.unlink(missing_ok=True)
            self.catalogue.close()
            moved_existing = False
            try:
                if self._catalogue_db.exists():
                    os.replace(self._catalogue_db, backup)
                    moved_existing = True
                os.replace(staged, self._catalogue_db)
                # This database is locally rebuilt, rather than a particular
                # signed release snapshot. A later user-approved release can
                # still supersede it normally.
                local_version_path(self._catalogue_db).unlink(missing_ok=True)
                self.catalogue = CatalogueRepository(self._catalogue_db)
            except Exception:
                if moved_existing and backup.exists():
                    self._catalogue_db.unlink(missing_ok=True)
                    os.replace(backup, self._catalogue_db)
                self.catalogue = CatalogueRepository(self._catalogue_db)
                raise
            else:
                self.comparison.invalidate()
                backup.unlink(missing_ok=True)

    @staticmethod
    def _card_payload(card: CardPrint) -> dict[str, Any]:
        return {
            "id": card.id,
            "english_name": card.english_name,
            "japanese_name": card.japanese_name,
            "set_code": card.set_code,
            "collector_number": card.collector_number,
            "rarity": card.rarity,
            "finish": card.finish.value,
            "finish_raw": card.finish_raw,
            "display_code": card.display_code,
            "source": card.source,
        }

    def _search_card_payload(self, card: CardPrint) -> dict[str, Any]:
        payload = self._card_payload(card)
        payload["family_print_count"] = len(self.catalogue.verified_reprint_family(card))
        return payload

    @classmethod
    def _comparison_payload(cls, result: ComparisonResult) -> dict[str, Any]:
        return {
            "card": cls._card_payload(result.card),
            "offers": [cls._offer_payload(offer) for offer in result.offers],
            "no_active_listing_stores": list(result.no_active_listing_stores),
            "unavailable_stores": list(result.unavailable_stores),
            "failed_stores": list(result.failed_stores),
            "duration_ms": result.duration_ms,
            "cached": result.cached,
            "store_timings": [
                {
                    "store_id": timing.store_id,
                    "store_name": timing.store_name,
                    "outcome": timing.outcome,
                    "elapsed_ms": timing.elapsed_ms,
                }
                for timing in result.store_timings
            ],
        }

    @classmethod
    def _family_comparison_payload(cls, result: CardFamilyComparisonResult) -> dict[str, Any]:
        return {
            "selected_card": cls._card_payload(result.selected_card),
            "print_count": len(result.printings),
            "offers": [
                {
                    **cls._offer_payload(family_offer.offer),
                    "card": cls._card_payload(family_offer.card),
                }
                for family_offer in result.offers
            ],
        }

    @staticmethod
    def _offer_payload(offer: StoreOffer) -> dict[str, Any]:
        listing_url = offer.listing_url
        if listing_url and not listing_url.startswith(("https://", "http://")):
            listing_url = None
        return {
            "store_id": offer.store_id,
            "store_name": offer.store_name,
            "raw_name": offer.raw_name,
            "price_yen": offer.price_yen,
            "price_display": offer.price_display,
            "availability": offer.availability.value,
            "listing_url": listing_url,
            "match_confidence": offer.match_confidence.value,
            "condition": offer.condition,
            "stock_count": offer.stock_count,
            "finish": offer.finish.value,
            "finish_raw": offer.finish_raw,
        }


class AsyncOperationRunner:
    """Run comparison work on one loop so browser selections reuse connections."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = Event()
        self._closed = False
        self._client = None
        self._thread = Thread(target=self._run_loop, name="jp-price-checker-requests", daemon=True)
        self._thread.start()
        self._ready.wait()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    async def _run(self, operation: Any) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=20, follow_redirects=True)
        with use_shared_request_client(self._client):
            return await operation

    def run(self, operation: Any) -> Any:
        if self._closed:
            operation.close()
            raise RuntimeError("The local web server is stopping.")
        return asyncio.run_coroutine_threadsafe(self._run(operation), self._loop).result()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        async def close_client() -> None:
            if self._client is not None:
                await self._client.aclose()

        try:
            asyncio.run_coroutine_threadsafe(close_client(), self._loop).result(timeout=5)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
            self._loop.close()


class LocalWebRequestHandler(BaseHTTPRequestHandler):
    """Same-origin handler for the local browser UI."""

    application: LocalPriceCheckWeb
    operation_runner: AsyncOperationRunner

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        request = urlsplit(self.path)
        if request.path == "/":
            self._send_html(INDEX_HTML)
            return
        if request.path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        parameters = parse_qs(request.query, keep_blank_values=True)
        if request.path == "/api/catalogue-rebuild":
            self._send_json(HTTPStatus.OK, self.application.catalogue_rebuild_status())
            return
        if request.path == "/api/catalogue-update":
            try:
                self._send_json(HTTPStatus.OK, self.application.catalogue_update_status())
            except CatalogueSnapshotError as error:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return
        if request.path == "/api/search":
            try:
                self._send_json(
                    HTTPStatus.OK,
                    self.application.search(
                        parameters.get("q", [""])[0],
                        rarities=tuple(parameters.get("rarity", [])),
                        finishes=tuple(parameters.get("finish", [])),
                    ),
                )
            except ValueError as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if request.path == "/api/compare":
            try:
                print_id = int(parameters.get("id", [""])[0])
                refresh = parameters.get("refresh", ["0"])[0] == "1"
                payload = self.operation_runner.run(self.application.compare(print_id, refresh=refresh))
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "A valid card selection is required."})
            except LookupError as error:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(error)})
            except Exception:
                self._send_json(
                    HTTPStatus.BAD_GATEWAY,
                    {"error": "Store comparison is temporarily unavailable. Please try again."},
                )
            else:
                self._send_json(HTTPStatus.OK, payload)
            return
        if request.path == "/api/compare-family":
            try:
                print_id = int(parameters.get("id", [""])[0])
                refresh = parameters.get("refresh", ["0"])[0] == "1"
                payload = self.operation_runner.run(self.application.compare_family(print_id, refresh=refresh))
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "A valid card selection is required."})
            except LookupError as error:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(error)})
            except Exception:
                self._send_json(
                    HTTPStatus.BAD_GATEWAY,
                    {"error": "Store comparison is temporarily unavailable. Please try again."},
                )
            else:
                self._send_json(HTTPStatus.OK, payload)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        request = urlsplit(self.path)
        if request.path == "/api/catalogue-rebuild":
            if self.headers.get("X-JP-Price-Checker") != "1":
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "Catalogue rebuilds must be started from this app."})
                return
            try:
                self._send_json(HTTPStatus.ACCEPTED, self.application.start_catalogue_rebuild())
            except CatalogueSnapshotError as error:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(error)})
            return
        if request.path == "/api/catalogue-update":
            if self.headers.get("X-JP-Price-Checker") != "1":
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "Catalogue updates must be started from this app."})
                return
            try:
                self._send_json(HTTPStatus.OK, self.application.apply_catalogue_update())
            except CatalogueSnapshotError as error:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            except Exception:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "The catalogue update could not be installed. Your previous catalogue is still available."},
                )
            return
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Only catalogue updates support POST."})

    def log_message(self, _: str, *args: object) -> None:
        """Keep ordinary browser requests out of the terminal."""

    def _send_html(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self._send_common_headers("text/html; charset=utf-8", len(encoded))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self._send_common_headers("application/json; charset=utf-8", len(encoded))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_common_headers(self, content_type: str, content_length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        )


def build_web_application(settings: Settings | None = None) -> LocalPriceCheckWeb:
    """Create the shared catalogue/store services without requiring Telegram."""
    settings = settings or Settings.from_environment()
    seed_bundled_catalogue(settings.catalogue_db)
    catalogue = CatalogueRepository(settings.catalogue_db)
    application = LocalPriceCheckWeb(
        catalogue,
        ComparisonService(()),
        update_client=CatalogueUpdateClient(settings.catalogue_update_manifest_url),
    )
    yuyutei = YuyuTeiConnector(promo_page_url=application.promo_catalogue_page_url)
    application.comparison = ComparisonService(
        (
            yuyutei,
            BigWebConnector(),
            CardRushConnector(),
            VanHappyConnector(),
            OltaConnector(),
            ManzokuyaConnector(),
            ManaSourceConnector(),
            FullAheadConnector(),
            AmenityDreamConnector(),
            TorecoloConnector(),
            CardMaxConnector(),
            AvalonConnector(),
            CLaboConnector(),
            RealizeConnector(),
            PAOConnector(),
            Net193Connector(),
            RyuunoshippoConnector(),
            NoahConnector(),
            IseiConnector(),
            AdvantageConnector(),
            GamersConnector(),
            TorecaPlazaConnector(),
            PachipachiConnector(),
            SquareBushiroadConnector(),
            MastersGuildConnector(),
            GProjectConnector(),
        )
    )
    return application


class LocalWebServer(ThreadingHTTPServer):
    """HTTP server that owns the reusable async request session."""

    def __init__(self, address: tuple[str, int], handler: type[LocalWebRequestHandler], runner: AsyncOperationRunner) -> None:
        self.operation_runner = runner
        super().__init__(address, handler)

    def server_close(self) -> None:
        super().server_close()
        self.operation_runner.close()


def create_server(application: LocalPriceCheckWeb, *, host: str = "127.0.0.1", port: int = 8787) -> LocalWebServer:
    """Create a local-only HTTP server without beginning its request loop."""
    runner = AsyncOperationRunner()
    handler = type(
        "BoundLocalWebRequestHandler",
        (LocalWebRequestHandler,),
        {"application": application, "operation_runner": runner},
    )
    try:
        return LocalWebServer((host, port), handler, runner)
    except Exception:
        runner.close()
        raise


def serve(application: LocalPriceCheckWeb, *, host: str = "127.0.0.1", port: int = 8787) -> None:
    """Serve the UI locally. The default address is inaccessible from a network."""
    server = create_server(application, host=host, port=port)
    print(f"JP Price Checker is running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nJP Price Checker stopped.")
    finally:
        server.server_close()
        application.catalogue.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local JP Price Checker browser interface.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: local computer only)")
    parser.add_argument("--port", type=int, default=8787, help="Local port (default: 8787)")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    serve(build_web_application(), host=args.host, port=args.port)


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>JP Price Checker</title>
  <style>
    :root { color-scheme:light; --paper:#f7f3eb; --paper-deep:#eee7dc; --surface:#fffdf9; --ink:#2e2925; --muted:#716960; --line:#ded4c6; --accent:#c9662d; --accent-hover:#aa5222; --accent-soft:#fff0e6; --danger:#ad3c32; }
    * { box-sizing:border-box; }
    body { margin:0; min-height:100vh; background:radial-gradient(circle at 8% 0%, #fffdf9 0, transparent 31rem), linear-gradient(135deg,var(--paper) 0%,var(--paper-deep) 100%); color:var(--ink); font:16px/1.5 ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    main { width:min(1180px,calc(100% - 32px)); margin:0 auto; padding:70px 0 88px; }
    h1 { margin:0; font-family:ui-serif,Georgia,Cambria,"Times New Roman",serif; font-size:clamp(1.45rem,3vw,2rem); font-weight:600; letter-spacing:-.04em; line-height:1.1; }
    .eyebrow { color:var(--accent); font-weight:750; letter-spacing:.12em; font-size:.74rem; text-transform:uppercase; margin-bottom:13px; }
    .intro { max-width:630px; color:var(--muted); margin:18px 0 34px; }
    .workspace { --pane-height:calc(100vh - 250px); display:grid; grid-template-columns:minmax(290px,.8fr) minmax(0,1.4fr); gap:40px; align-items:start; margin-top:22px; }
    .print-panel,.price-panel { min-width:0; min-height:0; height:max(260px,var(--pane-height)); display:flex; flex-direction:column; }
    .price-panel { position:sticky; top:26px; }
    .panel-label { color:var(--muted); font-size:.74rem; font-weight:750; letter-spacing:.11em; text-transform:uppercase; margin:0 0 10px; }
    form { display:flex; gap:10px; background:var(--surface); border:1px solid var(--line); padding:8px; border-radius:15px; box-shadow:0 12px 32px #5e433a12; }
    input { min-width:0; flex:1; border:0; border-radius:10px; color:var(--ink); background:transparent; padding:14px 15px; font:inherit; outline:none; }
    input::placeholder { color:#978d82; }
    input:focus { box-shadow:inset 0 0 0 2px #e9a17c; }
    button { border:1px solid transparent; border-radius:10px; padding:12px 18px; color:#fffaf5; background:var(--accent); font:750 15px inherit; cursor:pointer; transition:background .16s ease,border-color .16s ease,transform .16s ease; }
    button:hover { background:var(--accent-hover); transform:translateY(-1px); }
    button:disabled { cursor:wait; opacity:.6; transform:none; }
    .hint { color:var(--muted); font-size:.88rem; margin:12px 0 20px; }
    .hint button,.comparison-head button { color:var(--muted); background:transparent; border-color:var(--line); padding:5px 9px; margin-left:5px; font-size:.8rem; }
    .hint button:hover,.comparison-head button:hover { color:var(--accent-hover); background:var(--accent-soft); border-color:#e6ad8b; }
    .catalogue-tools { display:flex; flex-wrap:wrap; align-items:center; gap:9px; margin:-4px 0 20px; color:var(--muted); font-size:.82rem; }
    .catalogue-tools button { color:var(--muted); background:transparent; border-color:var(--line); padding:6px 10px; font-size:.8rem; }
    .catalogue-tools button:hover { color:var(--accent-hover); background:var(--accent-soft); border-color:#e6ad8b; }
    #status { min-height:1.5em; color:var(--muted); margin-bottom:12px; }
    #status.error { color:var(--danger); }
    .filters { display:flex; flex-wrap:wrap; gap:10px 18px; align-items:center; margin:0 0 18px; padding:12px 14px; background:#fff9f2; border:1px solid var(--line); border-radius:12px; }
    .filter-group { display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
    .filter-label { color:var(--muted); font-size:.77rem; font-weight:750; letter-spacing:.06em; text-transform:uppercase; }
    .filter-button { color:var(--muted); background:transparent; border-color:var(--line); padding:5px 9px; font-size:.8rem; }
    .filter-button:hover,.filter-button.active { color:var(--accent-hover); background:var(--accent-soft); border-color:#e6ad8b; }
    .filter-clear { color:var(--muted); background:transparent; border-color:transparent; padding:5px 2px; font-size:.8rem; text-decoration:underline; }
    .filter-clear:hover { color:var(--accent-hover); background:transparent; }
    .aggregate-button { margin-left:auto; white-space:nowrap; }
    .result-list { min-height:0; overflow-y:auto; overscroll-behavior:contain; scrollbar-gutter:stable; display:grid; align-content:start; gap:10px; padding-right:5px; }
    .card { width:100%; text-align:left; color:var(--ink); background:var(--surface); border:1px solid var(--line); padding:17px; border-radius:13px; display:flex; gap:16px; align-items:center; box-shadow:0 2px 5px #5e433a08; }
    .card:hover { border-color:#df8f64; background:#fffaf5; }
    .card.active { border-color:var(--accent); background:var(--accent-soft); box-shadow:0 0 0 2px #e8a47f55; }
    .card.active .code { color:var(--accent-hover); }
    .card h2 { font-size:1.05rem; margin:0 0 4px; }
    .card p { margin:0; color:var(--muted); font-size:.9rem; }
    .card .code { margin-left:auto; text-align:right; color:var(--accent); font-size:.84rem; white-space:nowrap; }
    #comparison { min-height:0; flex:1; overflow-y:auto; overscroll-behavior:contain; scrollbar-gutter:stable; margin-top:0; background:var(--surface); border:1px solid var(--line); border-radius:14px; padding:20px; box-shadow:0 2px 5px #5e433a08; }
    #comparison:empty { display:none; }
    .comparison-head { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; border-bottom:1px solid var(--line); padding-bottom:14px; margin-bottom:3px; }
    .comparison-head h2 { margin:0; font-size:1.35rem; }
    .comparison-head p { margin:3px 0 0; color:var(--muted); }
    .comparison-head button { flex:none; }
    .comparison-actions { display:flex; align-items:center; gap:5px; }
    .comparison-actions button { margin-left:0; }
    .sort-icon { min-width:38px; font-size:1rem !important; line-height:1; letter-spacing:0; }
    .check-timings { color:var(--muted); font-size:.82rem; border-bottom:1px solid var(--line); padding:10px 0 12px; margin-bottom:2px; }
    .check-timings summary { cursor:pointer; color:var(--muted); }
    .check-timings summary:hover { color:var(--accent-hover); }
    .check-timing-list { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:5px 16px; list-style:none; padding:9px 0 0; margin:0; }
    .check-timing-list li { display:flex; justify-content:space-between; gap:10px; }
    .check-timing-list span:last-child { white-space:nowrap; }
    .offer { display:grid; grid-template-columns:minmax(110px,1fr) auto auto; gap:14px; align-items:center; padding:14px 0; border-bottom:1px solid var(--line); }
    .offer a { color:var(--ink); font-weight:750; text-decoration:none; }
    .offer a:hover { color:var(--accent-hover); }
    .offer small { display:block; color:var(--muted); }
    .price { font-weight:800; color:var(--accent-hover); }
    .sold { color:var(--muted); }
    .tag { color:var(--muted); font-size:.75rem; text-align:right; }
    .notice { color:var(--muted); font-size:.9rem; margin:12px 0; }
    @media (max-width:780px) { main { padding-top:44px; } .workspace { grid-template-columns:1fr; gap:24px; } .print-panel,.price-panel { height:auto; } .price-panel { position:static; } .result-list,#comparison { overflow:visible; scrollbar-gutter:auto; padding-right:0; } form { padding:7px; } button { padding:11px 13px; } .card { align-items:flex-start; } .card .code { white-space:normal; } #comparison { flex:none; margin-top:0; } .offer { grid-template-columns:1fr auto; } .tag { grid-column:1 / -1; text-align:left; } }
  </style>
</head>
<body><main>
  <div class="eyebrow">Local card price comparison</div><h1>JP Price Checker</h1>
  <p class="intro">Search by English card name / Japanese serial number</p>
  <form id="search-form"><input id="query" type="search" maxlength="120" autocomplete="off" placeholder="Try: Youthberk, D-PR/953" autofocus><button id="search-button">Search</button></form>
  <div class="hint">Optional rarity at the end: <button type="button" data-query="Youthberk FFR">Youthberk FFR</button><button type="button" data-query="D-PR/953">D-PR/953</button></div>
  <div class="catalogue-tools"><button type="button" id="catalogue-update-button">Check catalogue update</button><span id="catalogue-update-status" aria-live="polite"></span><button type="button" id="catalogue-rebuild-button">Rebuild catalogue from sources</button><span id="catalogue-rebuild-status" aria-live="polite"></span></div>
  <div id="status" aria-live="polite"></div><section id="filters" class="filters" aria-label="Filter matching printings" hidden></section><div class="workspace"><section class="print-panel"><p class="panel-label">Matching printings</p><section id="results" class="result-list"></section></section><section class="price-panel"><p class="panel-label">Price comparison</p><section id="comparison"></section></section></div>
</main><script>
const query = document.querySelector('#query'), form = document.querySelector('#search-form'), searchButton = document.querySelector('#search-button'), status = document.querySelector('#status'), filters = document.querySelector('#filters'), results = document.querySelector('#results'), comparison = document.querySelector('#comparison'), catalogueUpdateButton = document.querySelector('#catalogue-update-button'), catalogueUpdateStatus = document.querySelector('#catalogue-update-status'), catalogueRebuildButton = document.querySelector('#catalogue-rebuild-button'), catalogueRebuildStatus = document.querySelector('#catalogue-rebuild-status');
let selectedId = null, searchCards = [], activeRarities = new Set(), activeFinishes = new Set(), currentComparison = null, comparisonMode = null, offerSort = 'lowest';
function setStatus(message, isError=false) { status.textContent = message; status.className = isError ? 'error' : ''; }
function clear(node) { node.replaceChildren(); }
function text(tag, value, className) { const node=document.createElement(tag); node.textContent=value || ''; if (className) node.className=className; return node; }
async function readJson(response) { const data=await response.json(); if (!response.ok) throw new Error(data.error || 'Something went wrong.'); return data; }
function filteredCards() { return searchCards.filter(card => (!activeRarities.size || activeRarities.has(card.rarity)) && (!activeFinishes.size || card.finish==='unknown' || activeFinishes.has(card.finish))); }
function aggregateCandidate() { const cards=filteredCards(); if (!cards.length || !cards[0].japanese_name) return null; const first=cards[0]; return cards.every(card=>card.japanese_name===first.japanese_name && card.english_name===first.english_name) ? first : null; }
function renderCards() { clear(results); const cards=filteredCards(), total=searchCards.length, hasFilters=activeRarities.size || activeFinishes.size; if (!cards.length) { setStatus('No matching print uses those filters. Clear a filter to see all '+total+' matching prints.', true); return; } setStatus(cards.length+' matching print'+(cards.length===1?'':'s')+(hasFilters ? ' of '+total : '')+' — choose one to compare stores.'); for (const card of cards) { const button=document.createElement('button'); button.type='button'; button.className='card'; button.dataset.printId=card.id; const copy=document.createElement('div'); copy.append(text('h2',card.english_name), text('p',card.japanese_name)); button.append(copy,text('div',card.display_code,'code')); button.addEventListener('click',()=>compare(card.id)); results.append(button); } highlightSelection(); }
function filterButton(label, selected, onClick) { const button=document.createElement('button'); button.type='button'; button.className='filter-button'+(selected ? ' active' : ''); button.textContent=label; button.setAttribute('aria-pressed',String(selected)); button.addEventListener('click',onClick); return button; }
function renderFilters() { clear(filters); const rarities=[...new Set(searchCards.map(card=>card.rarity).filter(Boolean))].sort(), finishes=[...new Set(searchCards.map(card=>card.finish).filter(finish=>finish && finish!=='unknown'))].sort(), aggregate=aggregateCandidate(); if (!rarities.length && !finishes.length && !aggregate) { filters.hidden=true; return; } filters.hidden=false; if (rarities.length) { const group=document.createElement('div'); group.className='filter-group'; group.append(text('span','Rarity','filter-label')); for (const rarity of rarities) group.append(filterButton(rarity,activeRarities.has(rarity),()=>{ activeRarities.has(rarity) ? activeRarities.delete(rarity) : activeRarities.add(rarity); renderFilters(); renderCards(); })); filters.append(group); } if (finishes.length) { const group=document.createElement('div'); group.className='filter-group'; group.append(text('span','Finish','filter-label')); for (const finish of finishes) { const label=finish==='holo' ? 'Holo' : finish==='standard' ? 'Standard' : finish; group.append(filterButton(label,activeFinishes.has(finish),()=>{ activeFinishes.has(finish) ? activeFinishes.delete(finish) : activeFinishes.add(finish); renderFilters(); renderCards(); })); } filters.append(group); } if (activeRarities.size || activeFinishes.size) { const reset=document.createElement('button'); reset.type='button'; reset.className='filter-clear'; reset.textContent='Clear filters'; reset.addEventListener('click',()=>{ activeRarities.clear(); activeFinishes.clear(); renderFilters(); renderCards(); }); filters.append(reset); } if (aggregate) { const button=document.createElement('button'); button.type='button'; button.className='filter-button aggregate-button'; button.textContent='Aggregate card prints'; button.title='Compare prices across '+aggregate.family_print_count+' verified Japanese printing'+(aggregate.family_print_count===1?'':'s')+' of this card.'; button.setAttribute('aria-label',button.title); button.addEventListener('click',()=>compareFamily(aggregate.id)); filters.append(button); } }
async function search(raw) { const value=(raw || query.value).trim(); if (!value) { setStatus('Enter a card name to search.', true); return; } query.value=value; clear(results); clear(comparison); clear(filters); filters.hidden=true; selectedId=null; searchCards=[]; currentComparison=null; comparisonMode=null; activeRarities.clear(); activeFinishes.clear(); setStatus('Finding matching prints…'); searchButton.disabled=true; try { const data=await readJson(await fetch('/api/search?q='+encodeURIComponent(value))); if (!data.cards.length) { setStatus('No Japanese-market print matched that name. Try a shorter spelling.'); return; } searchCards=data.cards; renderFilters(); renderCards(); } catch (error) { setStatus(error.message,true); } finally { searchButton.disabled=false; } }
function showCatalogueStatus(message, isError=false) { catalogueUpdateStatus.textContent=message; catalogueUpdateStatus.style.color=isError ? 'var(--danger)' : ''; }
async function checkCatalogueUpdate() { catalogueUpdateButton.disabled=true; showCatalogueStatus('Checking published catalogue…'); try { const data=await readJson(await fetch('/api/catalogue-update')); if (!data.enabled) { catalogueUpdateButton.hidden=true; showCatalogueStatus('Catalogue updates are included with this app release.'); return; } if (data.status==='up_to_date') { catalogueUpdateButton.textContent='Catalogue is current'; showCatalogueStatus('Version '+data.current_version+' is already installed.'); return; } catalogueUpdateButton.textContent='Install catalogue update'; catalogueUpdateButton.dataset.availableVersion=data.available_version || ''; showCatalogueStatus('Version '+(data.available_version || 'new')+' is ready. Installing replaces only your local card catalogue.'); } catch (error) { showCatalogueStatus(error.message,true); } finally { catalogueUpdateButton.disabled=false; } }
async function installCatalogueUpdate() { const version=catalogueUpdateButton.dataset.availableVersion || 'this'; if (!window.confirm('Install catalogue version '+version+'? Your card mappings will update, but no store prices are saved.')) return; catalogueUpdateButton.disabled=true; showCatalogueStatus('Downloading and checking catalogue…'); try { const data=await readJson(await fetch('/api/catalogue-update',{method:'POST',headers:{'X-JP-Price-Checker':'1'}})); if (data.status==='updated') { catalogueUpdateButton.textContent='Catalogue is current'; delete catalogueUpdateButton.dataset.availableVersion; showCatalogueStatus('Catalogue version '+data.current_version+' is now installed.'); } else { catalogueUpdateButton.textContent='Catalogue is current'; showCatalogueStatus('Version '+data.current_version+' is already installed.'); } } catch (error) { showCatalogueStatus(error.message,true); } finally { catalogueUpdateButton.disabled=false; } }
catalogueUpdateButton.addEventListener('click',()=>catalogueUpdateButton.dataset.availableVersion ? installCatalogueUpdate() : checkCatalogueUpdate());
function showCatalogueRebuildStatus(message, isError=false) { catalogueRebuildStatus.textContent=message; catalogueRebuildStatus.style.color=isError ? 'var(--danger)' : ''; }
function renderCatalogueRebuildStatus(data) { const running=data.status==='running'; catalogueRebuildButton.disabled=running; catalogueRebuildButton.textContent=running ? 'Rebuilding catalogue…' : 'Rebuild catalogue from sources'; let message=data.message || ''; if (data.status==='completed') { const imported=(data.english_prints_imported || 0)+(data.japanese_prints_imported || 0); message+=(imported ? ' Imported '+imported+' new catalogue print'+(imported===1?'':'s')+'.' : ' No newly published prints were found.'); if (data.fandom_failed_sets) message+=' '+data.fandom_failed_sets+' Fandom mapping page'+(data.fandom_failed_sets===1?' needs':'s need')+' review.'; } showCatalogueRebuildStatus(message,data.status==='failed'); return running; }
async function pollCatalogueRebuild() { try { const running=renderCatalogueRebuildStatus(await readJson(await fetch('/api/catalogue-rebuild'))); if (running) window.setTimeout(pollCatalogueRebuild,2500); } catch (error) { showCatalogueRebuildStatus(error.message,true); catalogueRebuildButton.disabled=false; } }
async function startCatalogueRebuild() { if (!window.confirm('Rebuild your local catalogue from approved official, Fandom and Yuyu-Tei sources? This can take several minutes and uses your internet, but does not check store prices. Your current catalogue stays usable until the rebuilt copy is ready.')) return; catalogueRebuildButton.disabled=true; showCatalogueRebuildStatus('Starting catalogue rebuild…'); try { const data=await readJson(await fetch('/api/catalogue-rebuild',{method:'POST',headers:{'X-JP-Price-Checker':'1'}})); if (renderCatalogueRebuildStatus(data)) window.setTimeout(pollCatalogueRebuild,2500); } catch (error) { showCatalogueRebuildStatus(error.message,true); catalogueRebuildButton.disabled=false; } }
catalogueRebuildButton.addEventListener('click',startCatalogueRebuild); pollCatalogueRebuild();
function safeLink(url) { try { const parsed=new URL(url); return ['https:','http:'].includes(parsed.protocol) ? parsed.href : null; } catch { return null; } }
function highlightSelection() { document.querySelectorAll('.card[data-print-id]').forEach(card=>card.classList.toggle('active',Number(card.dataset.printId)===selectedId)); }
function offerRow(offer, printLabel='') { const row=document.createElement('div'); row.className='offer'; const store=document.createElement(offer.listing_url ? 'a' : 'div'); store.textContent=(printLabel ? printLabel+' · ' : '')+offer.store_name+(offer.condition ? ' · '+offer.condition : ''); if (offer.listing_url) { const href=safeLink(offer.listing_url); if (href) { store.href=href; store.target='_blank'; store.rel='noopener noreferrer'; } } const detail=text('small',offer.raw_name); const storeWrap=document.createElement('div'); storeWrap.append(store,detail); const stock=Number.isInteger(offer.stock_count) ? offer.stock_count+' left' : offer.availability==='sold_out' ? '×' : offer.availability==='in_stock' ? '◯' : '?'; const finish=offer.finish_raw || (offer.finish==='holo' ? 'Holo' : offer.finish==='standard' ? 'Standard' : ''); const tags=[stock,finish].filter(Boolean).join(' · '); row.append(storeWrap,text('div',offer.price_display,offer.availability==='in_stock'?'price':'sold'),text('div',tags,'tag')); return row; }
function offerAvailabilityRank(offer) { return offer.availability==='in_stock' ? 0 : offer.availability==='sold_out' ? 1 : 2; }
function sortedOffers(offers) { return [...offers].sort((left,right)=>{ const availability=offerAvailabilityRank(left)-offerAvailabilityRank(right); if (availability) return availability; const leftMissing=!Number.isInteger(left.price_yen), rightMissing=!Number.isInteger(right.price_yen); if (leftMissing!==rightMissing) return leftMissing ? 1 : -1; if (!leftMissing && left.price_yen!==right.price_yen) return offerSort==='highest' ? right.price_yen-left.price_yen : left.price_yen-right.price_yen; return left.store_name.localeCompare(right.store_name); }); }
function renderCurrentComparison() { if (!currentComparison) return; comparisonMode==='aggregate' ? renderFamilyComparison(currentComparison) : renderComparison(currentComparison); }
function offerSortControl() { const icon=document.createElement('button'); icon.type='button'; icon.className='sort-icon'; const lowest=offerSort==='lowest'; icon.textContent=lowest ? '☰↑' : '☰↓'; const current=lowest ? 'Lowest price' : 'Highest price', next=lowest ? 'highest' : 'lowest'; icon.title='Sorting displayed offers: '+current+'. Activate to sort '+next+' first.'; icon.setAttribute('aria-label',icon.title); icon.addEventListener('click',()=>{ offerSort=lowest ? 'highest' : 'lowest'; renderCurrentComparison(); }); return icon; }
function elapsedLabel(milliseconds) { return milliseconds < 1000 ? milliseconds+' ms' : (milliseconds/1000).toFixed(milliseconds < 10000 ? 1 : 0)+' s'; }
function comparisonStatus(data) { if (data.cached) return 'Showing a recent comparison from the last two minutes. Refresh prices to check stores again.'; return 'Checked '+(data.store_timings || []).length+' stores in '+elapsedLabel(data.duration_ms || 0)+'.'; }
function appendStoreTimings(data) { const timings=data.store_timings || []; if (!timings.length) return; const details=document.createElement('details'); details.className='check-timings'; const summary=text('summary',(data.cached ? 'Recent result originally checked ' : 'Checked ')+timings.length+' stores in '+elapsedLabel(data.duration_ms || 0)+' — view timings'); details.append(summary); const list=document.createElement('ul'); list.className='check-timing-list'; const labels={offers:'offers found',no_active_listing:'no active listing',unavailable:'unavailable',failed:'could not check'}; for (const timing of [...timings].sort((left,right)=>right.elapsed_ms-left.elapsed_ms)) { const item=document.createElement('li'); item.append(text('span',timing.store_name),text('span',elapsedLabel(timing.elapsed_ms)+' · '+(labels[timing.outcome] || timing.outcome))); list.append(item); } details.append(list); comparison.append(details); }
function renderComparison(data) { if (!data) return; currentComparison=data; comparisonMode='print'; highlightSelection(); clear(comparison); const head=document.createElement('div'); head.className='comparison-head'; const copy=document.createElement('div'); copy.append(text('h2',data.card.english_name),text('p',(data.card.japanese_name ? data.card.japanese_name+' · ' : '')+data.card.display_code)); const actions=document.createElement('div'); actions.className='comparison-actions'; actions.append(offerSortControl()); const refresh=document.createElement('button'); refresh.textContent='Refresh prices'; refresh.addEventListener('click',()=>compare(data.card.id,true)); actions.append(refresh); head.append(copy,actions); comparison.append(head); appendStoreTimings(data); if (!data.offers.length) comparison.append(text('p','No active store listing is available for this printing right now.','notice')); for (const offer of sortedOffers(data.offers)) comparison.append(offerRow(offer)); const notices=[]; if (data.no_active_listing_stores.length) notices.push('No active listing (sold out or not stocked): '+data.no_active_listing_stores.join(', ')); if (data.unavailable_stores.length) notices.push('Set not listed: '+data.unavailable_stores.join(', ')); if (data.failed_stores.length) notices.push('Could not check: '+data.failed_stores.join(', ')); if (notices.length) comparison.append(text('p',notices.join(' · '),'notice')); }
function renderFamilyComparison(data) { if (!data) return; currentComparison=data; comparisonMode='aggregate'; highlightSelection(); clear(comparison); const head=document.createElement('div'); head.className='comparison-head'; const copy=document.createElement('div'); copy.append(text('h2','Aggregated card prints'),text('p',data.selected_card.english_name+' · '+data.print_count+' verified Japanese printings')); const actions=document.createElement('div'); actions.className='comparison-actions'; actions.append(offerSortControl()); const back=document.createElement('button'); back.textContent='Back to selected print'; back.addEventListener('click',()=>compare(data.selected_card.id)); actions.append(back); head.append(copy,actions); comparison.append(head); if (!data.offers.length) comparison.append(text('p','No store currently has a listed offer for these verified printings.','notice')); for (const offer of sortedOffers(data.offers)) comparison.append(offerRow(offer,offer.card.display_code)); }
async function compare(id, refresh=false) { selectedId=id; highlightSelection(); setStatus('Checking stores…'); comparison.replaceChildren(text('p','Comparing listings…','notice')); try { const data=await readJson(await fetch('/api/compare?id='+encodeURIComponent(id)+(refresh?'&refresh=1':''))); renderComparison(data); setStatus(comparisonStatus(data)); } catch (error) { clear(comparison); setStatus(error.message,true); } }
async function compareFamily(id, refresh=false) { selectedId=id; currentComparison=null; comparisonMode=null; highlightSelection(); setStatus('Aggregating listings across card prints…'); comparison.replaceChildren(text('p','Comparing listings across verified printings…','notice')); try { const data=await readJson(await fetch('/api/compare-family?id='+encodeURIComponent(id)+(refresh?'&refresh=1':''))); renderFamilyComparison(data); setStatus(''); } catch (error) { clear(comparison); setStatus(error.message,true); } }
form.addEventListener('submit',event=>{ event.preventDefault(); search(); }); document.querySelectorAll('[data-query]').forEach(button=>button.addEventListener('click',()=>search(button.dataset.query)));
</script></body></html>"""
