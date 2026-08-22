"""Data models for the Zyxel Switch integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True, slots=True)
class PortData:
    """Normalised state for one physical switch port."""

    number: int
    name: str = ""
    link_up: bool | None = None
    speed_mbps: int | None = None
    poe_capable: bool = False
    poe_enabled: bool | None = None
    poe_power_w: float | None = None
    rx_bytes: int | None = None
    tx_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class SwitchData:
    """Normalised state for a Zyxel switch."""

    family: str
    host: str
    model: str
    name: str
    mac: str | None = None
    firmware: str | None = None
    serial_number: str | None = None
    uptime_seconds: int | None = None
    led_eco: bool | None = None
    poe_consumption_w: float | None = None
    poe_budget_w: float | None = None
    poe_threshold_percent: float | None = None
    ports: Mapping[int, PortData] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Freeze the port mapping so equality is stable for the coordinator."""
        object.__setattr__(self, "ports", MappingProxyType(dict(self.ports)))

    @property
    def has_poe(self) -> bool:
        """Return whether any physical port is PoE capable."""
        return any(port.poe_capable for port in self.ports.values())


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Result returned by config-flow validation."""

    family: str
    data: SwitchData
