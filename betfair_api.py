"""
Betfair Exchange API client (JSON-RPC v1).

Docs: https://developer.betfair.com/exchange-api/
Authentication: non-interactive (username + password + app key).
A "Live" app key is required for real-time prices — a "Delay" key has a 60s lag.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_LOGIN_URL = "https://identitysso.betfair.com/api/login"
_API_URL   = "https://api.betfair.com/exchange/betting/json-rpc/v1"
_TENNIS_EVENT_TYPE = "2"


class BetfairClient:
    def __init__(self, username: str, password: str, app_key: str):
        self.username  = username
        self.password  = password
        self.app_key   = app_key
        self._token: Optional[str] = None
        self._session  = requests.Session()
        self._session.headers["Accept"] = "application/json"

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def login(self) -> bool:
        try:
            resp = self._session.post(
                _LOGIN_URL,
                data={"username": self.username, "password": self.password},
                headers={
                    "X-Application": self.app_key,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=15,
            )
            data = resp.json()
            if data.get("status") == "SUCCESS":
                self._token = data["token"]
                logger.info("Betfair: login OK")
                return True
            logger.error("Betfair: login failed — %s", data.get("error", "unknown"))
            return False
        except Exception as exc:
            logger.error("Betfair: login exception — %s", exc)
            return False

    # ------------------------------------------------------------------
    # JSON-RPC helper
    # ------------------------------------------------------------------

    def _rpc(self, method: str, params: dict) -> Optional[object]:
        if not self._token:
            if not self.login():
                return None

        payload = [{"jsonrpc": "2.0", "method": f"SportsAPING/v1.0/{method}", "params": params, "id": 1}]
        try:
            resp = self._session.post(
                _API_URL,
                json=payload,
                headers={
                    "X-Application":  self.app_key,
                    "X-Authentication": self._token,
                    "Content-Type":   "application/json",
                },
                timeout=20,
            )
            items = resp.json()
            if not (isinstance(items, list) and items):
                return None
            item = items[0]
            if "error" in item:
                err = str(item["error"])
                logger.error("Betfair RPC %s error: %s", method, err)
                if "NO_SESSION" in err or "INVALID_SESSION" in err:
                    self._token = None  # force re-login next call
                return None
            return item.get("result")
        except Exception as exc:
            logger.error("Betfair RPC %s exception: %s", method, exc)
            self._token = None
            return None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_live_tennis_odds(self) -> list[dict]:
        """
        Returns a list of dicts:
          {match, player1, player2, odds1, odds2, bookmaker}
        covering all available MATCH_ODDS markets for the next 24 hours.
        """
        now = datetime.now(timezone.utc)
        catalogue = self._rpc("listMarketCatalogue", {
            "filter": {
                "eventTypeIds": [_TENNIS_EVENT_TYPE],
                "marketTypeCodes": ["MATCH_ODDS"],
                "marketStartTime": {
                    "from": now.isoformat(),
                    "to": (now + timedelta(hours=24)).isoformat(),
                },
                "bspOnly": False,
            },
            "marketProjection": ["RUNNER_DESCRIPTION", "EVENT", "MARKET_START_TIME"],
            "maxResults": 200,
            "sort": "FIRST_TO_START",
        })

        if not catalogue:
            logger.warning("Betfair: no tennis markets returned")
            return []

        market_ids    = [m["marketId"] for m in catalogue]
        catalogue_map = {m["marketId"]: m for m in catalogue}

        # Fetch prices in batches of 40 (API hard limit)
        books: list[dict] = []
        for i in range(0, len(market_ids), 40):
            batch = self._rpc("listMarketBook", {
                "marketIds": market_ids[i : i + 40],
                "priceProjection": {
                    "priceData": ["EX_BEST_OFFERS"],
                    "exBestOffersOverrides": {"bestPricesDepth": 1},
                },
            })
            if batch:
                books.extend(batch)

        results: list[dict] = []
        for book in books:
            mid  = book.get("marketId")
            cat  = catalogue_map.get(mid)
            if not cat:
                continue

            runners = cat.get("runners", [])
            if len(runners) < 2:
                continue

            book_runner_map = {r["selectionId"]: r for r in book.get("runners", [])}

            player_odds: list[dict] = []
            for runner in runners[:2]:
                sel_id = runner["selectionId"]
                name   = runner.get("runnerName", "Unknown")
                br     = book_runner_map.get(sel_id, {})
                # Skip removed runners
                if br.get("status") == "REMOVED":
                    continue
                backs  = br.get("ex", {}).get("availableToBack", [])
                if not backs:
                    continue
                price = backs[0]["price"]
                if price > 1.0:
                    player_odds.append({"name": name, "odds": price})

            if len(player_odds) == 2:
                event_name = cat.get("event", {}).get("name", "")
                match_str  = event_name or f"{player_odds[0]['name']} v {player_odds[1]['name']}"
                results.append({
                    "match":     match_str,
                    "player1":   player_odds[0]["name"],
                    "player2":   player_odds[1]["name"],
                    "odds1":     player_odds[0]["odds"],
                    "odds2":     player_odds[1]["odds"],
                    "bookmaker": "Betfair Exchange",
                })

        logger.info("Betfair: %d usable tennis markets", len(results))
        return results
