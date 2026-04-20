"""
Tennis Arbitrage Scanner — Australian bookmakers + Betfair Exchange.

Usage:
    1. Install dependencies:  pip install -r requirements.txt
    2. Install Playwright browsers (first run only):  playwright install chromium
    3. Run:  python scanner.py

The scanner scrapes Sportsbet, Neds, Ladbrokes, Bet365, and Betfair via
Playwright, cross-matches fixtures using fuzzy player-name matching, and
alerts whenever a new arbitrage opportunity is detected. Rescans every 60s.

Switching Betfair to the official API:
    See the docstring in scrapers.scrape_betfair() — it's a one-function swap.
"""

import asyncio
import json
import logging
import re
import sys
import time
from datetime import datetime

from rapidfuzz import fuzz, process

from scrapers import scrape_all_bookmakers

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LOG_FILE        = "arb_log.txt"
SCAN_DATA_FILE  = "scan_data.json"
SCAN_INTERVAL   = 60     # seconds between scans
TOTAL_STAKE     = 100.0  # AUD for stake-split calculation
FUZZY_THRESHOLD = 82     # min rapidfuzz score (0–100) to merge player names

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("arb_scanner")

# ---------------------------------------------------------------------------
# Name normalisation & fuzzy match-key merging
# ---------------------------------------------------------------------------

def _normalise(name: str) -> str:
    """Lowercase, handle 'Last, First' reversal, strip punctuation."""
    name = name.lower().strip()
    if "," in name:
        last, first = name.split(",", 1)
        name = f"{first.strip()} {last.strip()}"
    name = re.sub(r"[^\w\s]", "", name)
    return re.sub(r"\s+", " ", name).strip()


def _make_key(p1: str, p2: str) -> str:
    """Canonical sorted match key for cross-bookmaker grouping."""
    return " | ".join(sorted([_normalise(p1), _normalise(p2)]))


def _fuzzy_key_match(key: str, existing_keys: list[str]) -> str | None:
    """Return the best-matching existing key, or None if below threshold."""
    if not existing_keys:
        return None
    result = process.extractOne(key, existing_keys, scorer=fuzz.token_sort_ratio)
    if result and result[1] >= FUZZY_THRESHOLD:
        return result[0]
    return None

# ---------------------------------------------------------------------------
# Arb calculation
# ---------------------------------------------------------------------------

def _arb_pct(o1: float, o2: float) -> float:
    return (1 / o1) + (1 / o2)


def _stake_split(o1: float, o2: float, total: float = TOTAL_STAKE) -> tuple[float, float, float]:
    """Returns (stake_p1, stake_p2, guaranteed_profit)."""
    ap  = _arb_pct(o1, o2)
    s1  = round(total * (1 / o1) / ap, 2)
    s2  = round(total * (1 / o2) / ap, 2)
    pnl = round(total * (1 / ap - 1), 2)
    return s1, s2, pnl

# ---------------------------------------------------------------------------
# Consolidation
# ---------------------------------------------------------------------------

def consolidate(all_odds: list[dict]) -> dict[str, dict]:
    """
    Group scraped odds by match using fuzzy name matching.
    Returns {match_key: {player1, player2, bets: [{player, odds, bookmaker}]}}.
    """
    groups: dict[str, dict] = {}

    for entry in all_odds:
        p1 = entry.get("player1", "").strip()
        p2 = entry.get("player2", "").strip()
        o1 = entry.get("odds1")
        o2 = entry.get("odds2")
        bk = entry.get("bookmaker", "Unknown")

        if not (p1 and p2 and o1 and o2):
            continue

        raw_key   = _make_key(p1, p2)
        match_key = _fuzzy_key_match(raw_key, list(groups.keys())) or raw_key

        if match_key not in groups:
            groups[match_key] = {"player1": p1, "player2": p2, "bets": []}

        groups[match_key]["bets"].extend([
            {"player": p1, "odds": o1, "bookmaker": bk},
            {"player": p2, "odds": o2, "bookmaker": bk},
        ])

    return groups


def best_odds_per_player(bets: list[dict]) -> dict[str, dict]:
    """Return the highest available odds for each player across all bookmakers."""
    best: dict[str, dict] = {}
    for bet in bets:
        norm = _normalise(bet["player"])
        if norm not in best or bet["odds"] > best[norm]["odds"]:
            best[norm] = bet
    return best

# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _format_arb(match_key: str, p1_bet: dict, p2_bet: dict, ap: float) -> str:
    margin         = round((1 - ap) * 100, 3)
    s1, s2, profit = _stake_split(p1_bet["odds"], p2_bet["odds"])
    lines = [
        "=" * 62,
        f"  ARB FOUND   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"  Match:      {match_key.replace(' | ', ' vs ').title()}",
        f"  Margin:     {margin:.3f}%",
        "-" * 62,
        f"  {p1_bet['player'].title():<28}  {p1_bet['odds']:.3f}  →  {p1_bet['bookmaker']}",
        f"  {p2_bet['player'].title():<28}  {p2_bet['odds']:.3f}  →  {p2_bet['bookmaker']}",
        "-" * 62,
        f"  Stake split (${TOTAL_STAKE:.0f} total)",
        f"    {p1_bet['player'].title():<28}  ${s1:.2f}",
        f"    {p2_bet['player'].title():<28}  ${s2:.2f}",
        f"  Guaranteed profit:              ${profit:.2f}",
        "=" * 62,
    ]
    return "\n".join(lines)


def _log(message: str) -> None:
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(message + "\n\n")


def _write_scan_data(matches: list[dict], source_counts: dict, scan_num: int) -> None:
    data = {
        "last_scan":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_scan_ts":  time.time(),
        "scan_num":      scan_num,
        "scan_interval": SCAN_INTERVAL,
        "total_matches": len(matches),
        "arb_count":     sum(1 for m in matches if m["is_arb"]),
        "source_counts": source_counts,
        "matches":       matches,
    }
    with open(SCAN_DATA_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh)

# ---------------------------------------------------------------------------
# Scan cycle
# ---------------------------------------------------------------------------

async def run_scan(seen_arbs: set[str], scan_num: int) -> int:
    """
    One full scan cycle. Scrapes all sources, finds arbs, logs new ones,
    writes scan_data.json for the dashboard.
    Returns the number of new arbs found. `seen_arbs` is mutated in-place.
    """
    logger.info("--- Scan started ---")

    all_odds = await scrape_all_bookmakers()
    logger.info("Total raw odds entries: %d", len(all_odds))

    source_counts: dict[str, int] = {}
    for entry in all_odds:
        bk = entry.get("bookmaker", "Unknown")
        source_counts[bk] = source_counts.get(bk, 0) + 1

    if not all_odds:
        logger.warning("No odds retrieved. Check scraper selectors and network access.")
        return 0

    groups = consolidate(all_odds)
    logger.info("Unique matches after consolidation: %d", len(groups))

    matches: list[dict] = []
    new_count = 0

    for match_key, data in groups.items():
        best    = best_odds_per_player(data["bets"])
        players = list(best.values())

        if len(players) < 2:
            continue

        p1_bet, p2_bet = players[0], players[1]
        ap     = _arb_pct(p1_bet["odds"], p2_bet["odds"])
        is_arb = ap < 1.0

        row: dict = {
            "match":    match_key.replace(" | ", " vs ").title(),
            "player1":  p1_bet["player"].title(),
            "bookie1":  p1_bet["bookmaker"],
            "odds1":    p1_bet["odds"],
            "player2":  p2_bet["player"].title(),
            "bookie2":  p2_bet["bookmaker"],
            "odds2":    p2_bet["odds"],
            "arb_pct":  round(ap, 5),
            "margin":   round((1 - ap) * 100, 3),
            "is_arb":   is_arb,
            "stake1":   None,
            "stake2":   None,
            "profit":   None,
        }

        if is_arb:
            s1, s2, profit = _stake_split(p1_bet["odds"], p2_bet["odds"])
            row.update({"stake1": s1, "stake2": s2, "profit": profit})

            arb_id = (
                f"{match_key}::"
                f"{p1_bet['bookmaker']}@{p1_bet['odds']:.3f}::"
                f"{p2_bet['bookmaker']}@{p2_bet['odds']:.3f}"
            )
            if arb_id not in seen_arbs:
                seen_arbs.add(arb_id)
                alert = _format_arb(match_key, p1_bet, p2_bet, ap)
                print(alert)
                _log(alert)
                new_count += 1

        matches.append(row)

    # Sort: arbs first (by margin desc), then by margin ascending
    matches.sort(key=lambda m: (not m["is_arb"], m["arb_pct"]))

    _write_scan_data(matches, source_counts, scan_num)

    if new_count == 0:
        logger.info("No new arbs found this scan.")
    else:
        logger.info("NEW arbs this scan: %d", new_count)

    return new_count

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    seen_arbs: set[str] = set()

    logger.info("Tennis Arb Scanner started — scanning every %ds.", SCAN_INTERVAL)
    logger.info("Results logged to: %s", LOG_FILE)
    logger.info("Press Ctrl+C to stop.\n")

    scan_num = 0
    while True:
        scan_num += 1
        logger.info("Scan #%d", scan_num)
        try:
            await run_scan(seen_arbs, scan_num)
        except Exception as exc:
            logger.error("Unexpected error during scan #%d: %s", scan_num, exc, exc_info=True)

        logger.info("Next scan in %ds...\n", SCAN_INTERVAL)
        try:
            await asyncio.sleep(SCAN_INTERVAL)
        except asyncio.CancelledError:
            break


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nScanner stopped.")
