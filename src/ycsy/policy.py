"""Optional maintenance channel.

A released build may point at a static JSON document. When that document says
the project is over, the tool stops instead of quietly hammering an API that no
longer matches.

Rules this module keeps:

* **Off by default** - with no ``policy_urls`` configured, no request is made.
* **Fail open** - timeout, non-200, invalid JSON, unknown schema: keep working.
  A broken URL must never brick somebody's install.
* **No local kill date** - there is no hard-coded expiry to get wrong.
* **Auditable** - the whole mechanism is these ~40 lines and one GET.

The payload is ``{"status": "disabled", "msg": "..."}``; anything else means
"carry on".
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional, Tuple

import requests

TIMEOUT_SECONDS = 3.0
RECHECK_SECONDS = 1800.0
DISABLED_STATES = {"disabled", "stop", "off", "retired"}


class Policy:
    def __init__(self, urls: Optional[List[str]] = None, log: Optional[Callable[[str], None]] = None,
                 fetcher: Optional[Callable[[str], dict]] = None) -> None:
        self.urls = [u for u in (urls or []) if str(u).strip()]
        self.log = log or (lambda message: None)
        self._fetch = fetcher or self._http_fetch
        self._verdict: Tuple[bool, str] = (True, "")
        self._checked_at = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.urls)

    def check(self, force: bool = False) -> Tuple[bool, str]:
        """Return ``(allowed, message)``. Never raises, never blocks for long."""
        if not self.enabled:
            return True, ""
        now = time.time()
        if not force and now - self._checked_at < RECHECK_SECONDS:
            return self._verdict
        for url in self.urls:
            try:
                payload = self._fetch(url)
            except Exception:  # network, TLS, JSON, anything
                continue
            if not isinstance(payload, dict):
                continue
            status = str(payload.get("status") or "").strip().lower()
            if status in DISABLED_STATES:
                message = str(payload.get("msg") or "").strip()
                self._verdict = (False, message)
                self._checked_at = now
                return self._verdict
            self._verdict = (True, "")
            self._checked_at = now
            return self._verdict
        # every source failed: fail open, retry on the next window
        self._verdict = (True, "")
        self._checked_at = now
        return self._verdict

    @staticmethod
    def _http_fetch(url: str) -> dict:
        response = requests.get(url, timeout=TIMEOUT_SECONDS,
                                headers={"User-Agent": "ycsy-book"})
        if response.status_code != 200:
            raise RuntimeError("status %s" % response.status_code)
        return response.json()
