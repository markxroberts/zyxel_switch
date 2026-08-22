"""Switch entities for Zyxel PoE and LED Eco controls."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import ZyxelCoordinatorEntity, ZyxelPortEntity
from .runtime_data import ZyxelRuntimeData

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[ZyxelRuntimeData],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up controllable switch entities."""
    coordinator = entry.runtime_data.coordinator
    entities: list[SwitchEntity] = [
        ZyxelPoeSwitch(coordinator, entry, port.number)
        for port in coordinator.data.ports.values()
        if port.poe_capable
    ]
    if coordinator.data.led_eco is not None:
        entities.append(ZyxelLedEcoSwitch(coordinator, entry))
    async_add_entities(entities)


class ZyxelPoeSwitch(ZyxelPortEntity, SwitchEntity):
    """Control PoE on one physical port."""

    _attr_translation_key = "port_poe"

    def __init__(self, coordinator, entry, port: int) -> None:
        super().__init__(coordinator, entry, port, "poe")

    @property
    def name(self) -> str:
        return f"{self.port_label} PoE"

    @property
    def is_on(self) -> bool | None:
        return self.port_data.poe_enabled

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_poe(self.port, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_poe(self.port, False)


class ZyxelLedEcoSwitch(ZyxelCoordinatorEntity, SwitchEntity):
    """Control the GS1200 front-panel LED Eco mode."""

    _attr_name = "LED Eco"
    _attr_translation_key = "led_eco"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "led-eco")

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.led_eco

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_led_eco(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_led_eco(False)
