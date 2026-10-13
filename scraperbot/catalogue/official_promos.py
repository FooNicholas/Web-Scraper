"""Japanese promo identities from Bushiroad's dedicated, paginated PR table."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
import httpx

from scraperbot.catalogue.official_source import OfficialSourceError
from scraperbot.catalogue.repository import CatalogueRepository
from scraperbot.models import JapaneseCardPrint


@dataclass(frozen=True)
class OfficialPromo:
    card: JapaneseCardPrint
    distribution: str
    available_from: str | None


class OfficialPromoSource:
    url = "https://cf-vanguard.com/cardlist/card_pr"
    api_url = "https://cf-vanguard.com/manage/card-pr-user/list"

    def __init__(self, client=None, *, page_delay=1.0):
        self.client = client
        self.page_delay = page_delay

    @classmethod
    def parse_page(cls, html):
        soup = BeautifulSoup(html, "lxml")
        rows = soup.select("tr.card_pr, tr.card_pr_new")
        if not rows:
            raise OfficialSourceError("Official PR table is missing; catalogue left unchanged.")
        entries = []
        for row in rows:
            cells = row.find_all("td", recursive=False)
            if len(cells) < 4:
                raise OfficialSourceError("Unrecognised official PR row.")
            serial = re.fullmatch(r"D-PR/(\d+)", re.sub(r"\s+", "", cells[0].get_text()))
            if not serial:
                continue  # Older PR/V-PR and other families are outside D-PR scope.
            for badge in cells[1].select(".new"):
                badge.decompose()
            name = cells[1].get_text(" ", strip=True)
            if not name:
                raise OfficialSourceError("Official PR row has no Japanese name.")
            number = serial.group(1)
            entries.append(OfficialPromo(
                JapaneseCardPrint("DPR", number, "", name,
                                  f"https://cf-vanguard.com/cardlist/?cardno=D-PR/{number}"),
                distribution=cells[-2].get_text(" ", strip=True),
                available_from=cells[2].get_text(" ", strip=True) if len(cells) == 5 else None,
            ))
        links = set()
        for link in soup.select("a[href]"):
            parsed = urlsplit(urljoin(cls.url, link["href"]))
            if parsed.netloc != "cf-vanguard.com" or parsed.path not in (
                "/cardlist/card-pr", "/cardlist/card_pr"
            ):
                continue
            page = parse_qs(parsed.query).get("page", [""])[0]
            if page.isdigit() and 1 < int(page) <= 200:
                links.add(int(page))
        return entries, links

    async def fetch(self, progress=None):
        if self.client is None:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                return await OfficialPromoSource(client, page_delay=self.page_delay).fetch(progress)
        pending, visited, entries = {1}, set(), {}
        api_mode = False
        while pending:
            page = min(pending)
            pending.remove(page)
            if visited:
                await asyncio.sleep(self.page_delay)
            url = self.api_url if api_mode else (
                self.url if page == 1 else f"{self.url}/?page={page}"
            )
            try:
                response = await self.client.get(
                    url,
                    params={"page": page} if api_mode else None,
                    headers={"User-Agent": "JP-Price-Checker/0.1 (local catalogue import)"},
                )
                response.raise_for_status()
            except httpx.HTTPError as error:
                raise OfficialSourceError(f"Official PR page {page} failed; catalogue left unchanged.") from error
            if api_mode:
                try:
                    payload = response.json()
                except ValueError as error:
                    raise OfficialSourceError("Official PR API did not return JSON.") from error
                batch, links = self.parse_api_page(payload)
            else:
                try:
                    batch, links = self.parse_page(response.text)
                except OfficialSourceError:
                    if page != 1:
                        raise
                    api_mode = True
                    pending.add(1)
                    continue
            for entry in batch:
                key = entry.card.collector_number
                previous = entries.get(key)
                if previous and previous.card.japanese_name != entry.card.japanese_name:
                    raise OfficialSourceError(f"Conflicting official names for D-PR/{key}.")
                # A repeated announcement must not replace a released row.
                if previous is None or entry.available_from is None:
                    entries[key] = entry
            visited.add(page)
            pending.update(links - visited)
            if progress:
                progress(f"Official PR page {page}: {len(batch)} D-PR entries")
        if not entries:
            raise OfficialSourceError("No Japanese D-PR records found; catalogue left unchanged.")
        return list(entries.values())

    @classmethod
    def parse_api_page(cls, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise OfficialSourceError("Official PR API returned an unrecognised card list.")
        pages = payload.get("pages")
        if not isinstance(pages, int) or not 1 <= pages <= 200:
            raise OfficialSourceError("Official PR API returned invalid pagination.")
        entries = []
        for rows, scheduled in ((payload["items"], False), (payload.get("scheduled", []), True)):
            if not isinstance(rows, list):
                raise OfficialSourceError("Official PR API returned invalid card rows.")
            for row in rows:
                if not isinstance(row, dict):
                    raise OfficialSourceError("Official PR API returned an invalid card row.")
                number = re.fullmatch(r"D-PR/(\d+)", str(row.get("card_number") or "").strip())
                if not number:
                    continue
                name = str(row.get("card_name") or "").strip()
                if not name:
                    raise OfficialSourceError("Official PR API card has no Japanese name.")
                serial = number.group(1)
                entries.append(OfficialPromo(
                    JapaneseCardPrint("DPR", serial, "", name,
                                      f"https://cf-vanguard.com/cardlist/?cardno=D-PR/{serial}"),
                    distribution=str(row.get("dist_way") or "").strip(),
                    available_from=str(row.get("card_use") or "").strip() or None if scheduled else None,
                ))
        page = payload.get("page")
        if not isinstance(page, int) or not 1 <= page <= pages:
            raise OfficialSourceError("Official PR API returned an invalid page number.")
        return entries, ({page + 1} if page < pages else set())


async def import_official_promos(database: Path, *, source=None, progress=None) -> int:
    # Fetch and validate the complete snapshot before making any data changes.
    entries = await (source or OfficialPromoSource()).fetch(progress)
    with CatalogueRepository(database) as catalogue:
        try:
            for entry in entries:
                card = entry.card
                old = catalogue._japanese_by_reference("DPR", card.collector_number)
                if old and old["japanese_name"] != card.japanese_name:
                    # English evidence attached to a different Japanese name
                    # must be reviewed again, including previously approved names.
                    ids = catalogue.connection.execute(
                        "SELECT id FROM card_prints WHERE set_code='DPR' AND collector_number=?",
                        (card.collector_number,),
                    ).fetchall()
                    catalogue.connection.executemany("DELETE FROM card_search WHERE print_id=?",
                                                     [(str(row[0]),) for row in ids])
                    catalogue.connection.execute("DELETE FROM card_prints WHERE set_code='DPR' AND collector_number=?",
                                                 (card.collector_number,))
                    catalogue.connection.execute("DELETE FROM english_name_mappings WHERE japanese_print_id=?", (old["id"],))
                catalogue.connection.execute(
                    """INSERT INTO official_promo_identities
                    (collector_number,japanese_name,source_url,distribution,available_from)
                    VALUES (?,?,?,?,?) ON CONFLICT(collector_number) DO UPDATE SET
                    japanese_name=excluded.japanese_name,source_url=excluded.source_url,
                    distribution=excluded.distribution,available_from=excluded.available_from,
                    checked_at=CURRENT_TIMESTAMP""",
                    (card.collector_number,card.japanese_name,card.source_url,entry.distribution,entry.available_from),
                )
                catalogue._upsert_japanese(card)
            catalogue.connection.commit()
        except Exception:
            catalogue.connection.rollback()
            raise
    return len(entries)


def main():
    parser = argparse.ArgumentParser(description="Import official Japanese PR identities, then rebuild English name mappings locally.")
    parser.add_argument("--database", type=Path, default=Path("data/catalogue.sqlite3"))
    args = parser.parse_args()
    from scraperbot.catalogue.promo_mapping_repair import repair_promo_mappings
    try:
        count = asyncio.run(import_official_promos(args.database, progress=lambda message: print(message, flush=True)))
        result = repair_promo_mappings(args.database)
    except (OfficialSourceError, ValueError) as error:
        parser.exit(2, f"{error}\n")
    print(f"Verified {count} official D-PR identities; {result.unresolved} playable promos need English review.")


if __name__ == "__main__":
    main()
