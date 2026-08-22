"""Diagnostics support for Zyxel Switch."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_SNMP_COMMUNITY
from .models import SwitchData
from .runtime_data import ZyxelRuntimeData

TO_REDACT = {
    "password",
    CONF_SNMP_COMMUNITY,
    "token",
    "cookie",
    "cookies",
}


def _serialise_data(data: SwitchData) -> dict[str, Any]:
    """Convert immutable coordinator data to JSON-safe diagnostics."""
    return {
        "family": data.family,
        "model": data.model,
        "name": data.name,
        "firmware": data.firmware,
        "mac": data.mac,
        "uptime_seconds": data.uptime_seconds,
        "led_eco": data.led_eco,
        "poe_consumption_w": data.poe_consumption_w,
        "poe_budget_w": data.poe_budget_w,
        "poe_threshold_percent": data.poe_threshold_percent,
        "ports": {
            str(number): {
                "number": port.number,
                "name": port.name,
                "link_up": port.link_up,
                "speed_mbps": port.speed_mbps,
                "poe_capable": port.poe_capable,
                "poe_enabled": port.poe_enabled,
                "poe_power_w": port.poe_power_w,
                "rx_bytes": port.rx_bytes,
                "tx_bytes": port.tx_bytes,
            }
            for number, port in data.ports.items()
        },
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry[ZyxelRuntimeData],
) -> dict[str, Any]:
    """Return redacted config-entry and coordinator diagnostics."""
    coordinator = entry.runtime_data.coordinator
    return {
        "entry": async_redact_data(
            {
                "title": entry.title,
                "data": dict(entry.data),
                "options": dict(entry.options),
                "version": entry.version,
                "minor_version": entry.minor_version,
            },
            TO_REDACT,
        ),
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval is not None
                else None
            ),
            "data": _serialise_data(coordinator.data),
        },
    }
