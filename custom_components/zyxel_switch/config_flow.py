"""UI configuration flows for Zyxel Switch."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import create_client
from .const import (
    CONF_FAMILY,
    CONF_POLL_INTERVAL,
    CONF_SNMP_COMMUNITY,
    CONF_SNMP_PORT,
    CONF_USE_HTTPS,
    CONF_VERIFY_SSL,
    DEFAULT_HTTP_PORT,
    DEFAULT_HTTPS_PORT,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_SNMP_PORT,
    DEFAULT_USERNAME,
    FAMILIES,
    FAMILY_AUTO,
    FAMILY_GS1200,
    FAMILY_GS1900,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    DOMAIN,
)
from .exceptions import (
    ZyxelCannotConnect,
    ZyxelError,
    ZyxelInvalidAuth,
    ZyxelSessionBusy,
    ZyxelUnsupported,
)
from .models import ValidationResult
from .runtime_data import ZyxelRuntimeData

_LOGGER = logging.getLogger(__name__)


def _password_selector() -> selector.TextSelector:
    return selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
    )


def _family_selector() -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=list(FAMILIES),
            mode=selector.SelectSelectorMode.DROPDOWN,
            translation_key="family",
        )
    )


def _normalise_host_input(data: dict[str, Any]) -> dict[str, Any]:
    """Normalise host/URL and make HTTP settings internally consistent."""
    result = dict(data)
    raw_host = str(result[CONF_HOST]).strip().rstrip("/")
    if "://" in raw_host:
        parsed = urlsplit(raw_host)
        result[CONF_HOST] = parsed.hostname or raw_host
        use_https = parsed.scheme.lower() == "https"
        result[CONF_USE_HTTPS] = use_https
        result[CONF_PORT] = (
            parsed.port
            if parsed.port is not None
            else DEFAULT_HTTPS_PORT if use_https else DEFAULT_HTTP_PORT
        )
    else:
        result[CONF_HOST] = raw_host.strip("[]")
    result[CONF_PORT] = int(result[CONF_PORT])
    result[CONF_SNMP_PORT] = int(result[CONF_SNMP_PORT])
    if not result.get(CONF_USE_HTTPS, False):
        result[CONF_VERIFY_SSL] = False
    return result


def _merge_preserved_secrets(
    data: dict[str, Any], existing: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Do not expose stored secrets in form defaults; blank preserves them."""
    if not existing:
        return data
    merged = dict(data)
    if not merged.get(CONF_PASSWORD):
        merged[CONF_PASSWORD] = existing.get(CONF_PASSWORD, "")
    if not merged.get(CONF_SNMP_COMMUNITY):
        merged[CONF_SNMP_COMMUNITY] = existing.get(CONF_SNMP_COMMUNITY, "")
    return merged


def _unique_id_for(result: ValidationResult, data: Mapping[str, Any]) -> str:
    """Prefer a hardware MAC; fall back to the management endpoint."""
    if result.data.mac:
        compact = "".join(
            character for character in result.data.mac if character.isalnum()
        )
        if len(compact) == 12:
            return compact.lower()
    return f"{str(data[CONF_HOST]).lower()}:{int(data[CONF_PORT])}"


def _looks_like_mac_unique_id(unique_id: str | None) -> bool:
    if not unique_id:
        return False
    compact = "".join(character for character in unique_id if character.isalnum())
    return len(compact) == 12 and all(
        character in "0123456789abcdefABCDEF" for character in compact
    )


class ZyxelSwitchConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle UI setup, reauthentication and reconfiguration."""

    VERSION = 2
    MINOR_VERSION = 1

    def _schema(
        self,
        defaults: Mapping[str, Any] | None = None,
        *,
        credentials_only: bool = False,
    ) -> vol.Schema:
        defaults = defaults or {}
        if credentials_only:
            return vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME,
                        default=defaults.get(CONF_USERNAME, DEFAULT_USERNAME),
                    ): str,
                    vol.Required(CONF_PASSWORD, default=""): _password_selector(),
                }
            )

        use_https = bool(defaults.get(CONF_USE_HTTPS, False))
        return vol.Schema(
            {
                vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
                vol.Required(
                    CONF_PORT,
                    default=defaults.get(
                        CONF_PORT,
                        DEFAULT_HTTPS_PORT if use_https else DEFAULT_HTTP_PORT,
                    ),
                ): vol.All(int, vol.Range(min=1, max=65535)),
                vol.Required(
                    CONF_USERNAME,
                    default=defaults.get(CONF_USERNAME, DEFAULT_USERNAME),
                ): str,
                vol.Required(CONF_PASSWORD, default=""): _password_selector(),
                vol.Required(
                    CONF_FAMILY,
                    default=defaults.get(CONF_FAMILY, FAMILY_AUTO),
                ): _family_selector(),
                vol.Required(
                    CONF_USE_HTTPS,
                    default=use_https,
                ): bool,
                vol.Required(
                    CONF_VERIFY_SSL,
                    default=defaults.get(CONF_VERIFY_SSL, False),
                ): bool,
                vol.Optional(CONF_SNMP_COMMUNITY, default=""): _password_selector(),
                vol.Required(
                    CONF_SNMP_PORT,
                    default=defaults.get(CONF_SNMP_PORT, DEFAULT_SNMP_PORT),
                ): vol.All(int, vol.Range(min=1, max=65535)),
            }
        )

    async def _async_validate_family(
        self,
        data: Mapping[str, Any],
        family: str,
    ) -> ValidationResult:
        session = async_create_clientsession(
            self.hass,
            auto_cleanup=False,
            cookie_jar=aiohttp.DummyCookieJar(),
        )
        client = create_client(session, data, family=family)
        try:
            switch_data = await client.async_fetch_data()
            return ValidationResult(family=family, data=switch_data)
        finally:
            if getattr(client, "has_cookies", False):
                try:
                    await client.async_logout()
                except ZyxelError as err:
                    _LOGGER.debug(
                        "Temporary validation logout failed for %s: %s",
                        data.get(CONF_HOST),
                        err,
                    )
            session.detach()

    async def _async_validate(self, data: Mapping[str, Any]) -> ValidationResult:
        selected = str(data[CONF_FAMILY])
        if selected != FAMILY_AUTO:
            return await self._async_validate_family(data, selected)

        errors: list[Exception] = []
        for family in (FAMILY_GS1200, FAMILY_GS1900):
            try:
                return await self._async_validate_family(data, family)
            except (ZyxelCannotConnect, ZyxelInvalidAuth, ZyxelUnsupported) as err:
                errors.append(err)
        if any(isinstance(error, ZyxelInvalidAuth) for error in errors):
            raise ZyxelInvalidAuth("The switch rejected the supplied credentials")
        if errors:
            raise ZyxelCannotConnect(str(errors[-1]))
        raise ZyxelUnsupported("Unable to detect a supported switch family")

    @staticmethod
    def _loaded_coordinator(entry):
        runtime = getattr(entry, "runtime_data", None)
        if isinstance(runtime, ZyxelRuntimeData):
            return runtime.coordinator
        return None

    async def _async_validate_with_handoff(
        self,
        entry,
        data: Mapping[str, Any],
    ) -> ValidationResult:
        coordinator = self._loaded_coordinator(entry)
        if coordinator is not None:
            await coordinator.async_pause_for_validation()
        succeeded = False
        try:
            result = await self._async_validate(data)
            succeeded = True
            return result
        finally:
            if coordinator is not None:
                await coordinator.async_resume_after_validation(refresh=not succeeded)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create a new config entry entirely through the UI."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _normalise_host_input(user_input)
            try:
                result = await self._async_validate(data)
            except ZyxelInvalidAuth:
                errors["base"] = "invalid_auth"
            except ZyxelSessionBusy:
                errors["base"] = "session_busy"
            except ZyxelUnsupported:
                errors["base"] = "unsupported"
            except ZyxelCannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error validating Zyxel switch")
                errors["base"] = "unknown"
            else:
                data[CONF_FAMILY] = result.family
                await self.async_set_unique_id(_unique_id_for(result, data))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=result.data.name or result.data.model or data[CONF_HOST],
                    data=data,
                    options={CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self._schema(user_input),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update host, protocol, credentials or SNMP settings."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _normalise_host_input(
                _merge_preserved_secrets(user_input, entry.data)
            )
            try:
                result = await self._async_validate_with_handoff(entry, data)
            except ZyxelInvalidAuth:
                errors["base"] = "invalid_auth"
            except ZyxelSessionBusy:
                errors["base"] = "session_busy"
            except ZyxelUnsupported:
                errors["base"] = "unsupported"
            except ZyxelCannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error reconfiguring Zyxel switch")
                errors["base"] = "unknown"
            else:
                new_unique_id = _unique_id_for(result, data)
                if (
                    _looks_like_mac_unique_id(entry.unique_id)
                    and _looks_like_mac_unique_id(new_unique_id)
                    and entry.unique_id != new_unique_id
                ):
                    coordinator = self._loaded_coordinator(entry)
                    if coordinator is not None:
                        await coordinator.async_request_refresh()
                    return self.async_abort(reason="wrong_device")
                data[CONF_FAMILY] = result.family
                return self.async_update_reload_and_abort(
                    entry,
                    title=result.data.name or result.data.model or entry.title,
                    data_updates=data,
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._schema(entry.data | (user_input or {})),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start a credentials-only reauthentication flow."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate replacement web credentials and reload the entry."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            updates = _merge_preserved_secrets(user_input, entry.data)
            data = dict(entry.data) | updates
            try:
                result = await self._async_validate_with_handoff(entry, data)
            except ZyxelInvalidAuth:
                errors["base"] = "invalid_auth"
            except ZyxelSessionBusy:
                errors["base"] = "session_busy"
            except ZyxelUnsupported:
                errors["base"] = "unsupported"
            except ZyxelCannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error reauthenticating Zyxel switch")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_USERNAME: data[CONF_USERNAME],
                        CONF_PASSWORD: data[CONF_PASSWORD],
                        CONF_FAMILY: result.family,
                    },
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self._schema(entry.data, credentials_only=True),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        """Return the options flow."""
        return ZyxelSwitchOptionsFlow(config_entry)


class ZyxelSwitchOptionsFlow(config_entries.OptionsFlow):
    """Configure the coordinator polling interval."""

    def __init__(self, config_entry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage integration options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = int(
            self._config_entry.options.get(
                CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
            )
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_POLL_INTERVAL, default=current): vol.All(
                        int,
                        vol.Range(
                            min=MIN_POLL_INTERVAL,
                            max=MAX_POLL_INTERVAL,
                        ),
                    )
                }
            ),
        )
