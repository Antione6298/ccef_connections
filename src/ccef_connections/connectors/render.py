"""
Render connector for CCEF connections library.

Render hosts our internal Flask tools (the first is ep-roving's director
review UI). This is the transport layer for its REST API: services, deploys,
build/deploy logs, custom domains, and environment variables — the things you
would otherwise click through the dashboard for.

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
}


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
    """

    def __init__(
        self,
        credential_name: str = "RENDER_API_KEY",
        blueprint_path: Optional[str] = None,
    ) -> None:
        super().__init__()
        self._credential_name = credential_name
        self._blueprint_path = blueprint_path
        self._api_key: Optional[str] = None
        self._blueprint_keys: Optional[set] = None

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
    ) -> Optional[Any]:
        """
        Central HTTP method with auth headers and standard error mapping.

        Returns parsed JSON, or None for 204/404.

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
                timeout=30,
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
