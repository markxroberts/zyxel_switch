"""Asynchronous read-only SNMPv2c support for GS1900 switches."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Any

from pysnmp.error import PySnmpError
from pysnmp.hlapi.v1arch.asyncio import (
    CommunityData,
    SnmpDispatcher,
    UdpTransportTarget,
    bulk_cmd,
    get_cmd,
)
from pysnmp.proto.rfc1902 import Null
from pysnmp.proto.rfc1905 import EndOfMibView

from .exceptions import ZyxelSnmpError
from .parsers import normalise_speed, parse_int

_LOGGER = logging.getLogger(__name__)

OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
OID_SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"
OID_BRIDGE_MAC = "1.3.6.1.2.1.17.1.1.0"

OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
OID_IF_TYPE = "1.3.6.1.2.1.2.2.1.3"
OID_IF_SPEED = "1.3.6.1.2.1.2.2.1.5"
OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
OID_IF_IN_OCTETS = "1.3.6.1.2.1.2.2.1.10"
OID_IF_OUT_OCTETS = "1.3.6.1.2.1.2.2.1.16"
OID_IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
OID_IF_HC_IN_OCTETS = "1.3.6.1.2.1.31.1.1.1.6"
OID_IF_HC_OUT_OCTETS = "1.3.6.1.2.1.31.1.1.1.10"
OID_IF_HIGH_SPEED = "1.3.6.1.2.1.31.1.1.1.15"
OID_IF_ALIAS = "1.3.6.1.2.1.31.1.1.1.18"

TABLE_OIDS = (
    OID_IF_DESCR,
    OID_IF_TYPE,
    OID_IF_SPEED,
    OID_IF_OPER_STATUS,
    OID_IF_IN_OCTETS,
    OID_IF_OUT_OCTETS,
    OID_IF_NAME,
    OID_IF_HC_IN_OCTETS,
    OID_IF_HC_OUT_OCTETS,
    OID_IF_HIGH_SPEED,
    OID_IF_ALIAS,
)


@dataclass(frozen=True, slots=True)
class SnmpPortData:
    """Read-only interface data from IF-MIB."""

    index: int
    name: str
    alias: str
    link_up: bool | None
    speed_mbps: int | None
    rx_bytes: int | None
    tx_bytes: int | None


@dataclass(frozen=True, slots=True)
class SnmpSwitchData:
    """Read-only GS1900 metadata and interfaces."""

    model: str
    name: str
    description: str
    firmware: str | None
    mac: str | None
    uptime_seconds: int | None
    ports: dict[int, SnmpPortData]


def _pretty(value: Any) -> str:
    return value.prettyPrint() if hasattr(value, "prettyPrint") else str(value)


def _oid_text(value: Any) -> str:
    return _pretty(value).strip(".")


def _normalise_mac(value: Any) -> str | None:
    """Normalise an SNMP OctetString or printable MAC address."""
    try:
        raw = bytes(value.asOctets())
    except (AttributeError, TypeError, ValueError):
        raw = b""
    if len(raw) == 6:
        return ":".join(f"{part:02x}" for part in raw)
    text = _pretty(value).strip().replace("-", ":")
    hex_pairs = re.findall(r"[0-9a-fA-F]{2}", text)
    if len(hex_pairs) == 6:
        return ":".join(part.lower() for part in hex_pairs)
    return None


def _model_from_description(description: str) -> str:
    match = re.search(r"\b(GS1900-[A-Z0-9-]+)\b", description, re.I)
    return match.group(1).upper() if match else "GS1900"


def _firmware_from_description(description: str) -> str | None:
    patterns = (
        r"\bV\d+(?:\.\d+)+(?:\([A-Z0-9.]+\))?\b",
        r"\b\d+\.\d+\([A-Z0-9.]+\)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, description, re.I)
        if match:
            return match.group(0)
    return None


def _physical_count(model: str, available_indexes: set[int]) -> int:
    match = re.search(r"GS1900-(\d+)", model, re.I)
    if match:
        return int(match.group(1))
    physical = [index for index in available_indexes if 0 < index < 1000]
    return max(physical, default=0)


class Gs1900SnmpClient:
    """Read GS1900 monitoring data without any SNMP SET operations."""

    def __init__(self, host: str, port: int, community: str) -> None:
        self.host = host
        self.port = port
        self.community = community

    async def _async_walk(
        self,
        dispatcher: SnmpDispatcher,
        target: UdpTransportTarget,
        oid: str,
    ) -> dict[int, Any]:
        """Walk a numeric OID subtree without invoking PySNMP's MIB loader.

        PySNMP's high-level ``bulk_walk_cmd`` always normalises its initial
        varbind through ``CommandGeneratorVarBinds.make_varbinds``. That builds
        a MIB view and performs blocking filesystem reads, even when the caller
        requests ``lookupMib=False``. Home Assistant correctly detects those
        reads as event-loop blocking I/O.

        Using ``bulk_cmd`` directly with raw numeric OID/value tuples keeps
        MIB lookup disabled and leaves the UDP transport fully asynchronous.
        """
        result: dict[int, Any] = {}
        request_oid: Any = oid
        rows_seen = 0

        try:
            while rows_seen < 256:
                (
                    error_indication,
                    error_status,
                    error_index,
                    var_binds,
                ) = await bulk_cmd(
                    dispatcher,
                    CommunityData(self.community),
                    target,
                    0,
                    min(25, 256 - rows_seen),
                    (request_oid, Null("")),
                    lookupMib=False,
                )

                if error_indication:
                    raise ZyxelSnmpError(str(error_indication))
                if error_status:
                    raise ZyxelSnmpError(
                        f"{error_status.prettyPrint()} at index {error_index}"
                    )
                if not var_binds:
                    break

                last_name: Any | None = None
                for name, value in var_binds:
                    full_oid = _oid_text(name)
                    if not full_oid.startswith(oid + "."):
                        return result
                    if isinstance(value, EndOfMibView):
                        return result

                    suffix = full_oid[len(oid) + 1 :]
                    try:
                        index = int(suffix.split(".")[-1])
                    except ValueError:
                        last_name = name
                        continue

                    result[index] = value
                    last_name = name
                    rows_seen += 1
                    if rows_seen >= 256:
                        return result

                if last_name is None or _oid_text(last_name) == _oid_text(request_oid):
                    break
                request_oid = last_name

        except (PySnmpError, TimeoutError) as err:
            raise ZyxelSnmpError(f"SNMP walk {oid} failed: {err}") from err

        return result

    async def async_fetch_data(self) -> SnmpSwitchData:
        """Fetch scalar and IF-MIB data asynchronously."""
        try:
            target = await UdpTransportTarget.create(
                (self.host, self.port), timeout=3, retries=1
            )
            with SnmpDispatcher() as dispatcher:
                scalar_result = await get_cmd(
                    dispatcher,
                    CommunityData(self.community),
                    target,
                    (OID_SYS_DESCR, Null("")),
                    (OID_SYS_UPTIME, Null("")),
                    (OID_SYS_NAME, Null("")),
                    (OID_BRIDGE_MAC, Null("")),
                    lookupMib=False,
                )
                (
                    error_indication,
                    error_status,
                    error_index,
                    scalar_binds,
                ) = scalar_result
                if error_indication:
                    raise ZyxelSnmpError(str(error_indication))
                if error_status:
                    raise ZyxelSnmpError(
                        f"{error_status.prettyPrint()} at index {error_index}"
                    )
                scalar_values = {
                    _oid_text(name): value for name, value in scalar_binds
                }

                tables: dict[str, dict[int, Any]] = {}
                for oid in TABLE_OIDS:
                    try:
                        tables[oid] = await self._async_walk(dispatcher, target, oid)
                    except ZyxelSnmpError as err:
                        # High-capacity counters and optional aliases may not exist.
                        if oid in {
                            OID_IF_HC_IN_OCTETS,
                            OID_IF_HC_OUT_OCTETS,
                            OID_IF_HIGH_SPEED,
                            OID_IF_ALIAS,
                            OID_IF_NAME,
                        }:
                            _LOGGER.debug("Optional SNMP table unavailable: %s", err)
                            tables[oid] = {}
                            continue
                        raise
        except (PySnmpError, TimeoutError, OSError) as err:
            raise ZyxelSnmpError(f"Unable to query SNMP on {self.host}: {err}") from err

        description = _pretty(scalar_values.get(OID_SYS_DESCR, ""))
        model = _model_from_description(description)
        name = _pretty(scalar_values.get(OID_SYS_NAME, "")).strip() or self.host
        uptime_ticks = parse_int(_pretty(scalar_values.get(OID_SYS_UPTIME, "")))
        uptime_seconds = int(uptime_ticks / 100) if uptime_ticks is not None else None
        mac = _normalise_mac(scalar_values.get(OID_BRIDGE_MAC))

        indexes: set[int] = set()
        for table in tables.values():
            indexes.update(table)
        port_count = _physical_count(model, indexes)

        ports: dict[int, SnmpPortData] = {}
        for index in sorted(indexes):
            if index < 1 or index > port_count:
                continue
            if_type = parse_int(_pretty(tables[OID_IF_TYPE].get(index, "")))
            if if_type not in (None, 6):  # ethernetCsmacd
                continue
            if_name = _pretty(tables.get(OID_IF_NAME, {}).get(index, "")).strip()
            if_descr = _pretty(tables[OID_IF_DESCR].get(index, "")).strip()
            alias = _pretty(tables.get(OID_IF_ALIAS, {}).get(index, "")).strip()
            oper = parse_int(_pretty(tables[OID_IF_OPER_STATUS].get(index, "")))
            high_speed = parse_int(
                _pretty(tables.get(OID_IF_HIGH_SPEED, {}).get(index, ""))
            )
            speed = high_speed or normalise_speed(
                _pretty(tables[OID_IF_SPEED].get(index, ""))
            )
            rx_value = tables.get(OID_IF_HC_IN_OCTETS, {}).get(index)
            if rx_value is None:
                rx_value = tables[OID_IF_IN_OCTETS].get(index)
            tx_value = tables.get(OID_IF_HC_OUT_OCTETS, {}).get(index)
            if tx_value is None:
                tx_value = tables[OID_IF_OUT_OCTETS].get(index)
            ports[index] = SnmpPortData(
                index=index,
                name=if_name or if_descr or f"Port {index}",
                alias=alias,
                link_up=True if oper == 1 else False if oper is not None else None,
                speed_mbps=speed,
                rx_bytes=parse_int(_pretty(rx_value)) if rx_value is not None else None,
                tx_bytes=parse_int(_pretty(tx_value)) if tx_value is not None else None,
            )

        if not ports:
            raise ZyxelSnmpError("SNMP returned no physical Ethernet ports")

        return SnmpSwitchData(
            model=model,
            name=name,
            description=description,
            firmware=_firmware_from_description(description),
            mac=mac,
            uptime_seconds=uptime_seconds,
            ports=ports,
        )
