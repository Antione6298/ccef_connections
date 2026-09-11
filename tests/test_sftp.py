"""Tests for the SFTP connector."""

import base64
import hashlib
from unittest.mock import MagicMock, patch

import paramiko  # noqa: F401  -- see note below
import pytest

# paramiko is imported here, at module load, purely so it is already in
# sys.modules by the time a test runs. The connector imports it lazily inside
# connect(), and several tests use patch.dict(..., clear=True); on Windows an
# empty environment has no SystemRoot, which leaves OpenSSL unable to seed its
# RNG, so importing paramiko *inside* a cleared environment dies with
# "entropy source strength too weak". Importing first makes the lazy import a
# cache hit.

from ccef_connections.connectors.sftp import RemoteFile, SFTPConnector
from ccef_connections.exceptions import (
    AuthenticationError,
    ConfigurationError,
    CredentialError,
    WriteError,
)

HOST_KEY_BLOB = b"pretend-host-key"
SHA256_FP = base64.b64encode(hashlib.sha256(HOST_KEY_BLOB).digest()).decode().rstrip("=")
MD5_HEX = hashlib.md5(HOST_KEY_BLOB).hexdigest()
MD5_FP = ":".join(MD5_HEX[i : i + 2] for i in range(0, len(MD5_HEX), 2))


# -- Fixtures ----------------------------------------------------------------


def _env(**overrides):
    """Environment for a fully configured ROI_SFTP host."""
    env = {
        "ROI_SFTP_HOST": "sftp.example.org",
        "ROI_SFTP_PORT": "22",
        "ROI_SFTP_USERNAME": "cmnc",
        "ROI_SFTP_HOST_FINGERPRINT": f"SHA256:{SHA256_FP}",
    }
    env.update(overrides)
    return env


def _make(credentials=None, **kwargs):
    """Build a connector whose credential manager returns ``credentials``."""
    connector = SFTPConnector(**kwargs)
    mock_cm = MagicMock()
    creds = credentials or {}
    mock_cm.get_credential.side_effect = lambda name, **kw: creds.get(name)
    connector._credential_manager = mock_cm
    return connector


@pytest.fixture
def host_key():
    key = MagicMock()
    key.asbytes.return_value = HOST_KEY_BLOB
    return key


@pytest.fixture
def connected():
    """A connector with a live-looking SFTP client attached."""
    connector = _make()
    connector._sftp = MagicMock()
    connector._is_connected = True
    return connector


# -- Configuration -----------------------------------------------------------


def test_missing_host_raises_configuration_error():
    with patch.dict("os.environ", _env(ROI_SFTP_HOST=""), clear=True):
        connector = _make({"ROI_SFTP_PRIVATE_KEY": "pem"})
        with pytest.raises(ConfigurationError, match="ROI_SFTP_HOST"):
            connector.connect()


def test_missing_username_raises_configuration_error():
    with patch.dict("os.environ", _env(ROI_SFTP_USERNAME=""), clear=True):
        connector = _make({"ROI_SFTP_PRIVATE_KEY": "pem"})
        with pytest.raises(ConfigurationError, match="ROI_SFTP_USERNAME"):
            connector.connect()


def test_non_numeric_port_raises_configuration_error():
    with patch.dict("os.environ", _env(ROI_SFTP_PORT="twenty-two"), clear=True):
        with pytest.raises(ConfigurationError, match="ROI_SFTP_PORT"):
            SFTPConnector()


def test_port_defaults_to_22():
    with patch.dict("os.environ", _env(ROI_SFTP_PORT=""), clear=True):
        assert SFTPConnector()._port == 22


def test_no_credential_raises_credential_error():
    with patch.dict("os.environ", _env(), clear=True):
        connector = _make({})
        with pytest.raises(CredentialError, match="ROI_SFTP_PRIVATE_KEY_PASSWORD"):
            connector.connect()


def test_prefix_selects_a_different_host():
    env = {"VENDOR_SFTP_HOST": "vendor.example.org", "VENDOR_SFTP_USERNAME": "ccef"}
    with patch.dict("os.environ", env, clear=True):
        connector = SFTPConnector(prefix="VENDOR_SFTP")
        assert connector._host == "vendor.example.org"
        assert connector._missing_settings() == []


# -- Host key verification ---------------------------------------------------


def test_sha256_fingerprint_accepted(host_key):
    with patch.dict("os.environ", _env(), clear=True):
        _make()._verify_host(host_key)


def test_md5_fingerprint_accepted(host_key):
    with patch.dict("os.environ", _env(ROI_SFTP_HOST_FINGERPRINT=MD5_FP), clear=True):
        _make()._verify_host(host_key)


def test_fingerprint_is_case_insensitive(host_key):
    with patch.dict("os.environ", _env(ROI_SFTP_HOST_FINGERPRINT=MD5_FP.upper()), clear=True):
        _make()._verify_host(host_key)


def test_mismatched_fingerprint_refuses_to_connect(host_key):
    with patch.dict("os.environ", _env(ROI_SFTP_HOST_FINGERPRINT="SHA256:nope"), clear=True):
        with pytest.raises(AuthenticationError, match="does not match"):
            _make()._verify_host(host_key)


def test_absent_fingerprint_refuses_rather_than_trusting(host_key):
    """An unset fingerprint must fail, not fall back to trust-on-first-use."""
    with patch.dict("os.environ", _env(ROI_SFTP_HOST_FINGERPRINT=""), clear=True):
        with pytest.raises(AuthenticationError, match="cannot be verified"):
            _make()._verify_host(host_key)


# -- Private key loading -----------------------------------------------------


def test_unreadable_key_names_the_env_var():
    with patch.dict("os.environ", _env(), clear=True):
        connector = _make({"ROI_SFTP_PRIVATE_KEY": "not a pem"})
        with pytest.raises(CredentialError, match="ROI_SFTP_PRIVATE_KEY_PASSWORD"):
            connector._load_private_key()


def test_escaped_newlines_are_restored():
    """Env vars usually carry PEM with literal backslash-n."""
    with patch.dict("os.environ", _env(), clear=True):
        connector = _make({"ROI_SFTP_PRIVATE_KEY": "line1\\nline2"})
        with patch("paramiko.Ed25519Key.from_private_key") as from_pem:
            connector._load_private_key()
            assert from_pem.call_args[0][0].getvalue() == "line1\nline2"


def test_no_key_configured_returns_none():
    with patch.dict("os.environ", _env(), clear=True):
        assert _make({})._load_private_key() is None


# -- Uploads -----------------------------------------------------------------


def _local_file(size=100):
    """Patch the local filesystem checks put() makes."""
    return (
        patch("os.path.isfile", return_value=True),
        patch("os.path.getsize", return_value=size),
    )


def test_put_stages_then_renames_into_place(connected):
    is_file, get_size = _local_file(size=100)
    connected._sftp.stat.side_effect = [IOError(), MagicMock(st_size=100, st_mtime=0)]
    with is_file, get_size:
        connected.put("local.csv", "/inbound/FlagEndDateAccount(2026-09-11)")

    staged = connected._sftp.put.call_args[0][1]
    assert staged.startswith("/inbound/.FlagEndDateAccount(2026-09-11)")
    assert staged.endswith(".part")
    connected._sftp.rename.assert_called_once_with(
        staged, "/inbound/FlagEndDateAccount(2026-09-11)"
    )


def test_put_refuses_to_clobber_without_overwrite(connected):
    is_file, get_size = _local_file()
    connected._sftp.stat.return_value = MagicMock(st_size=100, st_mtime=0)
    with is_file, get_size:
        with pytest.raises(WriteError, match="already exists"):
            connected.put("local.csv", "/inbound/flags.csv")
    connected._sftp.put.assert_not_called()


def test_put_rejects_a_short_upload(connected):
    """A truncated transfer must not be reported as success."""
    is_file, get_size = _local_file(size=500)
    connected._sftp.stat.side_effect = [IOError(), MagicMock(st_size=120, st_mtime=0)]
    with is_file, get_size:
        with pytest.raises(WriteError, match="landed incomplete"):
            connected.put("local.csv", "/inbound/flags.csv")


def test_put_removes_staging_file_when_transfer_fails(connected):
    is_file, get_size = _local_file()
    connected._sftp.stat.side_effect = IOError()
    connected._sftp.put.side_effect = OSError("connection reset")
    with is_file, get_size:
        with pytest.raises(WriteError, match="failed"):
            connected.put("local.csv", "/inbound/flags.csv")

    staged = connected._sftp.put.call_args[0][1]
    connected._sftp.remove.assert_called_once_with(staged)


def test_put_without_atomic_writes_directly(connected):
    is_file, get_size = _local_file()
    connected._sftp.stat.side_effect = [IOError(), MagicMock(st_size=100, st_mtime=0)]
    with is_file, get_size:
        connected.put("local.csv", "/inbound/flags.csv", atomic=False)

    assert connected._sftp.put.call_args[0][1] == "/inbound/flags.csv"
    connected._sftp.rename.assert_not_called()


def test_put_missing_local_file_raises(connected):
    with patch("os.path.isfile", return_value=False):
        with pytest.raises(WriteError, match="Local file not found"):
            connected.put("gone.csv", "/inbound/flags.csv")


# -- Listing -----------------------------------------------------------------


def test_list_dir_maps_entries(connected):
    entry = MagicMock(filename="flags.csv", st_size=2048, st_mtime=0, st_mode=0o100644)
    connected._sftp.listdir_attr.return_value = [entry]

    files = connected.list_dir("/inbound")
    assert files[0].name == "flags.csv"
    assert files[0].is_dir is False
    assert files[0].human_size == "2.0 KB"


def test_exists_is_false_when_stat_raises(connected):
    connected._sftp.stat.side_effect = IOError()
    assert connected.exists("/inbound/nope.csv") is False


def test_health_check_false_when_disconnected():
    assert _make().health_check() is False


def test_remote_file_human_size_bytes():
    assert RemoteFile("a", 512, None, False).human_size == "512 B"
