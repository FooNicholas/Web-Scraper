"""Local entry point for the Telegram price-comparison bot."""

from __future__ import annotations

from telegram import Update
from telegram.ext import Application, ApplicationBuilder

from scraperbot.bot import TelegramPriceBot
from scraperbot.catalogue.repository import CatalogueRepository
from scraperbot.config import Settings
from scraperbot.distribution import seed_bundled_catalogue
from scraperbot.connectors.advantage import AdvantageConnector
from scraperbot.connectors.bigweb import BigWebConnector
from scraperbot.connectors.cardmax import CardMaxConnector
from scraperbot.connectors.cardrush import CardRushConnector
from scraperbot.connectors.hobbystation import HobbyStationConnector
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
from scraperbot.services.comparison import ComparisonService


def build_application(settings: Settings | None = None) -> Application:
    """Assemble the local polling application without making network calls."""
    settings = settings or Settings.from_environment()
    seed_bundled_catalogue(settings.catalogue_db)
    catalogue = CatalogueRepository(settings.catalogue_db)
    yuyutei = YuyuTeiConnector(
        promo_page_url=lambda card: catalogue.promo_catalogue_page_url(
            "yuyutei", card.set_code, card.collector_number
        )
    )
    bot = TelegramPriceBot(
        catalogue,
        ComparisonService(
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
                HobbyStationConnector(),
            )
        ),
    )
    application = ApplicationBuilder().token(settings.require_bot_token()).build()
    for handler in bot.handlers():
        application.add_handler(handler)
    application.bot_data["catalogue"] = catalogue
    return application


def run() -> None:
    """Run locally with Telegram long polling; no hosting configuration is needed."""
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)
