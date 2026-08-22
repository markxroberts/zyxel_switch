"""Sensor entities for Zyxel switches."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import (
    PERCENTAGE,
    UnitOfDataRate,
    UnitOfInformation,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import ZyxelCoordinatorEntity, ZyxelPortEntity
from .models import PortData, SwitchData
from .runtime_data import ZyxelRuntimeData

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class ZyxelPortSensorDescription(SensorEntityDescription):
    """Describe a per-port sensor."""

    value_fn: Callable[[PortData], Any]
    requires_poe: bool = False
    unavailable_when_none: bool = False


PORT_SENSOR_DESCRIPTIONS = (
    ZyxelPortSensorDescription(
        key="speed",
        name="Speed",
        icon="mdi:speedometer",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda port: port.speed_mbps,
    ),
    ZyxelPortSensorDescription(
        key="poe_power",
        name="PoE power",
        icon="mdi:flash",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda port: port.poe_power_w,
        requires_poe=True,
        unavailable_when_none=True,
    ),
    ZyxelPortSensorDescription(
        key="rx_bytes",
        name="Received",
        icon="mdi:download-network-outline",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=False,
        value_fn=lambda port: port.rx_bytes,
    ),
    ZyxelPortSensorDescription(
        key="tx_bytes",
        name="Transmitted",
        icon="mdi:upload-network-outline",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=False,
        value_fn=lambda port: port.tx_bytes,
    ),
)


@dataclass(frozen=True, kw_only=True)
class ZyxelSwitchSensorDescription(SensorEntityDescription):
    """Describe a whole-switch sensor."""

    value_fn: Callable[[SwitchData], Any]


SWITCH_SENSOR_DESCRIPTIONS = (
    ZyxelSwitchSensorDescription(
        key="uptime",
        name="Uptime",
        icon="mdi:timer-outline",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data.uptime_seconds,
    ),
    ZyxelSwitchSensorDescription(
        key="poe_consumption",
        name="PoE consumption",
        icon="mdi:flash",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.poe_consumption_w,
    ),
    ZyxelSwitchSensorDescription(
        key="poe_budget",
        name="PoE budget",
        icon="mdi:flash-outline",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.poe_budget_w,
    ),
    ZyxelSwitchSensorDescription(
        key="poe_threshold",
        name="PoE threshold",
        icon="mdi:gauge",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.poe_threshold_percent,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[ZyxelRuntimeData],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up all sensor entities from the coordinator's first snapshot."""
    coordinator = entry.runtime_data.coordinator
    entities: list[SensorEntity] = []

    for port in coordinator.data.ports.values():
        for description in PORT_SENSOR_DESCRIPTIONS:
            if description.requires_poe and not port.poe_capable:
                continue
            # GS1200 has no byte counters; do not create permanently unknown entities.
            if (
                description.key in {"rx_bytes", "tx_bytes"}
                and description.value_fn(port) is None
            ):
                continue
            entities.append(
                ZyxelPortSensor(coordinator, entry, port.number, description)
            )

    for description in SWITCH_SENSOR_DESCRIPTIONS:
        if (
            description.key == "uptime"
            or description.value_fn(coordinator.data) is not None
        ):
            entities.append(ZyxelSwitchSensor(coordinator, entry, description))

    async_add_entities(entities)


class ZyxelPortSensor(ZyxelPortEntity, SensorEntity):
    """Represent one monitored value for a physical port."""

    entity_description: ZyxelPortSensorDescription

    def __init__(self, coordinator, entry, port: int, description) -> None:
        super().__init__(coordinator, entry, port, description.key)
        self.entity_description = description

    @property
    def name(self) -> str:
        return f"{self.port_label} {self.entity_description.name}"

    @property
    def native_value(self):
        return self.entity_description.value_fn(self.port_data)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return not (
            self.entity_description.unavailable_when_none
            and self.native_value is None
        )


class ZyxelSwitchSensor(ZyxelCoordinatorEntity, SensorEntity):
    """Represent a whole-switch monitored value."""

    entity_description: ZyxelSwitchSensorDescription

    def __init__(self, coordinator, entry, description) -> None:
        super().__init__(coordinator, entry, description.key)
        self.entity_description = description

    @property
    def native_value(self):
        return self.entity_description.value_fn(self.coordinator.data)
