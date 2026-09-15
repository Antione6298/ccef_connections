"""
CCEF Connections - Reusable connection library for CCEF data integrations.

This library provides unified connection management for Airtable, OpenAI,
Google Sheets, BigQuery and Snowflake with Civis credential compatibility.

Connectors are imported lazily (PEP 562): importing this package only loads
the lightweight core. Connectors whose third-party dependencies live behind
an extra raise an ImportError with the matching ``pip install`` hint if that
extra is not installed.
"""

import importlib
from typing import TYPE_CHECKING

from .core.credentials import CredentialManager, get_credential
from .exceptions import (
    CCEFConnectionError,
    CredentialError,
    ConnectionError,
    AuthenticationError,
    RateLimitError,
    ConfigurationError,
    QueryError,
    WriteError,
)

if TYPE_CHECKING:
    from .config import ConfigManager
    from .connectors.action_builder import ActionBuilderConnector
    from .connectors.action_network import ActionNetworkConnector
    from .connectors.airtable import AirtableConnector
    from .connectors.asana import AsanaConnector
    from .connectors.bigquery import BigQueryConnector
    from .connectors.civis import CivisConnector
    from .connectors.email_connector import EmailConnector
    from .connectors.geocodio import GeocodioConnector
    from .connectors.github import GitHubConnector
    from .connectors.helpscout import HelpScoutConnector
    from .connectors.hex import HexConnector
    from .connectors.openai import OpenAIConnector
    from .connectors.ptv import PTVConnector
    from .connectors.render import RenderConnector
    from .connectors.roi_crm import ROICRMConnector
    from .connectors.sftp import SFTPConnector
    from .connectors.sheets import SheetsConnector
    from .connectors.sheets_writer import SheetsWriterConnector
    from .connectors.snowflake import SnowflakeConnector
    from .connectors.chariot import ChariotConnector
    from .connectors.stripe import StripeConnector
    from .connectors.tatango import TatangoConnector
    from .connectors.user_profile import UserProfileConnector
    from .connectors.zendesk import ZendeskConnector
    from .connectors.zoom import ZoomConnector

__version__ = "0.14.0"

# Lazy attribute -> (module, required extra or None).
# Connectors with extra=None need only the base install (requests).
_LAZY_IMPORTS = {
    "ActionBuilderConnector": ("ccef_connections.connectors.action_builder", None),
    "ActionNetworkConnector": ("ccef_connections.connectors.action_network", None),
    "AirtableConnector": ("ccef_connections.connectors.airtable", "airtable"),
    "AsanaConnector": ("ccef_connections.connectors.asana", None),
    "BigQueryConnector": ("ccef_connections.connectors.bigquery", "bigquery"),
    "CivisConnector": ("ccef_connections.connectors.civis", None),
    "EmailConnector": ("ccef_connections.connectors.email_connector", None),
    "GeocodioConnector": ("ccef_connections.connectors.geocodio", None),
    "GitHubConnector": ("ccef_connections.connectors.github", None),
    "HelpScoutConnector": ("ccef_connections.connectors.helpscout", None),
    "HexConnector": ("ccef_connections.connectors.hex", None),
    "OpenAIConnector": ("ccef_connections.connectors.openai", "openai"),
    "PTVConnector": ("ccef_connections.connectors.ptv", None),
    "RenderConnector": ("ccef_connections.connectors.render", None),
    "ROICRMConnector": ("ccef_connections.connectors.roi_crm", None),
    "SFTPConnector": ("ccef_connections.connectors.sftp", "sftp"),
    "SheetsConnector": ("ccef_connections.connectors.sheets", "sheets"),
    "SheetsWriterConnector": ("ccef_connections.connectors.sheets_writer", "sheets"),
    "SnowflakeConnector": ("ccef_connections.connectors.snowflake", "snowflake"),
    "ChariotConnector": ("ccef_connections.connectors.chariot", None),
    "StripeConnector": ("ccef_connections.connectors.stripe", None),
    "TatangoConnector": ("ccef_connections.connectors.tatango", None),
    "UserProfileConnector": ("ccef_connections.connectors.user_profile", None),
    "ZendeskConnector": ("ccef_connections.connectors.zendesk", None),
    "ZoomConnector": ("ccef_connections.connectors.zoom", None),
    # ConfigManager reads config from Google Sheets, so it needs the sheets extra
    "ConfigManager": ("ccef_connections.config", "sheets"),
}

__all__ = [
    # Main connectors
    "ActionBuilderConnector",
    "ActionNetworkConnector",
    "AirtableConnector",
    "AsanaConnector",
    "BigQueryConnector",
    "CivisConnector",
    "EmailConnector",
    "GeocodioConnector",
    "GitHubConnector",
    "HelpScoutConnector",
    "HexConnector",
    "OpenAIConnector",
    "PTVConnector",
    "RenderConnector",
    "ROICRMConnector",
    "SFTPConnector",
    "SheetsConnector",
    "SheetsWriterConnector",
    "SnowflakeConnector",
    "ChariotConnector",
    "StripeConnector",
    "TatangoConnector",
    "UserProfileConnector",
    "ZendeskConnector",
    "ZoomConnector",
    # Configuration
    "ConfigManager",
    # Credentials
    "CredentialManager",
    "get_credential",
    # Exceptions
    "CCEFConnectionError",
    "CredentialError",
    "ConnectionError",
    "AuthenticationError",
    "RateLimitError",
    "ConfigurationError",
    "QueryError",
    "WriteError",
]


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        module_path, extra = _LAZY_IMPORTS[name]
        try:
            module = importlib.import_module(module_path)
        except ImportError as e:
            if extra is not None:
                raise ImportError(
                    f"{name} requires optional dependencies that are not installed. "
                    f'Install with: pip install "ccef-connections[{extra}]"'
                ) from e
            raise
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(__all__)
