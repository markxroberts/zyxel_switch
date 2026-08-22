"""Helpers for preserving entity registry identities across integration upgrades."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from .models import SwitchData


@dataclass(frozen=True, slots=True)
class ExpectedEntity:
    """Describe one entity created from a coordinator snapshot."""

    domain: str
    suffix: str


_FEATURE_ALIASES: dict[str, tuple[tuple[str, ...], ...]] = {
    "link": (("link",), ("connectivity",)),
    "speed": (("speed",),),
    "poe": (("poe",),),
    "poe_power": (("poe", "power"), ("power",)),
    "rx_bytes": (("rx", "bytes"), ("received",), ("receive",)),
    "tx_bytes": (("tx", "bytes"), ("transmitted",), ("transmit",)),
    "led_eco": (("led", "eco"),),
    "uptime": (("uptime",),),
    "poe_consumption": (("poe", "consumption"), ("poe", "used")),
    "poe_budget": (("poe", "budget"),),
    "poe_threshold": (("poe", "threshold"),),
}


def expected_entities(data: SwitchData) -> tuple[ExpectedEntity, ...]:
    """Return exactly the entities that the current platforms will create."""
    result: list[ExpectedEntity] = []

    for port in data.ports.values():
        result.extend(
            (
                ExpectedEntity("binary_sensor", f"port-{port.number}-link"),
                ExpectedEntity("sensor", f"port-{port.number}-speed"),
            )
        )
        if port.poe_capable:
            result.extend(
                (
                    ExpectedEntity("switch", f"port-{port.number}-poe"),
                    ExpectedEntity("sensor", f"port-{port.number}-poe_power"),
                )
            )
        if port.rx_bytes is not None:
            result.append(ExpectedEntity("sensor", f"port-{port.number}-rx_bytes"))
        if port.tx_bytes is not None:
            result.append(ExpectedEntity("sensor", f"port-{port.number}-tx_bytes"))

    if data.led_eco is not None:
        result.append(ExpectedEntity("switch", "led-eco"))

    # Uptime is always registered; it may temporarily have a None state.
    result.append(ExpectedEntity("sensor", "uptime"))
    for suffix, value in (
        ("poe_consumption", data.poe_consumption_w),
        ("poe_budget", data.poe_budget_w),
        ("poe_threshold", data.poe_threshold_percent),
    ):
        if value is not None:
            result.append(ExpectedEntity("sensor", suffix))

    return tuple(result)


def _tokens(*values: Any) -> tuple[str, ...]:
    text = " ".join(str(value) for value in values if value)
    return tuple(re.findall(r"[a-z0-9]+", text.casefold()))


def _contains_sequence(tokens: tuple[str, ...], sequence: tuple[str, ...]) -> bool:
    length = len(sequence)
    return any(
        tokens[index : index + length] == sequence
        for index in range(len(tokens))
    )


def _feature_matches(tokens: tuple[str, ...], feature: str) -> bool:
    return any(
        _contains_sequence(tokens, alias)
        for alias in _FEATURE_ALIASES.get(feature, ((feature,),))
    )


def entry_matches_expected(entry: Any, expected: ExpectedEntity) -> bool:
    """Return whether a registry-like entry represents an expected entity.

    The 0.1.x test releases used a different unique-ID namespace from 0.2.0.
    Matching therefore uses the registry unique ID, entity ID and original name,
    while requiring the same platform domain and a clear port/feature signature.
    """
    entry_domain = getattr(entry, "domain", None)
    if entry_domain is None:
        entity_id = str(getattr(entry, "entity_id", ""))
        entry_domain = entity_id.partition(".")[0]
    if entry_domain != expected.domain:
        return False

    tokens = _tokens(
        getattr(entry, "unique_id", None),
        getattr(entry, "entity_id", None),
        getattr(entry, "original_name", None),
        getattr(entry, "name", None),
    )

    port_match = re.fullmatch(r"port-(\d+)-(.+)", expected.suffix)
    if port_match:
        port = port_match.group(1)
        feature = port_match.group(2)
        has_port_signature = _contains_sequence(tokens, ("port", port))
        return has_port_signature and _feature_matches(tokens, feature)

    feature = expected.suffix.replace("-", "_")
    return _feature_matches(tokens, feature)
