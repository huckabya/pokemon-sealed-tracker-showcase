"""Minimal, polite client for tcgcsv.com (a keyless mirror of TCGplayer's API).

Etiquette (from the TCGCSV FAQ): identify yourself with a clear User-Agent,
pause ~0.25 s between requests, and pull at most once per day.
"""
from __future__ import annotations

import datetime as dt
import time
from typing import Any, Callable

import requests

BASE_URL = "https://tcgcsv.com"
POKEMON_CATEGORY_ID = 3
USER_AGENT = "sealed-tracker/0.1 (personal research; private repo)"


class TcgcsvError(RuntimeError):
    """Raised when TCGCSV returns an unexpected response after retries."""


class TcgcsvClient:
    def __init__(
        self,
        session: requests.Session | None = None,
        delay_s: float = 0.25,
        retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.delay_s = delay_s
        self.retries = retries
        self._sleep = sleep

    def _get(self, path: str) -> requests.Response:
        url = f"{BASE_URL}{path}"
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._sleep(self.delay_s)
            try:
                resp = self.session.get(url, timeout=30)
            except requests.RequestException as exc:  # network blip
                last_exc = exc
            else:
                if resp.status_code == 404 or resp.ok:
                    return resp
                last_exc = TcgcsvError(f"HTTP {resp.status_code} for {url}")
            self._sleep(2.0 * attempt)
        raise TcgcsvError(f"giving up on {url}: {last_exc}")

    def get_results(self, path: str) -> list[dict[str, Any]]:
        """GET a TCGCSV JSON collection; 404 means an empty collection."""
        resp = self._get(path)
        if resp.status_code == 404:
            return []
        body = resp.json()
        if body.get("success") is False:
            raise TcgcsvError(f"success=false for {path}: {body.get('errors')}")
        return list(body.get("results") or [])

    def last_updated(self) -> dt.datetime:
        """UTC timestamp of TCGCSV's most recent daily refresh."""
        resp = self._get("/last-updated.txt")
        if resp.status_code == 404:
            raise TcgcsvError("last-updated.txt missing")
        return parse_last_updated(resp.text)

    def groups(self, category_id: int = POKEMON_CATEGORY_ID) -> list[dict[str, Any]]:
        return self.get_results(f"/tcgplayer/{category_id}/groups")

    def products(self, group_id: int, category_id: int = POKEMON_CATEGORY_ID) -> list[dict[str, Any]]:
        return self.get_results(f"/tcgplayer/{category_id}/{group_id}/products")

    def prices(self, group_id: int, category_id: int = POKEMON_CATEGORY_ID) -> list[dict[str, Any]]:
        return self.get_results(f"/tcgplayer/{category_id}/{group_id}/prices")


def parse_last_updated(text: str) -> dt.datetime:
    """Parse last-updated.txt (ISO-8601, e.g. '2026-09-27T20:04:11+0000')."""
    raw = text.strip().replace("Z", "+00:00")
    if len(raw) >= 5 and raw[-5] in "+-" and raw[-3] != ":":
        raw = raw[:-2] + ":" + raw[-2:]  # +0000 -> +00:00
    value = dt.datetime.fromisoformat(raw)
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)
