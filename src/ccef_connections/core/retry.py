"""
Retry logic with exponential backoff for CCEF connections.

This module provides decorators and utilities for retrying failed operations
with intelligent backoff strategies tailored to different API rate limits.
"""

import logging
from typing import Any, Callable, Dict, Optional, Type, Tuple

from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception,
    retry_if_exception_type,
    before_sleep_log,
)

# NOTE: this ConnectionError is ours (ccef_connections.exceptions), and it
# shadows the builtin for the rest of this module. It is NOT related to
# requests.exceptions.ConnectionError or the builtin socket-level one — neither
# of those is a subclass, so naming it in a retry predicate matches only
# exceptions our own connectors raised deliberately.
from ..exceptions import RateLimitError, ConnectionError

logger = logging.getLogger(__name__)


# ── What is safe to retry ────────────────────────────────────────────────────
#
# Every decorator below retries rate limiting (HTTP 429), and — for gspread
# alone — transient 5xx. Nothing else. That is deliberate and applies across
# all services:
#
#   * A 429 means the request was *rejected* — nothing was applied server-side,
#     so replaying it cannot duplicate an effect. Safe to retry.
#   * A 4xx (auth, not-found, validation) will never succeed on attempt 2.
#     Retrying only delays the error message that says what to fix.
#   * A 5xx leaves the request in an *unknown* state. Several methods here are
#     not idempotent — AirtableConnector.batch_upsert,
#     ActionNetworkConnector.create_person — so replaying a 5xx risks a
#     duplicate write. Surface it and let the caller decide.
#
#     ONE BOUNDED EXEMPTION (added 0.13.0): transient 5xx from **gspread**
#     specifically is retried. See _GSPREAD_TRANSIENT_STATUSES for the full
#     argument. In short: gspread is the only vendor library here with no
#     retry of its own, a Sheets 503 is a routine multi-times-a-day blip
#     rather than a real fault, and every method this decorator guards on the
#     gspread side is replay-safe — the creates are all lookup-then-create and
#     the writes all converge. The exemption is deliberately NOT extended to
#     the api_core path, because BigQuery's `query`/`execute_dml`/`insert_rows`
#     sit under the same decorator and carry MERGE/INSERT that must never be
#     blindly replayed (and google-cloud-bigquery already retries transient
#     errors itself). Do not widen this to a general "retry 5xx" rule.
#   * An exception from our own code (KeyError, TypeError, AttributeError) is a
#     bug. Retrying it turns an instant traceback into a 30-second wait that
#     reads like a network problem.
#
# Do not add bare ``Exception`` to a retry predicate. It silently swallows all
# four cases above, and because ``reraise=True`` surfaces the right error in the
# end, the only visible symptom is unexplained slowness.


_GOOGLE_VENDOR_TYPES: Optional[Dict[str, Any]] = None


def _google_vendor_types() -> Dict[str, Any]:
    """
    Lazily resolve the vendor exception types the Google retry predicate needs.

    This module is imported by the base install, where neither the ``sheets``
    extra (gspread) nor the ``bigquery`` extra (google-cloud-bigquery) is
    guaranteed to be present, so these types cannot be imported at module
    scope. Resolved once and cached; a missing extra simply contributes nothing.

    Returns:
        Dict with ``api_core`` (tuple of exception types) and ``gspread``
        (the ``APIError`` class, or None if gspread is not installed).
    """
    global _GOOGLE_VENDOR_TYPES
    if _GOOGLE_VENDOR_TYPES is None:
        resolved: Dict[str, Any] = {"api_core": (), "gspread": None}
        try:
            from google.api_core.exceptions import TooManyRequests

            resolved["api_core"] = (TooManyRequests,)
        except ImportError:
            pass
        try:
            from gspread.exceptions import APIError

            resolved["gspread"] = APIError
        except ImportError:
            pass
        _GOOGLE_VENDOR_TYPES = resolved
    return _GOOGLE_VENDOR_TYPES


# Transient Sheets/Drive server errors, retried for gspread ONLY.
#
# WHY THIS EXISTS: ep-syncs' volunteer-sheets job failed on 5 of 8 consecutive
# nightly runs (2026-08-28..09-03) with
# ``APIError: [503]: The service is currently unavailable.`` — never a 429. It
# makes ~700 Sheets/Drive calls per run across 173 spreadsheets, so even a
# fraction-of-a-percent 503 rate reliably kills one or two targets a night and
# exits the job non-zero. Re-running the failed targets minutes later succeeds
# every time. Google does not promise otherwise: a 503 on Sheets means "try
# again", not "something is wrong with your request".
#
# WHY IT IS SAFE HERE, method by method — everything this decorator guards on
# the gspread side either reads, or converges on replay:
#   * get_or_create_spreadsheet / get_or_add_worksheet — lookup-then-create,
#     and the decorator wraps the WHOLE method, so a replay redoes the lookup
#     and adopts the object a lost response may have already created.
#   * write_worksheet — clear-then-write of the full contents. Converges.
#   * update_cell — sets one cell to one value. Last write wins.
#   * delete_worksheet_if_exists — conditional; a replay is a no-op.
#   * format_header_row / move_to_folder — declarative, idempotent.
#   * open_spreadsheet / get_range / get_all_values / get_*_as_dicts — reads.
#
# 501 (Not Implemented) is deliberately absent: it is a permanent answer.
_GSPREAD_TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})


def _is_google_retryable(exc: BaseException) -> bool:
    """
    True for a Google API failure that is safe to replay.

    Unlike the ``requests``-based connectors, the Google connectors do not
    translate HTTP status codes into our exception hierarchy — gspread and
    google-cloud-bigquery raise their own types straight through. So matching
    on ``RateLimitError`` alone would never fire, and gspread would lose 429
    handling entirely (it is the one vendor library here with no internal
    retry of its own; google-cloud-bigquery and pyairtable both retry 429
    themselves).

    gspread signals status via ``APIError.response.status_code`` rather than a
    typed subclass, so it needs a value check rather than an isinstance check.

    Matches:
      * our own ``RateLimitError``
      * api_core ``TooManyRequests`` (429) — and nothing else from api_core,
        because BigQuery DML sits under this same decorator
      * gspread ``APIError`` carrying 429, or a transient 5xx per
        ``_GSPREAD_TRANSIENT_STATUSES`` (see the note above that block for the
        per-method replay-safety argument)

    Args:
        exc: The exception raised by the decorated call

    Returns:
        True if this failure is safe to replay
    """
    if isinstance(exc, RateLimitError):
        return True

    vendor = _google_vendor_types()

    api_core_types = vendor["api_core"]
    if api_core_types and isinstance(exc, api_core_types):
        return True

    gspread_api_error = vendor["gspread"]
    if gspread_api_error is not None and isinstance(exc, gspread_api_error):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        return status == 429 or status in _GSPREAD_TRANSIENT_STATUSES

    return False


def retry_with_backoff(
    max_attempts: int = 3,
    min_wait: float = 1.0,
    max_wait: float = 60.0,
    multiplier: float = 2.0,
    exceptions: Tuple[Type[Exception], ...] = (ConnectionError, RateLimitError),
) -> Callable:
    """
    Decorator for retrying operations with exponential backoff.

    Args:
        max_attempts: Maximum number of retry attempts
        min_wait: Minimum wait time in seconds
        max_wait: Maximum wait time in seconds
        multiplier: Exponential backoff multiplier
        exceptions: Tuple of exception types to retry on

    Returns:
        Decorated function with retry logic

    Examples:
        >>> @retry_with_backoff(max_attempts=5, min_wait=0.5)
        ... def fetch_data():
        ...     # Some operation that might fail
        ...     pass
    """
    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=multiplier, min=min_wait, max=max_wait),
        retry=retry_if_exception_type(exceptions),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )


def retry_airtable_operation(func: Callable) -> Callable:
    """
    Decorator for Airtable operations with rate limit handling.

    Airtable has a rate limit of 5 requests per second per base.
    This decorator implements exponential backoff with appropriate timing.

    Only retries on RateLimitError. Note that pyairtable already retries 429
    internally (``Api(retry_strategy=True)`` is the default: urllib3 Retry with
    ``status_forcelist=(429,)``, 5 attempts), so in practice a rate limit is
    absorbed a layer below this one and this decorator is a thin outer net
    rather than the primary defence. It deliberately does NOT retry 5xx:
    ``batch_upsert`` is not idempotent, so replaying an unknown-state write
    risks duplicate records.

    Args:
        func: The function to decorate

    Returns:
        Decorated function with Airtable-specific retry logic

    Examples:
        >>> @retry_airtable_operation
        ... def update_records(table, records):
        ...     return table.batch_update(records)
    """
    return retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1.5, min=0.2, max=10.0),
        retry=retry_if_exception_type(RateLimitError),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )(func)


def retry_openai_operation(func: Callable) -> Callable:
    """
    Decorator for OpenAI API operations with rate limit handling.

    Handles 429 (rate limit) errors with exponential backoff.

    Only retries on RateLimitError. The connector goes through langchain onto
    the ``openai`` SDK, which already retries 429, 5xx, and connection errors
    internally (``max_retries=2`` by default), so transient failures are
    absorbed a layer below this one. Model/prompt/quota errors surface
    immediately rather than costing five attempts of backoff — and a completion
    is billed per attempt, which is another reason not to replay blindly.

    Args:
        func: The function to decorate

    Returns:
        Decorated function with OpenAI-specific retry logic

    Examples:
        >>> @retry_openai_operation
        ... def call_openai(prompt):
        ...     return llm.invoke(prompt)
    """
    return retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2.0, min=1.0, max=60.0),
        retry=retry_if_exception_type(RateLimitError),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )(func)


def retry_google_operation(func: Callable) -> Callable:
    """
    Decorator for Google API operations (Sheets, BigQuery) with retry logic.

    Google APIs have various rate limits depending on the service.
    This implements a conservative retry strategy.

    Shared by SheetsConnector, SheetsWriterConnector, and BigQueryConnector.
    Retries via :func:`_is_google_retryable` — a predicate rather than a type
    tuple because gspread reports status on ``APIError.response.status_code``
    instead of raising a typed subclass, and because the vendor types live
    behind optional extras.

    Retries 429 from every source, **plus transient 5xx from gspread only**
    (0.13.0; see ``_GSPREAD_TRANSIENT_STATUSES``). gspread has no retry of its
    own, so this decorator is the only retry the Sheets connectors get, and a
    Sheets 503 is a routine blip — it was failing ep-syncs' volunteer-sheets
    job on most nights. google-cloud-bigquery already retries transient errors
    internally via ``DEFAULT_RETRY``, and its DML methods sit under this
    decorator, so the 5xx exemption stops at the gspread boundary.

    Args:
        func: The function to decorate

    Returns:
        Decorated function with Google API retry logic

    Examples:
        >>> @retry_google_operation
        ... def query_bigquery(sql):
        ...     return client.query(sql)
    """
    return retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2.0, min=1.0, max=60.0),
        retry=retry_if_exception(_is_google_retryable),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )(func)


def retry_helpscout_operation(func: Callable) -> Callable:
    """
    Decorator for HelpScout API operations with retry logic.

    HelpScout API has rate limits; this implements exponential backoff
    with 5 attempts, matching the pattern used by OpenAI/Google decorators.

    Only retries on RateLimitError. The connector translates HTTP status into
    our exception hierarchy, so ConnectionError here wraps a 4xx/5xx response
    and should fail immediately — the caller sees the real error instead of
    waiting through five attempts.

    Args:
        func: The function to decorate

    Returns:
        Decorated function with HelpScout-specific retry logic

    Examples:
        >>> @retry_helpscout_operation
        ... def list_conversations(mailbox_id):
        ...     return client.get(f"/mailboxes/{mailbox_id}/conversations")
    """
    return retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2.0, min=1.0, max=60.0),
        retry=retry_if_exception_type(RateLimitError),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )(func)


def retry_zoom_operation(func: Callable) -> Callable:
    """
    Decorator for Zoom API operations with retry logic.

    Zoom API has rate limits; this implements exponential backoff
    with 5 attempts, matching the pattern used by other API decorators.

    Only retries on RateLimitError. The connector translates HTTP status into
    our exception hierarchy, so ConnectionError here wraps a 4xx/5xx response
    and should fail immediately — the caller sees the real error instead of
    waiting through five attempts.

    Args:
        func: The function to decorate

    Returns:
        Decorated function with Zoom-specific retry logic

    Examples:
        >>> @retry_zoom_operation
        ... def list_meetings(user_id):
        ...     return client.get(f"/users/{user_id}/meetings")
    """
    return retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2.0, min=1.0, max=60.0),
        retry=retry_if_exception_type(RateLimitError),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )(func)


def retry_action_network_operation(func: Callable) -> Callable:
    """
    Decorator for Action Network API operations with retry logic.

    Action Network has a rate limit of 4 requests per second.
    This implements exponential backoff with 5 attempts.

    Only retries on RateLimitError. The connector translates HTTP status into
    our exception hierarchy, so ConnectionError here wraps a 4xx/5xx response
    and should fail immediately. That matters more here than elsewhere: many of
    the decorated methods write (``create_person``, ``update_person``,
    ``unsubscribe_person``), and replaying an unknown-state write against a
    people database risks duplicate records.

    Args:
        func: The function to decorate

    Returns:
        Decorated function with Action Network-specific retry logic

    Examples:
        >>> @retry_action_network_operation
        ... def list_people():
        ...     return client.get("/people")
    """
    return retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2.0, min=1.0, max=60.0),
        retry=retry_if_exception_type(RateLimitError),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )(func)


def retry_ptv_operation(func: Callable) -> Callable:
    """
    Decorator for Protect the Vote (PTV) API operations with retry logic.

    Only retries on RateLimitError, matching every other decorator here.

    This one used to retry ConnectionError as well, which sounds like "retry
    transient network failures" but wasn't: the connector wraps BOTH a genuine
    ``requests`` transport failure and any 4xx/5xx response in the same
    ConnectionError, so a PTV 404 or a bad API key cost five attempts and ~30s
    of exponential backoff before surfacing. In a scheduled Civis job that
    turned a hard configuration error into what looked like a transient blip.
    A missing PTV_API_KEY_PASSWORD was the worst case — connect() wraps
    CredentialError into ConnectionError, so it too was retried five times.

    Losing retry on real transport blips is the accepted trade: the connector
    cannot distinguish them from a 4xx after wrapping, and the multi-state
    helpers (``get_all_users`` and friends) are the ones that would suffer, so
    a caller pulling 50 states can re-run rather than have every hard failure
    pay 30s first.

    Args:
        func: The function to decorate

    Returns:
        Decorated function with PTV-specific retry logic

    Examples:
        >>> @retry_ptv_operation
        ... def get_shift_volunteers(state_code):
        ...     return client.fetch_csv(state_code)
    """
    return retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2.0, min=1.0, max=60.0),
        retry=retry_if_exception_type(RateLimitError),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )(func)


def retry_roi_crm_operation(func: Callable) -> Callable:
    """
    Decorator for ROI CRM API operations with retry logic.

    ROI CRM is documented as allowing 500 requests per 5-minute rolling window
    (429 on breach). Measured 2026-09-01: 614 GETs in a 300s rolling window
    — 23% over that cap — drew no 429 and no throttling, so the documented
    limit is not enforced as written at this rate. Sequential throughput is
    bounded by latency (~470 ms/call, ~2.1 req/s), not by the cap.

    Concurrency measured the same day, 860 GETs, zero 429s: 4 workers
    9.0 req/s, 8 workers 16.7, 16 workers 29.3, 32 workers 36.0. The knee is
    at **16** — past it, median latency doubles (492 → 784 ms) for only 1.2x
    more throughput. A pooled Session is worth a further 1.12x (461 → 411 ms)
    since this module calls requests.request() per call and pays a fresh TLS
    handshake each time. All of this is measured on READS; concurrent write
    behavior is not yet characterised.
    Only retries on RateLimitError — the only genuinely transient condition.
    ConnectionError wraps HTTP 4xx/5xx responses and should fail immediately
    so the caller sees the real error without waiting through exponential backoff.

    Args:
        func: The function to decorate

    Returns:
        Decorated function with ROI CRM-specific retry logic

    Examples:
        >>> @retry_roi_crm_operation
        ... def get_donor(donor_id):
        ...     return client.get(f"/donors/{donor_id}/")
    """
    return retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2.0, min=1.0, max=60.0),
        retry=retry_if_exception_type(RateLimitError),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )(func)


def _wait_for_stripe_rate_limit(retry_state) -> float:
    """Wait as long as Stripe asks (Retry-After), plus a 1s buffer."""
    exc = retry_state.outcome.exception()
    if isinstance(exc, RateLimitError) and exc.retry_after:
        return float(exc.retry_after) + 1.0
    return 2.0


def retry_stripe_operation(func: Callable) -> Callable:
    """
    Decorator for Stripe API operations with retry logic.

    Stripe allows roughly 100 read requests/second in live mode and returns 429
    on breach, usually with a Retry-After header. Only RateLimitError is retried —
    it is the sole transient condition. AuthenticationError (401, and 403 for a
    missing restricted-key scope) and ConnectionError must fail immediately: a
    missing key permission is fixed in the Stripe dashboard, and retrying it just
    delays the message that says so.

    Waits as long as Stripe requests rather than using pure exponential backoff,
    so a long rate-limit window does not exhaust every attempt first.

    Args:
        func: The function to decorate

    Returns:
        Decorated function with Stripe-specific retry logic

    Examples:
        >>> @retry_stripe_operation
        ... def list_charges(**params):
        ...     return client.get("/charges", params=params)
    """
    return retry(
        stop=stop_after_attempt(5),
        wait=_wait_for_stripe_rate_limit,
        retry=retry_if_exception_type(RateLimitError),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )(func)


def _wait_for_ab_rate_limit(retry_state) -> float:
    """Wait the duration specified in the RateLimitError, plus a 2s buffer."""
    exc = retry_state.outcome.exception()
    if isinstance(exc, RateLimitError) and exc.retry_after:
        return float(exc.retry_after) + 2.0
    return 5.0


def retry_action_builder_operation(func: Callable) -> Callable:
    """
    Decorator for Action Builder API operations with retry logic.

    Action Builder has a rate limit of 4 requests per second.
    Only retries on RateLimitError (429) — the only genuinely transient
    condition. ConnectionError wraps HTTP 4xx/5xx responses and should
    fail immediately so the caller sees the real error without waiting
    through exponential backoff.

    Waits exactly as long as the API requests (Retry-After header) plus
    a 2-second buffer, rather than using exponential backoff which would
    exhaust all attempts before the rate limit window clears.

    Args:
        func: The function to decorate

    Returns:
        Decorated function with Action Builder-specific retry logic

    Examples:
        >>> @retry_action_builder_operation
        ... def list_campaigns():
        ...     return client.get("/campaigns")
    """
    return retry(
        stop=stop_after_attempt(5),
        wait=_wait_for_ab_rate_limit,
        retry=retry_if_exception_type(RateLimitError),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )(func)


def _wait_for_github_rate_limit(retry_state) -> float:
    """Wait the duration the GitHub API requested, plus a 2s buffer."""
    exc = retry_state.outcome.exception()
    if isinstance(exc, RateLimitError) and exc.retry_after:
        return float(exc.retry_after) + 2.0
    return 5.0


def retry_github_operation(func: Callable) -> Callable:
    """
    Decorator for GitHub API operations with retry logic.

    GitHub returns 429 (or 403 with x-ratelimit-remaining: 0) when rate
    limited, with a Retry-After or x-ratelimit-reset header indicating
    when to retry. The connector translates both into RateLimitError with
    retry_after populated; this decorator honors that exact wait time plus
    a 2s buffer, rather than guessing via exponential backoff.

    Only retries on RateLimitError — 4xx/5xx surface immediately so the
    caller sees the real error without waiting through five attempts.

    Args:
        func: The function to decorate

    Returns:
        Decorated function with GitHub-specific retry logic

    Examples:
        >>> @retry_github_operation
        ... def put_file(repo, path, content):
        ...     return connector._request("PUT", f"/repos/{repo}/contents/{path}", ...)
    """
    return retry(
        stop=stop_after_attempt(5),
        wait=_wait_for_github_rate_limit,
        retry=retry_if_exception_type(RateLimitError),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )(func)


def _wait_for_asana_rate_limit(retry_state) -> float:
    """Wait the duration the Asana API requested, plus a 2s buffer."""
    exc = retry_state.outcome.exception()
    if isinstance(exc, RateLimitError) and exc.retry_after:
        return float(exc.retry_after) + 2.0
    return 5.0


def retry_asana_operation(func: Callable) -> Callable:
    """
    Decorator for Asana API operations with retry logic.

    Asana returns 429 with a Retry-After header (seconds) when the
    per-token rate limit is exceeded (1,500 req/min on paid domains,
    150 on free). The connector translates that into RateLimitError with
    retry_after populated; this decorator honors that exact wait time plus
    a 2s buffer, rather than guessing via exponential backoff.

    Only retries on RateLimitError — 4xx/5xx (including 402 for paid-tier
    features on free workspaces) surface immediately so the caller sees
    the real error without waiting through five attempts.

    Args:
        func: The function to decorate

    Returns:
        Decorated function with Asana-specific retry logic

    Examples:
        >>> @retry_asana_operation
        ... def get_project_tasks(project_gid):
        ...     return connector._paginate("/tasks", params={"project": project_gid})
    """
    return retry(
        stop=stop_after_attempt(5),
        wait=_wait_for_asana_rate_limit,
        retry=retry_if_exception_type(RateLimitError),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )(func)


def _wait_for_zendesk_rate_limit(retry_state) -> float:
    """Wait the duration the Zendesk API requested, plus a 2s buffer."""
    exc = retry_state.outcome.exception()
    if isinstance(exc, RateLimitError) and exc.retry_after:
        return float(exc.retry_after) + 2.0
    return 10.0


def retry_zendesk_operation(func: Callable) -> Callable:
    """
    Decorator for Zendesk API operations with retry logic.

    Zendesk returns 429 with a Retry-After header (seconds) when the account's
    per-minute ceiling is exceeded (400/min on Suite Growth). The connector
    translates that into RateLimitError with retry_after populated; this
    decorator honors that exact wait plus a 2s buffer rather than guessing via
    exponential backoff.

    The rate budget is per-ACCOUNT, not per-credential, so on a shared instance
    a 429 may be caused by someone else's automation entirely. Backing off the
    full requested interval (rather than retrying tightly) is what keeps us from
    making a neighbour's burst worse.

    Only retries on RateLimitError -- 4xx/5xx surface immediately so the caller
    sees the real error instead of waiting through five attempts.

    Args:
        func: The function to decorate

    Returns:
        Decorated function with Zendesk-specific retry logic

    Examples:
        >>> @retry_zendesk_operation
        ... def list_groups():
        ...     return connector._paginate("/groups.json", resource_key="groups")
    """
    return retry(
        stop=stop_after_attempt(5),
        wait=_wait_for_zendesk_rate_limit,
        retry=retry_if_exception_type(RateLimitError),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )(func)


def retry_email_operation(func: Callable) -> Callable:
    """
    Decorator for transactional-email (Resend) operations with retry logic.

    Resend returns 429 when the per-second/daily send rate is exceeded. Only
    retries on RateLimitError — 4xx/5xx surface immediately so the caller sees
    the real error (a bad from-domain or malformed payload won't fix itself by
    retrying).

    Args:
        func: The function to decorate

    Returns:
        Decorated function with Resend-specific retry logic

    Examples:
        >>> @retry_email_operation
        ... def send(to, subject, html):
        ...     return connector._request("POST", "/emails", ...)
    """
    return retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2.0, min=1.0, max=60.0),
        retry=retry_if_exception_type(RateLimitError),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )(func)


def retry_tatango_operation(func: Callable) -> Callable:
    """
    Decorator for Tatango (MomoGood) Messaging v2 operations with retry logic.

    Tatango's rate-limit tier is unpublished (live-tested clean at
    1 request / 3 s — the connector also paces itself to that rate
    client-side). Only retries on RateLimitError: business-level refusals
    arrive inside HTTP 201 bodies (never exceptions), and real 4xx/5xx —
    including WAF 403 body blocks — should surface immediately so the
    caller sees the actual error.

    Args:
        func: The function to decorate

    Returns:
        Decorated function with Tatango-specific retry logic

    Examples:
        >>> @retry_tatango_operation
        ... def add_subscriber(phone):
        ...     return connector._request("POST", "/lists/1/subscribers", ...)
    """
    return retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2.0, min=3.0, max=60.0),
        retry=retry_if_exception_type(RateLimitError),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )(func)


def retry_snowflake_operation(func: Callable) -> Callable:
    """
    Decorator for Snowflake operations with retry logic.

    Retries rate limiting and nothing else, per the module policy above. For
    Snowflake that leaves very little, and deliberately so:

    * ``snowflake-connector-python`` already retries transient network failures
      internally, the same way ``google-cloud-bigquery`` does — so a transport error
      that reaches us has already been retried and should surface.
    * ⚠ **A statement timeout must never be retried.** ``READER_WH`` caps statements
      at 60 seconds at the *warehouse* level and ``PUBLIC`` cannot raise it, so a
      query that timed out will time out again. Retrying spends three more minutes
      failing and makes a deterministic limit look like a flaky one.
    * An IP-allowlist rejection, a bad password, a missing object and a read-only
      violation are all permanent until a human changes something.

    Args:
        func: The function to decorate

    Returns:
        Decorated function with Snowflake retry logic

    Examples:
        >>> @retry_snowflake_operation
        ... def run(sql):
        ...     return cursor.execute(sql)
    """
    return retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2.0, min=1.0, max=60.0),
        retry=retry_if_exception_type(RateLimitError),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )(func)


def _wait_for_civis_rate_limit(retry_state) -> float:
    """
    Wait as long as Civis asked, plus a 2s buffer, capped at 60s.

    The cap matters: Civis' budget is 1000 requests per *hour*, so an exhausted
    quota can legitimately mean "come back in forty minutes". Sleeping that long
    inside a decorator would look like a hang in a scheduled job and would blow
    past any surrounding timeout, so we make a bounded number of bounded
    attempts and let a genuinely exhausted quota surface as an error the caller
    can act on.
    """
    exc = retry_state.outcome.exception()
    if isinstance(exc, RateLimitError) and exc.retry_after:
        return min(float(exc.retry_after) + 2.0, 60.0)
    return 10.0


def retry_civis_operation(func: Callable) -> Callable:
    """
    Decorator for Civis Platform API operations with retry logic.

    Civis allows 1000 requests/hour per API key and returns 429 on breach. The
    connector translates that into RateLimitError with retry_after populated;
    this honors it (bounded — see :func:`_wait_for_civis_rate_limit`) rather
    than guessing via pure exponential backoff.

    Only retries on RateLimitError. Two reasons that matters more here than
    elsewhere:

    * ⚠ **A 401 from Civis usually means the API key EXPIRED**, not a blip.
      Civis caps keys at 30 days with no service-account alternative, so this is
      a routine end-state, and retrying it just delays the message that says to
      mint a new one. The connector raises AuthenticationError, which is not
      retried.
    * The decorated methods include run triggers and object mutations on a
      platform shared with other TMC member orgs. Replaying an unknown-state
      write is not something to do automatically.

    Args:
        func: The function to decorate

    Returns:
        Decorated function with Civis-specific retry logic

    Examples:
        >>> @retry_civis_operation
        ... def list_scheduled():
        ...     return connector._paginate("/jobs", params={"scheduled": True})
    """
    return retry(
        stop=stop_after_attempt(5),
        wait=_wait_for_civis_rate_limit,
        retry=retry_if_exception_type(RateLimitError),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )(func)


def retry_chariot_operation(func: Callable) -> Callable:
    """
    Decorator for Chariot API operations with retry logic.

    Only RateLimitError is retried, per the module rule — a 401 means the key is
    wrong and a 404 means the path is wrong, and neither improves by waiting.

    ⚠ Chariot publishes no rate limit and has never returned 429 in testing
    (2026-09-08). This decorator exists so that if one ever appears it is handled
    the same way as every other service, not because a limit is known.

    Args:
        func: The function to decorate

    Returns:
        Decorated function with Chariot-specific retry logic
    """
    return retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type(RateLimitError),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )(func)


def retry_geocodio_operation(func: Callable) -> Callable:
    """
    Decorator for Geocodio API operations with retry logic.

    Geocodio returns 429 when the account's lookup quota is exceeded or
    request rate is too high. Only retries on RateLimitError — 4xx/5xx
    API errors should surface immediately.

    Args:
        func: The function to decorate

    Returns:
        Decorated function with Geocodio-specific retry logic

    Examples:
        >>> @retry_geocodio_operation
        ... def geocode(address):
        ...     return client.get("/geocode", params={"q": address})
    """
    return retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2.0, min=1.0, max=60.0),
        retry=retry_if_exception_type(RateLimitError),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )(func)
