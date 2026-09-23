# JP Price Checker

JP Price Checker helps you compare public Japanese-market prices for
**Cardfight!! Vanguard** cards. Search with the English card name you know,
choose the Japanese printing, and compare its current listings across supported
stores.

It is a local-first app: the interface opens in your own browser, the card
catalogue lives on your computer, and it is not a public website or a browser
extension.

## Get the app

Download the latest archive from the [GitHub Releases page](https://github.com/FooNicholas/Web-Scraper/releases), then choose the file for your computer:

| Computer | Download | Open it |
| --- | --- | --- |
| Apple-silicon Mac (M1, M2, M3, M4) | `jp-price-checker-macos-arm64-…zip` | Unzip it, then open **JP Price Checker.app**. |
| Windows 64-bit | `jp-price-checker-windows-x64-…zip` | Extract the whole ZIP, keep its folder together, then open **JP Price Checker.exe**. |

The app starts a private local server and opens its address in your default
browser. If Chrome is your default, it appears as an ordinary Chrome tab. It
does not install a Chrome extension and cannot be reached by other people over
the internet.

Early releases may be unsigned while platform signing is being completed. Your
operating system can show a publisher warning in that case. Only run a download
from this project's Release page, and wait for a signed release if you are not
comfortable with that warning.

## Get started

1. Enter an English card name, an alias, or part of a name. Small spelling
   differences are supported.
2. Pick the Japanese printing on the left. Its set code, rarity, and holo or
   standard finish help distinguish reprints.
3. Read the store offers on the right. A number is the published stock count;
   `◯` means in stock, `×` means sold out, and `?` means the store did not
   expose a stock signal.
4. Use **Refresh prices** when you want a fresh check. Normal comparisons use a
   short local cache so repeatedly clicking a print does not repeatedly request
   every store.
5. Open **view timings** above the offers if a comparison feels slow. It shows
   how long each supported store took and whether it returned offers, no active
   listing, or an unavailable response.

Use **Aggregate card prints** to compare all verified Japanese reprints of the
same card. The sort button in the price pane changes the ordering of the offers
currently shown; it does not make another store request.

### Search by serial number

You normally do not need a serial number. It is useful for a very new promo or
a Japanese-only card that does not yet have a safe English name mapping. Enter
the full Japanese reference, for example:

```text
D-PR/953
DPR953
D-PR 953
```

Bare numbers are deliberately not accepted because the same number can occur
in different product families. English print serials are not used for price
search.

## Prices, stock, and catalogue updates

Selecting a printing checks supported stores at that time; card-name search
itself does not refresh store prices. A displayed sold-out price is kept and
labelled as sold out rather than being discarded.

All supported stores are checked for every fresh comparison. The app runs those
checks together, keeps connections warm while the app is open, and limits only
same-store follow-up requests to two at a time. This keeps comparisons complete
while avoiding a burst of detail-page requests to one store.

**Check catalogue update** updates card names, printings, mappings, and search
data only. It never refreshes store prices and never contacts the catalogue
sources. You see the incoming curator-published catalogue version and must
approve the update before it replaces your local copy.

### Rebuild your own catalogue

When a new Japanese set is released, you can choose **Rebuild catalogue from
sources**. The app clearly asks first, then makes a working copy of your local
catalogue and refreshes it from the approved official catalogues, Fandom name
mappings, and the Yuyu-Tei promo directory. It can take several minutes; it
does not check price stores, and the current catalogue remains usable until the
new copy has been checked and installed.

This is optional. **Check catalogue update** is faster and uses the reviewed
release snapshot. A newly published Japanese-only print is searchable by its
Japanese serial as soon as the official catalogue has it. English-name search
becomes available once there is direct mapping evidence; the app does not guess
translations or equate English and Japanese serial numbers.

## Stores and limitations

The app currently checks Yuyu-Tei, BigWeb, Card Rush, VanHappy, Olta,
Manzokuya, Mana Source, FullAhead, Amenity Dream, Torecolo, Card Max, Cardshop
Avalon, C-labo, REALiZE, PAO, 193net, Ryuunoshippo, TCG NOAH, Cardshop Isei,
TCG Advantage, Gamers, Toreca Plaza 55, Pachipachi TCG, Square Bushiroad,
Masters Guild, and G-Project TCG.

Prices and stock can change between the comparison and a store checkout. A
temporary store error, missing set, or sold-out listing does not prevent other
stores from appearing.

- **G-Project TCG** does not expose printed serials or stock quantities. Its
  offers are clearly labelled as exact Japanese-name matches and are accepted
  only after name, set, and rarity checks.
- **Hobby Station** is not included: its public results trigger browser
  verification for automated requests. The app does not bypass it.
- **Dragon Star / Dorasuta** is not included until there is a permitted,
  narrow Japanese-serial integration route.

## Optional Telegram bot

The browser app needs no Telegram account. If you want to run your own
Telegram bot, follow the source setup below, copy `.env.example` to `.env`,
add a BotFather token, then run:

```sh
scraperbot
```

Example messages:

```text
/price Youthberk
chronojet ffr
D-PR/953
```

Keep `.env` private. Never upload a Telegram token to GitHub or a release.

## Run from source

This route is for contributors or people who want to maintain their own local
catalogue. It requires Python 3.11+ and Conda.

```sh
conda create --prefix .conda python=3.12 -y
conda run --prefix .conda python -m pip install .
conda run --prefix .conda scraperbot-refresh-catalogue --apply --repair-regional
conda run --prefix .conda scraperbot-web
```

Then open [http://127.0.0.1:8787](http://127.0.0.1:8787). The first catalogue
refresh can take a while. It is opt-in and contacts the approved catalogue
sources, not every price store.

For the project layout, catalogue policy, contributor workflow, release
process, trade-offs, and roadmap, see [the maintainer guide](MAINTAINER_GUIDE.md).
