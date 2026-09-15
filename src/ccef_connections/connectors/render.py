"""
Render connector for CCEF connections library.

Render hosts our internal Flask tools (the first is ep-roving's director
review UI). This is the transport layer for its REST API: services and their
lifecycle, deploys, app/request/build logs, the event timeline, metrics,
custom domains, and environment variables — the things you would otherwise
click through the dashboard for.

Two design points worth understanding before using the write methods.

**The event log is the only place a crash is recorded.** An OOM kill does not
fail a deploy and does not mark a service unhealthy: Render restarts the
instance, the health check passes, and the dashboard goes back to green. The
deploy list, the service record and the log stream all look normal afterwards.
``list_events`` is where ``server_failed`` / ``oomKilled`` lives, and
``peak_memory`` is the number that says whether it is about to happen again.

The design point worth understanding before using the write methods:

**A Render Blueprint (``render.yaml``) and this API are two writers to the
same state, and the Blueprint wins.** An env var declared in the Blueprint with
a literal ``value:`` is re-applied on every sync, so a value PATCHed here is
silently reverted the next time anyone pushes to the deploy branch — attached
to an unrelated commit, days later. Vars declared ``sync: false``, and vars the
Blueprint does not mention at all, are preserved by Render and are safe to
write here.

Pass ``blueprint_path`` to the constructor and the connector enforces that
distinction for you: writes to Blueprint-owned keys raise ``WriteError``
naming the file to edit instead. Use it in any project that has a
``render.yaml``; leave it unset only for services created outside a Blueprint.

    >>> with RenderConnector(blueprint_path="render.yaml") as render:
    ...     svc = render.find_service("ep-roving-review")
    ...     render.set_env_var(svc["id"], "SECRET_KEY", new_key)   # sync:false — ok
    ...     render.set_env_var(svc["id"], "APP_BASE_URL", host)    # raises WriteError

Credentials follow the {NAME}_PASSWORD convention: RENDER_API_KEY_PASSWORD.
Note that Render API keys are workspace-wide and unscoped — there is no
read-only or per-service key — so the key is the blast radius.
"""

import logging
import time
from typing import Any, Dict, Iterator, List, Optional

import requests

from ..core.base import BaseConnection
from ..core.retry import retry_render_operation
from ..exceptions import (
    AuthenticationError,
    ConnectionError,
    CredentialError,
    RateLimitError,
    WriteError,
)

logger = logging.getLogger(__name__)

RENDER_API_BASE = "https://api.render.com/v1"

# Render wraps each item in a list response under a singular key alongside the
# pagination cursor: [{"service": {...}, "cursor": "..."}, ...]. The key
# differs per endpoint, so _paginate takes it as an argument.
_ITEM_KEYS = {
    "services": "service",
    "deploys": "deploy",
    "custom-domains": "customDomain",
    "env-vars": "envVar",
    "events": "event",
    "postgres": "postgres",
    "blueprints": "blueprint",
}

# Render keeps 30 days of logs and caps a single /logs query at 1000 entries
# (1001+ is a 400, and a startTime older than 30 days is a 400 — both verified
# against the live API 2026-09-15, neither documented as a number).
LOG_MAX_LIMIT = 1000
LOG_RETENTION_DAYS = 30

# The log endpoints scan a time window rather than fetch a record, and are
# markedly slower than the rest of the API — /logs/values in particular has been
# measured past 30s on a service with a week of request logs. A read timeout
# there surfaces as a bare ReadTimeout that reads like the API is down, so they
# get their own, longer budget.
DEFAULT_TIMEOUT = 30
LOG_QUERY_TIMEOUT = 90

# Metric series available from /metrics/{name}. The -limit pair is what makes
# the others legible: memory alone is a number, memory against memory-limit is
# how close the service is to being OOM-killed.
METRIC_NAMES = (
    "cpu",
    "cpu-limit",
    "memory",
    "memory-limit",
    "http-requests",
    "http-latency",
    "instance-count",
    "bandwidth",
    "active-connections",
    "disk-usage",
    "disk-capacity",
)

# Service states that mean "a deploy is mid-flight", for callers deciding
# whether it is safe to start another.
IN_FLIGHT_DEPLOY_STATUSES = frozenset({
    "created",
    "queued",
    "build_in_progress",
    "update_in_progress",
    "pre_deploy_in_progress",
})


class RenderConnector(BaseConnection):
    """
    Render connector for managing services, deploys, domains and env vars.

    Read methods are safe to call freely (Render allows 400 GETs/minute).
    Writes are rate-limited far more tightly — 30/minute, and 10/minute per
    service for deploys.

        >>> with RenderConnector() as render:
        ...     svc = render.find_service("ep-roving-review")
        ...     for domain in render.list_custom_domains(svc["id"]):
        ...         print(domain["name"], domain["verificationStatus"])

    Args:
        credential_name: Credential to read the API key from. The env var is
            {credential_name}_PASSWORD. Default: RENDER_API_KEY.
        blueprint_path: Path to the project's ``render.yaml``. When set,
            ``set_env_var`` refuses to write any key the Blueprint declares
            with a literal ``value:``, because Render would revert it on the
            next Blueprint sync. Strongly recommended for Blueprint-managed
            services.
        owner_id: Pin the workspace (``tea-...``). Only needed when the key
            reaches more than one; otherwise it is discovered once and cached.
        timeout: Read timeout in seconds for ordinary calls (default 30). Log
            queries use ``LOG_QUERY_TIMEOUT`` regardless.
    """

    def __init__(
        self,
        credential_name: str = "RENDER_API_KEY",
        blueprint_path: Optional[str] = None,
        owner_id: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        super().__init__()
        self._credential_name = credential_name
        self._blueprint_path = blueprint_path
        self._api_key: Optional[str] = None
        self._blueprint_keys: Optional[set] = None
        self._owner_id: Optional[str] = owner_id
        self._timeout = timeout

    # -- lifecycle -----------------------------------------------------

    def connect(self) -> None:
        """
        Load the API key into memory.

        Raises:
            CredentialError: If the API key is missing
            ConnectionError: If credential lookup fails for any other reason
        """
        try:
            self._api_key = self._credential_manager.get_render_api_key(
                self._credential_name
            )
            self._is_connected = True
            logger.info(
                f"Successfully connected to Render (credential: {self._credential_name})"
            )
        except CredentialError:
            logger.error(
                f"Failed to connect to Render: credential {self._credential_name} missing"
            )
            raise
        except Exception as e:
            logger.error(f"Failed to connect to Render: {e}")
            raise ConnectionError(f"Failed to connect to Render: {e}") from e

    def disconnect(self) -> None:
        """Clear the API key from memory."""
        self._api_key = None
        self._is_connected = False
        logger.debug("Disconnected from Render")

    def health_check(self) -> bool:
        """
        Check the connection by listing one service.

        Returns:
            True if the key is valid and the API is reachable, False otherwise
        """
        if not self._is_connected or not self._api_key:
            return False
        try:
            self._request("GET", "/services", params={"limit": 1})
            return True
        except Exception:
            return False

    # -- HTTP helpers --------------------------------------------------

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        timeout: Optional[int] = None,
    ) -> Optional[Any]:
        """
        Central HTTP method with auth headers and standard error mapping.

        Returns parsed JSON, or None for 204/404.

        Args:
            method: HTTP method.
            path: API path beginning with "/".
            params: Query parameters.
            json_body: JSON request body.
            timeout: Read timeout in seconds. Defaults to the connector's
                ``timeout``; the log endpoints override it with
                ``LOG_QUERY_TIMEOUT`` because they scan a time range rather
                than read a record and can genuinely take most of a minute.

        Raises:
            AuthenticationError: For 401/403
            RateLimitError: For 429, with retry_after derived from
                Ratelimit-Reset (a UTC epoch timestamp, not a delta)
            ConnectionError: For other 4xx/5xx or network failures
        """
        if not self._is_connected and not self._api_key:
            self.connect()

        try:
            resp = requests.request(
                method,
                f"{RENDER_API_BASE}{path}",
                headers=self._get_headers(),
                params=params,
                json=json_body,
                timeout=timeout or self._timeout,
            )
        except requests.RequestException as e:
            raise ConnectionError(f"Render API request failed: {e}") from e

        if resp.status_code == 429:
            raise RateLimitError(
                f"Render rate limit exceeded: {resp.text}",
                retry_after=_parse_retry_after(resp.headers),
            )

        if resp.status_code == 401:
            raise AuthenticationError(f"Render authentication failed: {resp.text}")

        if resp.status_code == 403:
            raise AuthenticationError(
                f"Render authorization failed (key lacks workspace access?): {resp.text}"
            )

        if resp.status_code in (204, 404):
            return None

        if resp.status_code >= 400:
            raise ConnectionError(f"Render API error {resp.status_code}: {resp.text}")

        return resp.json()

    def _paginate(
        self,
        path: str,
        item_key: str,
        params: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = None,
        page_size: int = 100,
    ) -> Iterator[Dict[str, Any]]:
        """
        Walk a cursor-paginated list endpoint, unwrapping each item.

        Render caps `limit` at 100 per page and returns the cursor on each
        item rather than once per page, so the cursor to continue from is the
        last item's.

        Args:
            path: API path, e.g. "/services".
            item_key: The singular wrapper key, e.g. "service".
            params: Extra query parameters.
            limit: Stop after this many items overall (None = all).
            page_size: Items per request (Render's maximum is 100). Exposed
                so the cursor-continuation path can be exercised against a
                collection smaller than one full page — otherwise it only
                ever runs in production, on whichever service crosses 100
                deploys first.

        Yields:
            The unwrapped objects.
        """
        base: Dict[str, Any] = dict(params or {})
        page_size = max(1, min(100, page_size))
        base["limit"] = min(page_size, limit) if limit else page_size
        cursor: Optional[str] = None
        yielded = 0

        while True:
            # A fresh dict per request rather than mutating one across the
            # loop: a caller (or a test double) that holds onto the params it
            # was handed would otherwise see it change underneath them.
            query = dict(base)
            if cursor:
                query["cursor"] = cursor
            page = self._request("GET", path, params=query) or []
            if not page:
                return
            for entry in page:
                item = entry.get(item_key) if isinstance(entry, dict) else None
                if item is None:
                    continue
                yield item
                yielded += 1
                if limit and yielded >= limit:
                    return
            cursor = page[-1].get("cursor") if isinstance(page[-1], dict) else None
            if not cursor:
                return

    # -- services ------------------------------------------------------

    @retry_render_operation
    def list_services(
        self, name: Optional[str] = None, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        List services in the workspace.

        Args:
            name: Filter by service name (Render matches on prefix).
            limit: Maximum services to return.

        Returns:
            A list of service objects.
        """
        params = {"name": name} if name else None
        return list(self._paginate("/services", _ITEM_KEYS["services"], params, limit))

    @retry_render_operation
    def get_service(self, service_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve one service by ID.

        Args:
            service_id: The Render service ID (``srv-...``).

        Returns:
            The service object, or None if it does not exist.
        """
        return self._request("GET", f"/services/{service_id}")

    def find_service(self, name: str) -> Dict[str, Any]:
        """
        Resolve a service by its exact name.

        Service IDs are opaque (``srv-d1abc...``) and nobody remembers them,
        but names are what's in render.yaml and in everyone's head. Render's
        name filter matches on prefix, so this re-filters for an exact match —
        otherwise "ep-roving" would ambiguously match "ep-roving-review".

        Args:
            name: Exact service name, e.g. "ep-roving-review".

        Returns:
            The service object.

        Raises:
            ConnectionError: If no service (or more than one) matches exactly.
        """
        matches = [s for s in self.list_services(name=name) if s.get("name") == name]
        if not matches:
            raise ConnectionError(f"No Render service named {name!r} in this workspace")
        if len(matches) > 1:
            raise ConnectionError(
                f"{len(matches)} Render services named {name!r}; resolve by ID instead"
            )
        return matches[0]

    # -- deploys -------------------------------------------------------

    @retry_render_operation
    def list_deploys(
        self, service_id: str, limit: Optional[int] = 20
    ) -> List[Dict[str, Any]]:
        """
        List a service's deploys, newest first.

        Args:
            service_id: The Render service ID.
            limit: Maximum deploys to return. Default 20.

        Returns:
            A list of deploy objects. Each carries ``status`` — one of
            ``created``, ``build_in_progress``, ``update_in_progress``,
            ``live``, ``deactivated``, ``build_failed``, ``update_failed``,
            ``canceled``, ``pre_deploy_in_progress``, ``pre_deploy_failed``.
        """
        return list(
            self._paginate(
                f"/services/{service_id}/deploys", _ITEM_KEYS["deploys"], limit=limit
            )
        )

    def latest_deploy(self, service_id: str) -> Optional[Dict[str, Any]]:
        """
        Return the most recent deploy, or None if the service has never deployed.

        Args:
            service_id: The Render service ID.

        Returns:
            The newest deploy object, or None.
        """
        deploys = self.list_deploys(service_id, limit=1)
        return deploys[0] if deploys else None

    @retry_render_operation
    def get_deploy(self, service_id: str, deploy_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve one deploy.

        Args:
            service_id: The Render service ID.
            deploy_id: The deploy ID (``dep-...``).

        Returns:
            The deploy object, or None if it does not exist.
        """
        return self._request("GET", f"/services/{service_id}/deploys/{deploy_id}")

    @retry_render_operation
    def trigger_deploy(
        self, service_id: str, clear_cache: bool = False
    ) -> Dict[str, Any]:
        """
        Trigger a new deploy of the service's current branch tip.

        Note this deploys whatever the branch currently points at — it is a
        redeploy, not a way to deploy uncommitted work.

        Args:
            service_id: The Render service ID.
            clear_cache: Rebuild without the build cache. Use when a
                dependency pin moved but the SHA in the lockfile did not.

        Returns:
            The created deploy object.

        Raises:
            WriteError: If Render rejects the request.
        """
        body = {"clearCache": "clear" if clear_cache else "do_not_clear"}
        result = self._request(
            "POST", f"/services/{service_id}/deploys", json_body=body
        )
        if not result:
            raise WriteError(f"Render refused to start a deploy of {service_id}")
        logger.info(f"Triggered deploy {result.get('id')} for {service_id}")
        return result

    def wait_for_deploy(
        self,
        service_id: str,
        deploy_id: str,
        timeout: int = 900,
        poll_interval: int = 10,
    ) -> Dict[str, Any]:
        """
        Block until a deploy reaches a terminal state.

        Args:
            service_id: The Render service ID.
            deploy_id: The deploy to watch.
            timeout: Seconds to wait before giving up. Default 900 (15 min).
            poll_interval: Seconds between polls. Default 10.

        Returns:
            The final deploy object. Check ``status`` — reaching a terminal
            state is not the same as succeeding.

        Raises:
            ConnectionError: If the deploy vanishes or the timeout expires.
        """
        terminal = {
            "live",
            "deactivated",
            "build_failed",
            "update_failed",
            "canceled",
            "pre_deploy_failed",
        }
        deadline = time.monotonic() + timeout
        while True:
            deploy = self.get_deploy(service_id, deploy_id)
            if deploy is None:
                raise ConnectionError(f"Deploy {deploy_id} not found on {service_id}")
            status = deploy.get("status")
            if status in terminal:
                logger.info(f"Deploy {deploy_id} finished: {status}")
                return deploy
            if time.monotonic() >= deadline:
                raise ConnectionError(
                    f"Deploy {deploy_id} still {status!r} after {timeout}s"
                )
            time.sleep(poll_interval)

    # -- custom domains ------------------------------------------------

    @retry_render_operation
    def list_custom_domains(self, service_id: str) -> List[Dict[str, Any]]:
        """
        List a service's custom domains.

        Args:
            service_id: The Render service ID.

        Returns:
            A list of custom-domain objects, each carrying ``name``,
            ``domainType`` (``apex`` or ``subdomain``) and
            ``verificationStatus`` (``verified`` or ``unverified``).

            Note there is **no certificate field** — Render's API exposes DNS
            verification only. A domain can read ``verified`` a moment before
            its TLS certificate is actually issued, so treat a successful
            HTTPS request to the host as the real readiness signal.
        """
        return list(
            self._paginate(
                f"/services/{service_id}/custom-domains",
                _ITEM_KEYS["custom-domains"],
            )
        )

    @retry_render_operation
    def add_custom_domain(self, service_id: str, name: str) -> Dict[str, Any]:
        """
        Attach a custom domain to a service.

        Prefer declaring domains in ``render.yaml`` (``domains:``) where the
        project has a Blueprint — that keeps which hosts a service answers for
        reviewable in git. This method is for services without one, or for a
        domain being added ahead of a Blueprint change.

        Adding a domain does not configure DNS; Render does not hold the zone.

        Args:
            service_id: The Render service ID.
            name: The hostname, e.g. "roving.electionprotectiontools.org".

        Returns:
            The created custom-domain object.

        Raises:
            WriteError: If Render rejects the domain (already attached to
                another service, invalid, or not a public suffix).
        """
        try:
            result = self._request(
                "POST", f"/services/{service_id}/custom-domains", json_body={"name": name}
            )
        except ConnectionError as e:
            raise WriteError(f"Render rejected custom domain {name!r}: {e}") from e
        if not result:
            raise WriteError(f"Unexpected empty response adding {name!r}")
        logger.info(f"Added custom domain {name} to {service_id}")
        return result

    @retry_render_operation
    def verify_custom_domain(self, service_id: str, domain_id_or_name: str) -> bool:
        """
        Ask Render to re-check a domain's DNS now.

        Render re-checks on its own schedule; this is the "Verify" button, for
        when you have just published the records and don't want to wait.

        Args:
            service_id: The Render service ID.
            domain_id_or_name: The domain's ID or its hostname.

        Returns:
            True if Render accepted the request. This means the check ran, not
            that it passed — read ``verificationStatus`` from
            ``list_custom_domains`` afterwards.
        """
        self._request(
            "POST",
            f"/services/{service_id}/custom-domains/{domain_id_or_name}/verify",
        )
        return True

    @retry_render_operation
    def delete_custom_domain(self, service_id: str, domain_id_or_name: str) -> bool:
        """
        Detach a custom domain from a service.

        Args:
            service_id: The Render service ID.
            domain_id_or_name: The domain's ID or its hostname.

        Returns:
            True once Render has accepted the deletion.
        """
        self._request(
            "DELETE", f"/services/{service_id}/custom-domains/{domain_id_or_name}"
        )
        logger.info(f"Removed custom domain {domain_id_or_name} from {service_id}")
        return True

    # -- environment variables -----------------------------------------

    @retry_render_operation
    def list_env_vars(self, service_id: str) -> List[Dict[str, Any]]:
        """
        List a service's environment variables.

        **This returns secret values in plaintext** — Render's API does not
        mask them. Don't log the result wholesale; compare keys, and compare
        values only where you need to.

        Args:
            service_id: The Render service ID.

        Returns:
            A list of ``{"key": ..., "value": ...}`` objects.
        """
        return list(
            self._paginate(f"/services/{service_id}/env-vars", _ITEM_KEYS["env-vars"])
        )

    def env_var_keys(self, service_id: str) -> set:
        """
        Return just the set of env var keys set on a service.

        The safe way to ask "is this configured?" without pulling secret
        values into memory or a log line.

        Args:
            service_id: The Render service ID.

        Returns:
            A set of key names.
        """
        return {v["key"] for v in self.list_env_vars(service_id) if "key" in v}

    @retry_render_operation
    def set_env_var(self, service_id: str, key: str, value: str) -> Dict[str, Any]:
        """
        Create or update a single environment variable.

        Setting a var triggers a redeploy of the service.

        **Refuses Blueprint-owned keys** when the connector was built with
        ``blueprint_path``. A key declared in ``render.yaml`` with a literal
        ``value:`` is re-applied on every Blueprint sync, so a write here holds
        only until the next push to the deploy branch and then silently
        reverts — a failure that surfaces days later attached to an unrelated
        commit. Change those by editing the Blueprint. Keys declared
        ``sync: false``, and keys the Blueprint does not mention, are preserved
        by Render and are written normally.

        Args:
            service_id: The Render service ID.
            key: The variable name.
            value: The value to set.

        Returns:
            The updated env var object.

        Raises:
            WriteError: If the key is Blueprint-owned, or Render rejects it.
        """
        managed = self._blueprint_managed_keys()
        if key in managed:
            raise WriteError(
                f"{key!r} is declared with a literal value in {self._blueprint_path}. "
                f"Render re-applies Blueprint values on every sync, so this write "
                f"would be reverted by the next push. Edit the Blueprint instead."
            )

        result = self._request(
            "PUT",
            f"/services/{service_id}/env-vars/{key}",
            json_body={"value": value},
        )
        if not result:
            raise WriteError(f"Render refused to set {key!r} on {service_id}")
        logger.info(f"Set env var {key} on {service_id}")
        return result

    @retry_render_operation
    def delete_env_var(self, service_id: str, key: str) -> bool:
        """
        Delete an environment variable.

        Args:
            service_id: The Render service ID.
            key: The variable name.

        Returns:
            True once Render has accepted the deletion.

        Raises:
            WriteError: If the key is Blueprint-owned (see ``set_env_var``).
        """
        if key in self._blueprint_managed_keys():
            raise WriteError(
                f"{key!r} is declared in {self._blueprint_path}; the next Blueprint "
                f"sync would recreate it. Remove it from the Blueprint instead."
            )
        self._request("DELETE", f"/services/{service_id}/env-vars/{key}")
        logger.info(f"Deleted env var {key} from {service_id}")
        return True

    # -- workspace -----------------------------------------------------

    @retry_render_operation
    def list_owners(self) -> List[Dict[str, Any]]:
        """
        List the workspaces (Render calls them "owners") this key can reach.

        Returns:
            A list of owner objects, each with ``id`` (``tea-...`` for a team,
            ``usr-...`` for a personal account), ``name``, ``type`` and
            ``email``.
        """
        entries = self._request("GET", "/owners") or []
        return [e.get("owner", e) for e in entries if isinstance(e, dict)]

    def owner_id(self) -> str:
        """
        The workspace id, cached for the life of the connector.

        Needed by every log and metric query — those endpoints are scoped to a
        workspace rather than to a service, so the service id alone is not
        enough to ask for its own logs.

        Returns:
            The owner id.

        Raises:
            ConnectionError: If the key reaches no workspace, or more than one
                and none was pinned via ``owner_id`` on the constructor.
        """
        if self._owner_id:
            return self._owner_id

        owners = self.list_owners()
        if not owners:
            raise ConnectionError(
                "This Render key reaches no workspace. It may have been revoked."
            )
        if len(owners) > 1:
            names = ", ".join(f"{o.get('name')} ({o.get('id')})" for o in owners)
            raise ConnectionError(
                f"This key reaches {len(owners)} workspaces ({names}); pass "
                f"owner_id= to the constructor to say which one."
            )
        self._owner_id = owners[0]["id"]
        return self._owner_id

    # -- logs ----------------------------------------------------------

    @retry_render_operation
    def list_logs(
        self,
        resource: str,
        log_type: Optional[str] = None,
        level: Optional[str] = None,
        text: Optional[str] = None,
        limit: int = 100,
        direction: str = "backward",
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        instance: Optional[str] = None,
        status_code: Optional[str] = None,
        method: Optional[str] = None,
        path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Read a service's logs.

        Three kinds of log share this one endpoint, distinguished by
        ``log_type``, and confusing them wastes a debugging session:

        * ``app`` — what the process wrote to stdout/stderr. Your own logging.
        * ``request`` — Render's HTTP access log, one line per request, carrying
          ``method``, ``path``, ``statusCode`` and response time. The process
          never sees these, so a 403 rejected at the edge appears ONLY here.
        * ``build`` — pip/npm output from a deploy's build phase.

        Omitting ``log_type`` returns all three interleaved, which is usually
        what you want when asking "what happened at 04:12".

        ⚠ Retention is 30 days and a single query caps at 1000 entries; both
        are enforced by Render with a 400, so they are validated here instead.

        Args:
            resource: The service id (``srv-...``) whose logs to read.
            log_type: "app", "request" or "build". None for all.
            level: Minimum-ish severity label, e.g. "error", "warning". Note
                this is Render's own label, not your logger's: anything a
                container writes to stderr can arrive tagged higher than you
                meant it.
            text: Substring filter, applied server-side.
            limit: Max entries (Render's ceiling is 1000).
            direction: "backward" for newest-first (the default, and what you
                want for "what just happened"), "forward" for oldest-first.
            start_time: ISO-8601 lower bound. Must be within 30 days.
            end_time: ISO-8601 upper bound.
            instance: Restrict to one instance id — the way to read a single
                replica after a crash.
            status_code: HTTP status filter, e.g. "502". Implies request logs.
            method: HTTP method filter, e.g. "POST". Implies request logs.
            path: Request path filter. Implies request logs.

        Returns:
            ``{"logs": [...], "hasMore": bool, "nextStartTime": ...,
            "nextEndTime": ...}``. Each entry carries ``message``,
            ``timestamp`` and a ``labels`` list; ``labels_map`` is added here
            as a flat dict because the list form is tedious to read.

        Raises:
            ValueError: If limit or start_time is outside what Render accepts.
        """
        if limit < 1 or limit > LOG_MAX_LIMIT:
            raise ValueError(
                f"limit must be 1-{LOG_MAX_LIMIT}; Render 400s above that. For "
                f"more than {LOG_MAX_LIMIT} entries, page with the returned "
                f"nextStartTime/nextEndTime."
            )
        if direction not in ("backward", "forward"):
            raise ValueError("direction must be 'backward' or 'forward'")

        params: Dict[str, Any] = {
            "ownerId": self.owner_id(),
            "resource": resource,
            "limit": limit,
            "direction": direction,
        }
        for key, value in (
            ("type", log_type),
            ("level", level),
            ("text", text),
            ("startTime", start_time),
            ("endTime", end_time),
            ("instance", instance),
            ("statusCode", status_code),
            ("method", method),
            ("path", path),
        ):
            if value:
                params[key] = value

        result = (
            self._request("GET", "/logs", params=params, timeout=LOG_QUERY_TIMEOUT)
            or {}
        )
        for entry in result.get("logs") or []:
            entry["labels_map"] = {
                label.get("name"): label.get("value")
                for label in entry.get("labels") or []
                if isinstance(label, dict)
            }
        return result

    @retry_render_operation
    def log_label_values(
        self, resource: str, label: str, log_type: Optional[str] = None
    ) -> List[str]:
        """
        List the values a log label actually takes for a service.

        The cheap way to find out what there is to filter on before filtering:
        which instances have run, which status codes have occurred, which hosts
        answered. An empty list means the label does not apply to this service's
        logs, not that the query failed — ``path`` is empty unless you also pass
        ``log_type="request"``, because only request logs carry one.

        Args:
            resource: The service id.
            label: One of "type", "level", "instance", "host", "statusCode",
                "method", "path".
            log_type: Restrict to one log type first.

        Returns:
            The list of observed values.
        """
        params: Dict[str, Any] = {
            "ownerId": self.owner_id(),
            "resource": resource,
            "label": label,
        }
        if log_type:
            params["type"] = log_type
        return (
            self._request(
                "GET", "/logs/values", params=params, timeout=LOG_QUERY_TIMEOUT
            )
            or []
        )

    # -- events and instances ------------------------------------------

    @retry_render_operation
    def list_events(
        self, service_id: str, limit: Optional[int] = 20
    ) -> List[Dict[str, Any]]:
        """
        A service's event timeline, newest first.

        The only place Render tells you why an instance died. Types seen in
        practice: ``build_started`` / ``build_ended``, ``deploy_started`` /
        ``deploy_ended``, ``server_available``, and ``server_failed`` — whose
        ``details.reason`` carries ``oomKilled`` (with the memory ceiling it hit)
        or ``evicted``.

        That last one is the reason to read this at all. An OOM kill does not
        fail a deploy and does not mark the service unhealthy: Render restarts
        the instance, the health check passes, the dashboard stays green, and
        nothing in the deploy list or the service record records that it
        happened. Only the event log does.

        Args:
            service_id: The Render service ID.
            limit: Maximum events to return. Default 20.

        Returns:
            A list of event objects, each with ``type``, ``timestamp`` and a
            type-specific ``details`` mapping.
        """
        return list(
            self._paginate(
                f"/services/{service_id}/events", _ITEM_KEYS["events"], limit=limit
            )
        )

    @retry_render_operation
    def list_instances(self, service_id: str) -> List[Dict[str, Any]]:
        """
        The service's currently running instances.

        Unlike most list endpoints here this one returns bare objects rather
        than cursor-wrapped ones, so it is not paginated.

        Args:
            service_id: The Render service ID.

        Returns:
            A list of ``{"id", "createdAt", "status", "ready"}`` objects. The
            ``createdAt`` is when the *instance* started, which is how you spot
            a service that has been silently restarting: an age far younger than
            its last deploy means something killed it.
        """
        return self._request("GET", f"/services/{service_id}/instances") or []

    # -- metrics -------------------------------------------------------

    @retry_render_operation
    def metrics(
        self,
        name: str,
        resource: str,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        resolution_seconds: Optional[int] = None,
        **extra: Any,
    ) -> List[Dict[str, Any]]:
        """
        Read one metric series for a resource.

        Pair a usage metric with its ceiling — ``memory`` against
        ``memory-limit``, ``cpu`` against ``cpu-limit`` — because the ceiling is
        set by the service's plan and is the number that decides whether the fix
        is a code change or a bigger plan.

        Args:
            name: A metric from ``METRIC_NAMES``. ``http-latency`` additionally
                requires ``quantile`` (e.g. ``quantile=0.95``) as an extra.
            resource: The service id (``srv-...``) or database id (``dpg-...``).
            start_time: ISO-8601 lower bound. Defaults to Render's own window.
            end_time: ISO-8601 upper bound.
            resolution_seconds: Seconds per data point.
            **extra: Any further query parameters the metric needs.

        Returns:
            A list of series, each ``{"labels": [...], "unit": ..., "values":
            [{"timestamp", "value"}, ...]}``. An empty list means the metric
            does not apply to this resource kind (``disk-usage`` on a service
            with no disk, say) rather than an error.

        Raises:
            ValueError: If ``name`` is not a known metric.
        """
        if name not in METRIC_NAMES:
            raise ValueError(
                f"{name!r} is not a Render metric; known: {', '.join(METRIC_NAMES)}"
            )
        params: Dict[str, Any] = {"resource": resource}
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        if resolution_seconds:
            params["resolutionSeconds"] = resolution_seconds
        params.update(extra)
        return self._request("GET", f"/metrics/{name}", params=params) or []

    def peak_memory(self, service_id: str, **kwargs: Any) -> Dict[str, Any]:
        """
        Memory high-water mark against the plan's ceiling.

        The single most useful number about a Render service, and the one the
        dashboard makes hardest to get: how close it came to the limit that
        would OOM-kill it.

        Args:
            service_id: The Render service ID.
            **kwargs: Passed through to ``metrics`` (``start_time`` etc.).

        Returns:
            ``{"peak_bytes", "limit_bytes", "pct_of_limit", "samples"}``.
            ``pct_of_limit`` is None when no limit series came back.
        """
        used = self.metrics("memory", service_id, **kwargs)
        ceiling = self.metrics("memory-limit", service_id, **kwargs)

        values = [
            point.get("value")
            for series in used
            for point in series.get("values") or []
            if point.get("value") is not None
        ]
        limits = [
            point.get("value")
            for series in ceiling
            for point in series.get("values") or []
            if point.get("value")
        ]
        peak = max(values) if values else None
        limit = max(limits) if limits else None
        return {
            "peak_bytes": peak,
            "limit_bytes": limit,
            "pct_of_limit": (100.0 * peak / limit) if peak and limit else None,
            "samples": len(values),
        }

    # -- service lifecycle ---------------------------------------------

    @retry_render_operation
    def restart_service(self, service_id: str) -> bool:
        """
        Restart a service's instances without rebuilding.

        The right tool when the code is fine and the process is not — a wedged
        thread pool, a leaked connection, a stuck consumer. It does not redeploy,
        so it cannot pick up a new commit; use ``trigger_deploy`` for that.

        Args:
            service_id: The Render service ID.

        Returns:
            True once Render has accepted the restart. There is a gap between
            acceptance and the new instance being ready — watch
            ``list_instances`` or the ``server_available`` event.
        """
        self._request("POST", f"/services/{service_id}/restart")
        logger.info(f"Restarted {service_id}")
        return True

    @retry_render_operation
    def suspend_service(self, service_id: str) -> bool:
        """
        Suspend a service — stop it running, without deleting it.

        Reversible via ``resume_service``, and the reason this connector exposes
        no delete: suspending keeps the service, its id, its env vars, its
        domains and its history, so a wrong suspend costs downtime rather than
        the object. A suspended web service stops answering — for anything that
        receives inbound data, that is data not arriving, not merely a tool
        being offline.

        Args:
            service_id: The Render service ID.

        Returns:
            True once Render has accepted the suspension.
        """
        self._request("POST", f"/services/{service_id}/suspend")
        logger.info(f"Suspended {service_id}")
        return True

    @retry_render_operation
    def resume_service(self, service_id: str) -> bool:
        """
        Resume a suspended service.

        Args:
            service_id: The Render service ID.

        Returns:
            True once Render has accepted the resume.
        """
        self._request("POST", f"/services/{service_id}/resume")
        logger.info(f"Resumed {service_id}")
        return True

    @retry_render_operation
    def scale_service(self, service_id: str, num_instances: int) -> bool:
        """
        Set the number of running instances.

        ⚠ Billing is per instance: three instances of a $25/month plan is
        $75/month. This is a spend decision, not a tuning knob.

        Horizontal scaling also assumes the service tolerates running more than
        once — a webhook receiver that dedupes in process memory, or any job
        holding a singleton lock, does not.

        Args:
            service_id: The Render service ID.
            num_instances: How many instances to run.

        Returns:
            True once Render has accepted the change.

        Raises:
            ValueError: If num_instances is below 1.
        """
        if num_instances < 1:
            raise ValueError(
                "num_instances must be at least 1; use suspend_service to stop "
                "a service, which is reversible and keeps its configuration."
            )
        self._request(
            "POST",
            f"/services/{service_id}/scale",
            json_body={"numInstances": num_instances},
        )
        logger.info(f"Scaled {service_id} to {num_instances} instance(s)")
        return True

    @retry_render_operation
    def update_service(self, service_id: str, **fields: Any) -> Dict[str, Any]:
        """
        Patch a service's configuration.

        This is where a **plan change** lives — ``serviceDetails`` carries
        ``plan``, and moving between plans changes both the monthly cost and the
        memory ceiling a process is killed at. It is also where the build and
        start commands, health check path and IP allow list live.

        ⚠ Blueprint-managed services: anything ``render.yaml`` declares is
        re-applied on the next sync, exactly as with env vars. The env-var guard
        here cannot see these fields, so for a Blueprint service, edit the file.

        Args:
            service_id: The Render service ID.
            **fields: Fields to patch, in Render's own camelCase, e.g.
                ``serviceDetails={"plan": "pro"}`` or ``branch="main"``.

        Returns:
            The updated service object.

        Raises:
            WriteError: If Render rejects the patch.
        """
        if not fields:
            raise WriteError("update_service called with nothing to change")
        try:
            result = self._request(
                "PATCH", f"/services/{service_id}", json_body=fields
            )
        except ConnectionError as e:
            raise WriteError(f"Render rejected the update to {service_id}: {e}") from e
        if not result:
            raise WriteError(f"Render refused to update {service_id}")
        logger.info(f"Updated {service_id}: {sorted(fields)}")
        return result

    @retry_render_operation
    def cancel_deploy(self, service_id: str, deploy_id: str) -> bool:
        """
        Cancel a deploy that is still building or updating.

        Args:
            service_id: The Render service ID.
            deploy_id: The deploy to cancel.

        Returns:
            True once Render has accepted the cancellation.
        """
        self._request(
            "POST", f"/services/{service_id}/deploys/{deploy_id}/cancel"
        )
        logger.info(f"Cancelled deploy {deploy_id} on {service_id}")
        return True

    @retry_render_operation
    def rollback_deploy(self, service_id: str, deploy_id: str) -> Dict[str, Any]:
        """
        Roll a service back to an earlier deploy.

        Render creates a NEW deploy that restores the old image rather than
        rewinding history, so the deploy list grows and the rollback is itself
        rollback-able.

        ⚠ Rolling back moves the code, not the configuration. Env vars, plan and
        domains stay as they are now, so a rollback does not undo a bad env-var
        write — and on a Blueprint-managed service the next push re-syncs
        ``render.yaml`` and can carry the rolled-back change straight back in.

        Note the route is ``POST /services/{id}/rollback`` with the deploy in
        the body; there is no ``/deploys/{id}/rollback`` (verified 2026-09-15 —
        that path 404s).

        Args:
            service_id: The Render service ID.
            deploy_id: The deploy to roll back TO, from ``list_deploys``.

        Returns:
            The new deploy object created by the rollback.

        Raises:
            WriteError: If Render rejects the rollback.
        """
        try:
            result = self._request(
                "POST",
                f"/services/{service_id}/rollback",
                json_body={"deployId": deploy_id},
            )
        except ConnectionError as e:
            raise WriteError(
                f"Render rejected rolling {service_id} back to {deploy_id}: {e}"
            ) from e
        if not result:
            raise WriteError(
                f"Render refused to roll {service_id} back to {deploy_id}. A "
                f"deploy that never went live, or whose image has been pruned, "
                f"cannot be rolled back to."
            )
        logger.info(f"Rolled {service_id} back to {deploy_id}")
        return result

    # -- service creation ----------------------------------------------

    @retry_render_operation
    def create_service(
        self,
        name: str,
        service_type: str,
        repo: str,
        owner_id: Optional[str] = None,
        branch: str = "master",
        runtime: str = "python",
        plan: str = "starter",
        region: str = "oregon",
        build_command: str = "",
        start_command: str = "",
        root_dir: str = "",
        env_vars: Optional[Dict[str, str]] = None,
        auto_deploy: bool = True,
        health_check_path: str = "",
        **extra: Any,
    ) -> Dict[str, Any]:
        """
        Create a new Render service.

        ⚠ **This starts a recurring monthly charge.** Render bills per service
        per plan, there is no billing endpoint to check the effect against, and
        nothing on the platform reminds anyone that a service exists. Treat
        creation as a spend decision with a named owner.

        Prefer a Blueprint where the project has one: a service declared in
        ``render.yaml`` is reviewable in git and reproducible, whereas one
        created through the API exists only on the platform. Creating an API
        service for a project that already has a Blueprint produces a second,
        un-declared service alongside the declared one — which is a bill nobody
        is looking at.

        Args:
            name: Service name. Also the default ``onrender.com`` hostname.
            service_type: "web_service", "background_worker", "private_service",
                "static_site" or "cron_job".
            repo: GitHub clone URL.
            owner_id: Workspace to create in. Defaults to this key's workspace.
            branch: Branch to deploy. Default "master".
            runtime: "python", "node", "docker", "ruby", "go", "rust", "elixir",
                "image".
            plan: Instance plan — the monthly cost. Default "starter".
            region: Render region. Default "oregon", where everything else of
                ours already runs; a service in another region pays cross-region
                latency to reach the same database.
            build_command: e.g. "pip install -r requirements.txt".
            start_command: e.g. "gunicorn app:app --bind 0.0.0.0:$PORT".
            root_dir: Subdirectory to build from, for a monorepo.
            env_vars: ``{key: value}`` set at creation.
            auto_deploy: Deploy on every push to the branch. Default True.
            health_check_path: Path Render probes, e.g. "/health". Strongly
                worth setting on a web service — without one, Render calls a
                process "up" if it merely accepted the port.
            **extra: Further ``serviceDetails`` fields.

        Returns:
            The created service object, with its first deploy already started.

        Raises:
            WriteError: If Render rejects the creation.
        """
        specific: Dict[str, Any] = {}
        if build_command:
            specific["buildCommand"] = build_command
        if start_command:
            specific["startCommand"] = start_command

        details: Dict[str, Any] = {
            "runtime": runtime,
            "plan": plan,
            "region": region,
            "envSpecificDetails": specific,
        }
        if health_check_path:
            details["healthCheckPath"] = health_check_path
        details.update(extra)

        body: Dict[str, Any] = {
            "type": service_type,
            "name": name,
            "ownerId": owner_id or self.owner_id(),
            "repo": repo,
            "branch": branch,
            "autoDeploy": "yes" if auto_deploy else "no",
            "serviceDetails": details,
        }
        if root_dir:
            body["rootDir"] = root_dir
        if env_vars:
            body["envVars"] = [
                {"key": k, "value": v} for k, v in env_vars.items()
            ]

        try:
            result = self._request("POST", "/services", json_body=body)
        except ConnectionError as e:
            raise WriteError(f"Render rejected creating {name!r}: {e}") from e
        if not result:
            raise WriteError(f"Render refused to create {name!r}")

        service = result.get("service", result)
        logger.info(
            f"Created Render service {name!r} ({service.get('id')}), plan {plan}"
        )
        return service

    # -- other resources -----------------------------------------------

    @retry_render_operation
    def list_postgres(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        List managed Postgres databases in the workspace.

        Part of the cost picture and easy to forget: a database bills like a
        service and outlives the thing that needed it. Also worth watching
        ``expiresAt`` — a free-tier database is deleted on that date, not
        downgraded.

        Args:
            limit: Maximum databases to return.

        Returns:
            A list of database objects.
        """
        return list(self._paginate("/postgres", _ITEM_KEYS["postgres"], limit=limit))

    @retry_render_operation
    def list_blueprints(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        List Blueprints (``render.yaml``-managed groups) in the workspace.

        Worth reading before any env-var or plan write: a Blueprint whose
        ``autoSync`` is true re-applies the file on every push to its branch,
        which is what silently reverts an API write. ``status`` reports whether
        the platform currently matches the file.

        Args:
            limit: Maximum Blueprints to return.

        Returns:
            A list of Blueprint objects with ``name``, ``repo``, ``branch``,
            ``path``, ``autoSync``, ``status`` and ``lastSync``.
        """
        return list(
            self._paginate("/blueprints", _ITEM_KEYS["blueprints"], limit=limit)
        )

    # -- blueprint awareness -------------------------------------------

    def _blueprint_managed_keys(self) -> set:
        """Keys the Blueprint declares with a literal value (cached)."""
        if self._blueprint_path is None:
            return set()
        if self._blueprint_keys is None:
            self._blueprint_keys = blueprint_literal_keys(self._blueprint_path)
        return self._blueprint_keys


def blueprint_literal_keys(blueprint_path: str) -> set:
    """
    Read a ``render.yaml`` and return the env var keys it owns.

    "Owns" means declared with a literal ``value:`` — those are re-applied on
    every Blueprint sync and must not be written through the API. Keys declared
    ``sync: false`` are deliberately excluded: the Blueprint names them but
    never supplies a value, Render preserves whatever is set, and writing them
    through the API is the supported way to rotate a secret.

    Args:
        blueprint_path: Path to the render.yaml file.

    Returns:
        The set of Blueprint-owned key names across every service in the file.
        An empty set if the file does not exist.

    Raises:
        ConfigurationError: If PyYAML is not installed.
    """
    try:
        import yaml
    except ImportError as e:  # pragma: no cover - depends on install extras
        from ..exceptions import ConfigurationError

        raise ConfigurationError(
            "Reading a render.yaml needs PyYAML: pip install pyyaml"
        ) from e

    try:
        with open(blueprint_path, "r", encoding="utf-8") as fh:
            spec = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        logger.warning(f"Blueprint {blueprint_path} not found; no keys are guarded")
        return set()

    keys = set()
    for service in spec.get("services") or []:
        for var in service.get("envVars") or []:
            if not isinstance(var, dict) or "key" not in var:
                continue
            # sync:false means "Blueprint declares the key, dashboard holds the
            # value" — Render never overwrites those, so they are writable.
            if var.get("sync") is False:
                continue
            if "value" in var:
                keys.add(var["key"])
    return keys


def _parse_retry_after(headers: Dict[str, str]) -> int:
    """
    Extract a retry-after duration in seconds from a Render 429 response.

    Render sends ``Ratelimit-Reset`` as a UTC epoch timestamp rather than a
    delta, so it has to be differenced against now. Falls back to the standard
    Retry-After header, then to 60s.
    """
    reset = headers.get("Ratelimit-Reset") or headers.get("ratelimit-reset")
    if reset:
        try:
            return max(int(reset) - int(time.time()), 1)
        except ValueError:
            pass

    retry_after = headers.get("Retry-After")
    if retry_after:
        try:
            return int(retry_after)
        except ValueError:
            pass

    return 60
