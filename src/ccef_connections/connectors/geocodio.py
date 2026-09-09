"""
Geocodio connector for CCEF connections library.

This module provides forward and reverse geocoding via the Geocodio API v1.10.
Supports single-address lookups and batch operations (up to 10,000 addresses
per request). Coverage: US and Canada (forward + reverse); Mexico (forward only).

Authentication uses a plain API key passed as the ``api_key`` query parameter
on every request. The key is read from the ``GEOCODIO_API_KEY_PASSWORD``
environment variable.
"""

import logging
from typing import Any, Dict, List, Optional, Union

import requests

from ..core.base import BaseConnection
from ..core.retry import retry_geocodio_operation
from ..exceptions import ConnectionError, CredentialError, RateLimitError

logger = logging.getLogger(__name__)

GEOCODIO_API_BASE = "https://api.geocod.io/v1.10"


class GeocodioConnector(BaseConnection):
    """
    Geocodio connector for forward and reverse geocoding.

    Provides single and batch geocoding against the Geocodio API v1.10.
    Supports optional field appends (congressional districts, state
    legislatures, census data, timezones, school districts, etc.).

    The API key is read from the ``GEOCODIO_API_KEY_PASSWORD`` environment
    variable (plain string, not JSON).

    Examples:
        >>> connector = GeocodioConnector()
        >>> connector.connect()
        >>> result = connector.geocode("1600 Pennsylvania Ave NW, Washington, DC")
        >>> print(result["results"][0]["location"])
        {'lat': 38.89767, 'lng': -77.03655}

        >>> results = connector.batch_geocode(
        ...     ["1600 Pennsylvania Ave NW, DC", "350 Fifth Ave, New York, NY"],
        ...     fields=["cd", "stateleg"],
        ... )
    """

    def __init__(self) -> None:
        """Initialize the Geocodio connector."""
        super().__init__()
        self._api_key: Optional[str] = None

    def connect(self) -> None:
        """
        Load the Geocodio API key and mark the connector as connected.

        Does not make a live API call; key validity is verified on first use.

        Raises:
            CredentialError: If ``GEOCODIO_API_KEY_PASSWORD`` is not set
            ConnectionError: If credential loading fails for any other reason
        """
        try:
            self._api_key = self._credential_manager.get_geocodio_key()
            self._is_connected = True
            logger.info("Successfully connected to Geocodio")
        except CredentialError:
            # Re-raised as-is: a missing credential is not a connection
            # failure, and the two are sibling classes so wrapping meant
            # `except CredentialError` could never fire. Matches the docstring
            # above and SheetsConnector/BigQueryConnector/etc.
            logger.error("Failed to connect to Geocodio: credentials missing")
            raise
        except Exception as e:
            logger.error(f"Failed to connect to Geocodio: {str(e)}")
            raise ConnectionError(f"Failed to connect to Geocodio: {str(e)}") from e

    def disconnect(self) -> None:
        """Clear the Geocodio API key and connection state."""
        self._api_key = None
        self._is_connected = False
        logger.debug("Disconnected from Geocodio")

    def health_check(self) -> bool:
        """
        Check whether the connector is connected and has a non-empty API key.

        Returns:
            True if connected with a valid-looking key, False otherwise.
        """
        return bool(self._is_connected and self._api_key)

    # ── Internal helpers ──────────────────────────────────────────────

    def _ensure_connected(self) -> None:
        """Auto-connect if not already connected."""
        if not self._is_connected:
            self.connect()

    def _base_params(self, fields: Optional[List[str]] = None) -> Dict[str, str]:
        """
        Build base query parameters (api_key + optional fields).

        Connects first if necessary. This ordering is load-bearing: every public
        method builds its params *before* calling ``_request`` (which is where
        ``_ensure_connected`` used to be reached first), so on a fresh connector
        that had never been explicitly ``connect()``-ed, ``self._api_key`` was
        still ``None`` when it was read here. ``requests`` silently *drops*
        query parameters whose value is ``None``, so the request went out with
        no ``api_key`` at all and Geocodio answered ``403 {"error": "Invalid API
        key"}`` — pointing every investigation at the credential instead of the
        call order. Callers that happened to call ``connect()`` or
        ``health_check()`` first were unaffected, which is why this survived.

        Args:
            fields: Optional list of Geocodio field appends (e.g. ``["cd", "stateleg"]``)

        Returns:
            Dict of query parameters

        Raises:
            ConnectionError: If no API key is available after connecting.
        """
        self._ensure_connected()
        if not self._api_key:
            raise ConnectionError(
                "Geocodio API key is unavailable after connect(); refusing to send an "
                "unauthenticated request (it would return a misleading 403 "
                '"Invalid API key")'
            )
        params: Dict[str, str] = {"api_key": self._api_key}
        if fields:
            params["fields"] = ",".join(fields)
        return params

    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Make an authenticated request to the Geocodio API.

        Args:
            method: HTTP method (``"GET"`` or ``"POST"``)
            endpoint: Path relative to base URL (e.g. ``"/geocode"``)
            params: Query parameters (api_key is included automatically)
            json_body: JSON request body for POST requests

        Returns:
            Parsed JSON response dict

        Raises:
            RateLimitError: On HTTP 429
            ConnectionError: On any other HTTP error or network failure
        """
        self._ensure_connected()
        url = f"{GEOCODIO_API_BASE}{endpoint}"

        try:
            resp = requests.request(
                method,
                url,
                params=params,
                json=json_body,
                timeout=60,
            )
        except requests.RequestException as e:
            raise ConnectionError(f"Geocodio request failed: {e}") from e

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 60))
            raise RateLimitError(
                f"Geocodio rate limit exceeded, retry after {retry_after}s",
                retry_after=retry_after,
            )

        if resp.status_code >= 400:
            raise ConnectionError(
                f"Geocodio API error {resp.status_code}: {resp.text}"
            )

        return resp.json()

    # ── Forward geocoding ─────────────────────────────────────────────

    @retry_geocodio_operation
    def geocode(
        self,
        address: str,
        fields: Optional[List[str]] = None,
        limit: int = 1,
    ) -> Dict[str, Any]:
        """
        Geocode a single address (address → coordinates).

        Args:
            address: Full address string to geocode
            fields: Optional Geocodio field appends, e.g. ``["cd", "stateleg",
                "census", "timezone", "school", "zip4"]``
            limit: Maximum number of candidate results to return (default 1)

        Returns:
            Geocodio response dict with keys ``"input"`` and ``"results"``.
            Each result in ``"results"`` contains ``"formatted_address"``,
            ``"location"`` (``{"lat": ..., "lng": ...}``), ``"accuracy"``,
            ``"accuracy_type"``, and ``"source"``.

        Raises:
            RateLimitError: If the account quota is exceeded
            ConnectionError: If the request fails or the address is invalid

        Examples:
            >>> result = connector.geocode("1600 Pennsylvania Ave NW, DC")
            >>> lat = result["results"][0]["location"]["lat"]
            >>> lng = result["results"][0]["location"]["lng"]
        """
        params = self._base_params(fields)
        params["q"] = address
        params["limit"] = str(limit)
        return self._request("GET", "/geocode", params=params)

    @retry_geocodio_operation
    def batch_geocode(
        self,
        addresses: Union[List[str], Dict[str, str]],
        fields: Optional[List[str]] = None,
        limit: int = 1,
    ) -> Dict[str, Any]:
        """
        Geocode up to 10,000 addresses in a single request.

        Args:
            addresses: Either a list of address strings, or a dict mapping
                arbitrary keys to address strings. Using a dict lets you
                correlate results back to your own identifiers.
            fields: Optional Geocodio field appends
            limit: Maximum candidate results per address (default 1)

        Returns:
            Geocodio response dict with a ``"results"`` key. When
            ``addresses`` is a list, results are indexed by position; when
            it is a dict, results are keyed by your original keys. Each
            value has ``"query"`` and ``"response"`` sub-keys.

        Raises:
            RateLimitError: If the account quota is exceeded
            ConnectionError: If the request fails

        Examples:
            >>> results = connector.batch_geocode(
            ...     ["1600 Pennsylvania Ave NW, DC", "350 Fifth Ave, NY"],
            ... )
            >>> for item in results["results"]:
            ...     print(item["response"]["results"][0]["location"])
        """
        params = self._base_params(fields)
        params["limit"] = str(limit)
        return self._request("POST", "/geocode", params=params, json_body=addresses)

    # ── Reverse geocoding ─────────────────────────────────────────────

    @retry_geocodio_operation
    def reverse_geocode(
        self,
        lat: float,
        lng: float,
        fields: Optional[List[str]] = None,
        limit: int = 1,
    ) -> Dict[str, Any]:
        """
        Reverse geocode a single coordinate pair (coordinates → address).

        Args:
            lat: Latitude (decimal degrees)
            lng: Longitude (decimal degrees)
            fields: Optional Geocodio field appends
            limit: Maximum number of candidate results (default 1)

        Returns:
            Geocodio response dict with ``"results"`` list. Each result
            contains ``"formatted_address"``, ``"location"``,
            ``"accuracy"``, ``"accuracy_type"``, and ``"source"``.

        Raises:
            RateLimitError: If the account quota is exceeded
            ConnectionError: If the request fails

        Examples:
            >>> result = connector.reverse_geocode(38.8976, -77.0366)
            >>> print(result["results"][0]["formatted_address"])
            '1600 Pennsylvania Ave NW, Washington, DC 20500'
        """
        params = self._base_params(fields)
        params["q"] = f"{lat},{lng}"
        params["limit"] = str(limit)
        return self._request("GET", "/reverse", params=params)

    @retry_geocodio_operation
    def batch_reverse_geocode(
        self,
        coordinates: Union[List[str], Dict[str, str]],
        fields: Optional[List[str]] = None,
        limit: int = 1,
    ) -> Dict[str, Any]:
        """
        Reverse geocode up to 10,000 coordinate pairs in a single request.

        Args:
            coordinates: Either a list of ``"lat,lng"`` strings, or a dict
                mapping arbitrary keys to ``"lat,lng"`` strings.
            fields: Optional Geocodio field appends
            limit: Maximum candidate results per coordinate (default 1)

        Returns:
            Geocodio response dict with a ``"results"`` key structured the
            same way as :meth:`batch_geocode`.

        Raises:
            RateLimitError: If the account quota is exceeded
            ConnectionError: If the request fails

        Examples:
            >>> results = connector.batch_reverse_geocode(
            ...     ["38.8976,-77.0366", "40.7484,-73.9967"],
            ... )
        """
        params = self._base_params(fields)
        params["limit"] = str(limit)
        return self._request("POST", "/reverse", params=params, json_body=coordinates)
