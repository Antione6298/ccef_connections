"""
Chariot connector for CCEF connections library.

Read access to Chariot donations, deposits, payment sources, properties and
organizations, using direct HTTP via the requests library — no Chariot SDK, so
this connector needs only the base install.

Chariot is a DAF/workplace-giving aggregator: donors give through a fund sponsor
(Daffy, Benevity, Fidelity Charitable…), Chariot collects the money and then
transfers it to the recipient's bank in **deposits**. A deposit is the unit that
reaches the bank; a donation is the unit that reaches the CRM.

⚠ **Amounts are in CENTS**, on both donations and deposits. `amount_gross` of
18000 is $180.00. Use :func:`dollars`.

⚠ **Two access paths existed and only this one should be used.** There is also a
claude.ai MCP connector for Chariot; its OAuth token expires every few days and
it is unreachable from a script. The REST key this connector uses carries over
between sessions and is the documented default (CCEF, 8 September 2026).

⚠ **The base path is `/v1/`.** The published Chariot docs advertise `/api/...`
paths; those 404 against this key. Verified 2 and 8 September 2026.

## What the API does NOT carry

⚠ **There is no "effective date" / "Created On" on a donation.** The only dates
are ``created_at`` (when Chariot created the record — which is the day the money
was *received*, not the day the grant was issued), ``settlement.received_at``,
``settlement.settled_at`` and ``updated_at``. The Chariot **UI CSV export** has a
``Created On`` column carrying the grant's own date, and it can differ by weeks:
donation ``donation_01m092nx…`` is ``Created On`` 2026-08-18 against ``created_at``
2026-08-31. Searched exhaustively on 8 September 2026 — every field of the
donation, ``/v1/grants`` (empty), and all six custom properties (all workflow
enums, no date). **If you need the grant date, you still need the CSV export.**

## Pagination

Cursor-based on ``next_page_token``. ⚠ **The results key is not uniform**:
donations, deposits, payment sources, properties and grants return ``results``;
organizations returns ``items``. :meth:`_paginate` takes the key as an argument
for that reason — assuming ``results`` everywhere silently yields nothing from
``/v1/organizations``.
"""

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Union

import requests

from ..core.base import BaseConnection
from ..core.retry import retry_chariot_operation
from ..exceptions import AuthenticationError, ConnectionError, RateLimitError

logger = logging.getLogger(__name__)

CHARIOT_API_BASE = "https://api.givechariot.com/v1"
CHARIOT_SANDBOX_BASE = "https://sandboxapi.givechariot.com/v1"

# Chariot's own page cap. Larger values are accepted and silently clamped.
CHARIOT_MAX_LIMIT = 100

TimeLike = Union[date, datetime, str]


def dollars(cents: Optional[int]) -> float:
    """Chariot money as dollars. ⚠ Every amount on the API is in cents."""
    return round((cents or 0) / 100.0, 2)


def _as_dt(value: Optional[str]) -> Optional[datetime]:
    """An ISO-8601 Chariot timestamp as an aware datetime, or None.

    Chariot mixes precision — ``2026-08-31T12:31:35Z`` on a settlement against
    ``2026-09-08T13:10:48.834223Z`` on a deposit — so this normalises both rather
    than letting a caller pick one format and trip over the other.
    """
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _as_day(value: TimeLike) -> str:
    """A date bound as ``YYYY-MM-DD``."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


# Platforms that batch a disbursement days or weeks after the grant is effective.
# ⚠ Workplace-giving intermediaries, not DAF sponsors — see `effective_date`.
DELAYED_PLATFORMS = frozenset({"benevity", "yourcause", "cybergrants", "america's charities"})


def effective_date(donation: Dict[str, Any]) -> Dict[str, Any]:
    """The date a Chariot gift is effective, and whether it can be trusted.

    Returns ``{"date": "YYYY-MM-DD" | None, "certain": bool, "platform": str,
    "why": str}``.

    ⚠ **`created_at` is the effective date for DAF platforms and NOT for workplace
    giving.** Measured 9 September 2026 against the 12 donations whose true
    ``Created On`` is known from a Chariot UI export:

        Daffy / Groundswell / TIFIN   11 of 11 match created_at exactly
        Benevity                       1 of 1  DIFFERS - effective 2026-08-18,
                                               created_at 2026-08-31

    A DAF sponsor sends the grant as it issues it, so Chariot creates the record the
    same day. A workplace-giving intermediary accumulates matches and disburses in a
    batch, so Chariot first sees the gift weeks after it was effective.

    ⚠ **For a delayed platform the true date is NOT recoverable from this API.** It
    sits in the vendor's own disbursement report, attached to the donation as an
    artifact — ``disbursement-report-221967.csv`` on the August row. ``/v1/files/{id}``
    returns that file's *metadata*, but there is **no download route**: every one of
    ``/download``, ``/content``, ``/v1/artifacts/{id}`` returns 404. So the content
    cannot be read programmatically, and the only source is a UI export.

    ⚠ **Do not average this risk away.** Workplace giving was **90% of August 2026's
    Chariot money** ($214.39 of $239.39) on one row out of two. Being wrong on the
    minority of *rows* can still be wrong on the majority of *dollars*.

    A caller that needs the batch date should use ``date`` when ``certain`` is True,
    and treat ``certain=False`` as "this needs the export or a human".
    """
    plat = ((donation.get("platform") or {}).get("name") or "").strip()
    created = (donation.get("created_at") or "")[:10] or None
    if plat.lower() in DELAYED_PLATFORMS:
        return {"date": created, "certain": False, "platform": plat,
                "why": (f"{plat} is a workplace-giving platform: it batches "
                        f"disbursements, so created_at is when Chariot received the "
                        f"gift, not when it was effective. The real date is only in "
                        f"the attached vendor report, which the API cannot download.")}
    return {"date": created, "certain": True, "platform": plat,
            "why": (f"{plat or 'DAF sponsor'} sends each grant as it is issued, so "
                    f"created_at is the effective date (verified 11/11)."
                    if plat else "created_at, no platform named on the donation")}


class ChariotConnector(BaseConnection):
    """
    Chariot connector for reconciliation-grade read access.

    **Credential**::

        CHARIOT_API_KEY_PASSWORD={"key":"sk_live_..."}

    Single-field JSON — no ``api_name``, because one credential is one Chariot
    membership. CCEF's key carries exactly one: Common Cause Education Fund
    (EIN 31-1705370), so this is **C3 only**; there is no C4 org on it.

    Examples:
        >>> chariot = ChariotConnector()
        >>> chariot.connect()
        >>> deposits = chariot.list_deposits(start="2026-08-01", end="2026-08-31")
        >>> for d in deposits:
        ...     print(d["settled_at"][:10], dollars(d["transfer"]["amount"]))
        >>> donations = chariot.list_donations(deposit_id=deposits[0]["id"])
    """

    def __init__(self, sandbox: bool = False) -> None:
        """
        Initialize the Chariot connector.

        Args:
            sandbox: Use the sandbox host instead of live. The credential is
                environment-specific — a live key does not authenticate against
                sandbox — so this is not a dry-run switch for production data.
        """
        super().__init__()
        self._key: Optional[str] = None
        self._base = CHARIOT_SANDBOX_BASE if sandbox else CHARIOT_API_BASE

    # ── Connection lifecycle ──────────────────────────────────────────

    def connect(self) -> None:
        """
        Load the Chariot key and verify it against the API.

        Verified with a one-row ``/v1/deposits`` read rather than a bare reachability
        check: an unauthenticated request to this host still returns a routed
        response, so only a real resource read proves the key works.

        Raises:
            AuthenticationError: If the key is missing or rejected
            ConnectionError: If the API is unreachable
        """
        self._key = self._credential_manager.get_chariot_key()
        self._request("GET", "/deposits", params={"limit": 1})
        self._is_connected = True
        logger.info("Connected to Chariot (%s)", self._base)

    def disconnect(self) -> None:
        """Release the key. There is no session to close — the API is stateless."""
        self._key = None
        self._is_connected = False

    def health_check(self) -> bool:
        """True when a one-row read succeeds."""
        try:
            self._request("GET", "/deposits", params={"limit": 1})
            return True
        except Exception as exc:  # noqa: BLE001 - health checks report, never raise
            logger.warning("Chariot health check failed: %s", exc)
            return False

    # ── Plumbing ──────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        if not self._key:
            raise AuthenticationError(
                "Chariot connector is not connected — call connect() first."
            )
        return {"Authorization": f"Bearer {self._key}",
                "Accept": "application/json"}

    def _request(self, method: str, path: str,
                 params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Make one Chariot API request.

        Raises:
            AuthenticationError: 401 or 403
            RateLimitError: 429
            ConnectionError: any other error status, or an unreachable API
        """
        url = f"{self._base}{path}"
        try:
            resp = requests.request(method, url, headers=self._headers(),
                                    params=params, timeout=60)
        except requests.RequestException as exc:
            raise ConnectionError(f"Chariot API request failed: {exc}") from exc

        if resp.status_code in (401, 403):
            err = AuthenticationError(
                f"Chariot rejected the key on {method} {path} "
                f"({resp.status_code}). Detail: {resp.text[:300]}"
            )
            err.status_code = resp.status_code
            raise err
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 5))
            raise RateLimitError(
                f"Chariot rate limit exceeded, retry after {retry_after}s",
                retry_after=retry_after,
            )
        if resp.status_code == 404:
            # ⚠ Worth its own message: the published docs advertise `/api/...`
            # paths that do not exist on this key, and a bare "404" sends people
            # looking at their credential instead of their path.
            raise ConnectionError(
                f"Chariot has no route for {method} {path} (404). The live prefix "
                f"is /v1/ — the published /api/... paths do not resolve. "
                f"Detail: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise ConnectionError(
                f"Chariot API error {resp.status_code} on {method} {path}: "
                f"{resp.text[:500]}"
            )
        return resp.json()

    @retry_chariot_operation
    def _page(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """One page of a list endpoint, retried on rate limit."""
        return self._request("GET", path, params=params)

    def _paginate(self, path: str, params: Optional[Dict[str, Any]] = None,
                  limit: Optional[int] = None,
                  results_key: str = "results") -> List[Dict[str, Any]]:
        """
        Walk a Chariot list endpoint to completion.

        ⚠ ``results_key`` is a parameter because Chariot is not consistent:
        ``/v1/organizations`` returns ``items`` where everything else returns
        ``results``. Hardcoding ``results`` makes that endpoint look empty.

        ⚠ ``limit`` is a **total cap on records**, not a page size — the same
        convention the other connectors here use.
        """
        out: List[Dict[str, Any]] = []
        page = dict(params or {})
        page["limit"] = min(CHARIOT_MAX_LIMIT, limit or CHARIOT_MAX_LIMIT)
        seen_tokens: set = set()
        while True:
            body = self._page(path, dict(page))
            batch = body.get(results_key) or []
            out.extend(batch)
            if limit is not None and len(out) >= limit:
                return out[:limit]
            token = body.get("next_page_token")
            # A server that echoes the same cursor would loop forever; stop instead.
            if not token or not batch or token in seen_tokens:
                return out
            seen_tokens.add(token)
            page["page_token"] = token

    # ── Reconciliation reads ──────────────────────────────────────────

    def list_deposits(self, start: Optional[TimeLike] = None,
                      end: Optional[TimeLike] = None,
                      limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Deposits — **money Chariot actually moved to the bank.**

        This is the closest thing Chariot has to a bank statement, and it is what a
        reconciliation against the receiving account compares to. Each carries
        ``id``, ``status``, ``bank_created_at``, ``settled_at`` and a ``transfer``
        block with ``amount`` (**cents**), ``currency``, ``type`` and ``description``.

        ⚠ **Filtered client-side on ``settled_at``.** Chariot's deposits endpoint
        exposes no date parameters, so a window is applied after fetching. That is
        honest but not cheap: it walks every deposit each time. The volume is tiny
        (12 in the account's whole history to September 2026), so this is fine —
        revisit if that changes by orders of magnitude.

        Args:
            start: Earliest ``settled_at`` (inclusive), date or ``YYYY-MM-DD``
            end: Latest ``settled_at`` (inclusive)
            limit: Stop after roughly this many records

        Returns:
            Deposit objects, oldest settlement first
        """
        rows = self._paginate("/deposits", limit=limit)
        lo = _as_day(start) if start is not None else None
        hi = _as_day(end) if end is not None else None

        def keep(d: Dict[str, Any]) -> bool:
            day = (d.get("settled_at") or "")[:10]
            if not day:
                return lo is None and hi is None
            return (lo is None or day >= lo) and (hi is None or day <= hi)

        return sorted((d for d in rows if keep(d)),
                      key=lambda d: d.get("settled_at") or "")

    def list_donations(self, start: Optional[TimeLike] = None,
                       end: Optional[TimeLike] = None,
                       deposit_id: Optional[str] = None,
                       limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Donations — the gift-level records behind the deposits.

        Each carries ``amount_gross`` / ``amount_fee`` / ``amount_net`` (**cents**),
        ``attribution.primary_donor`` (name, email, address), the source platform,
        and a ``settlement`` block naming the ``deposit_id`` that paid it out.

        ⚠ **``created_at`` is when Chariot created the record, not when the grant was
        issued.** For ``donation_01m092nx…`` those are 31 August and 18 August. The
        grant's own date is not on the API at all — see the module docstring.

        ⚠ **A donation with no ``settlement`` has not been paid out yet.** That is a
        real state, not missing data: the money is with Chariot and has not reached
        the bank.

        Args:
            start: Earliest settlement date (inclusive); unsettled rows are excluded
                whenever a window is given
            end: Latest settlement date (inclusive)
            deposit_id: Only donations settled by this deposit
            limit: Stop after roughly this many records

        Returns:
            Donation objects
        """
        rows = self._paginate("/donations", limit=limit)
        lo = _as_day(start) if start is not None else None
        hi = _as_day(end) if end is not None else None

        def keep(d: Dict[str, Any]) -> bool:
            settle = d.get("settlement") or {}
            if deposit_id and settle.get("deposit_id") != deposit_id:
                return False
            if lo is None and hi is None:
                return True
            day = (settle.get("settled_at") or "")[:10]
            if not day:
                return False
            return (lo is None or day >= lo) and (hi is None or day <= hi)

        return [d for d in rows if keep(d)]

    def get_donation(self, donation_id: str) -> Dict[str, Any]:
        """One donation by id."""
        return self._request("GET", f"/donations/{donation_id}")

    def get_deposit(self, deposit_id: str) -> Dict[str, Any]:
        """One deposit by id."""
        return self._request("GET", f"/deposits/{deposit_id}")

    def donations_by_deposit(self, start: Optional[TimeLike] = None,
                             end: Optional[TimeLike] = None
                             ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Donations grouped by the deposit that settled them, for a settlement window.

        The shape a reconciliation wants: one bank line, and the gifts inside it.
        Donations not yet settled are grouped under ``""``, so money still held at
        Chariot is visible rather than dropped.
        """
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for d in self.list_donations(start=start, end=end):
            key = (d.get("settlement") or {}).get("deposit_id") or ""
            grouped.setdefault(key, []).append(d)
        return grouped

    def deposit_ties_out(self, deposit: Dict[str, Any],
                         donations: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Whether a deposit's transfer equals the net of the donations inside it.

        Chariot's own invariant and the cheapest possible self-check: if these
        disagree, either a donation is missing from the pull or the deposit carries
        something the gift rows do not explain. Returns the two figures and their
        difference in **dollars** rather than a bare bool, because the size of a
        break is what tells you which of those it is.
        """
        transfer = dollars((deposit.get("transfer") or {}).get("amount"))
        net = round(sum(dollars(d.get("amount_net")) for d in donations), 2)
        return {"deposit_id": deposit.get("id"), "transfer": transfer,
                "donations_net": net, "difference": round(net - transfer, 2),
                "ties": abs(net - transfer) < 0.005}

    # ── Reference data ────────────────────────────────────────────────

    def find_organizations(self, ein: Optional[str] = None,
                           limit: int = 25) -> List[Dict[str, Any]]:
        """
        Look a charity up in Chariot's organization directory.

        ⚠⚠ **This is the WHOLE nonprofit registry, not "my organizations".** It is a
        lookup table of every charity Chariot knows — the first page comes back
        "Daughters of Zion INC" — so it says nothing about what this credential owns.
        Do not use it to discover scope; use the donations that actually arrive.

        ⚠ **Never page it unbounded.** An earlier version called ``_paginate`` with no
        filter and walked the registry until a page returned **HTTP 500**. Always pass
        an ``ein``, and note that ``limit`` here is a hard cap, not a cursor.

        ⚠ Returns rows under ``items``, not ``results`` — the one endpoint that
        differs.

        Args:
            ein: Employer identification number, digits only (``"311705370"``).
                CCEF resolves to ``Common Cause Education Fund``.
            limit: Maximum rows to return

        Returns:
            Organization objects: ``id``, ``ein``, ``name``, ``classification``
        """
        params: Dict[str, Any] = {"limit": min(limit, CHARIOT_MAX_LIMIT)}
        if ein:
            params["ein"] = str(ein).replace("-", "").strip()
        body = self._page("/organizations", params)
        return (body.get("items") or [])[:limit]

    def list_payment_sources(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """The payment sources donations arrive through."""
        return self._paginate("/payment_sources", limit=limit)

    def list_properties(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        The custom properties defined on donations.

        All six on CCEF's account are workflow enums — Synced to Finance, CRM status,
        Review Status, Restriction Status, Assignee, Internal note. **None is a date**,
        which is why the grant date cannot be recovered from here either.
        """
        return self._paginate("/properties", limit=limit)
