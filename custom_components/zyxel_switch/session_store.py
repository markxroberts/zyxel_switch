"""Persistent recovery-cookie storage for embedded Zyxel web sessions."""

from __future__ import annotations

from collections.abc import Mapping

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import SESSION_STORE_KEY_PREFIX, SESSION_STORE_VERSION


class ZyxelSessionStore:
    """Store only the opaque cookies needed to close an orphaned session."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, str]] = Store(
            hass,
            SESSION_STORE_VERSION,
            f"{SESSION_STORE_KEY_PREFIX}.{entry_id}",
            private=True,
        )

    async def async_load(self) -> dict[str, str]:
        """Load recovery cookies."""
        data = await self._store.async_load()
        if not isinstance(data, dict):
            return {}
        return {str(key): str(value) for key, value in data.items() if value}

    async def async_save(self, cookies: Mapping[str, str]) -> None:
        """Persist opaque recovery cookies."""
        await self._store.async_save(
            {str(key): str(value) for key, value in cookies.items() if value}
        )

    async def async_clear(self) -> None:
        """Clear recovery cookies after a successful logout."""
        await self._store.async_save({})

    async def async_remove(self) -> None:
        """Remove the private recovery store."""
        await self._store.async_remove()
