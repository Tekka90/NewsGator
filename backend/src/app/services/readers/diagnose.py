"""Explain a failed reader login: probe likely endpoints and fingerprint the service."""

from dataclasses import dataclass
from urllib.parse import urlparse

from app.services.readers import greader


@dataclass(frozen=True)
class ServiceProfile:
    name: str
    markers: tuple[str, ...]


KNOWN_SERVICES = (
    ServiceProfile("FreshRSS", ("freshrss",)),
    ServiceProfile("Miniflux", ("miniflux",)),
    ServiceProfile("Tiny Tiny RSS", ("tt-rss", "tiny tiny rss")),
    ServiceProfile("Nextcloud News", ("nextcloud",)),
    ServiceProfile("Inoreader", ("inoreader",)),
    ServiceProfile("BazQux", ("bazqux",)),
    ServiceProfile("The Old Reader", ("theoldreader", "the old reader")),
)

# Where services mount their Google Reader endpoint relative to the server root.
API_PATH_SUFFIXES = ("", "/api/greader.php")
# Endpoint tails users commonly paste by mistake.
PASTED_TAILS = ("/accounts/ClientLogin", "/reader/api/0", "/reader/api", "/reader")


def candidate_bases(entered: str) -> list[str]:
    """Candidate API base URLs derived from what the user typed, most specific first."""
    base = entered.strip().rstrip("/")
    for tail in PASTED_TAILS:
        if base.lower().endswith(tail.lower()):
            base = base[: -len(tail)]
            break
    parsed = urlparse(base)
    if not parsed.scheme or not parsed.netloc:
        return [base]
    origin = f"{parsed.scheme}://{parsed.netloc}"
    candidates = [base]
    for suffix in API_PATH_SUFFIXES:
        candidates += [base + suffix, origin + suffix]
    unique: dict[str, str] = {}
    for candidate in candidates:
        unique.setdefault(candidate.lower(), candidate)
    return list(unique.values())


async def _probe(base: str, username: str, password: str) -> str:
    """Classify an endpoint as 'works', 'refuses' (it exists but denied the login) or 'absent'."""
    try:
        status, content, _ = await greader._http_request(
            "POST",
            f"{base}/accounts/ClientLogin",
            data={"Email": username, "Passwd": password},
            request_timeout=8.0,
        )
    except Exception:  # an unreachable candidate is simply not the endpoint
        return "absent"
    if 200 <= status < 300 and b"Auth=" in content:
        return "works"
    return "refuses" if status in (401, 403) else "absent"


async def detect_service(entered: str) -> str | None:
    parsed = urlparse(entered)
    if not parsed.scheme or not parsed.netloc:
        return None
    try:
        status, content, headers = await greader._http_request(
            "GET", f"{parsed.scheme}://{parsed.netloc}/", request_timeout=8.0
        )
    except Exception:  # fingerprinting is best effort
        return None
    del status
    haystack = (
        content[:65536].decode("utf-8", errors="replace")
        + " ".join(f"{k} {v}" for k, v in headers.items())
    ).lower()
    for profile in KNOWN_SERVICES:
        if any(marker in haystack for marker in profile.markers):
            return profile.name
    return None


async def diagnose_login(api_base_url: str, username: str, password: str, error: str) -> str:
    entered = api_base_url.strip().rstrip("/")
    message = (
        "No Google Reader API answered at this address. Make sure the service's Google Reader "
        "or third-party API is enabled. The address usually ends with /api/greader.php "
        "(FreshRSS) or is the server's root address (Miniflux, Inoreader, BazQux). "
        f"({error})"
    )
    for candidate in candidate_bases(api_base_url):
        outcome = await _probe(candidate, username, password)
        if outcome == "works":
            message = f"This address doesn't expose a reader API, but {candidate} does. Use it."
            break
        if outcome == "refuses":
            if candidate.lower() == entered.lower():
                message = (
                    "A reader API answered at this address but refused the login. Check the "
                    "username and the API password: some services use a separate API password "
                    "instead of your account password."
                )
            else:
                message = (
                    f"A reader API answers at {candidate} but refused the login. Use that "
                    "address and check the username and API password."
                )
            break
    service = await detect_service(entered)
    return f"{message} Detected service: {service}." if service else message
