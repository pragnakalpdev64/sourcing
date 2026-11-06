"""VendorAdapter: each vendor portal is one adapter (login, auth probe, search)."""
from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from typing import Optional
from urllib.parse import parse_qsl, quote, quote_plus, unquote, urlencode, urlparse, urlsplit, urlunsplit

from ..credentials.base import VendorCredential
from ..extract import part_context
from ..limits import effective_limits
from ..models import LlmUsage, Offer, RequestRow, SearchOutcome

logger = logging.getLogger("sourcing.vendors")

# Where the part number goes in a direct-search address template, e.g.
# "https://host/PartSearchResults.aspx?id={part}" or "https://host/parts/{part}".
PART_PLACEHOLDER = "{part}"
_PLACEHOLDER_TOKEN = "SOURCINGPARTPLACEHOLDER"   # survives urlencode unescaped
_TRACKING_PARAMS = re.compile(r"^(_gl|_ga.*|gclid|fbclid|utm_.*)$", re.I)


def host_allowed(url: str, allowed_hosts) -> bool:
    """True if the URL's host is one of `allowed_hosts` or a subdomain of one."""
    host = (urlparse(url).hostname or "").lower()
    return bool(host) and any(host == h or host.endswith("." + h) for h in allowed_hosts)


def has_real_offer(offers) -> bool:
    """An offer with something a team member can use: a price, stock, lead time or warehouses."""
    return any(o.price or o.qty_available or o.lead_time or o.warehouses for o in offers)


def _same_part(value: str, part_number: str) -> bool:
    return value.strip().upper() == part_number.strip().upper()


def part_url_template(url: str, part_number: str) -> Optional[str]:
    """Learn a direct-search address from a results page's URL: the query value
    (first) or path segment equal to the part number becomes {part}. Analytics
    parameters and the fragment are dropped. None when the URL doesn't hold the
    part number (a postback page, a session id address...)."""
    if not part_number.strip():
        return None
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not _TRACKING_PARAMS.match(k)]
    if any(_same_part(v, part_number) for _, v in query):
        query = [(k, _PLACEHOLDER_TOKEN if _same_part(v, part_number) else v) for k, v in query]
        built = urlunsplit(parts._replace(query=urlencode(query), fragment=""))
        return built.replace(_PLACEHOLDER_TOKEN, PART_PLACEHOLDER)
    segments = parts.path.split("/")
    if any(_same_part(unquote(s), part_number) for s in segments):
        path = "/".join(PART_PLACEHOLDER if _same_part(unquote(s), part_number) else s
                        for s in segments)
        return urlunsplit(parts._replace(path=path, query=urlencode(query), fragment=""))
    return None


def fill_part_url(template: str, part_number: str) -> str:
    """The template with {part} replaced by the part number, escaped for where
    it sits (query value or path segment)."""
    query_at = template.find("?")
    in_query = query_at != -1 and template.find(PART_PLACEHOLDER) > query_at
    value = quote_plus(part_number, safe="") if in_query else quote(part_number, safe="")
    return template.replace(PART_PLACEHOLDER, value)


def check_login_url(url: str, allowed_hosts) -> Optional[str]:
    """Why `url` can't be used as a login start page, or None if it can."""
    parsed = urlparse(url or "")
    if parsed.scheme != "https":
        return f"not an https URL ({url!r})"
    if not parsed.hostname:
        return f"no host in {url!r}"
    if not host_allowed(url, allowed_hosts):
        return (f"host {parsed.hostname} is not one of this vendor's sites "
                f"({', '.join(allowed_hosts)})")
    return None


class SessionExpired(Exception):
    """The portal logged us out mid-run (redirect to login, 401, expiry text)."""


class InvalidCredentials(Exception):
    """The portal rejected the username/password. Never retried automatically."""


class LoginBlocked(Exception):
    """Lockout, CAPTCHA or bot block. Stop and let a person look."""


class LoginFailed(Exception):
    """A login attempt didn't get through for an unclear reason.

    retryable: whether a second attempt is safe. Only when the portal neither
    rejected the credentials nor flagged anything: a timeout, a server error
    page, or a failure before the credentials were sent. A portal may lock the
    account after one failed login (Jamaica Bearings, client 2026-10-21), so a
    submit that just stays on the login form is not retried.

    credentials_sent: whether the portal got the username and password. Only
    then does the failure count towards the vendor's lockout (sourcing/limits.py)."""

    def __init__(self, message: str, *, retryable: bool = True,
                 credentials_sent: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.credentials_sent = credentials_sent


class CaptchaFailed(LoginFailed):
    """The login page shows a CAPTCHA (AAR 2026-11-03: "reCAPTCHA verification
    failed."). For now not a stop: tried again (MAX_LOGIN_ATTEMPTS), and the
    next run tries again too. The password wasn't judged, so it doesn't count
    towards the vendor's lockout."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True, credentials_sent=False)


class RateLimited(Exception):
    """The portal says we sent too many requests. Searching on it stops for the
    vendor's back-off time (limits.backoff_minutes); never pushed through."""


# How portals say they are rate limiting (checked on results pages). Full
# phrases only: results pages hold part descriptions ("flow rate limiter").
RATE_LIMIT_HINTS = ("too many requests", "rate limit exceeded", "request limit exceeded",
                    "you have been temporarily blocked")


def check_rate_limited(page_text: str) -> None:
    """Raise RateLimited when a results page says we sent too many requests."""
    text = (page_text or "").lower()
    hint = next((h for h in RATE_LIMIT_HINTS if h in text), None)
    if hint:
        raise RateLimited(f"Portal says {hint!r}")


class BulkSearchUnavailable(Exception):
    """The bulk search couldn't be used (form missing, portal error message...).
    The pipeline falls back to one-by-one search for the rest of the run."""


class BulkResultsIncomplete(BulkSearchUnavailable):
    """One bulk search's results couldn't all be read (paging trouble). Unlike
    its parent, it doesn't turn bulk search off: the next batch tries again."""


class MfaRequired(Exception):
    """A 2FA step the code can't complete (SMS / email / push / rejected TOTP).

    input_selector: the box the code goes in, when the page has one. With it a
    person can send the code from Google Chat (sourcing/auth/hitl.py) and the
    adapter types it in (submit_mfa_code) on the page that is still open."""

    def __init__(self, method: str, message: str = "",
                 input_selector: Optional[str] = None) -> None:
        super().__init__(message or f"2FA required ({method})")
        self.method = method
        self.input_selector = input_selector


def may_be_signed_in(exc: BaseException) -> bool:
    """After a login attempt raised `exc`: could the portal have signed us in anyway?
    Then we log out before closing the browser, or the vendor session stays open
    until the portal expires it, and many portals allow only a few at once (DASI
    2026-10-28: signed in, but the check couldn't tell, so no logout was done).
    Not after a CAPTCHA / bot check (client rule 5: touch nothing, tell the client),
    a rejected password, or a failure before the password was sent."""
    if isinstance(exc, (LoginBlocked, InvalidCredentials)):
        return False
    if isinstance(exc, LoginFailed):
        return exc.credentials_sent
    return True


class VendorAdapter(ABC):
    vendor_id: str = ""
    display_name: str = ""
    # Sites this vendor's login may run on. Credentials are only ever typed into
    # pages on these hosts (or their subdomains), whatever URL was configured.
    allowed_hosts: tuple[str, ...] = ()
    # Where to start if nothing better is known: ideally the login page itself,
    # but a home page works too (the adapter then follows its Login link).
    default_login_url: str = ""
    # Most parts the portal accepts in one search (1 = no bulk search). The
    # batch actually used is `bulk_batch_size`, which can be lower.
    max_parts_per_search: int = 1
    default_bulk_batch_size: int = 1
    # Fewest parts in a run that switch it to bulk search (when the adapter has
    # one); fewer parts are searched one by one. None = always one by one.
    # REMOVE-IF-PER-PART-DROPPED: the None case (and its handling below and in
    # pipeline.py) only exists for --one-by-one.
    default_bulk_min_parts: Optional[int] = 2
    # Two kinds of waiting, both overridable per vendor (config, in seconds):
    #   action_timeout_seconds     find / fill / click an element
    #   page_load_timeout_seconds  a page load or postback after a submit
    # Portals like Procart reload the whole page on every search, which can take
    # well over the action timeout, so submits wait on the page-load timeout.
    default_action_timeout_seconds: float = 20
    default_page_load_timeout_seconds: float = 90
    # Direct-search address with {part} where the part number goes (see
    # part_url_template). Opening it is one page load instead of open search
    # page + fill + submit. "" = always use the site's search form.
    default_part_url_template: str = ""
    # Currency code for a portal that shows only a symbol such as "$" (the
    # extractor won't read a code from a symbol). "" = only what the page shows.
    default_currency: str = ""
    # Wording a portal uses when it doesn't sell a part (vendor config
    # `not_sold_phrases` overrides). A part with no offers and one of these on
    # its own results becomes NOT_SOLD instead of NOT_FOUND.
    default_not_sold_phrases: tuple[str, ...] = ()
    # This adapter's own login / request limits, on top of limits.DEFAULT_LIMITS
    # and below the vendor record's `limits` (e.g. a fake portal with no gap).
    default_limits: dict = {}
    # False for adapters with no real portal behind them (dry runs): they never
    # mark a vendor's limits as missing in the store.
    real_portal: bool = True
    # Browserbase proxy for this vendor's sessions (vendor
    # config `browser_stealth` wins). Off by default; AAR turns it on for testing.
    default_browser_stealth: bool = False
    # Substitute lines (listing "substitute") written for this vendor (vendor config
    # `show_substitutes` wins). Off for Wencor and Aviall (team, 2026-11-03).
    default_show_substitutes: bool = True
    # Login fields typed one character at a time, after a mouse move and a click,
    # instead of filled at once (vendor config `human_typing` wins). Generic login
    # only. On for AAR (testing 2026-11-04, its invisible reCAPTCHA).
    default_human_typing: bool = False
    # RFQ Delivery (days) for an in-stock line, instead of 0: a vendor that takes a
    # fixed time to ship (vendor config `delivery_days` wins). Incora: 7 (client,
    # 2026-11-06: "they typically take 7 days to ship"). "" = 0 in stock.
    default_delivery_days: str = ""

    def __init__(self, *, extractor=None, selectors: Optional[dict] = None,
                 vendor_config: Optional[dict] = None, max_page_chars: int = 60000) -> None:
        self.extractor = extractor
        self.selectors = dict(selectors or {})
        self.config = dict(vendor_config or {})
        self.max_page_chars = max_page_chars
        self.learned: dict[str, str] = {}  # selectors discovered this run, saved by the pipeline
        self.usage = LlmUsage()            # LLM spend outside extraction (e.g. finding fields)
        self.part_url_failed: Optional[str] = None  # set once the direct address fails this run
        # Page actions the coded way couldn't do, done by the LLM and checked by the
        # adapter (assist.py); its events go to the run summary.
        from .assist import PageAssist
        self.assist = PageAssist(self)
        # A vendor record's name wins over the class's (e.g. a generic adapter's).
        if self.config.get("display_name"):
            self.display_name = self.config["display_name"]

    def confirmed_learned(self) -> dict:
        """What the pipeline saves for later runs. Adapters with their own known
        selectors save all they learned; the generic adapter only what worked."""
        return dict(self.learned)

    @property
    def show_substitutes(self) -> bool:
        return bool(self.config.get("show_substitutes", self.default_show_substitutes))

    @property
    def human_typing(self) -> bool:
        return bool(self.config.get("human_typing", self.default_human_typing))

    @property
    def delivery_days(self) -> str:
        """Days an in-stock line takes to ship ("" = 0)."""
        value = self.config.get("delivery_days", self.default_delivery_days)
        return str(value).strip() if value not in (None, "") else ""

    @property
    def browser_options(self) -> dict:
        """Extra arguments for browser.open(); empty unless the vendor asks for stealth."""
        on = self.config.get("browser_stealth", self.default_browser_stealth)
        return {"stealth": True} if on else {}

    @property
    def limits(self) -> dict:
        """Login / request limits for this vendor (see sourcing/limits.py)."""
        return effective_limits(self.config, self.default_limits)[0]

    @property
    def limits_set(self) -> bool:
        """Whether the vendor record has its own `limits` (False = defaults only)."""
        return effective_limits(self.config, self.default_limits)[1]

    @property
    def part_url(self) -> str:
        """Direct-search address template, "" for none: vendor config
        `part_url_template` (an explicit "" turns direct search off), else the one
        learned on an earlier run, else the adapter's default."""
        if "part_url_template" in self.config:
            return self.config.get("part_url_template") or ""
        return self.selectors.get("part_url_template") or self.default_part_url_template

    def not_sold_reason(self, page_text: str, part_number: str,
                        offers: list[Offer] = ()) -> Optional[str]:
        """The portal's own sentences saying it doesn't sell this part, or None.
        Only for a part without a real offer (one with a price, stock, lead time
        or warehouses), and only from that part's own records on the page."""
        if has_real_offer(offers):
            return None
        phrases = self.not_sold_phrases
        text = part_context(page_text, part_number, strict=True)
        found: list[str] = []
        for phrase in phrases:
            for match in re.finditer(rf"[^.\n:]*{re.escape(phrase)}[^.\n]*", text, re.IGNORECASE):
                sentence = " ".join(match.group().split()).rstrip(".")
                if sentence and sentence not in found:
                    found.append(sentence)
        return ". ".join(found) + "." if found else None

    @property
    def not_sold_phrases(self) -> tuple[str, ...]:
        """How the portal says it doesn't sell a part: vendor config, else the adapter's."""
        return tuple(self.config.get("not_sold_phrases", self.default_not_sold_phrases) or ())

    @property
    def currency(self) -> str:
        """Vendor config `currency` (an explicit "" turns it off), else the adapter's default."""
        if "currency" in self.config:
            return (self.config.get("currency") or "").strip().upper()
        return self.default_currency

    def _seconds(self, key: str, default: float) -> int:
        value = self.config.get(key)
        return int(float(value if value is not None else default) * 1000)

    @property
    def action_timeout_ms(self) -> int:
        return self._seconds("action_timeout_seconds", self.default_action_timeout_seconds)

    @property
    def page_load_timeout_ms(self) -> int:
        return self._seconds("page_load_timeout_seconds", self.default_page_load_timeout_seconds)

    def apply_timeouts(self, page) -> None:
        page.set_default_timeout(self.action_timeout_ms)
        page.set_default_navigation_timeout(self.page_load_timeout_ms)

    @property
    def bulk_min_parts(self) -> Optional[int]:
        """Vendor config `bulk_min_parts` (null/absent = the adapter's default)."""
        value = self.config.get("bulk_min_parts", self.default_bulk_min_parts)
        return None if value in (None, "") else int(value)

    @property
    def bulk_batch_size(self) -> int:
        """Parts per bulk search: vendor config `bulk_batch_size` (0/1 turns bulk off),
        else the adapter default, never above the portal's limit."""
        configured = self.config.get("bulk_batch_size")
        size = int(configured) if configured is not None else self.default_bulk_batch_size
        return max(1, min(size, self.max_parts_per_search))

    async def search_many(self, page, rows: list[RequestRow]
                          ) -> tuple[dict[str, SearchOutcome], LlmUsage]:
        """Search several parts in one go: ({row_id: outcome}, usage for the whole
        batch). Rows missing from the dict were not found. Raises SessionExpired
        when logged out, BulkSearchUnavailable to fall back to one-by-one."""
        raise BulkSearchUnavailable(f"{self.display_name} has no bulk search")

    async def logout(self, page) -> bool:
        """Sign out of the portal at the end of a run. True if signed out."""
        return False

    async def _submit_and_wait(self, page, action, what: str) -> bool:
        """Run a click/press that submits a form and wait for what it loads, with
        this vendor's timeouts (see web.submit_and_wait)."""
        from .web import submit_and_wait

        return await submit_and_wait(page, action, what,
                                     action_timeout_ms=self.action_timeout_ms,
                                     page_load_timeout_ms=self.page_load_timeout_ms)

    @property
    def hosts(self) -> tuple[str, ...]:
        configured = self.config.get("allowed_hosts")
        return tuple(h.lower() for h in configured) if configured else self.allowed_hosts

    def login_url_candidates(self, cred: VendorCredential) -> list[tuple[str, str]]:
        """Login start URLs in priority order, as (source, url), invalid ones dropped.

        1. vendor config `login_url` (set by an admin)
        2. the login page found on an earlier run (saved with the selectors)
        3. the `url` in the vendor's secret
        4. the vendor record's `website` (a home page; the Login link is followed)
        5. the adapter's default
        """
        raw = [
            ("vendor config login_url", self.config.get("login_url")),
            ("login page found on an earlier run", self.selectors.get("login_page_url")),
            (f"secret {cred.secret_key} url", cred.url),
            ("vendor record website", self.config.get("website")),
            ("adapter default", self.default_login_url),
        ]
        out: list[tuple[str, str]] = []
        for source, url in raw:
            if not url or any(url == u for _, u in out):
                continue
            problem = check_login_url(url, self.hosts)
            if problem:
                logger.warning("%s login URL from %s ignored: %s", self.display_name, source,
                               problem)
                continue
            out.append((source, url))
        return out

    @abstractmethod
    async def is_authenticated(self, page) -> bool:
        """Auth probe: open a page that needs login and check we are still signed in."""

    @abstractmethod
    async def login(self, page, cred: VendorCredential) -> None:
        """Log in, including TOTP when the credential has a seed.

        Raises InvalidCredentials, MfaRequired, LoginBlocked or LoginFailed.
        """

    # Whether submit_mfa_code is implemented (a person's code can be typed in).
    accepts_mfa_codes: bool = False

    # Set by login() when it saw the same proof the auth probe looks for (a
    # logout link) on the page it ended on; None = not proven. The auth manager
    # then skips the probe right after login: on DASI it reloaded /my-orders
    # ~35s after the login had already seen Log out (seen in logs 2026-10-29).
    signed_in_proof: Optional[str] = None
    # Set by login() when it ended believing it is signed in (its rules or the LLM
    # read the page so) without the probe's proof. The auth manager then searches
    # once instead of logging in again when the probe can't confirm it: the search
    # shows a sign-in wall, or offers (team, 2026-11-04; Monroe was signed in, the
    # probe said no, and the second login suspended the account).
    login_claimed: bool = False

    async def submit_mfa_code(self, page, cred: VendorCredential, code: str,
                              input_selector: str) -> None:
        """Type a code a person sent into the 2FA page left open by login(), and
        finish the login. Raises like login(); MfaRequired again = code rejected."""
        raise MfaRequired("unsupported", f"{self.display_name} can't take a 2FA code")

    # Whether person_finished_login is implemented (Live View handoff).
    accepts_person_login: bool = False

    async def person_finished_login(self, page) -> bool:
        """While a person logs in through Live View: is the open page signed in
        now? Must not navigate (the person is using the page). Raises
        LoginBlocked on a CAPTCHA / bot check (client rule)."""
        return False

    # Whether person_finished_logout is implemented (logout handoff, hitl.py).
    accepts_person_logout: bool = False

    async def person_finished_logout(self, page) -> bool:
        """While a person logs out through Live View (the program couldn't
        confirm its own logout): is the session signed out now? Must not touch
        the person's page. Raises LoginBlocked on a CAPTCHA / bot check."""
        return False

    @abstractmethod
    async def search(self, page, row: RequestRow) -> SearchOutcome:
        """Search one part and extract offers. Raises SessionExpired when logged out."""
