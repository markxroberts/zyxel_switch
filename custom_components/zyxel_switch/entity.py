"""Common coordinator entity for Zyxel switches."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_USE_HTTPS, DOMAIN, MANUFACTURER
from .coordinator import ZyxelSwitchCoordinator
from .models import PortData
from .runtime_data import ZyxelRuntimeData


def _physical_device_id(entry: ConfigEntry, mac: str | None) -> str | None:
    """Return the stable MAC identity used by 0.1.x device registry entries."""
    # The config-entry unique ID is the hardware identity captured when the
    # switch was first configured. Prefer it over a later-discovered MAC so an
    # integration upgrade cannot silently move the entities to a new device.
    candidates = (entry.unique_id, mac)
    for candidate in candidates:
        if not candidate:
            continue
        compact = "".join(char for char in str(candidate) if char.isalnum())
        if len(compact) != 12 or any(
            char not in "0123456789abcdefABCDEF" for char in compact
        ):
            continue
        return dr.format_mac(compact)
    return None


class ZyxelCoordinatorEntity(CoordinatorEntity[ZyxelSwitchCoordinator]):
    """Base class for all Zyxel switch entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ZyxelSwitchCoordinator,
        entry: ConfigEntry[ZyxelRuntimeData],
        unique_suffix: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._unique_base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{self._unique_base}-{unique_suffix}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return current device registry information."""
        data = self.coordinator.data
        physical_id = _physical_device_id(self._entry, data.mac)
        connections = (
            {(CONNECTION_NETWORK_MAC, physical_id)}
            if physical_id is not None
            else set()
        )
        identifiers = (
            {(DOMAIN, physical_id)}
            if physical_id is not None
            else {(DOMAIN, self._entry.entry_id)}
        )
        host = str(self._entry.data[CONF_HOST])
        display_host = (
            f"[{host}]" if ":" in host and not host.startswith("[") else host
        )
        scheme = "https" if self._entry.data.get(CONF_USE_HTTPS) else "http"
        return DeviceInfo(
            identifiers=identifiers,
            connections=connections,
            manufacturer=MANUFACTURER,
            model=data.model,
            name=data.name or self._entry.title,
            sw_version=data.firmware,
            serial_number=data.serial_number,
            configuration_url=(
                f"{scheme}://{display_host}:{self._entry.data[CONF_PORT]}"
            ),
        )


class ZyxelPortEntity(ZyxelCoordinatorEntity):
    """Base class for an entity associated with one physical port."""

    def __init__(
        self,
        coordinator: ZyxelSwitchCoordinator,
        entry: ConfigEntry[ZyxelRuntimeData],
        port: int,
        unique_suffix: str,
    ) -> None:
        super().__init__(coordinator, entry, f"port-{port}-{unique_suffix}")
        self.port = port

    @property
    def available(self) -> bool:
        """Return unavailable if the port disappears from a snapshot."""
        return super().available and self.port in self.coordinator.data.ports

    @property
    def port_data(self) -> PortData:
        """Return current data for this port."""
        return self.coordinator.data.ports[self.port]

    @property
    def port_label(self) -> str:
        """Return a stable, human-friendly port label including IF-MIB alias."""
        name = self.port_data.name.strip()
        default_names = {f"Port {self.port}", str(self.port), f"{self.port}"}
        if name and name not in default_names:
            return f"Port {self.port} {name}"
        return f"Port {self.port}"
