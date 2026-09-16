"""
Action Network connector for CCEF connections library.

This module provides full read/write access to the Action Network API v2,
covering People, Tags, Taggings, Events, Attendances, Petitions, Signatures,
Forms, Submissions, Fundraising Pages, Donations, Lists, Messages (including
the send/schedule helpers), Wrappers, Custom Fields, and Event Campaigns.

Uses a static API key in the OSDI-API-Token header.  Resources follow the
OSDI/HAL+JSON format with pagination via ``_links.next.href``.
"""

import logging
from typing import Any, Dict, List, Optional

import requests

from ..core.base import BaseConnection
from ..core.retry import retry_action_network_operation
from ..exceptions import (
    AuthenticationError,
    ConnectionError,
    CredentialError,
    RateLimitError,
)

logger = logging.getLogger(__name__)

ACTION_NETWORK_API_BASE = "https://actionnetwork.org/api/v2"


class ActionNetworkConnector(BaseConnection):
    """
    Action Network connector for activist CRM operations.

    Provides full CRUD access to Action Network resources using the
    OSDI/HAL+JSON API v2 with a static API key.

    Examples:
        >>> connector = ActionNetworkConnector()
        >>> connector.connect()
        >>> people = connector.list_people()
        >>> connector.create_person(
        ...     email="activist@example.com",
        ...     given_name="Jane",
        ...     family_name="Doe",
        ...     tags=["volunteer"],
        ... )
    """

    def __init__(self) -> None:
        """Initialize the Action Network connector."""
        super().__init__()
        self._api_key: Optional[str] = None

    def connect(self) -> None:
        """
        Establish connection to Action Network by validating the API key.

        Raises:
            CredentialError: If the API key is missing
            ConnectionError: If connection setup fails
        """
        try:
            self._api_key = self._credential_manager.get_action_network_key()
            self._is_connected = True
            logger.info("Successfully connected to Action Network")
        except CredentialError:
            # Re-raised as-is: a missing credential is not a connection
            # failure, and the two are sibling classes so wrapping meant
            # `except CredentialError` could never fire. Matches the docstring
            # above and SheetsConnector/BigQueryConnector/etc.
            logger.error("Failed to connect to Action Network: credentials missing")
            raise
        except Exception as e:
            logger.error(f"Failed to connect to Action Network: {str(e)}")
            raise ConnectionError(
                f"Failed to connect to Action Network: {str(e)}"
            ) from e

    def disconnect(self) -> None:
        """Clear the Action Network connection."""
        self._api_key = None
        self._is_connected = False
        logger.debug("Disconnected from Action Network")

    def health_check(self) -> bool:
        """
        Check connection health by hitting the API Entry Point.

        Returns:
            True if connected and API responds, False otherwise
        """
        if not self._is_connected or not self._api_key:
            return False
        try:
            self._request("GET", "")
            return True
        except Exception:
            return False

    # -- HTTP helpers ---------------------------------------------------------

    def _get_headers(self) -> Dict[str, str]:
        """Return request headers with the API key."""
        return {
            "OSDI-API-Token": self._api_key or "",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Central HTTP method with error handling.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE)
            path: API path relative to /api/v2 (e.g. '/people')
            params: Query parameters
            json_body: JSON request body

        Returns:
            Parsed JSON response, or None for 204 No Content

        Raises:
            AuthenticationError: On 401 responses
            RateLimitError: On 429 responses
            ConnectionError: On other HTTP errors or network failures
        """
        if not self._is_connected and not self._api_key:
            self.connect()

        url = f"{ACTION_NETWORK_API_BASE}{path}"

        try:
            resp = requests.request(
                method,
                url,
                headers=self._get_headers(),
                params=params,
                json=json_body,
                timeout=30,
            )
        except requests.RequestException as e:
            raise ConnectionError(
                f"Action Network API request failed: {e}"
            ) from e

        if resp.status_code == 401:
            raise AuthenticationError(
                f"Action Network authentication failed ({resp.status_code}): {resp.text}"
            )

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 1))
            raise RateLimitError(
                f"Action Network rate limit exceeded, retry after {retry_after}s",
                retry_after=retry_after,
            )

        if resp.status_code == 204:
            return None

        if resp.status_code >= 400:
            raise ConnectionError(
                f"Action Network API error {resp.status_code}: {resp.text}"
            )

        return resp.json()

    def _paginate(
        self,
        path: str,
        resource_key: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Follow ``_links.next.href`` across pages and collect resources.

        Args:
            path: Initial API path
            resource_key: Key inside ``_embedded`` (e.g. 'osdi:people')
            params: Query parameters for the first request

        Returns:
            Combined list of all resources across pages
        """
        results: List[Dict[str, Any]] = []
        current_path = path
        current_params = params

        while True:
            data = self._request("GET", current_path, params=current_params)
            if data is None:
                break

            embedded = data.get("_embedded", {})
            if resource_key in embedded:
                results.extend(embedded[resource_key])

            next_link = data.get("_links", {}).get("next", {}).get("href")
            if not next_link:
                break

            if next_link.startswith(ACTION_NETWORK_API_BASE):
                current_path = next_link[len(ACTION_NETWORK_API_BASE):]
            else:
                current_path = next_link
            current_params = None

        return results

    # -- People ---------------------------------------------------------------

    @retry_action_network_operation
    def list_people(self, **filters: Any) -> List[Dict[str, Any]]:
        """
        List all people, paginated.

        Args:
            **filters: Query parameters (e.g. filter, page)

        Returns:
            List of person resources
        """
        return self._paginate("/people", "osdi:people", params=filters or None)

    @retry_action_network_operation
    def get_person(self, person_id: str) -> Dict[str, Any]:
        """
        Get a single person by ID.

        Args:
            person_id: Action Network person UUID

        Returns:
            Person resource dict
        """
        result = self._request("GET", f"/people/{person_id}")
        return result or {}

    @retry_action_network_operation
    def create_person(
        self,
        email: str,
        given_name: Optional[str] = None,
        family_name: Optional[str] = None,
        tags: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Create (or update) a person via the Person Signup Helper.

        Uses ``POST /people`` which deduplicates by email address.
        Supports inline tagging via ``add_tags``.

        Args:
            email: Email address (used for dedup)
            given_name: First name
            family_name: Last name
            tags: List of tag names to apply
            **kwargs: Additional person fields

        Returns:
            Created/updated person resource
        """
        person: Dict[str, Any] = {
            "email_addresses": [{"address": email}],
        }
        if given_name:
            person["given_name"] = given_name
        if family_name:
            person["family_name"] = family_name
        person.update(kwargs)

        body: Dict[str, Any] = {"person": person}
        if tags:
            body["add_tags"] = tags

        result = self._request("POST", "/people", json_body=body)
        return result or {}

    @retry_action_network_operation
    def update_person(
        self, person_id: str, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Update a person.

        Args:
            person_id: Action Network person UUID
            fields: Fields to update

        Returns:
            Updated person resource
        """
        result = self._request("PUT", f"/people/{person_id}", json_body=fields)
        return result or {}

    @retry_action_network_operation
    def unsubscribe_person(self, person_id: str) -> Dict[str, Any]:
        """
        Unsubscribe a person from the email list associated with this API key.

        Uses ``PUT /people/{id}`` to set the primary email status to
        ``"unsubscribed"``.  In a federated/networked Action Network
        structure, the unsubscription is **scoped to the group whose API
        key you are using** — it does not propagate up the network or
        affect other groups.

        Args:
            person_id: Action Network person UUID

        Returns:
            Updated person resource
        """
        result = self._request(
            "PUT",
            f"/people/{person_id}",
            json_body={
                "email_addresses": [{"status": "unsubscribed"}],
            },
        )
        return result or {}

    @retry_action_network_operation
    def unsubscribe_person_by_email(self, email: str) -> Dict[str, Any]:
        """
        Unsubscribe a person by email address (no UUID needed).

        Uses the Person Signup Helper (``POST /people``) which
        deduplicates by email address.  If the person exists, their
        email status is set to ``"unsubscribed"``.  If the person does
        not exist, they are added in an unsubscribed state.

        In a federated/networked Action Network structure, the
        unsubscription is **scoped to the group whose API key you are
        using**.

        Args:
            email: Email address of the person to unsubscribe

        Returns:
            Person resource (existing or newly created)
        """
        result = self._request(
            "POST",
            "/people",
            json_body={
                "person": {
                    "email_addresses": [
                        {"address": email, "status": "unsubscribed"},
                    ],
                },
            },
        )
        return result or {}

    # -- Phone / SMS ops --------------------------------------------------------
    #
    # Live-verified facts these methods encode (tatango-sync AN live test,
    # 2026-06-10, docs/an_live_test_plan.md):
    # - AN stores phones as 11 digits with leading `1`, no `+`.
    # - The `phone_number eq` filter is exact-match, ~1 s, and freshly indexed.
    # - A phone PUT that carries `number` REPLACES the person's phone (the
    #   array stays single-element — AN effectively holds one phone per
    #   person). A status-only PUT is the safe write shape.
    # - Phone and email channels are independent: a phone-status write leaves
    #   email status untouched.

    @retry_action_network_operation
    def find_people_by_phone(self, phone_number: str) -> List[Dict[str, Any]]:
        """
        Find people by exact phone-number match.

        Args:
            phone_number: Phone in AN's stored format — 11 digits with a
                leading ``1``, no ``+`` (e.g. ``13125550123``)

        Returns:
            List of matching person resources (typically 0 or 1)
        """
        return self._paginate(
            "/people",
            "osdi:people",
            params={"filter": f"phone_number eq '{phone_number}'"},
        )

    @retry_action_network_operation
    def get_person_phone(self, person_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a person's phone entry (number, status, number_type).

        AN effectively holds one phone per person, so this returns the
        first (only) ``phone_numbers`` entry.

        Args:
            person_id: Action Network person UUID

        Returns:
            The phone entry dict, or None if the person has no phone
        """
        result = self._request("GET", f"/people/{person_id}") or {}
        phones = result.get("phone_numbers") or []
        return phones[0] if phones else None

    @retry_action_network_operation
    def set_person_phone_status(
        self, person_id: str, status: str
    ) -> Dict[str, Any]:
        """
        Set a person's SMS subscription status (status-only PUT).

        Sends ``{"phone_numbers": [{"status": ...}]}`` with **no number** —
        including a number in a phone PUT replaces the person's phone
        outright (live-verified), so this method never accepts one. To
        change a number, that destructive semantic must be chosen
        explicitly via :meth:`update_person`.

        The write is scoped to the group whose API key is in use; email
        status is untouched (channels are independent).

        Args:
            person_id: Action Network person UUID
            status: ``"subscribed"`` or ``"unsubscribed"``

        Returns:
            Updated person resource
        """
        result = self._request(
            "PUT",
            f"/people/{person_id}",
            json_body={"phone_numbers": [{"status": status}]},
        )
        return result or {}

    # -- Tags -----------------------------------------------------------------

    @retry_action_network_operation
    def list_tags(self) -> List[Dict[str, Any]]:
        """List all tags, paginated."""
        return self._paginate("/tags", "osdi:tags")

    @retry_action_network_operation
    def get_tag(self, tag_id: str) -> Dict[str, Any]:
        """Get a single tag by ID."""
        result = self._request("GET", f"/tags/{tag_id}")
        return result or {}

    @retry_action_network_operation
    def create_tag(self, name: str) -> Dict[str, Any]:
        """
        Create a new tag.

        Args:
            name: Tag name

        Returns:
            Created tag resource
        """
        result = self._request("POST", "/tags", json_body={"name": name})
        return result or {}

    # -- Taggings -------------------------------------------------------------

    @retry_action_network_operation
    def list_taggings(self, tag_id: str) -> List[Dict[str, Any]]:
        """List all taggings for a tag, paginated."""
        return self._paginate(
            f"/tags/{tag_id}/taggings", "osdi:taggings"
        )

    @retry_action_network_operation
    def add_tagging(
        self, tag_id: str, person_identifiers: List[str]
    ) -> Dict[str, Any]:
        """
        Tag a person (create a tagging).

        Args:
            tag_id: Tag UUID
            person_identifiers: List of person identifier URIs

        Returns:
            Created tagging resource
        """
        body: Dict[str, Any] = {
            "_links": {
                "osdi:person": {
                    "href": person_identifiers[0],
                },
            },
        }
        if len(person_identifiers) > 1:
            body["_links"]["osdi:person"] = [
                {"href": pid} for pid in person_identifiers
            ]

        result = self._request(
            "POST", f"/tags/{tag_id}/taggings", json_body=body
        )
        return result or {}

    @retry_action_network_operation
    def delete_tagging(self, tag_id: str, tagging_id: str) -> None:
        """
        Remove a tagging.

        Args:
            tag_id: Tag UUID
            tagging_id: Tagging UUID
        """
        self._request("DELETE", f"/tags/{tag_id}/taggings/{tagging_id}")

    # -- Events ---------------------------------------------------------------

    @retry_action_network_operation
    def list_events(self) -> List[Dict[str, Any]]:
        """List all events, paginated."""
        return self._paginate("/events", "osdi:events")

    @retry_action_network_operation
    def get_event(self, event_id: str) -> Dict[str, Any]:
        """Get a single event by ID."""
        result = self._request("GET", f"/events/{event_id}")
        return result or {}

    @retry_action_network_operation
    def create_event(
        self, title: str, start_date: Optional[str] = None, **kwargs: Any
    ) -> Dict[str, Any]:
        """
        Create an event.

        Args:
            title: Event title
            start_date: ISO-8601 start date/time
            **kwargs: Additional event fields

        Returns:
            Created event resource
        """
        body: Dict[str, Any] = {"title": title}
        if start_date:
            body["start_date"] = start_date
        body.update(kwargs)
        result = self._request("POST", "/events", json_body=body)
        return result or {}

    @retry_action_network_operation
    def update_event(
        self, event_id: str, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update an event."""
        result = self._request("PUT", f"/events/{event_id}", json_body=fields)
        return result or {}

    # -- Attendances ----------------------------------------------------------

    @retry_action_network_operation
    def list_attendances(self, event_id: str) -> List[Dict[str, Any]]:
        """List attendances for an event, paginated."""
        return self._paginate(
            f"/events/{event_id}/attendances", "osdi:attendances"
        )

    @retry_action_network_operation
    def get_attendance(
        self, event_id: str, attendance_id: str
    ) -> Dict[str, Any]:
        """Get a single attendance record."""
        result = self._request(
            "GET", f"/events/{event_id}/attendances/{attendance_id}"
        )
        return result or {}

    @retry_action_network_operation
    def create_attendance(
        self, event_id: str, person_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Record an attendance for an event.

        Args:
            event_id: Event UUID
            person_data: Person data dict (embedded person signup helper)

        Returns:
            Created attendance resource
        """
        result = self._request(
            "POST", f"/events/{event_id}/attendances", json_body=person_data
        )
        return result or {}

    # -- Petitions ------------------------------------------------------------

    @retry_action_network_operation
    def list_petitions(self) -> List[Dict[str, Any]]:
        """List all petitions, paginated."""
        return self._paginate("/petitions", "osdi:petitions")

    @retry_action_network_operation
    def get_petition(self, petition_id: str) -> Dict[str, Any]:
        """Get a single petition by ID."""
        result = self._request("GET", f"/petitions/{petition_id}")
        return result or {}

    @retry_action_network_operation
    def create_petition(self, title: str, **kwargs: Any) -> Dict[str, Any]:
        """
        Create a petition.

        Args:
            title: Petition title
            **kwargs: Additional petition fields

        Returns:
            Created petition resource
        """
        body: Dict[str, Any] = {"title": title}
        body.update(kwargs)
        result = self._request("POST", "/petitions", json_body=body)
        return result or {}

    @retry_action_network_operation
    def update_petition(
        self, petition_id: str, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update a petition."""
        result = self._request(
            "PUT", f"/petitions/{petition_id}", json_body=fields
        )
        return result or {}

    # -- Signatures -----------------------------------------------------------

    @retry_action_network_operation
    def list_signatures(self, petition_id: str) -> List[Dict[str, Any]]:
        """List signatures for a petition, paginated."""
        return self._paginate(
            f"/petitions/{petition_id}/signatures", "osdi:signatures"
        )

    @retry_action_network_operation
    def get_signature(
        self, petition_id: str, signature_id: str
    ) -> Dict[str, Any]:
        """Get a single signature."""
        result = self._request(
            "GET", f"/petitions/{petition_id}/signatures/{signature_id}"
        )
        return result or {}

    @retry_action_network_operation
    def create_signature(
        self, petition_id: str, person_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Create a signature on a petition.

        Args:
            petition_id: Petition UUID
            person_data: Person data dict

        Returns:
            Created signature resource
        """
        result = self._request(
            "POST",
            f"/petitions/{petition_id}/signatures",
            json_body=person_data,
        )
        return result or {}

    @retry_action_network_operation
    def update_signature(
        self, petition_id: str, signature_id: str, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update a signature."""
        result = self._request(
            "PUT",
            f"/petitions/{petition_id}/signatures/{signature_id}",
            json_body=fields,
        )
        return result or {}

    # -- Forms ----------------------------------------------------------------

    @retry_action_network_operation
    def list_forms(self) -> List[Dict[str, Any]]:
        """List all forms, paginated."""
        return self._paginate("/forms", "osdi:forms")

    @retry_action_network_operation
    def get_form(self, form_id: str) -> Dict[str, Any]:
        """Get a single form by ID."""
        result = self._request("GET", f"/forms/{form_id}")
        return result or {}

    @retry_action_network_operation
    def create_form(self, title: str, **kwargs: Any) -> Dict[str, Any]:
        """
        Create a form.

        Args:
            title: Form title
            **kwargs: Additional form fields

        Returns:
            Created form resource
        """
        body: Dict[str, Any] = {"title": title}
        body.update(kwargs)
        result = self._request("POST", "/forms", json_body=body)
        return result or {}

    @retry_action_network_operation
    def update_form(
        self, form_id: str, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update a form."""
        result = self._request("PUT", f"/forms/{form_id}", json_body=fields)
        return result or {}

    # -- Submissions ----------------------------------------------------------

    @retry_action_network_operation
    def list_submissions(self, form_id: str) -> List[Dict[str, Any]]:
        """List submissions for a form, paginated."""
        return self._paginate(
            f"/forms/{form_id}/submissions", "osdi:submissions"
        )

    @retry_action_network_operation
    def get_submission(
        self, form_id: str, submission_id: str
    ) -> Dict[str, Any]:
        """Get a single submission."""
        result = self._request(
            "GET", f"/forms/{form_id}/submissions/{submission_id}"
        )
        return result or {}

    @retry_action_network_operation
    def create_submission(
        self, form_id: str, person_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Create a submission on a form.

        Args:
            form_id: Form UUID
            person_data: Person data dict

        Returns:
            Created submission resource
        """
        result = self._request(
            "POST", f"/forms/{form_id}/submissions", json_body=person_data
        )
        return result or {}

    # -- Fundraising Pages ----------------------------------------------------

    @retry_action_network_operation
    def list_fundraising_pages(self) -> List[Dict[str, Any]]:
        """List all fundraising pages, paginated."""
        return self._paginate(
            "/fundraising_pages", "osdi:fundraising_pages"
        )

    @retry_action_network_operation
    def get_fundraising_page(self, page_id: str) -> Dict[str, Any]:
        """Get a single fundraising page by ID."""
        result = self._request("GET", f"/fundraising_pages/{page_id}")
        return result or {}

    @retry_action_network_operation
    def create_fundraising_page(
        self, title: str, **kwargs: Any
    ) -> Dict[str, Any]:
        """
        Create a fundraising page.

        Args:
            title: Fundraising page title
            **kwargs: Additional fields

        Returns:
            Created fundraising page resource
        """
        body: Dict[str, Any] = {"title": title}
        body.update(kwargs)
        result = self._request(
            "POST", "/fundraising_pages", json_body=body
        )
        return result or {}

    @retry_action_network_operation
    def update_fundraising_page(
        self, page_id: str, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update a fundraising page."""
        result = self._request(
            "PUT", f"/fundraising_pages/{page_id}", json_body=fields
        )
        return result or {}

    # -- Donations ------------------------------------------------------------

    @retry_action_network_operation
    def list_donations(
        self, fundraising_page_id: str
    ) -> List[Dict[str, Any]]:
        """List donations for a fundraising page, paginated."""
        return self._paginate(
            f"/fundraising_pages/{fundraising_page_id}/donations",
            "osdi:donations",
        )

    @retry_action_network_operation
    def get_donation(
        self, fundraising_page_id: str, donation_id: str
    ) -> Dict[str, Any]:
        """Get a single donation."""
        result = self._request(
            "GET",
            f"/fundraising_pages/{fundraising_page_id}/donations/{donation_id}",
        )
        return result or {}

    @retry_action_network_operation
    def create_donation(
        self, fundraising_page_id: str, person_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Create a donation on a fundraising page.

        Args:
            fundraising_page_id: Fundraising page UUID
            person_data: Person + donation data dict

        Returns:
            Created donation resource
        """
        result = self._request(
            "POST",
            f"/fundraising_pages/{fundraising_page_id}/donations",
            json_body=person_data,
        )
        return result or {}

    # -- Lists ----------------------------------------------------------------

    @retry_action_network_operation
    def list_lists(self) -> List[Dict[str, Any]]:
        """List all lists (queries/segments), paginated."""
        return self._paginate("/lists", "osdi:lists")

    @retry_action_network_operation
    def get_list(self, list_id: str) -> Dict[str, Any]:
        """Get a single list by ID."""
        result = self._request("GET", f"/lists/{list_id}")
        return result or {}

    # -- Messages -------------------------------------------------------------

    @retry_action_network_operation
    def list_messages(self) -> List[Dict[str, Any]]:
        """List all messages, paginated."""
        return self._paginate("/messages", "osdi:messages")

    @retry_action_network_operation
    def get_message(self, message_id: str) -> Dict[str, Any]:
        """Get a single message by ID."""
        result = self._request("GET", f"/messages/{message_id}")
        return result or {}

    @retry_action_network_operation
    def create_message(
        self,
        subject: str,
        body: Optional[str] = None,
        targets: Optional[List[Dict[str, Any]]] = None,
        from_email: Optional[str] = None,
        reply_to: Optional[str] = None,
        wrapper_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Create a message. POSTing here always creates a **draft** — it shows
        up as an email in the Action Network UI but is never sent. Sending
        or scheduling delivery requires :meth:`send_message` /
        :meth:`schedule_message` as a separate call against the id this
        returns.

        AN requires ``subject``, ``body``, ``from``, and ``reply_to`` to send
        or schedule a message (creation itself only requires ``subject``).
        Without ``targets``, AN defaults to targeting the full list.
        Target/wrapper changes are rate-limited by AN to one POST/PUT every
        30 seconds.

        Args:
            subject: Message subject line
            body: Message HTML body
            targets: List of target dicts, each ``{"href": "<query-url>"}``
            from_email: Sender address (AN field name is the reserved word
                ``from``, so it can't be a plain kwarg)
            reply_to: Reply-to address
            wrapper_id: Email wrapper UUID to apply (builds the
                ``_links.osdi:wrapper`` href). Falls back to the
                organizer's default wrapper if omitted.
            **kwargs: Additional message fields (e.g. ``name``, ``tag_list``)

        Returns:
            Created message resource (draft; not yet sent)
        """
        payload: Dict[str, Any] = {"subject": subject}
        if body:
            payload["body"] = body
        if targets:
            payload["targets"] = targets
        if from_email:
            payload["from"] = from_email
        if reply_to:
            payload["reply_to"] = reply_to
        payload.update(kwargs)
        if wrapper_id:
            links = payload.setdefault("_links", {})
            links["osdi:wrapper"] = {
                "href": f"{ACTION_NETWORK_API_BASE}/wrappers/{wrapper_id}"
            }
        result = self._request("POST", "/messages", json_body=payload)
        return result or {}

    @retry_action_network_operation
    def update_message(
        self, message_id: str, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Update a draft message (PUT). Only ``draft``-status messages can be
        updated; changes to ``targets`` are rate-limited by AN to one
        PUT every 30 seconds, same as create.

        Args:
            message_id: Message UUID
            fields: Fields to update

        Returns:
            Updated message resource
        """
        result = self._request(
            "PUT", f"/messages/{message_id}", json_body=fields
        )
        return result or {}

    @retry_action_network_operation
    def send_message(self, message_id: str) -> Dict[str, Any]:
        """
        Trigger immediate delivery of a message via AN's send helper.

        Creating a message with :meth:`create_message` only ever produces a
        draft — this is the separate call AN requires to actually send it.
        AN requires the message to be in ``draft`` status with
        ``total_targeted`` greater than 0 (i.e. targeting has finished
        calculating and resolved to a non-empty audience); calling this too
        soon after create/update — while AN is still calculating targets —
        will fail.

        Args:
            message_id: Message UUID

        Returns:
            AN's send-helper response
        """
        result = self._request("POST", f"/messages/{message_id}/send")
        return result or {}

    @retry_action_network_operation
    def schedule_message(
        self, message_id: str, scheduled_start_date: str
    ) -> Dict[str, Any]:
        """
        Schedule a message for future delivery via AN's schedule helper.

        Same preconditions as :meth:`send_message` (``draft`` status,
        ``total_targeted`` > 0), plus ``scheduled_start_date`` must be in
        the future.

        Args:
            message_id: Message UUID
            scheduled_start_date: ISO-8601 UTC timestamp, e.g.
                ``"2026-10-01T12:00:00Z"``

        Returns:
            AN's schedule-helper response
        """
        result = self._request(
            "POST",
            f"/messages/{message_id}/schedule",
            json_body={"scheduled_start_date": scheduled_start_date},
        )
        return result or {}

    # -- Wrappers -------------------------------------------------------------

    @retry_action_network_operation
    def list_wrappers(self) -> List[Dict[str, Any]]:
        """List all email wrappers, paginated."""
        return self._paginate("/wrappers", "osdi:wrappers")

    @retry_action_network_operation
    def get_wrapper(self, wrapper_id: str) -> Dict[str, Any]:
        """Get a single wrapper by ID."""
        result = self._request("GET", f"/wrappers/{wrapper_id}")
        return result or {}

    @retry_action_network_operation
    def create_wrapper(self, **kwargs: Any) -> Dict[str, Any]:
        """
        Create an email wrapper.

        Args:
            **kwargs: Wrapper fields (header, footer, etc.)

        Returns:
            Created wrapper resource
        """
        result = self._request("POST", "/wrappers", json_body=kwargs)
        return result or {}

    @retry_action_network_operation
    def update_wrapper(
        self, wrapper_id: str, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update an email wrapper."""
        result = self._request(
            "PUT", f"/wrappers/{wrapper_id}", json_body=fields
        )
        return result or {}

    # -- Custom Fields (metadata) ---------------------------------------------

    @retry_action_network_operation
    def list_custom_fields(self) -> List[Dict[str, Any]]:
        """List all custom field definitions, paginated."""
        return self._paginate("/metadata", "osdi:metadata")

    @retry_action_network_operation
    def get_custom_field(self, field_id: str) -> Dict[str, Any]:
        """Get a single custom field definition by ID."""
        result = self._request("GET", f"/metadata/{field_id}")
        return result or {}

    @retry_action_network_operation
    def create_custom_field(
        self, name: str, format: str, **kwargs: Any
    ) -> Dict[str, Any]:
        """
        Create a custom field definition.

        Args:
            name: Field name
            format: Field format (e.g. 'text', 'number', 'boolean')
            **kwargs: Additional metadata fields

        Returns:
            Created metadata resource
        """
        body: Dict[str, Any] = {"name": name, "format": format}
        body.update(kwargs)
        result = self._request("POST", "/metadata", json_body=body)
        return result or {}

    @retry_action_network_operation
    def update_custom_field(
        self, field_id: str, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update a custom field definition."""
        result = self._request(
            "PUT", f"/metadata/{field_id}", json_body=fields
        )
        return result or {}

    # -- Event Campaigns ------------------------------------------------------

    @retry_action_network_operation
    def list_event_campaigns(self) -> List[Dict[str, Any]]:
        """List all event campaigns, paginated."""
        return self._paginate(
            "/event_campaigns", "action_network:event_campaigns"
        )

    @retry_action_network_operation
    def get_event_campaign(self, campaign_id: str) -> Dict[str, Any]:
        """Get a single event campaign by ID."""
        result = self._request("GET", f"/event_campaigns/{campaign_id}")
        return result or {}

    @retry_action_network_operation
    def create_event_campaign(
        self, title: str, **kwargs: Any
    ) -> Dict[str, Any]:
        """
        Create an event campaign.

        Args:
            title: Campaign title
            **kwargs: Additional campaign fields

        Returns:
            Created event campaign resource
        """
        body: Dict[str, Any] = {"title": title}
        body.update(kwargs)
        result = self._request(
            "POST", "/event_campaigns", json_body=body
        )
        return result or {}

    @retry_action_network_operation
    def update_event_campaign(
        self, campaign_id: str, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update an event campaign."""
        result = self._request(
            "PUT", f"/event_campaigns/{campaign_id}", json_body=fields
        )
        return result or {}

    @retry_action_network_operation
    def list_campaign_events(
        self, campaign_id: str
    ) -> List[Dict[str, Any]]:
        """List events in an event campaign, paginated."""
        return self._paginate(
            f"/event_campaigns/{campaign_id}/events", "osdi:events"
        )

    @retry_action_network_operation
    def create_campaign_event(
        self, campaign_id: str, event_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Create an event within an event campaign.

        Args:
            campaign_id: Event campaign UUID
            event_data: Event data dict

        Returns:
            Created event resource
        """
        result = self._request(
            "POST",
            f"/event_campaigns/{campaign_id}/events",
            json_body=event_data,
        )
        return result or {}
