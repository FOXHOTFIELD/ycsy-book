"""Optional maintenance channel.

A released build may point at a static JSON document. When that document says
the project is over, the tool stops instead of quietly hammering an API that no
longer matches.

Rules this module keeps:

* **Off by default** - with no ``policy_urls`` configured, no request is made.
* **Fail open** - timeout, non-200, invalid JSON, unknown schema: keep working.
  A broken URL must never brick somebody's install.
* **No local kill date** - there is no hard-coded expiry to get wrong.
* **Auditable** - the whole mechanism is these ~150 lines and one GET.
* **Boring on the wire** - see "Network guards" below.

The payload is ``{"status": "disabled", "msg": "..."}``; anything else means
"carry on".

Network guards
--------------
The URL comes from a config file, so it is treated as untrusted input:

* **HTTPS only** - ``http://`` is refused outright instead of silently upgraded.
* **Host allowlist** - only the GitHub raw hosts this project actually uses.
* **Public addresses only** - the hostname is resolved first and every returned
  address must be public; loopback / private / link-local / reserved / multicast
  answers are refused, which is what stops the config from being pointed at
  ``127.0.0.1``, ``169.254.169.254`` or an intranet host.
* **Redirects are not followed blindly** - each hop is re-validated against the
  same three rules, and more than two hops is an error.
* **Small responses only** - the body is streamed and cut off at 64 KiB, so a
  hostile or broken endpoint cannot feed the parser forever.
* **Short timeouts** - connect/read are bounded and the caller fails open.

Limits are documented on purpose: DNS is resolved twice (once here, once by the
HTTP client), so a rebinding attacker that answers differently between the two
lookups is out of scope. The blast radius is one boolean, and every failure mode
above ends in "keep working".
"""

from __future__ import annotations

import ipaddress
import socket
import time
from typing import Callable, List, Optional, Tuple
from urllib.parse import urlsplit

import requests

TIMEOUT_SECONDS = 3.0
RECHECK_SECONDS = 1800.0
DISABLED_STATES = {"disabled", "stop", "off", "retired"}

# Only the hosts this project publishes the flag on.
ALLOWED_HOSTS = frozenset({"raw.githubusercontent.com", "gist.githubusercontent.com"})
MAX_BYTES = 64 * 1024
MAX_REDIRECTS = 2
CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 5.0
USER_AGENT = "ycsy-book"


class PolicyUrlError(ValueError):
    """The configured URL (or a redirect target) failed the network guards."""


def _resolve_public(host: str) -> None:
    """Reject a hostname whose A/AAAA records point anywhere but the internet."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:                      # offline / no DNS: let the GET fail
        raise PolicyUrlError("cannot resolve %s: %s" % (host, exc)) from exc
    if not infos:
        raise PolicyUrlError("no address for %s" % host)
    for info in infos:
        raw = info[4][0].split("%", 1)[0]       # strip IPv6 zone id
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise PolicyUrlError("bad address %r for %s" % (raw, host)) from exc
        if not address.is_global or address.is_multicast:
            raise PolicyUrlError("%s resolves to non-public address %s" % (host, address))


def check_url(url: str) -> str:
    """Validate one URL and return its host. Raises :class:`PolicyUrlError`."""
    parsed = urlsplit(str(url).strip())
    if parsed.scheme.lower() != "https":
        raise PolicyUrlError("only https is allowed, got %r" % (parsed.scheme or "none"))
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise PolicyUrlError("host %r is not on the allowlist" % host)
    if parsed.port not in (None, 443):
        raise PolicyUrlError("unexpected port %s" % parsed.port)
    if parsed.username or parsed.password:
        raise PolicyUrlError("credentials in the URL are not allowed")
    _resolve_public(host)
    return host


def _read_capped(response: requests.Response) -> bytes:
    chunks: List[bytes] = []
    total = 0
    for chunk in response.iter_content(4096):
        total += len(chunk)
        if total > MAX_BYTES:
            raise PolicyUrlError("response larger than %d bytes" % MAX_BYTES)
        chunks.append(chunk)
    return b"".join(chunks)


def fetch(url: str) -> dict:
    """GET one policy document, with the guards applied to every hop."""
    for _ in range(MAX_REDIRECTS + 1):
        check_url(url)
        response = requests.get(
            url,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            allow_redirects=False,
            stream=True,
            verify=True,
        )
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location") or ""
                if not location:
                    raise PolicyUrlError("redirect without a Location header")
                url = requests.compat.urljoin(url, location)
                continue                        # next loop re-validates it
            if response.status_code != 200:
                raise PolicyUrlError("status %s" % response.status_code)
            body = _read_capped(response)
        finally:
            response.close()
        try:
            import json

            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise PolicyUrlError("payload is not UTF-8 JSON: %s" % exc) from exc
        if not isinstance(payload, dict):
            raise PolicyUrlError("payload is not a JSON object")
        return payload
    raise PolicyUrlError("too many redirects")


class Policy:
    def __init__(self, urls: Optional[List[str]] = None, log: Optional[Callable[[str], None]] = None,
                 fetcher: Optional[Callable[[str], dict]] = None) -> None:
        self.urls = [u for u in (urls or []) if str(u).strip()]
        self.log = log or (lambda message: None)
        self._fetch = fetcher or fetch
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
            except Exception:  # network, TLS, guards, JSON, anything
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
