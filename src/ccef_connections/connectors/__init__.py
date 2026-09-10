"""Connectors for various services.

Connectors are imported lazily (PEP 562) so that importing this package does
not require the third-party dependencies of every connector. Connectors whose
dependencies live behind an extra raise an ImportError with the matching
``pip install`` hint if that extra is not installed.
"""

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .action_builder import ActionBuilderConnector
    from .action_network import ActionNetworkConnector
    from .airtable import AirtableConnector
    from .asana import AsanaConnector
    from .bigquery import BigQueryConnector
    from .civis import CivisConnector
    from .email_connector import EmailConnector
    from .geocodio import GeocodioConnector
    from .github import GitHubConnector
    from .helpscout import HelpScoutConnector
    from .hex import HexConnector
    from .openai import OpenAIConnector
    from .ptv import PTVConnector
    from .roi_crm import ROICRMConnector
    from .sheets import SheetsConnector
    from .sheets_writer import SheetsWriterConnector
    from .snowflake import SnowflakeConnector
    from .chariot import ChariotConnector
    from .stripe import StripeConnector
    from .tatango import TatangoConnector
    from .user_profile import UserProfileConnector
    from .zendesk import ZendeskConnector
    from .zoom import ZoomConnector

# Lazy attribute -> (submodule, required extra or None).
_LAZY_IMPORTS = {
    "ActionBuilderConnector": ("action_builder", None),
    "ActionNetworkConnector": ("action_network", None),
    "AirtableConnector": ("airtable", "airtable"),
    "AsanaConnector": ("asana", None),
    "BigQueryConnector": ("bigquery", "bigquery"),
    "CivisConnector": ("civis", None),
    "EmailConnector": ("email_connector", None),
    "GeocodioConnector": ("geocodio", None),
    "GitHubConnector": ("github", None),
    "HelpScoutConnector": ("helpscout", None),
    "HexConnector": ("hex", None),
    "OpenAIConnector": ("openai", "openai"),
    "PTVConnector": ("ptv", None),
    "ROICRMConnector": ("roi_crm", None),
    "SheetsConnector": ("sheets", "sheets"),
    "SheetsWriterConnector": ("sheets_writer", "sheets"),
    "SnowflakeConnector": ("snowflake", "snowflake"),
    "ChariotConnector": ("chariot", None),
    "StripeConnector": ("stripe", None),
    "TatangoConnector": ("tatango", None),
    "UserProfileConnector": ("user_profile", None),
    "ZendeskConnector": ("zendesk", None),
    "ZoomConnector": ("zoom", None),
}

__all__ = list(_LAZY_IMPORTS)


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        submodule, extra = _LAZY_IMPORTS[name]
        try:
            module = importlib.import_module(f".{submodule}", __name__)
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
