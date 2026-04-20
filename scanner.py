"""
Tennis & Soccer Arbitrage Scanner — Australian bookmakers + Betfair Exchange.

Usage:
    python scanner.py          (scanner only)
    python dashboard.py        (web UI — run in a separate terminal)

Scans both sports every 60 seconds in one browser session (10 pages parallel).
Writes scan_data_tennis.json and scan_data_soccer.json for the dashboard.
"""

import asyncio
import json
import logging
import re
import sys
import time
from datetime import datetime

from rapidfuzz import fuzz, process

from scrapers import scrape_all_sports

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LOG_FILE             = "arb_log.txt"
SCAN_DATA_TENNIS     = "scan_data_tennis.json"
SCAN_DATA_SOCCER     = "scan_data_soccer.json"
SCAN_INTERVAL        = 60
TOTAL_STAKE          = 100.0
FUZZY_THRESHOLD      = 82
# Sanity guards — reject arbs that are suspiciously large (bad/virtual data)
# or where all best odds come from the same bookmaker (can't cross-book arb)
MAX_VALID_MARGIN     = 8.0    # % — anything above this is almost certainly bad data

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("arb_scanner")

# ---------------------------------------------------------------------------
# Name / key helpers  (shared by both sports)
# ---------------------------------------------------------------------------

def _normalise(name: str) -> str:
    name = name.lower().strip()
    if "," in name:
        last, first = name.split(",", 1)
        name = f"{first.strip()} {last.strip()}"
    name = re.sub(r"[^\w\s]", "", name)
    return re.sub(r"\s+", " ", name).strip()


def _make_key(a: str, b: str) -> str:
    return " | ".join(sorted([_normalise(a), _normalise(b)]))


def _fuzzy_match(key: str, existing: list[str]) -> str | None:
    if not existing:
        return None
    result = process.extractOne(key, existing, scorer=fuzz.token_sort_ratio)
    if result and result[1] >= FUZZY_THRESHOLD:
        return result[0]
    return None

# ---------------------------------------------------------------------------
# 2-way arb  (tennis)
# ---------------------------------------------------------------------------

def _arb2(o1: float, o2: float) -> float:
    return (1 / o1) + (1 / o2)


def _stakes2(o1: float, o2: float, total: float = TOTAL_STAKE) -> tuple[float, float, float]:
    ap = _arb2(o1, o2)
    return (round(total * (1/o1) / ap, 2),
            round(total * (1/o2) / ap, 2),
            round(total * (1/ap - 1), 2))

# ---------------------------------------------------------------------------
# 3-way arb  (soccer)
# ---------------------------------------------------------------------------

def _arb3(oh: float, od: float, oa: float) -> float:
    return (1 / oh) + (1 / od) + (1 / oa)


def _stakes3(oh: float, od: float, oa: float, total: float = TOTAL_STAKE) -> tuple[float, float, float, float]:
    ap = _arb3(oh, od, oa)
    return (round(total * (1/oh) / ap, 2),
            round(total * (1/od) / ap, 2),
            round(total * (1/oa) / ap, 2),
            round(total * (1/ap - 1), 2))

# ---------------------------------------------------------------------------
# Consolidation
# ---------------------------------------------------------------------------

def consolidate_tennis(all_odds: list[dict]) -> dict:
    groups: dict = {}
    for e in all_odds:
        p1, p2 = e.get("player1", "").strip(), e.get("player2", "").strip()
        o1, o2, bk = e.get("odds1"), e.get("odds2"), e.get("bookmaker", "?")
        if not (p1 and p2 and o1 and o2):
            continue
        raw = _make_key(p1, p2)
        key = _fuzzy_match(raw, list(groups)) or raw
        if key not in groups:
            groups[key] = {"bets": []}
        groups[key]["bets"] += [{"player": p1, "odds": o1, "bookmaker": bk},
                                 {"player": p2, "odds": o2, "bookmaker": bk}]
    return groups


def best_tennis(bets: list[dict]) -> dict:
    best: dict = {}
    for b in bets:
        n = _normalise(b["player"])
        if n not in best or b["odds"] > best[n]["odds"]:
            best[n] = b
    return best


def consolidate_soccer(all_odds: list[dict]) -> dict:
    groups: dict = {}
    for e in all_odds:
        t1, t2 = e.get("team1", "").strip(), e.get("team2", "").strip()
        oh, od, oa = e.get("odds_home"), e.get("odds_draw"), e.get("odds_away")
        bk = e.get("bookmaker", "?")
        if not (t1 and t2 and oh and od and oa):
            continue
        raw = _make_key(t1, t2)
        key = _fuzzy_match(raw, list(groups)) or raw
        if key not in groups:
            groups[key] = {"team1": t1, "team2": t2,
                           "home_bets": [], "draw_bets": [], "away_bets": []}
        groups[key]["home_bets"].append({"odds": oh, "bookmaker": bk})
        groups[key]["draw_bets"].append({"odds": od, "bookmaker": bk})
        groups[key]["away_bets"].append({"odds": oa, "bookmaker": bk})
    return groups

# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------

def _write_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)

# ---------------------------------------------------------------------------
# Alert formatting + logging
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n\n")


def _fmt_tennis_arb(key: str, p1: dict, p2: dict, ap: float) -> str:
    margin = round((1 - ap) * 100, 3)
    s1, s2, profit = _stakes2(p1["odds"], p2["odds"])
    return "\n".join([
        "=" * 62,
        f"  [TENNIS ARB]  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"  Match:    {key.replace(' | ', ' vs ').title()}",
        f"  Margin:   +{margin:.3f}%",
        "-" * 62,
        f"  {p1['player'].title():<28}  {p1['odds']:.3f}  @ {p1['bookmaker']}",
        f"  {p2['player'].title():<28}  {p2['odds']:.3f}  @ {p2['bookmaker']}",
        "-" * 62,
        f"  Stake ${TOTAL_STAKE:.0f}: ${s1} / ${s2}   Profit: ${profit}",
        "=" * 62,
    ])


def _fmt_soccer_arb(key: str, home: dict, draw: dict, away: dict, t1: str, t2: str, ap: float) -> str:
    margin = round((1 - ap) * 100, 3)
    sh, sd, sa, profit = _stakes3(home["odds"], draw["odds"], away["odds"])
    return "\n".join([
        "=" * 62,
        f"  [SOCCER ARB]  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"  Match:    {key.replace(' | ', ' vs ').title()}",
        f"  Margin:   +{margin:.3f}%",
        "-" * 62,
        f"  Home ({t1.title()})  {home['odds']:.3f}  @ {home['bookmaker']}  ${sh}",
        f"  Draw            {draw['odds']:.3f}  @ {draw['bookmaker']}  ${sd}",
        f"  Away ({t2.title()})  {away['odds']:.3f}  @ {away['bookmaker']}  ${sa}",
        "-" * 62,
        f"  Stake ${TOTAL_STAKE:.0f} total   Profit: ${profit}",
        "=" * 62,
    ])

# ---------------------------------------------------------------------------
# Scan cycle
# ---------------------------------------------------------------------------

async def run_scan(seen_tennis: set, seen_soccer: set, scan_num: int) -> None:
    logger.info("--- Scan #%d started ---", scan_num)

    tennis_odds, soccer_odds = await scrape_all_sports()

    # Source counts
    def _counts(odds):
        c: dict = {}
        for e in odds:
            bk = e.get("bookmaker", "?")
            c[bk] = c.get(bk, 0) + 1
        return c

    # ---- Tennis ----
    tennis_groups = consolidate_tennis(tennis_odds)
    logger.info("Tennis: %d raw entries, %d matches", len(tennis_odds), len(tennis_groups))
    tennis_rows: list[dict] = []

    for key, data in tennis_groups.items():
        best = best_tennis(data["bets"])
        players = list(best.values())
        if len(players) < 2:
            continue
        p1, p2 = players[0], players[1]
        ap     = _arb2(p1["odds"], p2["odds"])
        margin_pct = (1 - ap) * 100
        is_arb = (ap < 1.0
                  and p1["bookmaker"] != p2["bookmaker"]
                  and margin_pct <= MAX_VALID_MARGIN)
        row: dict = {
            "match":   key.replace(" | ", " vs ").title(),
            "player1": p1["player"].title(), "bookie1": p1["bookmaker"], "odds1": p1["odds"],
            "player2": p2["player"].title(), "bookie2": p2["bookmaker"], "odds2": p2["odds"],
            "arb_pct": round(ap, 5), "margin": round((1 - ap) * 100, 3), "is_arb": is_arb,
            "stake1": None, "stake2": None, "profit": None,
        }
        if is_arb:
            s1, s2, profit = _stakes2(p1["odds"], p2["odds"])
            row.update({"stake1": s1, "stake2": s2, "profit": profit})
            arb_id = f"{key}::{p1['bookmaker']}@{p1['odds']:.3f}::{p2['bookmaker']}@{p2['odds']:.3f}"
            if arb_id not in seen_tennis:
                seen_tennis.add(arb_id)
                alert = _fmt_tennis_arb(key, p1, p2, ap)
                print(alert)
                _log(alert)
        tennis_rows.append(row)

    tennis_rows.sort(key=lambda r: (not r["is_arb"], r["arb_pct"]))
    _write_json(SCAN_DATA_TENNIS, {
        "sport": "tennis", "last_scan": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_scan_ts": time.time(), "scan_num": scan_num, "scan_interval": SCAN_INTERVAL,
        "total_matches": len(tennis_rows),
        "arb_count": sum(1 for r in tennis_rows if r["is_arb"]),
        "source_counts": _counts(tennis_odds), "matches": tennis_rows,
    })

    # ---- Soccer ----
    soccer_groups = consolidate_soccer(soccer_odds)
    logger.info("Soccer: %d raw entries, %d matches", len(soccer_odds), len(soccer_groups))
    soccer_rows: list[dict] = []

    for key, data in soccer_groups.items():
        if not (data["home_bets"] and data["draw_bets"] and data["away_bets"]):
            continue
        home = max(data["home_bets"], key=lambda x: x["odds"])
        draw = max(data["draw_bets"], key=lambda x: x["odds"])
        away = max(data["away_bets"], key=lambda x: x["odds"])
        ap     = _arb3(home["odds"], draw["odds"], away["odds"])
        # Reject combined odds that don't look like a real 1X2 market.
        # Taking MAX odds independently can mix entries from different markets
        # (e.g. group-stage futures merged via fuzzy match), producing impossible
        # implied-prob sums. Filter these out before showing anything.
        if not (0.75 <= ap <= 1.35):
            continue
        # Require at least 2 different bookmakers AND a realistic margin cap.
        # Single-bookie "arbs" are virtual/promo markets that can't be cross-booked.
        bookies_involved = len({home["bookmaker"], draw["bookmaker"], away["bookmaker"]})
        margin_pct       = (1 - ap) * 100
        is_arb = (ap < 1.0
                  and bookies_involved >= 2
                  and margin_pct <= MAX_VALID_MARGIN)
        t1, t2 = data["team1"].title(), data["team2"].title()
        row: dict = {
            "match": key.replace(" | ", " vs ").title(),
            "team1": t1, "team2": t2,
            "odds_home": home["odds"], "bookie_home": home["bookmaker"],
            "odds_draw": draw["odds"], "bookie_draw": draw["bookmaker"],
            "odds_away": away["odds"], "bookie_away": away["bookmaker"],
            "arb_pct": round(ap, 5), "margin": round((1 - ap) * 100, 3), "is_arb": is_arb,
            "stake_home": None, "stake_draw": None, "stake_away": None, "profit": None,
        }
        if is_arb:
            sh, sd, sa, profit = _stakes3(home["odds"], draw["odds"], away["odds"])
            row.update({"stake_home": sh, "stake_draw": sd, "stake_away": sa, "profit": profit})
            arb_id = f"{key}::{home['bookmaker']}@{home['odds']:.3f}::{draw['bookmaker']}@{draw['odds']:.3f}::{away['bookmaker']}@{away['odds']:.3f}"
            if arb_id not in seen_soccer:
                seen_soccer.add(arb_id)
                alert = _fmt_soccer_arb(key, home, draw, away, t1, t2, ap)
                print(alert)
                _log(alert)
        soccer_rows.append(row)

    soccer_rows.sort(key=lambda r: (not r["is_arb"], r["arb_pct"]))
    _write_json(SCAN_DATA_SOCCER, {
        "sport": "soccer", "last_scan": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_scan_ts": time.time(), "scan_num": scan_num, "scan_interval": SCAN_INTERVAL,
        "total_matches": len(soccer_rows),
        "arb_count": sum(1 for r in soccer_rows if r["is_arb"]),
        "source_counts": _counts(soccer_odds), "matches": soccer_rows,
    })

    logger.info("Scan #%d complete — tennis: %d matches, soccer: %d matches",
                scan_num, len(tennis_rows), len(soccer_rows))

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    seen_tennis: set = set()
    seen_soccer: set = set()
    scan_num = 0

    logger.info("Arb Scanner started — tennis + soccer, every %ds.", SCAN_INTERVAL)
    logger.info("Open dashboard: python dashboard.py  →  http://localhost:5000")
    logger.info("Press Ctrl+C to stop.\n")

    while True:
        scan_num += 1
        try:
            await run_scan(seen_tennis, seen_soccer, scan_num)
        except Exception as exc:
            logger.error("Scan #%d error: %s", scan_num, exc, exc_info=True)
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
