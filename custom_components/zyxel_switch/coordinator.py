"""DataUpdateCoordinator for Zyxel switches."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ZyxelApiClient
from .const import DOMAIN
from .exceptions import ZyxelError, ZyxelInvalidAuth
from .models import SwitchData

_LOGGER = logging.getLogger(__name__)


class ZyxelSwitchCoordinator(DataUpdateCoordinator[SwitchData]):
    """Centralise polling, actions and session handoff for one switch."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: ZyxelApiClient,
        poll_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(seconds=poll_interval),
            always_update=False,
        )
        self.client = client
        self._operation_lock = asyncio.Lock()
        self._poll_allowed = asyncio.Event()
        self._poll_allowed.set()
        self._shutdown = False

    async def _async_update_data(self) -> SwitchData:
        """Fetch one coherent snapshot for every entity."""
        await self._poll_allowed.wait()
        async with self._operation_lock:
            if self._shutdown:
                raise UpdateFailed("Coordinator is shutting down")
            try:
                return await self.client.async_fetch_data()
            except ZyxelInvalidAuth as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except ZyxelError as err:
                raise UpdateFailed(str(err)) from err

    @staticmethod
    def _action_error(error: ZyxelError) -> HomeAssistantError:
        """Build a translated entity-action exception."""
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="action_failed",
            translation_placeholders={"error": str(error)},
        )

    async def async_set_poe(self, port: int, enabled: bool) -> None:
        """Serialise a PoE action with polling and refresh afterwards."""
        await self._poll_allowed.wait()
        try:
            async with self._operation_lock:
                if self._shutdown:
                    raise UpdateFailed("Coordinator is shutting down")
                await self.client.async_set_poe(port, enabled)
        except ZyxelError as err:
            raise self._action_error(err) from err
        await self.async_request_refresh()

    async def async_set_led_eco(self, enabled: bool) -> None:
        """Serialise an LED Eco action with polling and refresh afterwards."""
        await self._poll_allowed.wait()
        try:
            async with self._operation_lock:
                if self._shutdown:
                    raise UpdateFailed("Coordinator is shutting down")
                await self.client.async_set_led_eco(enabled)
        except ZyxelError as err:
            raise self._action_error(err) from err
        await self.async_request_refresh()

    async def async_pause_for_validation(self) -> None:
        """Stop new I/O and release the active web session for a flow."""
        self._poll_allowed.clear()
        async with self._operation_lock:
            try:
                await self.client.async_logout()
            except ZyxelError as err:
                _LOGGER.debug(
                    "Session logout before reconfiguration failed for %s: %s",
                    self.config_entry.title,
                    err,
                )

    async def async_resume_after_validation(self, *, refresh: bool) -> None:
        """Allow normal coordinator work after a validation handoff."""
        if self._shutdown:
            return
        self._poll_allowed.set()
        if refresh:
            await self.async_request_refresh()

    async def async_shutdown(self) -> None:
        """Cancel coordinator work and close the switch web session."""
        if self._shutdown:
            return
        self._shutdown = True
        self._poll_allowed.set()
        await super().async_shutdown()
        async with self._operation_lock:
            try:
                await self.client.async_logout()
            except ZyxelError as err:
                _LOGGER.debug(
                    "Session logout during unload failed for %s: %s",
                    self.config_entry.title,
                    err,
                )
