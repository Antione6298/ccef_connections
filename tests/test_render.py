"""Tests for the Render connector."""

import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from ccef_connections.connectors.render import (
    RENDER_API_BASE,
    RenderConnector,
    _parse_retry_after,
    blueprint_literal_keys,
)
from ccef_connections.exceptions import (
    AuthenticationError,
    ConnectionError,
    CredentialError,
    RateLimitError,
    WriteError,
)


# -- Fixtures ----------------------------------------------------------------


FAKE_KEY = "rnd_fake_api_key_value"
SERVICE_ID = "srv-d8397fnavr4c739oi3kg"


def _make_response(status_code=200, json_data=None, text="", headers=None):
    """Create a mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = text
    resp.headers = headers or {}
    resp.json.return_value = json_data if json_data is not None else {}
    return resp


def _page(items, item_key, cursor=None):
    """Build a Render list page: [{<item_key>: {...}, "cursor": "..."}, ...].

    Render puts the cursor on every item rather than once per page, so the
    cursor to continue from is the last item's.
    """
    return [{item_key: item, "cursor": cursor or f"cur-{i}"} for i, item in enumerate(items)]


@pytest.fixture
def connector():
    """A RenderConnector with mocked credentials and no blueprint guard."""
    with patch.object(RenderConnector, "_credential_manager", create=True) as mock_cm:
        mock_cm.get_render_api_key.return_value = FAKE_KEY
        c = RenderConnector()
        c._credential_manager = mock_cm
        yield c


@pytest.fixture
def connected(connector):
    """A connector already 'connected' with a fake key."""
    connector._api_key = FAKE_KEY
    connector._is_connected = True
    return connector


@pytest.fixture
def blueprint(tmp_path):
    """A render.yaml shaped like ep-roving's: literal values and sync:false."""
    path = tmp_path / "render.yaml"
    path.write_text(
        """
services:
  - type: web
    name: ep-roving-review
    domains:
      - roving.example.org
    envVars:
      - key: APP_BASE_URL
        value: https://ep-roving-review.onrender.com
      - key: REDIRECT_HOSTS
        value: example.org
      - key: SECRET_KEY
        sync: false
      - key: BIGQUERY_CREDENTIALS_PASSWORD
        sync: false
""",
        encoding="utf-8",
    )
    return path


# -- Connection lifecycle ----------------------------------------------------


class TestConnection:
    def test_connect_loads_key(self, connector):
        connector.connect()
        assert connector._api_key == FAKE_KEY
        assert connector._is_connected is True

    def test_connect_propagates_missing_credential(self, connector):
        connector._credential_manager.get_render_api_key.side_effect = CredentialError("nope")
        with pytest.raises(CredentialError):
            connector.connect()

    def test_disconnect_clears_key(self, connected):
        connected.disconnect()
        assert connected._api_key is None
        assert connected._is_connected is False

    @patch("ccef_connections.connectors.render.requests.request")
    def test_health_check_true(self, mock_request, connected):
        mock_request.return_value = _make_response(200, [])
        assert connected.health_check() is True

    @patch("ccef_connections.connectors.render.requests.request")
    def test_health_check_false_on_error(self, mock_request, connected):
        mock_request.return_value = _make_response(401, text="bad key")
        assert connected.health_check() is False

    def test_health_check_false_when_disconnected(self, connector):
        assert connector.health_check() is False


# -- HTTP error mapping ------------------------------------------------------


class TestRequestErrorMapping:
    @patch("ccef_connections.connectors.render.requests.request")
    def test_uses_bearer_auth_and_base_url(self, mock_request, connected):
        mock_request.return_value = _make_response(200, {"id": SERVICE_ID})
        connected._request("GET", f"/services/{SERVICE_ID}")
        _, kwargs = mock_request.call_args
        assert mock_request.call_args[0][1] == f"{RENDER_API_BASE}/services/{SERVICE_ID}"
        assert kwargs["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"

    @patch("ccef_connections.connectors.render.requests.request")
    def test_401_is_authentication_error(self, mock_request, connected):
        mock_request.return_value = _make_response(401, text="unauthorized")
        with pytest.raises(AuthenticationError):
            connected._request("GET", "/services")

    @patch("ccef_connections.connectors.render.requests.request")
    def test_403_is_authentication_error(self, mock_request, connected):
        mock_request.return_value = _make_response(403, text="forbidden")
        with pytest.raises(AuthenticationError):
            connected._request("GET", "/services")

    @patch("ccef_connections.connectors.render.requests.request")
    def test_404_returns_none(self, mock_request, connected):
        mock_request.return_value = _make_response(404, text="not found")
        assert connected._request("GET", "/services/srv-nope") is None

    @patch("ccef_connections.connectors.render.requests.request")
    def test_204_returns_none(self, mock_request, connected):
        mock_request.return_value = _make_response(204)
        assert connected._request("DELETE", "/services/x/env-vars/Y") is None

    @patch("ccef_connections.connectors.render.requests.request")
    def test_500_is_connection_error(self, mock_request, connected):
        mock_request.return_value = _make_response(500, text="boom")
        with pytest.raises(ConnectionError):
            connected._request("GET", "/services")

    @patch("ccef_connections.connectors.render.requests.request")
    def test_network_failure_is_connection_error(self, mock_request, connected):
        mock_request.side_effect = requests.RequestException("dns")
        with pytest.raises(ConnectionError):
            connected._request("GET", "/services")

    @patch("ccef_connections.connectors.render.requests.request")
    def test_429_is_rate_limit_error_with_retry_after(self, mock_request, connected):
        reset = int(time.time()) + 30
        mock_request.return_value = _make_response(
            429, text="slow down", headers={"Ratelimit-Reset": str(reset)}
        )
        with pytest.raises(RateLimitError) as exc:
            connected._request("GET", "/services")
        # Derived from the epoch reset, not slept on directly.
        assert 25 <= exc.value.retry_after <= 31


class TestParseRetryAfter:
    def test_prefers_ratelimit_reset_as_epoch_delta(self):
        """Render sends a UTC epoch timestamp, not a delta — the classic way to
        accidentally sleep for decades."""
        reset = int(time.time()) + 45
        assert 40 <= _parse_retry_after({"Ratelimit-Reset": str(reset)}) <= 46

    def test_lowercase_header_also_works(self):
        reset = int(time.time()) + 20
        assert 15 <= _parse_retry_after({"ratelimit-reset": str(reset)}) <= 21

    def test_past_reset_clamps_to_at_least_one(self):
        assert _parse_retry_after({"Ratelimit-Reset": str(int(time.time()) - 500)}) == 1

    def test_falls_back_to_retry_after(self):
        assert _parse_retry_after({"Retry-After": "17"}) == 17

    def test_defaults_to_60(self):
        assert _parse_retry_after({}) == 60

    def test_unparseable_values_fall_through(self):
        assert _parse_retry_after({"Ratelimit-Reset": "soon", "Retry-After": "later"}) == 60


# -- Pagination --------------------------------------------------------------


class TestPagination:
    @patch("ccef_connections.connectors.render.requests.request")
    def test_unwraps_items(self, mock_request, connected):
        mock_request.side_effect = [
            _make_response(200, _page([{"id": "a"}, {"id": "b"}], "service")),
            _make_response(200, []),
        ]
        items = list(connected._paginate("/services", "service"))
        assert items == [{"id": "a"}, {"id": "b"}]

    @patch("ccef_connections.connectors.render.requests.request")
    def test_follows_the_cursor_across_pages(self, mock_request, connected):
        mock_request.side_effect = [
            _make_response(200, _page([{"id": "a"}], "service", cursor="c1")),
            _make_response(200, _page([{"id": "b"}], "service", cursor="c2")),
            _make_response(200, []),
        ]
        items = list(connected._paginate("/services", "service", page_size=1))
        assert [i["id"] for i in items] == ["a", "b"]
        # Second call must carry the first page's last cursor.
        assert mock_request.call_args_list[1][1]["params"]["cursor"] == "c1"

    @patch("ccef_connections.connectors.render.requests.request")
    def test_stops_at_limit_without_extra_requests(self, mock_request, connected):
        mock_request.side_effect = [
            _make_response(200, _page([{"id": "a"}, {"id": "b"}, {"id": "c"}], "service")),
        ]
        items = list(connected._paginate("/services", "service", limit=2))
        assert len(items) == 2
        assert mock_request.call_count == 1

    @patch("ccef_connections.connectors.render.requests.request")
    def test_empty_page_terminates(self, mock_request, connected):
        mock_request.return_value = _make_response(200, [])
        assert list(connected._paginate("/services", "service")) == []

    @patch("ccef_connections.connectors.render.requests.request")
    def test_missing_cursor_terminates(self, mock_request, connected):
        """No cursor on the last item means no next page — without this the
        walk would re-request page one forever."""
        mock_request.side_effect = [
            _make_response(200, [{"service": {"id": "a"}}]),
        ]
        assert [i["id"] for i in connected._paginate("/services", "service")] == ["a"]

    @patch("ccef_connections.connectors.render.requests.request")
    def test_page_size_is_clamped_to_render_maximum(self, mock_request, connected):
        mock_request.return_value = _make_response(200, [])
        list(connected._paginate("/services", "service", page_size=5000))
        assert mock_request.call_args[1]["params"]["limit"] == 100


# -- Services ----------------------------------------------------------------


class TestFindService:
    @patch("ccef_connections.connectors.render.requests.request")
    def test_exact_match(self, mock_request, connected):
        mock_request.side_effect = [
            _make_response(200, _page([{"id": SERVICE_ID, "name": "ep-roving-review"}], "service")),
            _make_response(200, []),
        ]
        assert connected.find_service("ep-roving-review")["id"] == SERVICE_ID

    @patch("ccef_connections.connectors.render.requests.request")
    def test_prefix_matches_are_rejected(self, mock_request, connected):
        """Render's name filter matches on prefix, so asking for 'ep-roving'
        must not silently return 'ep-roving-review'."""
        mock_request.side_effect = [
            _make_response(200, _page([{"id": SERVICE_ID, "name": "ep-roving-review"}], "service")),
            _make_response(200, []),
        ]
        with pytest.raises(ConnectionError, match="No Render service"):
            connected.find_service("ep-roving")

    @patch("ccef_connections.connectors.render.requests.request")
    def test_ambiguous_name_raises(self, mock_request, connected):
        mock_request.side_effect = [
            _make_response(
                200,
                _page([{"id": "srv-1", "name": "dup"}, {"id": "srv-2", "name": "dup"}], "service"),
            ),
            _make_response(200, []),
        ]
        with pytest.raises(ConnectionError, match="resolve by ID"):
            connected.find_service("dup")


# -- Deploys -----------------------------------------------------------------


class TestDeploys:
    @patch("ccef_connections.connectors.render.requests.request")
    def test_latest_deploy_returns_none_when_never_deployed(self, mock_request, connected):
        mock_request.return_value = _make_response(200, [])
        assert connected.latest_deploy(SERVICE_ID) is None

    @patch("ccef_connections.connectors.render.requests.request")
    def test_trigger_deploy_maps_clear_cache(self, mock_request, connected):
        mock_request.return_value = _make_response(200, {"id": "dep-1"})
        connected.trigger_deploy(SERVICE_ID, clear_cache=True)
        assert mock_request.call_args[1]["json"] == {"clearCache": "clear"}

        connected.trigger_deploy(SERVICE_ID, clear_cache=False)
        assert mock_request.call_args[1]["json"] == {"clearCache": "do_not_clear"}

    @patch("ccef_connections.connectors.render.requests.request")
    def test_trigger_deploy_empty_response_is_write_error(self, mock_request, connected):
        mock_request.return_value = _make_response(404)
        with pytest.raises(WriteError):
            connected.trigger_deploy(SERVICE_ID)

    @patch("ccef_connections.connectors.render.requests.request")
    def test_wait_for_deploy_returns_on_terminal_status(self, mock_request, connected):
        mock_request.side_effect = [
            _make_response(200, {"id": "dep-1", "status": "build_in_progress"}),
            _make_response(200, {"id": "dep-1", "status": "live"}),
        ]
        with patch("ccef_connections.connectors.render.time.sleep"):
            result = connected.wait_for_deploy(SERVICE_ID, "dep-1", poll_interval=0)
        assert result["status"] == "live"

    @patch("ccef_connections.connectors.render.requests.request")
    def test_wait_for_deploy_returns_failures_rather_than_raising(self, mock_request, connected):
        """A failed deploy is a terminal state, not an exception — the caller
        decides what a build_failed means."""
        mock_request.return_value = _make_response(200, {"id": "d", "status": "build_failed"})
        with patch("ccef_connections.connectors.render.time.sleep"):
            assert connected.wait_for_deploy(SERVICE_ID, "d")["status"] == "build_failed"

    @patch("ccef_connections.connectors.render.requests.request")
    def test_wait_for_deploy_times_out(self, mock_request, connected):
        mock_request.return_value = _make_response(200, {"id": "d", "status": "build_in_progress"})
        with patch("ccef_connections.connectors.render.time.sleep"):
            with pytest.raises(ConnectionError, match="still"):
                connected.wait_for_deploy(SERVICE_ID, "d", timeout=0, poll_interval=0)

    @patch("ccef_connections.connectors.render.requests.request")
    def test_wait_for_deploy_missing_deploy_raises(self, mock_request, connected):
        mock_request.return_value = _make_response(404)
        with pytest.raises(ConnectionError, match="not found"):
            connected.wait_for_deploy(SERVICE_ID, "dep-gone")


# -- Custom domains ----------------------------------------------------------


class TestCustomDomains:
    @patch("ccef_connections.connectors.render.requests.request")
    def test_list_unwraps_custom_domain_key(self, mock_request, connected):
        mock_request.side_effect = [
            _make_response(
                200,
                _page(
                    [{"id": "dom-1", "name": "roving.example.org", "verificationStatus": "verified"}],
                    "customDomain",
                ),
            ),
            _make_response(200, []),
        ]
        domains = connected.list_custom_domains(SERVICE_ID)
        assert domains[0]["verificationStatus"] == "verified"

    @patch("ccef_connections.connectors.render.requests.request")
    def test_add_rejection_becomes_write_error(self, mock_request, connected):
        mock_request.return_value = _make_response(400, text="already in use")
        with pytest.raises(WriteError, match="rejected"):
            connected.add_custom_domain(SERVICE_ID, "roving.example.org")

    @patch("ccef_connections.connectors.render.requests.request")
    def test_verify_posts_to_the_verify_path(self, mock_request, connected):
        mock_request.return_value = _make_response(200, {})
        assert connected.verify_custom_domain(SERVICE_ID, "dom-1") is True
        assert mock_request.call_args[0][1].endswith(f"/custom-domains/dom-1/verify")
        assert mock_request.call_args[0][0] == "POST"


# -- Environment variables and the blueprint guard ---------------------------


class TestBlueprintLiteralKeys:
    def test_literal_values_are_owned_and_sync_false_is_not(self, blueprint):
        """sync:false means the blueprint names the key but never supplies a
        value, so Render preserves whatever is set — those stay writable."""
        assert blueprint_literal_keys(str(blueprint)) == {"APP_BASE_URL", "REDIRECT_HOSTS"}

    def test_missing_file_guards_nothing(self, tmp_path):
        assert blueprint_literal_keys(str(tmp_path / "absent.yaml")) == set()

    def test_empty_file_guards_nothing(self, tmp_path):
        path = tmp_path / "render.yaml"
        path.write_text("", encoding="utf-8")
        assert blueprint_literal_keys(str(path)) == set()


class TestEnvVars:
    @patch("ccef_connections.connectors.render.requests.request")
    def test_env_var_keys_returns_names_only(self, mock_request, connected):
        mock_request.side_effect = [
            _make_response(
                200,
                _page([{"key": "SECRET_KEY", "value": "s3cret"}, {"key": "CARTO_API_KEY", "value": "k"}], "envVar"),
            ),
            _make_response(200, []),
        ]
        assert connected.env_var_keys(SERVICE_ID) == {"SECRET_KEY", "CARTO_API_KEY"}

    @patch("ccef_connections.connectors.render.requests.request")
    def test_set_env_var_without_blueprint_is_unguarded(self, mock_request, connected):
        mock_request.return_value = _make_response(200, {"key": "APP_BASE_URL"})
        assert connected.set_env_var(SERVICE_ID, "APP_BASE_URL", "https://x.org")

    @patch("ccef_connections.connectors.render.requests.request")
    def test_set_env_var_refuses_blueprint_owned_key(self, mock_request, blueprint):
        """The whole point of the guard: Render re-applies blueprint literals on
        the next sync, so this write would silently revert days later."""
        c = RenderConnector(blueprint_path=str(blueprint))
        c._api_key, c._is_connected = FAKE_KEY, True
        with pytest.raises(WriteError, match="render.yaml"):
            c.set_env_var(SERVICE_ID, "APP_BASE_URL", "https://x.org")
        mock_request.assert_not_called()

    @patch("ccef_connections.connectors.render.requests.request")
    def test_set_env_var_allows_sync_false_key(self, mock_request, blueprint):
        """Rotating a secret is the supported use of this method."""
        mock_request.return_value = _make_response(200, {"key": "SECRET_KEY"})
        c = RenderConnector(blueprint_path=str(blueprint))
        c._api_key, c._is_connected = FAKE_KEY, True
        assert c.set_env_var(SERVICE_ID, "SECRET_KEY", "rotated")
        assert mock_request.call_args[1]["json"] == {"value": "rotated"}

    @patch("ccef_connections.connectors.render.requests.request")
    def test_set_env_var_allows_undeclared_key(self, mock_request, blueprint):
        mock_request.return_value = _make_response(200, {"key": "AD_HOC"})
        c = RenderConnector(blueprint_path=str(blueprint))
        c._api_key, c._is_connected = FAKE_KEY, True
        assert c.set_env_var(SERVICE_ID, "AD_HOC", "1")

    @patch("ccef_connections.connectors.render.requests.request")
    def test_delete_env_var_refuses_blueprint_owned_key(self, mock_request, blueprint):
        c = RenderConnector(blueprint_path=str(blueprint))
        c._api_key, c._is_connected = FAKE_KEY, True
        with pytest.raises(WriteError, match="render.yaml"):
            c.delete_env_var(SERVICE_ID, "REDIRECT_HOSTS")
        mock_request.assert_not_called()

    def test_blueprint_keys_are_read_once(self, blueprint):
        c = RenderConnector(blueprint_path=str(blueprint))
        first = c._blueprint_managed_keys()
        blueprint.unlink()
        assert c._blueprint_managed_keys() == first
