"""Small shared Selenium runtime for stores that need a rendered browser page."""

from __future__ import annotations

import atexit
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import RLock

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.support.ui import WebDriverWait

from scraperbot.connectors.base import StoreUnavailableError


_PAGE_LOAD_TIMEOUT_SECONDS = 25
_PAGE_READY_TIMEOUT_SECONDS = 18
_BROWSER_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pricecheck-browser")
_DRIVER_LOCK = RLock()
_DRIVER: webdriver.Chrome | None = None


def _driver() -> webdriver.Chrome:
    global _DRIVER
    if _DRIVER is not None:
        return _DRIVER

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1440,1100")
    options.add_argument("--disable-dev-shm-usage")
    options.page_load_strategy = "eager"
    _DRIVER = webdriver.Chrome(options=options)
    _DRIVER.set_page_load_timeout(_PAGE_LOAD_TIMEOUT_SECONDS)
    return _DRIVER


def _verification_pending(driver: webdriver.Chrome) -> bool:
    title = driver.title.casefold()
    body = driver.find_element("tag name", "body").text.casefold()
    markers = (
        "just a moment",
        "performing security verification",
        "checking your browser",
        "verify you are human",
    )
    return any(marker in title or marker in body for marker in markers)


def _fetch_page(url: str, store_name: str, title_fragment: str) -> str:
    with _DRIVER_LOCK:
        try:
            driver = _driver()
            try:
                driver.get(url)
            except TimeoutException:
                driver.execute_script("window.stop();")
            try:
                WebDriverWait(driver, _PAGE_READY_TIMEOUT_SECONDS, poll_frequency=0.25).until(
                    lambda current: title_fragment.casefold() in current.title.casefold()
                    and not _verification_pending(current)
                )
            except TimeoutException as error:
                reason = (
                    "is requesting browser verification"
                    if _verification_pending(driver)
                    else "did not finish rendering its results"
                )
                raise StoreUnavailableError(f"{store_name} {reason}.") from error
            return driver.page_source
        except StoreUnavailableError:
            raise
        except (TimeoutException, WebDriverException) as error:
            _discard_driver()
            raise StoreUnavailableError(
                f"{store_name} could not complete its browser lookup or verification."
            ) from error


def _discard_driver() -> None:
    global _DRIVER
    driver, _DRIVER = _DRIVER, None
    if driver is not None:
        try:
            driver.quit()
        except WebDriverException:
            pass


def _shutdown_driver() -> None:
    with _DRIVER_LOCK:
        _discard_driver()


atexit.register(_shutdown_driver)


async def fetch_rendered_html(url: str, *, store_name: str, title_fragment: str) -> str:
    """Fetch a page in one reused browser session without solving challenges."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _BROWSER_EXECUTOR,
        _fetch_page,
        url,
        store_name,
        title_fragment,
    )
