"""
SFTP connector for CCEF connections.

Key-based (or password) SFTP access with host-key verification, built for
delivery drops such as the ROI Solutions upload directory.

Settings are read from a configurable environment prefix, so one connector class
serves several hosts::

    ROI_SFTP_HOST                   sftp.example.org
    ROI_SFTP_PORT                   22 (default)
    ROI_SFTP_USERNAME               cmnc
    ROI_SFTP_HOST_FINGERPRINT       SHA256:xxxx  or  aa:bb:cc:... (MD5 hex)
    ROI_SFTP_PRIVATE_KEY_PASSWORD   the PEM private key
    ROI_SFTP_KEY_PASSPHRASE_PASSWORD   passphrase, if the key has one (optional)
    ROI_SFTP_PASSWORD_PASSWORD      password, if using password auth (optional)

Credentials follow the ``{CREDENTIAL_NAME}_PASSWORD`` convention, so the private
key lives in ``ROI_SFTP_PRIVATE_KEY_PASSWORD``. PEM text may use real newlines or
``\\n`` escapes; both are accepted.

**Host-key verification is mandatory by default.** A delivery target that starts
answering with a different host key is the one case where failing loudly matters
more than the job completing, so an unknown key raises rather than being trusted
on first use. Set ``verify_host_key=False`` only for throwaway exploration.

**Uploads are atomic by default.** The file is written under a staging name and
renamed into place once the bytes have landed, because a watcher that consumes
files as they appear will otherwise happily read a half-written one. The staging
name is dot-prefixed so it does not match a job's filename pattern while in
flight.
"""

import base64
import hashlib
import logging
import os
import posixpath
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
from typing import Any, List, Optional

from ..core.base import BaseConnection
from ..exceptions import (
    AuthenticationError,
    ConfigurationError,
    ConnectionError,
    CredentialError,
    WriteError,
)

logger = logging.getLogger(__name__)

DEFAULT_PORT = 22
DEFAULT_PREFIX = "ROI_SFTP"


@dataclass(frozen=True)
class RemoteFile:
    """One entry in a remote directory listing."""

    name: str
    size: int
    modified: datetime
    is_dir: bool

    @property
    def human_size(self) -> str:
        """Size rendered for logs, e.g. ``12.3 KB``."""
        value = float(self.size)
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
            value /= 1024
        return f"{value:.1f} GB"


class SFTPConnector(BaseConnection):
    """
    SFTP connector for file delivery and retrieval.

    Examples:
        >>> with SFTPConnector() as sftp:
        ...     sftp.list_dir("/inbound")
        ...     sftp.put("output/flags.csv", "/inbound/FlagEndDateAccount(2026-09-11)")
        >>>
        >>> # A different host, same class
        >>> vendor = SFTPConnector(prefix="VENDOR_SFTP")
    """

    def __init__(
        self,
        prefix: str = DEFAULT_PREFIX,
        host: Optional[str] = None,
        port: Optional[int] = None,
        username: Optional[str] = None,
        verify_host_key: bool = True,
    ) -> None:
        """
        Initialize the SFTP connector.

        Args:
            prefix: Environment-variable prefix for this host (default ``ROI_SFTP``)
            host: Hostname, overriding ``{prefix}_HOST``
            port: Port, overriding ``{prefix}_PORT`` (default 22)
            username: Username, overriding ``{prefix}_USERNAME``
            verify_host_key: Require the server's key to match
                ``{prefix}_HOST_FINGERPRINT``. Leave on for anything scheduled
        """
        super().__init__()
        self._prefix = prefix
        self._host = host or os.getenv(f"{prefix}_HOST") or ""
        self._username = username or os.getenv(f"{prefix}_USERNAME") or ""
        self._verify_host_key = verify_host_key

        raw_port = port if port is not None else os.getenv(f"{prefix}_PORT")
        try:
            self._port = int(raw_port) if raw_port else DEFAULT_PORT
        except (TypeError, ValueError):
            raise ConfigurationError(
                f"{prefix}_PORT must be a number, got {raw_port!r}"
            )

        self._transport: Optional[Any] = None
        self._sftp: Optional[Any] = None

    # -- configuration -------------------------------------------------------

    def _missing_settings(self) -> List[str]:
        """Names of required settings that resolved empty."""
        missing = []
        if not self._host:
            missing.append(f"{self._prefix}_HOST")
        if not self._username:
            missing.append(f"{self._prefix}_USERNAME")
        return missing

    def _load_private_key(self) -> Optional[Any]:
        """
        Build a paramiko key object from the configured PEM, if there is one.

        Returns:
            A paramiko PKey, or None when no private key is configured

        Raises:
            CredentialError: If the PEM is present but unreadable by any key type
        """
        import paramiko

        pem = self._credential_manager.get_credential(
            f"{self._prefix}_PRIVATE_KEY", required=False
        )
        if not pem:
            return None

        # Env vars commonly carry PEM with literal \n rather than real newlines.
        pem = str(pem).replace("\\n", "\n").strip()
        passphrase = self._credential_manager.get_credential(
            f"{self._prefix}_KEY_PASSPHRASE", required=False
        )

        # Key type is not recorded anywhere, so try each; PEM headers differ but
        # paramiko's per-class parsing is the reliable discriminator. Resolved by
        # name because the available classes vary by version -- paramiko 4 dropped
        # DSSKey, so naming it directly is an AttributeError on a current install.
        errors = []
        key_classes = [
            getattr(paramiko, name)
            for name in ("Ed25519Key", "RSAKey", "ECDSAKey", "DSSKey")
            if hasattr(paramiko, name)
        ]
        for key_class in key_classes:
            try:
                return key_class.from_private_key(
                    StringIO(pem), password=passphrase or None
                )
            except Exception as exc:  # noqa: BLE001 - try the next key type
                errors.append(f"{key_class.__name__}: {exc}")

        raise CredentialError(
            f"Could not read {self._prefix}_PRIVATE_KEY_PASSWORD as any supported "
            f"key type. Tried -- " + "; ".join(errors)
        )

    @staticmethod
    def _fingerprints(key: Any) -> List[str]:
        """All accepted spellings of a host key's fingerprint, lowercased."""
        blob = key.asbytes()
        sha256 = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
        md5 = hashlib.md5(blob).hexdigest()
        return [
            f"sha256:{sha256}".lower(),
            sha256.lower(),
            ":".join(md5[i : i + 2] for i in range(0, len(md5), 2)),
            md5.lower(),
        ]

    def _verify_host(self, key: Any) -> None:
        """
        Check the server's host key against the configured fingerprint.

        Raises:
            AuthenticationError: If it does not match, or none is configured
        """
        expected = (os.getenv(f"{self._prefix}_HOST_FINGERPRINT") or "").strip()
        if not expected:
            raise AuthenticationError(
                f"{self._prefix}_HOST_FINGERPRINT is not set, so the server's "
                f"identity cannot be verified. Set it, or construct the connector "
                f"with verify_host_key=False if you accept the risk."
            )

        if expected.lower() not in self._fingerprints(key):
            raise AuthenticationError(
                f"Host key for {self._host} does not match "
                f"{self._prefix}_HOST_FINGERPRINT. Refusing to connect. If the "
                f"server was legitimately rekeyed, update the fingerprint."
            )
        logger.debug("Host key verified for %s", self._host)

    # -- connection ----------------------------------------------------------

    def connect(self) -> None:
        """
        Open the SFTP session.

        Raises:
            ConfigurationError: If host or username is missing
            CredentialError: If no usable credential is configured
            AuthenticationError: If the host key or the credentials are rejected
            ConnectionError: If the connection cannot be established
        """
        try:
            import paramiko
        except ImportError as e:
            raise ImportError(
                "SFTPConnector requires paramiko. "
                'Install with: pip install "ccef-connections[sftp]"'
            ) from e

        missing = self._missing_settings()
        if missing:
            raise ConfigurationError(
                f"SFTP connection is missing required setting(s): "
                f"{', '.join(missing)}. Set the environment variables or pass "
                f"them to SFTPConnector(...)."
            )

        key = self._load_private_key()
        password = self._credential_manager.get_credential(
            f"{self._prefix}_PASSWORD", required=False
        )
        if not key and not password:
            raise CredentialError(
                f"No SFTP credential found. Set {self._prefix}_PRIVATE_KEY_PASSWORD "
                f"(preferred) or {self._prefix}_PASSWORD_PASSWORD."
            )

        try:
            transport = paramiko.Transport((self._host, self._port))
            transport.connect()
        except Exception as e:
            raise ConnectionError(
                f"Could not reach SFTP host {self._host}:{self._port}: {e}"
            ) from e

        try:
            if self._verify_host_key:
                self._verify_host(transport.get_remote_server_key())

            # Auth is a separate step from connect() so the host key can be
            # checked before any credential is put on the wire.
            if key:
                transport.auth_publickey(self._username, key)
            else:
                transport.auth_password(self._username, str(password))

            self._transport = transport
            self._sftp = paramiko.SFTPClient.from_transport(transport)
            self._is_connected = True
            logger.info("Connected to SFTP %s@%s:%s", self._username, self._host, self._port)
        except AuthenticationError:
            transport.close()
            raise
        except Exception as e:
            transport.close()
            raise AuthenticationError(
                f"SFTP authentication failed for {self._username}@{self._host}: {e}"
            ) from e

    def disconnect(self) -> None:
        """Close the SFTP session and underlying transport."""
        for resource in (self._sftp, self._transport):
            try:
                if resource is not None:
                    resource.close()
            except Exception:  # noqa: BLE001 - closing must not raise
                logger.debug("Error closing SFTP resource", exc_info=True)
        self._sftp = None
        self._transport = None
        self._is_connected = False

    def health_check(self) -> bool:
        """
        Check the session is usable by listing the working directory.

        Returns:
            True if the session answers, False otherwise
        """
        if not self._is_connected or self._sftp is None:
            return False
        try:
            self._sftp.listdir(".")
            return True
        except Exception:  # noqa: BLE001
            return False

    def _require_session(self) -> Any:
        """The live SFTP client, connecting first if needed."""
        if self._sftp is None or not self._is_connected:
            self.connect()
        return self._sftp

    # -- operations ----------------------------------------------------------

    def list_dir(self, path: str = ".") -> List[RemoteFile]:
        """
        List a remote directory.

        Args:
            path: Remote directory (default: the login directory)

        Returns:
            Entries, directories included

        Raises:
            ConnectionError: If the listing fails
        """
        sftp = self._require_session()
        try:
            entries = sftp.listdir_attr(path)
        except Exception as e:
            raise ConnectionError(f"Could not list {path}: {e}") from e

        return [
            RemoteFile(
                name=entry.filename,
                size=entry.st_size or 0,
                modified=datetime.fromtimestamp(entry.st_mtime or 0, tz=timezone.utc),
                is_dir=stat.S_ISDIR(entry.st_mode or 0),
            )
            for entry in entries
        ]

    def exists(self, path: str) -> bool:
        """
        Check whether a remote path exists.

        Args:
            path: Remote path

        Returns:
            True if it exists
        """
        sftp = self._require_session()
        try:
            sftp.stat(path)
            return True
        except IOError:
            return False

    def put(
        self,
        local_path: str,
        remote_path: str,
        atomic: bool = True,
        overwrite: bool = False,
    ) -> RemoteFile:
        """
        Upload a local file.

        Args:
            local_path: Path to the local file
            remote_path: Full remote destination path
            atomic: Upload under a staging name and rename into place, so a
                watcher never sees a partial file. Leave on for delivery drops
            overwrite: Permit replacing an existing remote file

        Returns:
            The uploaded file's remote listing entry

        Raises:
            WriteError: If the local file is missing, the destination exists and
                ``overwrite`` is False, or the transfer does not land intact
        """
        if not os.path.isfile(local_path):
            raise WriteError(f"Local file not found: {local_path}")

        sftp = self._require_session()

        if not overwrite and self.exists(remote_path):
            raise WriteError(
                f"Remote file already exists: {remote_path}. Pass overwrite=True "
                f"to replace it."
            )

        local_size = os.path.getsize(local_path)
        directory = posixpath.dirname(remote_path) or "."
        target = remote_path

        if atomic:
            # Dot-prefixed so a pattern-matching watcher ignores it in flight.
            staging = posixpath.join(
                directory, f".{posixpath.basename(remote_path)}.{uuid.uuid4().hex[:8]}.part"
            )
            target = staging

        try:
            sftp.put(local_path, target)
        except Exception as e:
            if atomic:
                try:
                    sftp.remove(target)
                except Exception:  # noqa: BLE001 - cleanup is best effort
                    logger.debug("Could not remove staging file %s", target, exc_info=True)
            raise WriteError(f"Upload of {local_path} to {target} failed: {e}") from e

        if atomic:
            try:
                if overwrite and self.exists(remote_path):
                    sftp.remove(remote_path)
                sftp.rename(target, remote_path)
            except Exception as e:
                try:
                    sftp.remove(target)
                except Exception:  # noqa: BLE001
                    logger.debug("Could not remove staging file %s", target, exc_info=True)
                raise WriteError(
                    f"Uploaded {local_path} but could not move it into place at "
                    f"{remote_path}: {e}"
                ) from e

        try:
            attrs = sftp.stat(remote_path)
        except Exception as e:
            raise WriteError(
                f"Uploaded {local_path} but {remote_path} is not readable back: {e}"
            ) from e

        remote_size = attrs.st_size or 0
        if remote_size != local_size:
            raise WriteError(
                f"Upload of {local_path} landed incomplete: {local_size} bytes sent, "
                f"{remote_size} bytes on the server."
            )

        logger.info("Uploaded %s -> %s (%s bytes)", local_path, remote_path, remote_size)
        return RemoteFile(
            name=posixpath.basename(remote_path),
            size=remote_size,
            modified=datetime.fromtimestamp(attrs.st_mtime or 0, tz=timezone.utc),
            is_dir=False,
        )

    def get(self, remote_path: str, local_path: str) -> str:
        """
        Download a remote file.

        Args:
            remote_path: Remote source path
            local_path: Local destination path

        Returns:
            The local path written

        Raises:
            ConnectionError: If the download fails
        """
        sftp = self._require_session()
        parent = os.path.dirname(local_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            sftp.get(remote_path, local_path)
        except Exception as e:
            raise ConnectionError(f"Could not download {remote_path}: {e}") from e
        return local_path

    def remove(self, remote_path: str) -> None:
        """
        Delete a remote file.

        Args:
            remote_path: Remote path to delete

        Raises:
            WriteError: If the delete fails
        """
        sftp = self._require_session()
        try:
            sftp.remove(remote_path)
        except Exception as e:
            raise WriteError(f"Could not delete {remote_path}: {e}") from e
        logger.info("Deleted remote file %s", remote_path)
