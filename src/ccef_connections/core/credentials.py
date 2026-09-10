"""
Credential management for CCEF connections.

This module handles loading credentials from environment variables using the
{CREDENTIAL_NAME}_PASSWORD naming convention required for Civis compatibility.
Supports both local development (.env files) and Civis production environments.
"""

import json
import logging
import os
from typing import Any, Dict, Optional

from dotenv import load_dotenv

from ..exceptions import CredentialError

logger = logging.getLogger(__name__)


class CredentialManager:
    """
    Manages credentials for CCEF connections.

    Supports the {CREDENTIAL_NAME}_PASSWORD naming convention for Civis
    compatibility while also working seamlessly in local development
    environments with .env files.
    """

    _instance: Optional["CredentialManager"] = None
    _credentials_cache: Dict[str, Any] = {}
    _env_loaded: bool = False

    def __new__(cls) -> "CredentialManager":
        """Implement singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        """Initialize the credential manager."""
        if not self._env_loaded:
            # Load .env file if it exists (local development)
            load_dotenv()
            self._env_loaded = True
            logger.debug("Environment variables loaded")

    def get_credential(
        self, name: str, required: bool = True, is_json: bool = False
    ) -> Optional[Any]:
        """
        Get a credential by name using the {NAME}_PASSWORD pattern.

        Args:
            name: The credential name (e.g., 'AIRTABLE_API_KEY')
            required: Whether the credential is required (raises error if missing)
            is_json: Whether to parse the credential as JSON

        Returns:
            The credential value (string or parsed JSON dict)

        Raises:
            CredentialError: If a required credential is missing or invalid

        Examples:
            >>> manager = CredentialManager()
            >>> api_key = manager.get_credential('AIRTABLE_API_KEY')
            >>> gcp_creds = manager.get_credential('BIGQUERY_CREDENTIALS', is_json=True)
        """
        # Check cache first
        if name in self._credentials_cache:
            logger.debug(f"Retrieved credential from cache: {name}")
            return self._credentials_cache[name]

        # Construct the environment variable name
        env_var_name = f"{name}_PASSWORD"

        # Get the credential from environment
        credential = os.getenv(env_var_name)

        if credential is None:
            if required:
                raise CredentialError(
                    f"Required credential not found: {env_var_name}\n"
                    f"Please set the environment variable {env_var_name} "
                    f"or add it to your .env file."
                )
            logger.debug(f"Optional credential not found: {name}")
            return None

        # Parse JSON if requested
        if is_json:
            try:
                credential = json.loads(credential)
                logger.debug(f"Parsed JSON credential: {name}")
            except json.JSONDecodeError as e:
                raise CredentialError(
                    f"Failed to parse JSON credential {env_var_name}: {str(e)}\n"
                    f"Ensure the credential is valid JSON."
                )

        # Cache the credential
        self._credentials_cache[name] = credential
        logger.debug(f"Loaded and cached credential: {name}")

        return credential

    def get_airtable_key(self) -> str:
        """
        Get the Airtable API key.

        Returns:
            The Airtable API key

        Raises:
            CredentialError: If the credential is missing
        """
        return str(self.get_credential("AIRTABLE_API_KEY"))

    def get_openai_key(self) -> str:
        """
        Get the OpenAI API key.

        Returns:
            The OpenAI API key

        Raises:
            CredentialError: If the credential is missing
        """
        return str(self.get_credential("OPENAI_API_KEY"))

    def get_google_sheets_credentials(self) -> Dict[str, Any]:
        """
        Get Google Sheets service account credentials.

        Returns:
            The parsed service account JSON credentials

        Raises:
            CredentialError: If the credential is missing or invalid JSON
        """
        creds = self.get_credential("GOOGLE_SHEETS_CREDENTIALS", is_json=True)
        if not isinstance(creds, dict):
            raise CredentialError(
                "GOOGLE_SHEETS_CREDENTIALS_PASSWORD must be a valid JSON object"
            )
        return creds

    def get_bigquery_credentials(self) -> Dict[str, Any]:
        """
        Get BigQuery service account credentials.

        Returns:
            The parsed service account JSON credentials

        Raises:
            CredentialError: If the credential is missing or invalid JSON
        """
        creds = self.get_credential("BIGQUERY_CREDENTIALS", is_json=True)
        if not isinstance(creds, dict):
            raise CredentialError("BIGQUERY_CREDENTIALS_PASSWORD must be a valid JSON object")
        return creds

    def get_helpscout_credentials(self) -> Dict[str, Any]:
        """
        Get HelpScout OAuth2 client credentials.

        Returns:
            Dict with 'app_id' and 'app_secret' keys

        Raises:
            CredentialError: If the credential is missing, invalid JSON,
                or missing required keys
        """
        creds = self.get_credential("HELPSCOUT_CREDENTIALS", is_json=True)
        if not isinstance(creds, dict):
            raise CredentialError(
                "HELPSCOUT_CREDENTIALS_PASSWORD must be a valid JSON object"
            )
        missing = [k for k in ("app_id", "app_secret") if k not in creds]
        if missing:
            raise CredentialError(
                f"HELPSCOUT_CREDENTIALS_PASSWORD missing required keys: {', '.join(missing)}"
            )
        return creds

    def get_zendesk_credentials(self) -> Dict[str, Any]:
        """
        Get Zendesk OAuth2 client credentials.

        Zendesk is removing API tokens as an auth method (creation blocked
        2026-10-27, all tokens deactivated 2027-04-30), so this is the
        client_credentials pair, not a token. ``client_id`` is the OAuth
        client's *Identifier* field -- an author-chosen slug -- not its
        numeric id.

        Returns:
            Dict with 'client_id' and 'client_secret' keys

        Raises:
            CredentialError: If the credential is missing, invalid JSON,
                or missing required keys
        """
        creds = self.get_credential("ZENDESK_CREDENTIALS", is_json=True)
        if not isinstance(creds, dict):
            raise CredentialError(
                "ZENDESK_CREDENTIALS_PASSWORD must be a valid JSON object"
            )
        missing = [k for k in ("client_id", "client_secret") if k not in creds]
        if missing:
            raise CredentialError(
                f"ZENDESK_CREDENTIALS_PASSWORD missing required keys: {', '.join(missing)}"
            )
        return creds

    def get_action_network_key(self) -> str:
        """
        Get the Action Network API key.

        Returns:
            The Action Network API key

        Raises:
            CredentialError: If the credential is missing
        """
        return str(self.get_credential("ACTION_NETWORK_API_KEY"))

    def get_zoom_credentials(self) -> Dict[str, Any]:
        """
        Get Zoom Server-to-Server OAuth credentials.

        Returns:
            Dict with 'account_id', 'client_id', and 'client_secret' keys

        Raises:
            CredentialError: If the credential is missing, invalid JSON,
                or missing required keys
        """
        creds = self.get_credential("ZOOM_CREDENTIALS", is_json=True)
        if not isinstance(creds, dict):
            raise CredentialError(
                "ZOOM_CREDENTIALS_PASSWORD must be a valid JSON object"
            )
        missing = [
            k for k in ("account_id", "client_id", "client_secret") if k not in creds
        ]
        if missing:
            raise CredentialError(
                f"ZOOM_CREDENTIALS_PASSWORD missing required keys: {', '.join(missing)}"
            )
        return creds

    def get_ptv_api_key(self) -> str:
        """
        Get the Protect the Vote (PTV) API key.

        Returns:
            The PTV API key

        Raises:
            CredentialError: If the credential is missing
        """
        return str(self.get_credential("PTV_API_KEY"))

    def get_action_builder_credentials(self) -> Dict[str, Any]:
        """
        Get Action Builder API credentials.

        Returns:
            Dict with 'api_token' and 'subdomain' keys

        Raises:
            CredentialError: If the credential is missing, invalid JSON,
                or missing required keys
        """
        creds = self.get_credential("ACTION_BUILDER_CREDENTIALS", is_json=True)
        if not isinstance(creds, dict):
            raise CredentialError(
                "ACTION_BUILDER_CREDENTIALS_PASSWORD must be a valid JSON object"
            )
        missing = [k for k in ("api_token", "subdomain") if k not in creds]
        if missing:
            raise CredentialError(
                f"ACTION_BUILDER_CREDENTIALS_PASSWORD missing required keys: {', '.join(missing)}"
            )
        return creds

    def get_asana_api_key(self) -> str:
        """
        Get the Asana Personal Access Token.

        Returns:
            The Asana Personal Access Token

        Raises:
            CredentialError: If the credential is missing
        """
        return str(self.get_credential("ASANA_API_KEY"))

    def get_geocodio_key(self) -> str:
        """
        Get the Geocodio API key.

        Returns:
            The Geocodio API key

        Raises:
            CredentialError: If the credential is missing
        """
        return str(self.get_credential("GEOCODIO_API_KEY"))

    def get_user_profile_credentials(self) -> Dict[str, str]:
        """
        Get the Power Automate user-profile flow endpoint and automation key.

        The URL is held in ``USER_PROFILE_API_URI`` rather than under the
        ``_PASSWORD`` convention, because it is an address rather than a
        secret in the usual sense -- though it is treated as one, since it
        embeds the Power Platform environment ID, the workflow GUID and a SAS
        signature.

        Surrounding quotes are stripped from the URL. An unbalanced trailing
        quote from a hand-edited .env lands inside the SAS signature and makes
        every call fail with a 401 that looks like a bad key.

        Returns:
            Dict with ``uri`` and ``key``

        Raises:
            CredentialError: If either value is missing
        """
        uri = os.getenv("USER_PROFILE_API_URI")
        if not uri or not uri.strip():
            raise CredentialError(
                "Required credential not found: USER_PROFILE_API_URI\n"
                "Please set the environment variable USER_PROFILE_API_URI "
                "or add it to your .env file."
            )
        return {
            "uri": uri.strip().strip('"').strip("'"),
            "key": str(self.get_credential("USER_PROFILE_API_CREDENTIALS")).strip(),
        }

    def get_resend_api_key(self) -> str:
        """
        Get the Resend transactional-email API key.

        Returns:
            The Resend API key

        Raises:
            CredentialError: If the credential is missing
        """
        return str(self.get_credential("RESEND_API_KEY"))

    def get_github_pat(self, credential_name: str = "GITHUB_PAT") -> str:
        """
        Get a GitHub Personal Access Token.

        Args:
            credential_name: Name of the credential (default: "GITHUB_PAT").
                The env var read is {credential_name}_PASSWORD. Use a
                project-specific name when running multiple GitHub-writing
                scripts in the same environment so each holds a scope-
                minimized token (e.g. "DYNAMIC_ACTION_MAP_GITHUB_PAT").

        Returns:
            The PAT as a string

        Raises:
            CredentialError: If the credential is missing
        """
        return str(self.get_credential(credential_name))

    def get_hex_api_key(self, credential_name: str = "HEX_API_KEY") -> str:
        """
        Get a Hex personal access token.

        Args:
            credential_name: Name of the credential (default: "HEX_API_KEY").
                The env var read is {credential_name}_PASSWORD.

        Returns:
            The token as a string

        Raises:
            CredentialError: If the credential is missing
        """
        return str(self.get_credential(credential_name))

    def get_chariot_key(self, credential_name: str = "CHARIOT_API_KEY") -> str:
        """
        Get the Chariot REST API key.

        ⚠ **Single-field JSON, not a bare string**: ``{"key": "sk_live_..."}``. There
        is no ``api_name`` because Chariot is one account per credential, unlike
        Stripe's several. A bare string is accepted too, for tolerance.

        Args:
            credential_name: Name of the credential (default: "CHARIOT_API_KEY").
                The env var read is {credential_name}_PASSWORD.

        Returns:
            The API key as a string

        Raises:
            CredentialError: If the credential is missing or carries no key
        """
        raw = self.get_credential(credential_name)
        if isinstance(raw, str):
            text = raw.strip()
            if text.startswith("{"):
                import json as _json

                try:
                    raw = _json.loads(text)
                except ValueError:
                    return text
            else:
                return text
        if isinstance(raw, dict):
            for field in ("key", "api_key", "token", "secret"):
                if raw.get(field):
                    return str(raw[field])
            raise CredentialError(
                f"{credential_name}_PASSWORD is JSON but carries no key field; "
                f"expected one of key/api_key/token/secret, got {sorted(raw)}"
            )
        return str(raw)

    def get_civis_api_key(self, credential_name: str = "CIVIS_API_KEY") -> str:
        """
        Get a Civis Platform API key.

        ⚠ Unlike every other credential here, this one has a hard shelf life:
        Civis caps API keys at 30 days and offers no service-account path, so
        the key WILL expire and an expired key 401s exactly like a wrong one.
        ``CivisConnector.api_key_status()`` reads the live expiry back from the
        platform — use it rather than tracking the date by hand.

        Note the direction of the naming convention here. Everywhere else,
        ``{NAME}_PASSWORD`` means "a secret that Civis will inject into a
        container". This one is the key that talks *to* Civis, and it carries
        the same suffix purely for consistency with the rest of the fleet.

        Args:
            credential_name: Name of the credential (default: "CIVIS_API_KEY").
                The env var read is {credential_name}_PASSWORD.

        Returns:
            The API key as a string

        Raises:
            CredentialError: If the credential is missing
        """
        return str(self.get_credential(credential_name))

    def get_roi_crm_credentials(self) -> Dict[str, Any]:
        """
        Get ROI CRM OAuth2 Client Credentials.

        Returns:
            Dict with 'client_id', 'client_secret', 'audience', and
            'roi_client_code' keys

        Raises:
            CredentialError: If the credential is missing, invalid JSON,
                or missing required keys
        """
        creds = self.get_credential("ROI_CRM_CREDENTIALS", is_json=True)
        if not isinstance(creds, dict):
            raise CredentialError(
                "ROI_CRM_CREDENTIALS_PASSWORD must be a valid JSON object"
            )
        missing = [
            k
            for k in ("client_id", "client_secret", "audience", "roi_client_code")
            if k not in creds
        ]
        if missing:
            raise CredentialError(
                f"ROI_CRM_CREDENTIALS_PASSWORD missing required keys: {', '.join(missing)}"
            )
        return creds

    # ── Stripe ────────────────────────────────────────────────────────
    #
    # ⚠ A Stripe API key is scoped to ONE Stripe account, and CCEF runs several
    # side by side (C3/Action Network, C4 main, the online stores). So Stripe is
    # the one credential here that is inherently plural, and two layouts are
    # accepted:
    #
    #   one variable per account -- the preferred form, because the meta-project
    #   reseeds a fixed set of named keys and this lets each account be granted
    #   and rotated on its own:
    #       STRIPE_C3_CREDENTIALS_PASSWORD={"api_name":"stripeclaudec3","key":"rk_live_..."}
    #       STRIPE_C4_CREDENTIALS_PASSWORD={"api_name":"stripeclaudec4","key":"rk_live_..."}
    #
    #   one variable holding them all, for a project that would rather keep a
    #   single entry, or a bare key for a project that only touches one account:
    #       STRIPE_CREDENTIALS_PASSWORD={"c3":"rk_live_...","c4":"rk_live_..."}
    #       STRIPE_CREDENTIALS_PASSWORD=rk_live_...
    #
    # The account name in the per-variable form comes from the variable itself:
    # STRIPE_C3_CREDENTIALS -> "c3", STRIPE_STORE_TX_CREDENTIALS -> "store_tx".

    _STRIPE_PREFIX = "STRIPE_"
    _STRIPE_SUFFIX = "_CREDENTIALS_PASSWORD"

    def get_stripe_accounts(self) -> Dict[str, Dict[str, str]]:
        """
        Get every configured Stripe account, with its key and optional api_name.

        See the module comment above for the two accepted layouts. Reads the
        environment directly rather than through the credential cache, because the
        set of accounts is discovered by scanning variable names.

        Returns:
            Dict of account name -> {"key": ..., "api_name": ...}, where
            ``api_name`` is "" when the credential did not carry one

        Raises:
            CredentialError: If no Stripe credential is set, or one is malformed,
                or a key does not look like a Stripe key

        Examples:
            >>> manager = CredentialManager()
            >>> sorted(manager.get_stripe_accounts())
            ['c3', 'c4']
            >>> manager.get_stripe_accounts()["c3"]["api_name"]
            'stripeclaudec3'
        """
        found: Dict[str, Dict[str, str]] = {}

        def record(account: str, key: str, api_name: str, source: str) -> None:
            self._validate_stripe_key(account, key, source)
            found[account] = {"key": key, "api_name": api_name}

        # 1. One variable per account.
        for env_name, raw in sorted(os.environ.items()):
            if not (env_name.startswith(self._STRIPE_PREFIX)
                    and env_name.endswith(self._STRIPE_SUFFIX)):
                continue
            middle = env_name[len(self._STRIPE_PREFIX):-len(self._STRIPE_SUFFIX)]
            if not middle:
                continue  # this is the combined STRIPE_CREDENTIALS_PASSWORD form
            account = middle.lower()
            text = (raw or "").strip()
            if not text:
                # A placeholder line the meta-project has not filled in yet is not
                # an error -- it is how --reseed-credentials knows to deliver here.
                logger.debug("Stripe account %r declared but empty; skipped", account)
                continue
            if text.startswith("{"):
                try:
                    obj = json.loads(text)
                except json.JSONDecodeError as e:
                    raise CredentialError(
                        f"Failed to parse {env_name} as JSON: {e}\n"
                        f'Expected {{"api_name":"...","key":"rk_live_..."}} '
                        f"or a bare key."
                    ) from e
                if not isinstance(obj, dict) or "key" not in obj:
                    raise CredentialError(
                        f'{env_name} must be a JSON object with a "key" field, '
                        f'e.g. {{"api_name":"...","key":"rk_live_..."}}'
                    )
                record(account, str(obj["key"]).strip(),
                       str(obj.get("api_name") or "").strip(), env_name)
            else:
                record(account, text, "", env_name)

        # 2. The combined variable, if present. Per-account entries win, so a
        #    project can override one account without rewriting the whole map.
        combined = os.environ.get(f"{self._STRIPE_PREFIX.rstrip('_')}"
                                  f"{self._STRIPE_SUFFIX}")
        if combined and combined.strip():
            text = combined.strip()
            if text.startswith("{"):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError as e:
                    raise CredentialError(
                        f"Failed to parse STRIPE_CREDENTIALS_PASSWORD as JSON: {e}\n"
                        f'Expected {{"c3":"rk_live_...","c4":"rk_live_..."}} '
                        f"or a single bare key."
                    ) from e
                if not isinstance(parsed, dict) or not parsed:
                    raise CredentialError(
                        "STRIPE_CREDENTIALS_PASSWORD must be a non-empty JSON "
                        "object of account name -> API key"
                    )
                for k, v in parsed.items():
                    if str(k) not in found:
                        record(str(k), str(v).strip(), "",
                               "STRIPE_CREDENTIALS_PASSWORD")
            elif "default" not in found:
                record("default", text, "", "STRIPE_CREDENTIALS_PASSWORD")

        if not found:
            raise CredentialError(
                "No Stripe credential found. Set either a per-account variable, "
                'e.g. STRIPE_C3_CREDENTIALS_PASSWORD={"api_name":"...",'
                '"key":"rk_live_..."}, or the combined '
                "STRIPE_CREDENTIALS_PASSWORD."
            )
        return found

    @staticmethod
    def _validate_stripe_key(account: str, key: str, source: str) -> None:
        """
        Check a Stripe key's shape before anything tries to use it.

        ⚠ The length check is the one that earns its keep: **a key truncated on
        copy/paste is rejected by Stripe with the same 401 as a revoked one**, and
        nothing downstream points at the paste. A real key runs to about 107
        characters.
        """
        if not key:
            raise CredentialError(f"{source}: account {account!r} has an empty key")
        if not key.startswith(("sk_live_", "sk_test_", "rk_live_", "rk_test_")):
            raise CredentialError(
                f"{source}: account {account!r} does not look like a Stripe secret "
                f"or restricted key (expected an sk_live_/sk_test_/rk_live_/"
                f"rk_test_ prefix)"
            )
        if len(key) < 80:
            raise CredentialError(
                f"{source}: account {account!r} is only {len(key)} characters, "
                f"which is short for a Stripe key — it was most likely truncated "
                f"when copied. Re-copy it in full from the Stripe dashboard."
            )

    def get_stripe_credentials(self) -> Dict[str, str]:
        """
        Get Stripe API keys as a map of account name -> key.

        A thin view over :meth:`get_stripe_accounts` for callers that do not need
        the ``api_name``.

        Returns:
            Dict of account name -> API key

        Raises:
            CredentialError: If no Stripe credential is set or one is malformed

        Examples:
            >>> sorted(CredentialManager().get_stripe_credentials())
            ['c3', 'c4']
        """
        return {name: rec["key"]
                for name, rec in self.get_stripe_accounts().items()}

    def get_snowflake_credentials(self) -> Dict[str, str]:
        """
        Get Snowflake connection settings and password.

        Snowflake is the one connector here that needs a handful of non-secret
        connection settings alongside its password, and the established layout
        across every CCEF project keeps them as separate variables rather than one
        JSON blob — which is also what lets ``--reseed-credentials`` rotate the
        password without touching the rest:

            SNOWFLAKE_ACCOUNT=gptulaf-cmnc_reader
            SNOWFLAKE_USER=...
            SNOWFLAKE_ROLE=PUBLIC
            SNOWFLAKE_WAREHOUSE=READER_WH
            SNOWFLAKE_DATABASE=CMNC_DATA
            SNOWFLAKE_SCHEMA=ROI
            SNOWFLAKE_CREDENTIALS_PASSWORD=...

        Only the password is required here. The connector applies defaults and
        decides which of the rest it cannot proceed without, so that the error names
        the specific missing setting.

        Returns:
            Dict with 'password' plus whichever of 'account', 'user', 'role',
            'warehouse', 'database' and 'schema' are set

        Raises:
            CredentialError: If SNOWFLAKE_CREDENTIALS_PASSWORD is missing
        """
        settings: Dict[str, str] = {
            "password": str(self.get_credential("SNOWFLAKE_CREDENTIALS")),
        }
        for key in ("account", "user", "role", "warehouse", "database", "schema"):
            value = os.getenv(f"SNOWFLAKE_{key.upper()}")
            if value:
                settings[key] = value
        return settings

    def get_tatango_credentials(self) -> Dict[str, str]:
        """
        Get Tatango (MomoGood) Messaging API v2 credentials.

        Tatango authenticates with HTTP Basic where the username is the
        Tatango **login email** and the password is the API key, so this
        reads two credentials:

        - ``TATANGO_LOGIN_EMAIL_PASSWORD`` — the login email (not a
          secret, but carried as a credential for Civis compatibility)
        - ``TATANGO_API_KEY_PASSWORD`` — the API key

        Returns:
            Dict with 'email' and 'api_key' keys

        Raises:
            CredentialError: If either credential is missing
        """
        return {
            "email": str(self.get_credential("TATANGO_LOGIN_EMAIL")),
            "api_key": str(self.get_credential("TATANGO_API_KEY")),
        }

    def clear_cache(self) -> None:
        """Clear the credentials cache. Useful for testing or credential rotation."""
        self._credentials_cache.clear()
        logger.debug("Credentials cache cleared")

    def has_credential(self, name: str) -> bool:
        """
        Check if a credential exists without raising an error.

        Args:
            name: The credential name to check

        Returns:
            True if the credential exists, False otherwise
        """
        try:
            self.get_credential(name, required=False)
            return True
        except CredentialError:
            return False


# Global singleton instance
_credential_manager = CredentialManager()


def get_credential(name: str, required: bool = True, is_json: bool = False) -> Optional[Any]:
    """
    Convenience function to get a credential using the global manager.

    Args:
        name: The credential name (e.g., 'AIRTABLE_API_KEY')
        required: Whether the credential is required
        is_json: Whether to parse as JSON

    Returns:
        The credential value

    Examples:
        >>> from ccef_connections.core.credentials import get_credential
        >>> api_key = get_credential('AIRTABLE_API_KEY')
    """
    return _credential_manager.get_credential(name, required, is_json)
