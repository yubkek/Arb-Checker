"""
Playwright-based scrapers for Australian bookmakers + Betfair Exchange.

Tennis scrapers return:  {match, player1, player2, odds1, odds2, bookmaker}
Soccer scrapers return:  {match, team1, team2, odds_home, odds_draw, odds_away, bookmaker}

Selectors verified against live HTML on 2026-04-20.
If a scraper returns 0 results, re-run diagnose.py and update the selector constants.

TO SWAP BETFAIR WEBSITE → OFFICIAL API:
  In scrape_betfair() / scrape_betfair_soccer(), replace the body with a
  BetfairClient call. The return format is identical.
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

_NAV_TIMEOUT  = 60_000
_WAIT_TIMEOUT = 30_000
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


_VIRTUAL_TEAM_WORDS = {
    "city", "fc", "united", "rangers", "rovers", "reds", "blues", "stars",
    "whites", "greens", "juniors", "seniors", "farmers", "boys", "athletic",
    "hotspur", "wanderers", "dynamo", "olympic", "sporting",
}


def _clean_name(text: str) -> str:
    """Strip live indicators, replacement chars, and trailing junk from scraped names."""
    text = text.replace("\uFFFD", "").replace("\u25CF", "").replace("\u2022", "")
    text = re.sub(r"[^\x20-\x7E\u00C0-\u024F/]", "", text)  # keep Latin + ASCII
    # Strip trailing live-indicator 'v'/'V' appended by Sportsbet on live events.
    # Space-separated case (" v" / " V") is always safe to strip.
    text = re.sub(r"\s+[Vv]$", "", text)
    # Attached case: only strip when preceded by a consonant (safe — avoids corrupting
    # real Slavic surnames ending in -ev/-ov/-av like Medvedev, Rublev, Khachanov).
    text = re.sub(r"(?<=[bcdfghjklmnpqrstwxyz])[Vv]$", "", text, flags=re.IGNORECASE)
    return text.strip()


def _is_virtual_team(name: str) -> bool:
    """Detect Betfair Simulated Reality team names (not real tennis players)."""
    return bool(set(name.lower().split()) & _VIRTUAL_TEAM_WORDS)


def _valid_match_odds(oh: float, od: float, oa: float) -> bool:
    """Reject outright/futures markets — implied probs must sum to 75%–135%."""
    total = (1 / oh) + (1 / od) + (1 / oa)
    return 0.75 <= total <= 1.35


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


# ===========================================================================
# TENNIS SCRAPERS  (2-way markets)
# ===========================================================================

async def scrape_sportsbet(ctx: BrowserContext) -> list[dict]:
    """
    Sportsbet tennis — verified selectors 2026-04-20:
      Card:   [data-automation-id$="-competition-event-card"]
      Teams:  [data-automation-id$="-competition-event-participant-1/2"]
      Odds:   [data-automation-id$="-two-outcome-captioned-text"]  (2 per card)
    """
    CARD_SEL = '[data-automation-id$="-competition-event-card"]'
    P1_SEL   = '[data-automation-id$="-competition-event-participant-1"]'
    P2_SEL   = '[data-automation-id$="-competition-event-participant-2"]'
    ODDS_SEL = '[data-automation-id$="-two-outcome-captioned-text"]'

    page = await _open(ctx, "https://www.sportsbet.com.au/betting/tennis")
    if not page:
        return []
    results: list[dict] = []
    try:
        await asyncio.sleep(5)
        try:
            await page.wait_for_selector(CARD_SEL, timeout=_WAIT_TIMEOUT)
        except PWTimeout:
            logger.warning("Sportsbet tennis: no event cards found")
            return []
        for card in await page.query_selector_all(CARD_SEL):
            try:
                p1_el   = await card.query_selector(P1_SEL)
                p2_el   = await card.query_selector(P2_SEL)
                odd_els = await card.query_selector_all(ODDS_SEL)
                if not (p1_el and p2_el and len(odd_els) >= 2):
                    continue
                p1 = _clean_name(await _safe_text(p1_el))
                p2 = _clean_name(await _safe_text(p2_el))
                o1 = _parse_odds(await _safe_text(odd_els[0]))
                o2 = _parse_odds(await _safe_text(odd_els[1]))
                if p1 and p2 and o1 and o2:
                    results.append({"match": f"{p1} v {p2}", "player1": p1, "player2": p2,
                                    "odds1": o1, "odds2": o2, "bookmaker": "Sportsbet"})
            except Exception as exc:
                logger.debug("Sportsbet tennis card error: %s", exc)
    finally:
        await page.close()
    logger.info("Sportsbet tennis: %d matches", len(results))
    return results


async def _scrape_entain_tennis(ctx: BrowserContext, url: str, name: str) -> list[dict]:
    """Shared logic for Neds + Ladbrokes tennis (same Entain platform)."""
    WAIT_FOR   = '[data-testid="price-button-odds"]'
    BUTTON_SEL = 'button[class*="has-name"]'
    NAME_SEL   = '[data-testid="price-button-name"] span, [data-testid="price-button-name"]'
    ODDS_SEL   = '[data-testid="price-button-odds"]'

    page = await _open(ctx, url)
    if not page:
        return []
    results: list[dict] = []
    try:
        await asyncio.sleep(3)
        try:
            await page.wait_for_selector(WAIT_FOR, timeout=_WAIT_TIMEOUT)
        except PWTimeout:
            logger.warning("%s tennis: no price elements found", name)
            return []
        buttons = await page.query_selector_all(BUTTON_SEL)
        for i in range(0, len(buttons) - 1, 2):
            try:
                b1, b2 = buttons[i], buttons[i + 1]
                n1 = _clean_name(await _safe_text(await b1.query_selector(NAME_SEL)))
                n2 = _clean_name(await _safe_text(await b2.query_selector(NAME_SEL)))
                o1 = _parse_odds(await _safe_text(await b1.query_selector(ODDS_SEL)))
                o2 = _parse_odds(await _safe_text(await b2.query_selector(ODDS_SEL)))
                if n1 and n2 and o1 and o2 and n1.lower() != "draw":
                    results.append({"match": f"{n1} v {n2}", "player1": n1, "player2": n2,
                                    "odds1": o1, "odds2": o2, "bookmaker": name})
            except Exception as exc:
                logger.debug("%s tennis pair %d error: %s", name, i, exc)
    finally:
        await page.close()
    logger.info("%s tennis: %d matches", name, len(results))
    return results


async def scrape_neds(ctx: BrowserContext) -> list[dict]:
    return await _scrape_entain_tennis(ctx, "https://www.neds.com.au/sports/tennis", "Neds")


async def scrape_ladbrokes(ctx: BrowserContext) -> list[dict]:
    return await _scrape_entain_tennis(ctx, "https://www.ladbrokes.com.au/sports/tennis", "Ladbrokes")


async def scrape_bet365(ctx: BrowserContext) -> list[dict]:
    page = await _open(ctx, "https://www.bet365.com.au/#/AC/B13/C1/D1002/F2/", wait_until="networkidle")
    if not page:
        return []
    results: list[dict] = []
    try:
        await asyncio.sleep(5)
        NAME_SEL = '[class*="ParticipantBorderless_Name"], [class*="ParticipantName"]'
        ODDS_SEL = '[class*="ParticipantBorderless_Odds"], [class*="ParticipantOdds"]'
        try:
            await page.wait_for_selector(NAME_SEL, timeout=_WAIT_TIMEOUT)
        except PWTimeout:
            logger.warning("Bet365 tennis: timed out or blocked")
            return []
        names = await page.query_selector_all(NAME_SEL)
        odds  = await page.query_selector_all(ODDS_SEL)
        for i in range(0, min(len(names), len(odds)) - 1, 2):
            try:
                p1 = _clean_name(await _safe_text(names[i]))
                p2 = _clean_name(await _safe_text(names[i + 1]))
                o1 = _parse_odds(await _safe_text(odds[i]))
                o2 = _parse_odds(await _safe_text(odds[i + 1]))
                if p1 and p2 and o1 and o2:
                    results.append({"match": f"{p1} v {p2}", "player1": p1, "player2": p2,
                                    "odds1": o1, "odds2": o2, "bookmaker": "Bet365"})
            except Exception as exc:
                logger.debug("Bet365 tennis pair %d error: %s", i, exc)
    finally:
        await page.close()
    logger.info("Bet365 tennis: %d matches", len(results))
    return results


async def scrape_betfair(ctx: BrowserContext) -> list[dict]:
    """
    Betfair tennis — .runner-name + [class*="ui-display-decimal-price"], pairs of 2.
    """
    page = await _open(ctx, "https://www.betfair.com.au/sport/tennis")
    if not page:
        return []
    results: list[dict] = []
    try:
        await asyncio.sleep(4)
        try:
            await page.wait_for_selector(".runner-name", timeout=_WAIT_TIMEOUT)
        except PWTimeout:
            logger.warning("Betfair tennis: no runner elements")
            return []
        names  = await page.query_selector_all(".runner-name")
        prices = await page.query_selector_all('[class*="ui-display-decimal-price"]')
        for i in range(0, min(len(names), len(prices)) - 1, 2):
            try:
                p1 = _clean_name(await _safe_text(names[i]))
                p2 = _clean_name(await _safe_text(names[i + 1]))
                o1 = _parse_odds(await _safe_text(prices[i]))
                o2 = _parse_odds(await _safe_text(prices[i + 1]))
                if (p1 and p2 and o1 and o2
                        and "draw" not in p1.lower() and "draw" not in p2.lower()
                        and not _is_virtual_team(p1) and not _is_virtual_team(p2)):
                    results.append({"match": f"{p1} v {p2}", "player1": p1, "player2": p2,
                                    "odds1": o1, "odds2": o2, "bookmaker": "Betfair Exchange"})
            except Exception as exc:
                logger.debug("Betfair tennis pair %d error: %s", i, exc)
    finally:
        await page.close()
    logger.info("Betfair tennis: %d matches", len(results))
    return results


# ===========================================================================
# SOCCER SCRAPERS  (3-way: home / draw / away)
# ===========================================================================

async def scrape_sportsbet_soccer(ctx: BrowserContext) -> list[dict]:
    """
    Sportsbet soccer — verified 2026-04-20:
      Odds: [data-automation-id$="-three-outcome-captioned-text"]  [home, draw, away]
    """
    CARD_SEL = '[data-automation-id$="-competition-event-card"]'
    P1_SEL   = '[data-automation-id$="-competition-event-participant-1"]'
    P2_SEL   = '[data-automation-id$="-competition-event-participant-2"]'
    ODDS_SEL = '[data-automation-id$="-three-outcome-captioned-text"]'

    page = await _open(ctx, "https://www.sportsbet.com.au/betting/soccer")
    if not page:
        return []
    results: list[dict] = []
    try:
        await asyncio.sleep(5)
        try:
            await page.wait_for_selector(CARD_SEL, timeout=_WAIT_TIMEOUT)
        except PWTimeout:
            logger.warning("Sportsbet soccer: no event cards")
            return []
        for card in await page.query_selector_all(CARD_SEL):
            try:
                t1_el   = await card.query_selector(P1_SEL)
                t2_el   = await card.query_selector(P2_SEL)
                odd_els = await card.query_selector_all(ODDS_SEL)
                if not (t1_el and t2_el and len(odd_els) >= 3):
                    continue
                t1 = _clean_name(await _safe_text(t1_el))
                t2 = _clean_name(await _safe_text(t2_el))
                oh = _parse_odds(await _safe_text(odd_els[0]))
                od = _parse_odds(await _safe_text(odd_els[1]))
                oa = _parse_odds(await _safe_text(odd_els[2]))
                if t1 and t2 and oh and od and oa and _valid_match_odds(oh, od, oa):
                    results.append({"match": f"{t1} v {t2}", "team1": t1, "team2": t2,
                                    "odds_home": oh, "odds_draw": od, "odds_away": oa,
                                    "bookmaker": "Sportsbet"})
            except Exception as exc:
                logger.debug("Sportsbet soccer card error: %s", exc)
    finally:
        await page.close()
    logger.info("Sportsbet soccer: %d matches", len(results))
    return results


async def _scrape_entain_soccer(ctx: BrowserContext, url: str, name: str) -> list[dict]:
    """
    Shared logic for Neds + Ladbrokes soccer.
    Buttons appear as Home, Draw, Away in groups of 3.
    The Draw button has displayTitle = 'Draw'.
    """
    WAIT_FOR   = '[data-testid="price-button-odds"]'
    BUTTON_SEL = 'button[class*="has-name"]'
    NAME_SEL   = '[data-testid="price-button-name"] span, [data-testid="price-button-name"]'
    ODDS_SEL   = '[data-testid="price-button-odds"]'

    page = await _open(ctx, url)
    if not page:
        return []
    results: list[dict] = []
    try:
        await asyncio.sleep(3)
        try:
            await page.wait_for_selector(WAIT_FOR, timeout=_WAIT_TIMEOUT)
        except PWTimeout:
            logger.warning("%s soccer: no price elements found", name)
            return []
        buttons = await page.query_selector_all(BUTTON_SEL)
        for i in range(0, len(buttons) - 2, 3):
            try:
                bh, bd, ba = buttons[i], buttons[i + 1], buttons[i + 2]
                t1  = _clean_name(await _safe_text(await bh.query_selector(NAME_SEL)))
                mid = await _safe_text(await bd.query_selector(NAME_SEL))
                t2  = _clean_name(await _safe_text(await ba.query_selector(NAME_SEL)))
                oh  = _parse_odds(await _safe_text(await bh.query_selector(ODDS_SEL)))
                od  = _parse_odds(await _safe_text(await bd.query_selector(ODDS_SEL)))
                oa  = _parse_odds(await _safe_text(await ba.query_selector(ODDS_SEL)))
                # Validate the middle button really is the draw
                if mid.lower() not in ("draw", "x", "tie"):
                    continue
                if t1 and t2 and oh and od and oa and _valid_match_odds(oh, od, oa):
                    results.append({"match": f"{t1} v {t2}", "team1": t1, "team2": t2,
                                    "odds_home": oh, "odds_draw": od, "odds_away": oa,
                                    "bookmaker": name})
            except Exception as exc:
                logger.debug("%s soccer group %d error: %s", name, i, exc)
    finally:
        await page.close()
    logger.info("%s soccer: %d matches", name, len(results))
    return results


async def scrape_neds_soccer(ctx: BrowserContext) -> list[dict]:
    return await _scrape_entain_soccer(ctx, "https://www.neds.com.au/sports/soccer", "Neds")


async def scrape_ladbrokes_soccer(ctx: BrowserContext) -> list[dict]:
    return await _scrape_entain_soccer(ctx, "https://www.ladbrokes.com.au/sports/soccer", "Ladbrokes")


async def scrape_bet365_soccer(ctx: BrowserContext) -> list[dict]:
    """Bet365 soccer — best effort, likely blocked."""
    page = await _open(ctx, "https://www.bet365.com.au/#/AC/B1/C1/D8/", wait_until="networkidle")
    if not page:
        return []
    results: list[dict] = []
    try:
        await asyncio.sleep(5)
        NAME_SEL = '[class*="ParticipantBorderless_Name"], [class*="ParticipantName"]'
        ODDS_SEL = '[class*="ParticipantBorderless_Odds"], [class*="ParticipantOdds"]'
        try:
            await page.wait_for_selector(NAME_SEL, timeout=_WAIT_TIMEOUT)
        except PWTimeout:
            logger.warning("Bet365 soccer: timed out or blocked")
            return []
        names = await page.query_selector_all(NAME_SEL)
        odds  = await page.query_selector_all(ODDS_SEL)
        for i in range(0, min(len(names), len(odds)) - 2, 3):
            try:
                t1  = _clean_name(await _safe_text(names[i]))
                mid = await _safe_text(names[i + 1])
                t2  = _clean_name(await _safe_text(names[i + 2]))
                oh  = _parse_odds(await _safe_text(odds[i]))
                od  = _parse_odds(await _safe_text(odds[i + 1]))
                oa  = _parse_odds(await _safe_text(odds[i + 2]))
                if mid.lower() not in ("draw", "x", "the draw"):
                    continue
                if t1 and t2 and oh and od and oa and _valid_match_odds(oh, od, oa):
                    results.append({"match": f"{t1} v {t2}", "team1": t1, "team2": t2,
                                    "odds_home": oh, "odds_draw": od, "odds_away": oa,
                                    "bookmaker": "Bet365"})
            except Exception as exc:
                logger.debug("Bet365 soccer group %d error: %s", i, exc)
    finally:
        await page.close()
    logger.info("Bet365 soccer: %d matches", len(results))
    return results


async def scrape_betfair_soccer(ctx: BrowserContext) -> list[dict]:
    """
    Betfair football — verified 2026-04-20:
      .runner-name gives [Home, "The Draw", Away] in groups of 3.
    """
    page = await _open(ctx, "https://www.betfair.com.au/sport/football")
    if not page:
        return []
    results: list[dict] = []
    try:
        await asyncio.sleep(4)
        try:
            await page.wait_for_selector(".runner-name", timeout=_WAIT_TIMEOUT)
        except PWTimeout:
            logger.warning("Betfair soccer: no runner elements")
            return []
        names  = await page.query_selector_all(".runner-name")
        prices = await page.query_selector_all('[class*="ui-display-decimal-price"]')
        for i in range(0, min(len(names), len(prices)) - 2, 3):
            try:
                t1  = _clean_name(await _safe_text(names[i]))
                mid = await _safe_text(names[i + 1])
                t2  = _clean_name(await _safe_text(names[i + 2]))
                oh  = _parse_odds(await _safe_text(prices[i]))
                od  = _parse_odds(await _safe_text(prices[i + 1]))
                oa  = _parse_odds(await _safe_text(prices[i + 2]))
                if "draw" not in mid.lower():
                    continue
                if t1 and t2 and oh and od and oa and _valid_match_odds(oh, od, oa):
                    results.append({"match": f"{t1} v {t2}", "team1": t1, "team2": t2,
                                    "odds_home": oh, "odds_draw": od, "odds_away": oa,
                                    "bookmaker": "Betfair Exchange"})
            except Exception as exc:
                logger.debug("Betfair soccer group %d error: %s", i, exc)
    finally:
        await page.close()
    logger.info("Betfair soccer: %d matches", len(results))
    return results


# ===========================================================================
# Combined orchestrator — one browser, all 10 pages in parallel
# ===========================================================================

async def scrape_all_sports() -> tuple[list[dict], list[dict]]:
    """
    Launches ONE Chromium browser, scrapes all 5 bookmakers for both sports
    simultaneously (10 pages), returns (tennis_odds, soccer_odds).
    One page failing does not affect the others.
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

        tennis_tasks = [
            scrape_sportsbet(ctx),
            scrape_neds(ctx),
            scrape_ladbrokes(ctx),
            scrape_bet365(ctx),
            scrape_betfair(ctx),
        ]
        soccer_tasks = [
            scrape_sportsbet_soccer(ctx),
            scrape_neds_soccer(ctx),
            scrape_ladbrokes_soccer(ctx),
            scrape_bet365_soccer(ctx),
            scrape_betfair_soccer(ctx),
        ]
        tennis_labels = ["Sportsbet", "Neds", "Ladbrokes", "Bet365", "Betfair Exchange"]
        soccer_labels = [f"{l} (soccer)" for l in tennis_labels]

        all_results = await asyncio.gather(
            *(tennis_tasks + soccer_tasks), return_exceptions=True
        )

        tennis: list[dict] = []
        soccer: list[dict] = []
        for label, res in zip(tennis_labels + soccer_labels, all_results):
            if isinstance(res, Exception):
                logger.error("%s raised: %s", label, res)
            elif isinstance(res, list):
                if "(soccer)" in label:
                    soccer.extend(res)
                else:
                    tennis.extend(res)

        await ctx.close()
        await browser.close()

    return tennis, soccer
