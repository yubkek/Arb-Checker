"""
Playwright-based scrapers for Australian bookmakers.

Each scraper returns a list of dicts:
  {match, player1, player2, odds1, odds2, bookmaker}

IMPORTANT — Selector maintenance:
  These sites are JS-rendered SPAs. If a scraper returns 0 results,
  open the site in Chrome DevTools, inspect an odds element, and update
  the CSS selectors in the relevant function below.

Sites scraped:
  - Sportsbet  (sportsbet.com.au)
  - Neds       (neds.com.au)
  - Ladbrokes  (ladbrokes.com.au)
  - Bet365     (bet365.com.au) — may be blocked by bot detection
"""

import asyncio
import logging
import re
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    async_playwright,
    TimeoutError as PWTimeout,
)

logger = logging.getLogger(__name__)

_NAV_TIMEOUT   = 45_000   # ms — page navigation
_WAIT_TIMEOUT  = 20_000   # ms — waiting for elements
_STEALTH_UA    = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_odds(text: str) -> Optional[float]:
    """Parse a decimal or fractional odds string into a float."""
    text = text.strip().replace("$", "").replace(",", "")
    if "/" in text:
        try:
            num, den = text.split("/", 1)
            return round(int(num) / int(den) + 1, 4)
        except (ValueError, ZeroDivisionError):
            return None
    m = re.search(r"(\d+\.\d+)", text)
    if m:
        val = float(m.group(1))
        return val if val > 1.0 else None
    return None


async def _safe_text(el, default: str = "") -> str:
    try:
        return (await el.inner_text()).strip()
    except Exception:
        return default


async def _new_page(ctx: BrowserContext, url: str) -> Optional[Page]:
    """Open a new page, navigate to url, return page or None on failure."""
    try:
        page = await ctx.new_page()
        await page.set_extra_http_headers({"User-Agent": _STEALTH_UA})
        await page.goto(url, timeout=_NAV_TIMEOUT, wait_until="domcontentloaded")
        return page
    except Exception as exc:
        logger.error("Navigation to %s failed: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Sportsbet
# ---------------------------------------------------------------------------

async def scrape_sportsbet(ctx: BrowserContext) -> list[dict]:
    """
    Tennis page: https://www.sportsbet.com.au/betting/tennis

    Sportsbet uses data-automation-id attributes extensively.
    Match cards are rendered as <div data-automation-id="market-coupon-sport-event">.
    Each outcome/selection has a price button.

    Selector update guide:
      1. Open https://www.sportsbet.com.au/betting/tennis in Chrome
      2. Right-click an odds button → Inspect
      3. Find the parent container (event card) and note its data-automation-id
      4. Note the player-name element and price element selectors
      5. Update CARD_SEL, NAME_SEL, PRICE_SEL below
    """
    CARD_SEL  = '[data-automation-id="market-coupon-sport-event"], [data-automation-id*="event-card"]'
    NAME_SEL  = '[data-automation-id*="competitor-name"], [class*="competitorName"], [class*="participantName"]'
    PRICE_SEL = '[data-automation-id*="price-button"], [class*="priceText"], button[class*="price"]'

    page = await _new_page(ctx, "https://www.sportsbet.com.au/betting/tennis")
    if not page:
        return []

    results: list[dict] = []
    try:
        # Wait for at least one odds element
        try:
            await page.wait_for_selector(PRICE_SEL, timeout=_WAIT_TIMEOUT)
        except PWTimeout:
            logger.warning("Sportsbet: timed out waiting for price elements")
            return []

        cards = await page.query_selector_all(CARD_SEL)
        logger.debug("Sportsbet: found %d potential event cards", len(cards))

        for card in cards:
            try:
                names  = await card.query_selector_all(NAME_SEL)
                prices = await card.query_selector_all(PRICE_SEL)

                if len(names) < 2 or len(prices) < 2:
                    continue

                p1 = await _safe_text(names[0])
                p2 = await _safe_text(names[1])
                o1 = _parse_odds(await _safe_text(prices[0]))
                o2 = _parse_odds(await _safe_text(prices[1]))

                if p1 and p2 and o1 and o2:
                    results.append({
                        "match":     f"{p1} v {p2}",
                        "player1":   p1,
                        "player2":   p2,
                        "odds1":     o1,
                        "odds2":     o2,
                        "bookmaker": "Sportsbet",
                    })
            except Exception as exc:
                logger.debug("Sportsbet: error parsing card: %s", exc)

    finally:
        await page.close()

    logger.info("Sportsbet: scraped %d matches", len(results))
    return results


# ---------------------------------------------------------------------------
# Neds  (Entain platform)
# ---------------------------------------------------------------------------

async def scrape_neds(ctx: BrowserContext) -> list[dict]:
    """
    Tennis page: https://www.neds.com.au/sports/tennis

    Neds uses Entain's "Sky" platform. Match rows contain competitor names
    and price buttons with class patterns starting with 'KambiBC-'.

    Selector update guide:
      1. Open https://www.neds.com.au/sports/tennis in Chrome
      2. Inspect an odds price button
      3. Note the class prefix (often 'KambiBC-bet-offer-button__odds' or similar)
      4. Update CARD_SEL, NAME_SEL, PRICE_SEL below
    """
    CARD_SEL  = ".KambiBC-event-item, [class*='event-item'], [class*='EventItem']"
    NAME_SEL  = ".KambiBC-event-item__title, [class*='eventParticipant'], [class*='competitor']"
    PRICE_SEL = ".KambiBC-bet-offer-button__odds, [class*='odds'], [class*='Odds']"

    page = await _new_page(ctx, "https://www.neds.com.au/sports/tennis")
    if not page:
        return []

    results: list[dict] = []
    try:
        try:
            await page.wait_for_selector(PRICE_SEL, timeout=_WAIT_TIMEOUT)
        except PWTimeout:
            logger.warning("Neds: timed out waiting for price elements")
            return []

        cards = await page.query_selector_all(CARD_SEL)
        logger.debug("Neds: found %d potential event cards", len(cards))

        for card in cards:
            try:
                names  = await card.query_selector_all(NAME_SEL)
                prices = await card.query_selector_all(PRICE_SEL)

                if len(names) < 2 or len(prices) < 2:
                    continue

                p1 = await _safe_text(names[0])
                p2 = await _safe_text(names[1])
                o1 = _parse_odds(await _safe_text(prices[0]))
                o2 = _parse_odds(await _safe_text(prices[1]))

                if p1 and p2 and o1 and o2:
                    results.append({
                        "match":     f"{p1} v {p2}",
                        "player1":   p1,
                        "player2":   p2,
                        "odds1":     o1,
                        "odds2":     o2,
                        "bookmaker": "Neds",
                    })
            except Exception as exc:
                logger.debug("Neds: error parsing card: %s", exc)

    finally:
        await page.close()

    logger.info("Neds: scraped %d matches", len(results))
    return results


# ---------------------------------------------------------------------------
# Ladbrokes  (also Entain platform — near-identical to Neds)
# ---------------------------------------------------------------------------

async def scrape_ladbrokes(ctx: BrowserContext) -> list[dict]:
    """
    Tennis page: https://www.ladbrokes.com.au/sports/tennis

    Ladbrokes AU runs the same Entain/Kambi platform as Neds.
    Selectors are usually identical — update in sync with Neds if needed.
    """
    CARD_SEL  = ".KambiBC-event-item, [class*='event-item'], [class*='EventItem']"
    NAME_SEL  = ".KambiBC-event-item__title, [class*='eventParticipant'], [class*='competitor']"
    PRICE_SEL = ".KambiBC-bet-offer-button__odds, [class*='odds'], [class*='Odds']"

    page = await _new_page(ctx, "https://www.ladbrokes.com.au/sports/tennis")
    if not page:
        return []

    results: list[dict] = []
    try:
        try:
            await page.wait_for_selector(PRICE_SEL, timeout=_WAIT_TIMEOUT)
        except PWTimeout:
            logger.warning("Ladbrokes: timed out waiting for price elements")
            return []

        cards = await page.query_selector_all(CARD_SEL)
        logger.debug("Ladbrokes: found %d potential event cards", len(cards))

        for card in cards:
            try:
                names  = await card.query_selector_all(NAME_SEL)
                prices = await card.query_selector_all(PRICE_SEL)

                if len(names) < 2 or len(prices) < 2:
                    continue

                p1 = await _safe_text(names[0])
                p2 = await _safe_text(names[1])
                o1 = _parse_odds(await _safe_text(prices[0]))
                o2 = _parse_odds(await _safe_text(prices[1]))

                if p1 and p2 and o1 and o2:
                    results.append({
                        "match":     f"{p1} v {p2}",
                        "player1":   p1,
                        "player2":   p2,
                        "odds1":     o1,
                        "odds2":     o2,
                        "bookmaker": "Ladbrokes",
                    })
            except Exception as exc:
                logger.debug("Ladbrokes: error parsing card: %s", exc)

    finally:
        await page.close()

    logger.info("Ladbrokes: scraped %d matches", len(results))
    return results


# ---------------------------------------------------------------------------
# Bet365  (heavy bot-detection — may fail; graceful degradation)
# ---------------------------------------------------------------------------

async def scrape_bet365(ctx: BrowserContext) -> list[dict]:
    """
    Tennis page: https://www.bet365.com.au/#/AC/B13/C1/D1002/F2/

    Bet365 uses heavily obfuscated, auto-generated class names that change
    on every deploy. This scraper uses partial class-name matching and
    ARIA attributes as a best-effort approach.

    If Bet365 returns 0 results, the rest of the scan continues unaffected.
    The most common failure modes are:
      - Bot detection / Cloudflare block  → page shows error / CAPTCHA
      - Class names rotated               → selectors no longer match
    To debug: run with PWDEBUG=1 and inspect the live page manually.
    """
    page = await _new_page(ctx, "https://www.bet365.com.au/#/AC/B13/C1/D1002/F2/")
    if not page:
        return []

    results: list[dict] = []
    try:
        # Bet365 renders via WebSockets — wait longer
        await page.wait_for_load_state("networkidle", timeout=_NAV_TIMEOUT)

        # Their odds elements tend to carry aria-label="N.NN" or role="button"
        # and class names like "gl-ParticipantOddsOnly_Odds" (historically)
        PRICE_SEL = '[class*="ParticipantOdds"], [class*="participant-odds"], [aria-label][role="button"]'
        NAME_SEL  = '[class*="ParticipantName"], [class*="participant-name"], [class*="TeamName"]'

        try:
            await page.wait_for_selector(PRICE_SEL, timeout=_WAIT_TIMEOUT)
        except PWTimeout:
            logger.warning("Bet365: timed out — likely blocked or no live tennis")
            return []

        # Pair up names and prices positionally (Bet365 renders them interleaved)
        names  = await page.query_selector_all(NAME_SEL)
        prices = await page.query_selector_all(PRICE_SEL)

        # Expect pairs: [p1, p2, p1, p2, ...]  and  [o1, o2, o1, o2, ...]
        for i in range(0, min(len(names), len(prices)) - 1, 2):
            try:
                p1 = await _safe_text(names[i])
                p2 = await _safe_text(names[i + 1])
                o1 = _parse_odds(await _safe_text(prices[i]))
                o2 = _parse_odds(await _safe_text(prices[i + 1]))

                if p1 and p2 and o1 and o2:
                    results.append({
                        "match":     f"{p1} v {p2}",
                        "player1":   p1,
                        "player2":   p2,
                        "odds1":     o1,
                        "odds2":     o2,
                        "bookmaker": "Bet365",
                    })
            except Exception as exc:
                logger.debug("Bet365: error parsing entry %d: %s", i, exc)

    finally:
        await page.close()

    logger.info("Bet365: scraped %d matches", len(results))
    return results


# ---------------------------------------------------------------------------
# Orchestrator — run all scrapers in parallel
# ---------------------------------------------------------------------------

async def scrape_all_bookmakers() -> list[dict]:
    """
    Launch a single Chromium browser, scrape all four bookmakers concurrently,
    close the browser, and return the combined list of odds dicts.
    One scraper failing does not affect the others.
    """
    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        # All scrapers share one browser context (shared cookie jar / cache)
        ctx: BrowserContext = await browser.new_context(
            user_agent=_STEALTH_UA,
            viewport={"width": 1280, "height": 900},
            locale="en-AU",
        )

        # Mask webdriver fingerprint
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        scraper_tasks = [
            scrape_sportsbet(ctx),
            scrape_neds(ctx),
            scrape_ladbrokes(ctx),
            scrape_bet365(ctx),
        ]
        results_per_bookie = await asyncio.gather(*scraper_tasks, return_exceptions=True)

        combined: list[dict] = []
        names = ["Sportsbet", "Neds", "Ladbrokes", "Bet365"]
        for name, res in zip(names, results_per_bookie):
            if isinstance(res, Exception):
                logger.error("%s scraper raised an exception: %s", name, res)
            elif isinstance(res, list):
                combined.extend(res)

        await ctx.close()
        await browser.close()

    return combined
