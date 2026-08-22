"""Constants for the Zyxel Switch integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "zyxel_switch"
NAME: Final = "Zyxel Switch"
MANUFACTURER: Final = "Zyxel"
VERSION: Final = "0.2.3"

PLATFORMS: Final = (Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH)

CONF_FAMILY: Final = "family"
CONF_USE_HTTPS: Final = "use_https"
CONF_VERIFY_SSL: Final = "verify_ssl"
CONF_SNMP_COMMUNITY: Final = "snmp_community"
CONF_SNMP_PORT: Final = "snmp_port"
CONF_POLL_INTERVAL: Final = "poll_interval"

FAMILY_AUTO: Final = "auto"
FAMILY_GS1200: Final = "gs1200"
FAMILY_GS1900: Final = "gs1900"
FAMILIES: Final = (FAMILY_AUTO, FAMILY_GS1200, FAMILY_GS1900)

DEFAULT_USERNAME: Final = "admin"
DEFAULT_HTTP_PORT: Final = 80
DEFAULT_HTTPS_PORT: Final = 443
DEFAULT_SNMP_PORT: Final = 161
DEFAULT_SNMP_COMMUNITY: Final = "public"
DEFAULT_POLL_INTERVAL: Final = 60
MIN_POLL_INTERVAL: Final = 30
MAX_POLL_INTERVAL: Final = 300
DEFAULT_UPDATE_INTERVAL: Final = timedelta(seconds=DEFAULT_POLL_INTERVAL)

HTTP_TIMEOUT: Final = 10
HTTP_RETRIES: Final = 3
HTTP_RETRY_DELAY: Final = 2
FULL_FETCH_RETRIES: Final = 2

SESSION_STORE_VERSION: Final = 1
SESSION_STORE_KEY_PREFIX: Final = f"{DOMAIN}.session"

LEGACY_SESSION_KEYS: Final = (
    "session_token",
    "recovery_token",
    "token",
)
