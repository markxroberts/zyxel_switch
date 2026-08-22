"""Binary sensors for Zyxel switch port links."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import ZyxelPortEntity
from .runtime_data import ZyxelRuntimeData

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[ZyxelRuntimeData],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up link sensors from a config entry."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        ZyxelLinkBinarySensor(coordinator, entry, port)
        for port in coordinator.data.ports
    )


class ZyxelLinkBinarySensor(ZyxelPortEntity, BinarySensorEntity):
    """Represent physical link state for one port."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_icon = "mdi:ethernet"

    def __init__(self, coordinator, entry, port: int) -> None:
        super().__init__(coordinator, entry, port, "link")

    @property
    def name(self) -> str:
        return f"{self.port_label} link"

    @property
    def is_on(self) -> bool | None:
        return self.port_data.link_up
