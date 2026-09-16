# JP Price Checker maintainer guide

This guide is for contributors and release curators. User installation and
everyday search instructions are in [README.md](README.md).

## Product boundaries

JP Price Checker compares **Japanese Vanguard printings**. English names are a
search convenience: they resolve to a canonical Japanese card identity, which
then exposes all verified Japanese reprints. The app does not compare English
market listings.

The product is deliberately local-first:

- The browser UI binds to `127.0.0.1`; it is not hosted publicly.
- Telegram is optional and uses local long polling when a maintainer supplies
  a private bot token.
- The curated SQLite catalogue is local. Desktop downloads receive a reviewed
  snapshot rather than scraping the catalogue on first launch.
- Price lookups happen only after the user chooses a print or explicitly
  aggregates a card's prints. They are not catalogue-refresh jobs.

## Architecture

```text
English name / Japanese serial
             │
             ▼
      Local SQLite catalogue
             │  selected Japanese physical print
             ▼
   Concurrent, exact-print store connectors
             │
             ▼
 Browser UI or Telegram response
```

### Main components

| Area | Key files | Responsibility |
| --- | --- | --- |
| Browser interface | `scraperbot/web.py`, `scraperbot/desktop.py` | Serves the local browser UI and API. The small UI is intentionally inline in `web.py`, avoiding a JavaScript build system. The desktop launcher opens the local URL in the default browser. |
| Telegram interface | `scraperbot/main.py`, `scraperbot/bot.py` | Builds the optional polling bot using the same catalogue and comparison service. |
| Search and domain model | `scraperbot/query.py`, `scraperbot/models.py` | Normalises queries, serials, rarity, finish, names, and offer states. |
| Catalogue repository | `scraperbot/catalogue/repository.py` | Owns SQLite schema, fuzzy search, Japanese serial lookup, identity links, and migrations. |
| Catalogue importers | `scraperbot/catalogue/` | Imports official Japanese/English references, Fandom mappings, official promo identity, and Yuyu-Tei promo locations. |
| Price comparison | `scraperbot/services/comparison.py` | Starts every connector concurrently, records each outcome and elapsed time, and keeps a 120-second in-memory result cache. |
| Store connectors | `scraperbot/connectors/` | Performs bounded public lookup of one selected Japanese print at a time. The browser shares a warm HTTP session between selected cards. |
| Distribution | `scraperbot/distribution.py`, `scraperbot/catalogue_snapshot.py` | Builds, verifies, seeds, checks, and atomically installs SQLite catalogue snapshots. |
| Native packaging | `scripts/build_desktop.py`, `.github/workflows/build-desktop-downloads.yml` | Packages matching Apple-silicon macOS and Windows x64 builds and attaches them to a GitHub Release draft. |

### Data model and equality rule

`japanese_prints` holds physical Japanese printings: printed set code,
collector number, rarity, finish, Japanese name, and official source URL.
`card_identities` holds a canonical Japanese name plus its approved English
name, aliases, and mapping provenance. `japanese_print_identity_links` joins
the physical prints to that identity.

This distinction is the central correctness rule:

> A Japanese card identity is decided by its exact canonical Japanese name,
> never by an English set number or collector number.

`english_print_references` and `archived_english_reference_mappings` remain as
reference data for future reviewed extensions. They must not participate in
Japanese-card equality, grouping, propagation, or price search. This prevents
regional-number collisions such as the historic `D-PR/953` and `DZ-SS10`
mis-mappings.

Other supporting tables are intentionally separate:

- `official_promo_identities` verifies Japanese D-PR names and source URLs.
- `promo_catalogue_entries` stores a retailer's known Japanese promo location,
  page, and product ID without changing the card's English mapping.
- Import checkpoint tables make large catalogue jobs resumable.

## Search and comparison behaviour

English search uses SQLite full-text search and fuzzy ranking over approved
English names, aliases, and Japanese names. A formatted Japanese serial takes
the direct exact-print path. A selected card retains its exact Japanese
reference all the way through every connector.

`Aggregate card prints` asks each connector about every linked Japanese
printing, then joins the results. It does not loosen a retailer search into a
name search. Price sorting is presentation-only; the **Refresh prices** action
is the explicit way to bypass the 120-second comparison cache.

Every fresh selected-print comparison starts all registered stores together;
the result is complete only after every connector has returned an outcome.
The browser shows the overall elapsed time and a collapsed per-store timing
list. It keeps one HTTP client alive for the local app session, so a shopper
moving through different cards can reuse existing connections. Connectors with
follow-up requests use `bounded_map` with `DETAIL_REQUEST_CONCURRENCY = 2`:
this preserves every candidate in the existing per-store cap while avoiding the
old serial detail-page wait and an aggressive same-store request burst. The
limiter belongs to the connector, so an aggregate request cannot multiply that
cap across several printings.

Offers preserve price, availability, listed stock quantity when present,
condition, raw finish text, and matching confidence. In-stock offers sort ahead
of sold-out and unknown-stock offers; unavailable prices stay last.

## Catalogue sources and mapping policy

The approved catalogue refresh uses:

1. Official Japanese and English card catalogues for print/reference facts.
2. Bushiroad's official PR table for Japanese D-PR identity.
3. The Vanguard Wiki Fandom public API for English search-name mappings where
   exact Japanese-title evidence exists.
4. Yuyu-Tei D-Promo category pages for Japanese promo locations and retailer
   finish annotations.

Fandom is accepted for Japanese-only set and promo mapping where no equivalent
official English source exists. A mapping is safe only with direct evidence:
exact Japanese name, explicit cross-print evidence, or an explicit approved
review. Do not translate, infer from a similar name, or reuse an English promo
serial merely because its number looks equal.

Energy, Energy Generator, Quick Shield, and Persona Shield records are
intentionally Japanese-serial-searchable only. They may appear together for a
shared-name lookup; English mappings for them are on hold rather than missing
work.

## Store connector policy

Each connector must make a narrow, public query for the selected Japanese
printing, verify the printed reference, and reject an explicit finish conflict.
It should return the displayed price, stock state, and quantity only when the
site exposes them. A connector must fail closed with `StoreUnavailableError`
when a set is not listed, the response is overbroad, or a matching print cannot
be verified.

When a connector needs several follow-up pages after an already-bounded search,
use `bounded_map` with `semaphore=self.bounded_request_limiter()` rather than a
serial loop. Keep the existing candidate cap, preserve the returned order, and
leave `DETAIL_REQUEST_CONCURRENCY` at two unless a new measured,
permitted-store policy is reviewed. Do not omit a store from a comparison
merely because it is slow; the timing data identifies the store for maintenance.

G-Project is the documented exception: its public catalogue does not expose a
printed serial. Its connector is restricted to exact Japanese-name, set, and
rarity checks and labels its lower confidence accordingly.

Do not add a broad catalogue crawl, loose retailer-name match, CAPTCHA solver,
Cloudflare bypass, copied browser cookie, or detection-evasion code. A site
that blocks a narrow request is unavailable until it provides an official API,
export, allowlisting, or other explicit permitted route.

To add a permitted connector:

1. Implement `StoreConnector.search(card)` in `scraperbot/connectors/`.
2. Add bounded parser fixtures and exact-serial/finish mismatch tests.
3. Register it in both `scraperbot/main.py` and `scraperbot/web.py`.
4. Record any confidence or stock limitation in README and this guide.

## Maintaining the catalogue

All commands below run inside the project's `.conda` environment. The database
is deliberately ignored by Git.

### Routine maintenance

Start with a read-only status report:

```sh
conda run --prefix .conda scraperbot-catalogue-status
```

Run the approved-source refresh only when you intend to update the local
catalogue. It contacts approved catalogue sources and Yuyu-Tei promo pages;
it does not query every retailer connector.

```sh
conda run --prefix .conda scraperbot-refresh-catalogue --apply --repair-regional
```

Use `--refresh-promos` when the already-known Yuyu-Tei D-Promo range pages
themselves must be reread. Review the status again, run the automated test
suite, and spot-check any newly released card before building a snapshot:

```sh
conda run --prefix .conda python -m pytest -q
conda run --prefix .conda scraperbot-catalogue-status
```

### Mapping a new Japanese set or a disputed promo

The full refresh is normally sufficient. For focused work, import the Japanese
set, then map it from Fandom's exact Japanese evidence:

```sh
conda run --prefix .conda scraperbot-import-japanese --set DZ-BT16
conda run --prefix .conda scraperbot-map-fandom --set DZ-BT16
```

For a D-PR or regional Special Series correction, use the official promo
identity import and regional mapping repair instead of editing numbers by hand:

```sh
conda run --prefix .conda scraperbot-import-official-promos
conda run --prefix .conda scraperbot-repair-regional-mappings --set D-PR
```

When direct evidence is absent, export a review file, record only reviewed
approvals with source evidence, and apply that file. The importer rejects stale
or conflicting records rather than overwriting a current identity.

```sh
conda run --prefix .conda scraperbot-promo-review export data/promo-review-new.json
conda run --prefix .conda scraperbot-promo-review apply data/promo-review-new.json
```

## Building and releasing desktop downloads

Desktop users receive a known-good catalogue snapshot bundled into the app.
They can later click **Check catalogue update** to install only a curator-made
snapshot. The update is HTTPS-only, checked against a SHA-256 digest, validated
as the expected SQLite schema, and atomically replaced; a failed update keeps
the prior database.

### Release process

1. Refresh, test, and review the local catalogue.
2. Create fresh snapshot assets. Use a date or release-specific catalogue
   version, not an English print number.

   ```sh
   conda run --prefix .conda scraperbot-build-catalogue-snapshot \
     --database data/catalogue.sqlite3 \
     --output build/catalogue-release \
     --version 2026.09.16
   ```

3. Create a **draft** GitHub Release for the application version. Upload the
   resulting `catalogue.sqlite3` and `catalogue-manifest.json` with those exact
   names.
4. Run **Build desktop downloads** from the current `main` branch. Provide the
   existing draft tag and the catalogue version. Start a new workflow run after
   editing the workflow; do not use **Re-run jobs**, which uses the historical
   workflow revision.
5. Verify the draft contains the Mac and Windows ZIPs, and test both platforms.
6. Sign and notarize the macOS build with a Developer ID; sign the Windows
   build with a trusted code-signing certificate. Then publish the release.

GitHub Actions is used only to build native artifacts on their matching
operating systems. PyInstaller does not reliably cross-compile a Windows app
from macOS. Actions do not host the app, refresh store prices, or scrape the
catalogue.

The release channel must be public for friends' in-app catalogue updates to
work without GitHub accounts. Never attach `.env`, credentials, or a Telegram
token to a release.

## Trade-offs and limitations

| Decision | Benefit | Cost |
| --- | --- | --- |
| Japanese canonical names determine identity | Prevents regional serial collisions and wrong reprint grouping | Some legitimate promos remain serial-only until direct mapping evidence exists. |
| English names are search mappings | English-name discovery works without pretending English prints are Japanese prints | English names do not establish card equality. |
| Exact-print retailer checks | Strong protection against price being attributed to the wrong rarity or foil | Stores without serials cannot be supported safely, except the documented G-Project constraint. |
| Local snapshot instead of per-user catalogue crawl | Predictable startup, low retailer/source traffic, and shared curated data | A curator must refresh and publish catalogue updates. |
| Local browser server | No hosting bill, credentials, or exposed public service | Friends run a desktop process and view it in their own browser. |
| Manual price refresh with short cache | Avoids repeatedly hitting every store while browsing | Results are a point-in-time comparison, not a persistent price history. |

## Current roadmap

### In progress

| Work | Next action |
| --- | --- |
| First public desktop release | Test the generated Mac and Windows archives, complete platform signing/notarization, then publish the release. |
| Catalogue upkeep | Run an opt-in refresh when new sets or promos arrive; resolve only mappings with direct evidence. |
| Promo review backlog | Review playable Japanese promos that remain unmapped. Utility cards stay serial-only by design. |
| Connector health | Periodically run fixture tests and live spot checks; adjust parsers only when the public storefront changes. |
| Slow-store investigation | Use the in-app timing list after normal shopper sessions to identify persistent outliers before changing a connector's query or parser. |

### On hold or gated

| Work | Condition to resume |
| --- | --- |
| Hobby Station connector | Official API, export, explicit allowlisting, or another permitted narrow access route. No browser-verification reproduction. |
| Dragon Star / Dorasuta connector | A permitted narrow Japanese-serial route with verifiable public price and stock. |
| Detection-bypass work | On hold indefinitely. The project will not bypass Cloudflare, CAPTCHAs, access controls, or store anti-bot measures. |
| Shared hosting | Requires a separate rate-limited, authenticated architecture and explicit operator approval. |

### Completed foundations

- English fuzzy/partial search and exact Japanese serial search.
- Canonical Japanese identity with English mapping provenance and reprint
  grouping; English serial equality is archived and excluded from active logic.
- Rarity, holo/standard finish, stock, sold-out price, aggregation, and price
  sorting in browser and Telegram flows.
- Reviewed official/Fandom/Yuyu-Tei promo and regional mapping workflow.
- Local browser interface, Telegram interface, 26 supported store connectors,
  and a cautious documented exception for G-Project.
- Complete all-store fan-out, per-store timing diagnostics, warm browser-session
  connections, and bounded two-at-a-time product-detail retrieval.
- Versioned, verified catalogue snapshots and native Apple-silicon/Windows
  packaging workflow.

## Verification checklist

Before merging or releasing a functional change:

```sh
conda run --prefix .conda python -m pytest -q
git diff --check
```

For a connector or mapping change, add an offline fixture for the exact serial,
an ambiguity/mismatch case, and the relevant stock or finish behaviour. Do not
make live-store requests part of the automated test suite.
