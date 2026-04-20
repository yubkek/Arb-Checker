"""
Playwright-based scrapers for Australian bookmakers + Betfair Exchange.

Each scraper returns a list of dicts:
  {match, player1, player2, odds1, odds2, bookmaker}

Selectors were verified against live rendered HTML on 2026-04-20.
If a scraper returns 0 results, re-run diagnose.py and update the
relevant WAIT_FOR / NAME_SEL / ODDS_SEL constants below.

----- TO SWAP BETFAIR WEBSITE → OFFICIAL API ----------------------------
In scrape_betfair(), replace the body with:
    from betfair_api import BetfairClient
    client = BetfairClient(username, password, app_key)
    return client.get_live_tennis_odds()
The return format is identical; nothing else in the codebase changes.
-------------------------------------------------------------------------
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

_NAV_TIMEOUT  = 60_000   # ms — page navigation
_WAIT_TIMEOUT = 30_000   # ms — waiting for first element to appear
_STEALTH_UA   = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_odds(text: str) -> Optional[float]:
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


async def _open(ctx: BrowserContext, url: str, wait_until: str = "domcontentloaded") -> Optional[Page]:
    try:
        page = await ctx.new_page()
        await page.goto(url, timeout=_NAV_TIMEOUT, wait_until=wait_until)
        return page
    except Exception as exc:
        logger.error("Navigation to %s failed: %s", url, exc)
        try:
            await page.close()
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# Sportsbet
# ---------------------------------------------------------------------------
# Verified selectors (2026-04-20):
#   Card:    [data-automation-id$="-competition-event-card"]
#   Player1: [data-automation-id$="-competition-event-participant-1"]
#   Player2: [data-automation-id$="-competition-event-participant-2"]
#   Odds:    [data-automation-id$="-two-outcome-captioned-text"]  (2 per card, in player order)

async def scrape_sportsbet(ctx: BrowserContext) -> list[dict]:
    WAIT_FOR  = '[data-automation-id$="-competition-event-card"]'
    CARD_SEL  = '[data-automation-id$="-competition-event-card"]'
    P1_SEL    = '[data-automation-id$="-competition-event-participant-1"]'
    P2_SEL    = '[data-automation-id$="-competition-event-participant-2"]'
    ODDS_SEL  = '[data-automation-id$="-two-outcome-captioned-text"]'

    page = await _open(ctx, "https://www.sportsbet.com.au/betting/tennis")
    if not page:
        return []

    results: list[dict] = []
    try:
        await asyncio.sleep(5)  # let React finish rendering
        try:
            await page.wait_for_selector(WAIT_FOR, timeout=_WAIT_TIMEOUT)
        except PWTimeout:
            logger.warning("Sportsbet: no event cards found")
            return []

        cards = await page.query_selector_all(CARD_SEL)
        logger.debug("Sportsbet: %d cards", len(cards))

        for card in cards:
            try:
                p1_el   = await card.query_selector(P1_SEL)
                p2_el   = await card.query_selector(P2_SEL)
                odd_els = await card.query_selector_all(ODDS_SEL)

                if not (p1_el and p2_el and len(odd_els) >= 2):
                    continue

                p1 = await _safe_text(p1_el)
                p2 = await _safe_text(p2_el)
                o1 = _parse_odds(await _safe_text(odd_els[0]))
                o2 = _parse_odds(await _safe_text(odd_els[1]))

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
                logger.debug("Sportsbet card parse error: %s", exc)
    finally:
        await page.close()

    logger.info("Sportsbet: %d matches", len(results))
    return results


# ---------------------------------------------------------------------------
# Neds  (verified selectors 2026-04-20)
# ---------------------------------------------------------------------------
# Each price button (class contains "has-name") wraps both the player name
# and odds. Buttons appear in pairs — one per player — per match.
#
#   Button:  button[class*="has-name"]
#   Name:    [data-testid="price-button-name"] span.displayTitle  (within button)
#   Odds:    [data-testid="price-button-odds"]                    (within button)

async def scrape_neds(ctx: BrowserContext) -> list[dict]:
    WAIT_FOR   = '[data-testid="price-button-odds"]'
    BUTTON_SEL = 'button[class*="has-name"]'
    NAME_SEL   = '[data-testid="price-button-name"] span, [data-testid="price-button-name"]'
    ODDS_SEL   = '[data-testid="price-button-odds"]'

    page = await _open(ctx, "https://www.neds.com.au/sports/tennis")
    if not page:
        return []

    results: list[dict] = []
    try:
        await asyncio.sleep(3)
        try:
            await page.wait_for_selector(WAIT_FOR, timeout=_WAIT_TIMEOUT)
        except PWTimeout:
            logger.warning("Neds: no price elements found")
            return []

        buttons = await page.query_selector_all(BUTTON_SEL)
        logger.debug("Neds: %d price buttons", len(buttons))

        # Process in pairs: button[i] = player1, button[i+1] = player2
        for i in range(0, len(buttons) - 1, 2):
            try:
                b1 = buttons[i]
                b2 = buttons[i + 1]

                name1_el = await b1.query_selector(NAME_SEL)
                name2_el = await b2.query_selector(NAME_SEL)
                odds1_el = await b1.query_selector(ODDS_SEL)
                odds2_el = await b2.query_selector(ODDS_SEL)

                if not all([name1_el, name2_el, odds1_el, odds2_el]):
                    continue

                p1 = await _safe_text(name1_el)
                p2 = await _safe_text(name2_el)
                o1 = _parse_odds(await _safe_text(odds1_el))
                o2 = _parse_odds(await _safe_text(odds2_el))

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
                logger.debug("Neds pair %d parse error: %s", i, exc)
    finally:
        await page.close()

    logger.info("Neds: %d matches", len(results))
    return results


# ---------------------------------------------------------------------------
# Ladbrokes  (same Entain platform as Neds — identical selectors)
# ---------------------------------------------------------------------------

async def scrape_ladbrokes(ctx: BrowserContext) -> list[dict]:
    WAIT_FOR   = '[data-testid="price-button-odds"]'
    BUTTON_SEL = 'button[class*="has-name"]'
    NAME_SEL   = '[data-testid="price-button-name"] span, [data-testid="price-button-name"]'
    ODDS_SEL   = '[data-testid="price-button-odds"]'

    page = await _open(ctx, "https://www.ladbrokes.com.au/sports/tennis")
    if not page:
        return []

    results: list[dict] = []
    try:
        await asyncio.sleep(3)
        try:
            await page.wait_for_selector(WAIT_FOR, timeout=_WAIT_TIMEOUT)
        except PWTimeout:
            logger.warning("Ladbrokes: no price elements found")
            return []

        buttons = await page.query_selector_all(BUTTON_SEL)
        logger.debug("Ladbrokes: %d price buttons", len(buttons))

        for i in range(0, len(buttons) - 1, 2):
            try:
                b1, b2 = buttons[i], buttons[i + 1]

                name1_el = await b1.query_selector(NAME_SEL)
                name2_el = await b2.query_selector(NAME_SEL)
                odds1_el = await b1.query_selector(ODDS_SEL)
                odds2_el = await b2.query_selector(ODDS_SEL)

                if not all([name1_el, name2_el, odds1_el, odds2_el]):
                    continue

                p1 = await _safe_text(name1_el)
                p2 = await _safe_text(name2_el)
                o1 = _parse_odds(await _safe_text(odds1_el))
                o2 = _parse_odds(await _safe_text(odds2_el))

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
                logger.debug("Ladbrokes pair %d parse error: %s", i, exc)
    finally:
        await page.close()

    logger.info("Ladbrokes: %d matches", len(results))
    return results


# ---------------------------------------------------------------------------
# Bet365
# ---------------------------------------------------------------------------
# Bet365 renders entirely via JS. The CSS defines gl-ParticipantBorderless_Name
# and gl-ParticipantBorderless_Odds but actual elements use obfuscated classes.
# This scraper tries aria-label on price buttons as a fallback.
# If it returns 0, the rest of the scan continues unaffected.

async def scrape_bet365(ctx: BrowserContext) -> list[dict]:
    page = await _open(ctx, "https://www.bet365.com.au/#/AC/B13/C1/D1002/F2/", wait_until="networkidle")
    if not page:
        return []

    results: list[dict] = []
    try:
        await asyncio.sleep(5)

        NAME_SEL  = '[class*="ParticipantBorderless_Name"], [class*="ParticipantName"]'
        ODDS_SEL  = '[class*="ParticipantBorderless_Odds"], [class*="ParticipantOdds"]'

        try:
            await page.wait_for_selector(NAME_SEL, timeout=_WAIT_TIMEOUT)
        except PWTimeout:
            logger.warning("Bet365: timed out — likely blocked or no live tennis")
            return []

        names = await page.query_selector_all(NAME_SEL)
        odds  = await page.query_selector_all(ODDS_SEL)

        for i in range(0, min(len(names), len(odds)) - 1, 2):
            try:
                p1 = await _safe_text(names[i])
                p2 = await _safe_text(names[i + 1])
                o1 = _parse_odds(await _safe_text(odds[i]))
                o2 = _parse_odds(await _safe_text(odds[i + 1]))

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
                logger.debug("Bet365 pair %d error: %s", i, exc)
    finally:
        await page.close()

    logger.info("Bet365: %d matches", len(results))
    return results


# ---------------------------------------------------------------------------
# Betfair Exchange  (website scrape)
# ---------------------------------------------------------------------------
# Verified selectors (2026-04-20):
#   Runner names: .runner-name
#   Back prices:  [class*="ui-display-decimal-price"]
#
# The landing page /sport/tennis shows featured/sidebar markets.
# Runners and prices appear interleaved in DOM order — pair every 2.

async def scrape_betfair(ctx: BrowserContext) -> list[dict]:
    WAIT_FOR  = '.runner-name'
    NAME_SEL  = '.runner-name'
    PRICE_SEL = '[class*="ui-display-decimal-price"]'

    page = await _open(ctx, "https://www.betfair.com.au/sport/tennis")
    if not page:
        return []

    results: list[dict] = []
    try:
        await asyncio.sleep(4)
        try:
            await page.wait_for_selector(WAIT_FOR, timeout=_WAIT_TIMEOUT)
        except PWTimeout:
            logger.warning("Betfair: no runner elements found")
            return []

        names  = await page.query_selector_all(NAME_SEL)
        prices = await page.query_selector_all(PRICE_SEL)
        logger.debug("Betfair: %d runners, %d prices", len(names), len(prices))

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
                        "bookmaker": "Betfair Exchange",
                    })
            except Exception as exc:
                logger.debug("Betfair pair %d error: %s", i, exc)
    finally:
        await page.close()

    logger.info("Betfair: %d matches", len(results))
    return results


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def scrape_all_bookmakers() -> list[dict]:
    """
    One shared Chromium browser, all five sources scraped concurrently.
    One source failing does not affect the others.

    Betfair source: scrape_betfair() (website).
    To switch to the official API, see the module docstring above.
    """
    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx: BrowserContext = await browser.new_context(
            user_agent=_STEALTH_UA,
            viewport={"width": 1280, "height": 900},
            locale="en-AU",
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        labels = ["Sportsbet", "Neds", "Ladbrokes", "Bet365", "Betfair Exchange"]
        tasks  = [
            scrape_sportsbet(ctx),
            scrape_neds(ctx),
            scrape_ladbrokes(ctx),
            scrape_bet365(ctx),
            scrape_betfair(ctx),   # ← swap for API call when ready
        ]

        raw = await asyncio.gather(*tasks, return_exceptions=True)

        combined: list[dict] = []
        for label, res in zip(labels, raw):
            if isinstance(res, Exception):
                logger.error("%s raised an exception: %s", label, res)
            elif isinstance(res, list):
                combined.extend(res)

        await ctx.close()
        await browser.close()

    return combined
