"""Pure response parsers used by the Zyxel Switch clients."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import logging
import re
from typing import Any

from bs4 import BeautifulSoup

_LOGGER = logging.getLogger(__name__)

_JS_ASSIGNMENT_TEMPLATE = (
    r"(?:\bvar\s+)?{name}\s*=\s*(?P<value>"
    r"\[[\s\S]*?\]|'(?:\\.|[^'])*'|"
    r'"(?:\\.|[^"])*"|[^;\r\n]+)\s*;?'
)


def js_value(text: str, name: str) -> str | None:
    """Return a JavaScript variable value without surrounding quotes."""
    match = re.search(
        _JS_ASSIGNMENT_TEMPLATE.format(name=re.escape(name)),
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    raw = match.group("value").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    return raw


def js_array(text: str, *names: str) -> list[str]:
    """Return a simple JavaScript array as strings."""
    raw: str | None = None
    for name in names:
        raw = js_value(text, name)
        if raw is not None:
            break
    if raw is None:
        return []
    candidate = raw.strip()
    if not candidate.startswith("["):
        candidate = f"[{candidate}]"
    try:
        value = ast.literal_eval(candidate)
    except (SyntaxError, ValueError):
        inner = candidate.strip()[1:-1]
        return [item.strip().strip("'\"") for item in inner.split(",") if item.strip()]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value]


def parse_int(value: Any, default: int | None = None) -> int | None:
    """Convert common web/SNMP values to int."""
    if value is None:
        return default
    try:
        return int(str(value).strip(), 0)
    except (TypeError, ValueError):
        match = re.search(r"-?\d+", str(value))
        return int(match.group(0)) if match else default


def parse_float(value: Any, default: float | None = None) -> float | None:
    """Convert a value containing a number to float."""
    if value is None:
        return default
    match = re.search(r"-?\d+(?:[.,]\d+)?", str(value))
    if not match:
        return default
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return default


def bit_values(mask: int, count: int) -> list[bool]:
    """Expand a bit mask from least-significant port to highest."""
    return [bool(mask & (1 << bit)) for bit in range(count)]


def bools_to_mask(values: list[bool]) -> int:
    """Build a bit mask from least-significant port to highest."""
    mask = 0
    for index, value in enumerate(values):
        if value:
            mask |= 1 << index
    return mask


def normalise_speed(value: str | int | None) -> int | None:
    """Normalise web/SNMP speed representations to Mbit/s."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text or text in {"0", "down", "none", "unknown", "--", "-"}:
        return None
    number = parse_float(text)
    if number is None:
        return None
    if "gb" in text:
        return int(number * 1000)
    if "kb" in text:
        return max(1, int(number / 1000))
    if "mb" in text:
        return int(number)
    numeric = int(number)
    # ifSpeed is in bits/s; web UI values are usually already Mbit/s.
    if numeric >= 1_000_000:
        return int(numeric / 1_000_000)
    return numeric


def canonical_gs1200_model(raw_model: str, port_count: int) -> tuple[str, bool, int]:
    """Return canonical model, PoE capability and number of PoE ports."""
    model = re.sub(r"\s+", " ", raw_model.strip())
    upper = model.upper().replace("_", "-")

    explicit_non_poe = bool(
        re.search(r"GS1200-(?:5|8)(?:\s+V2)?$", upper)
        and "HP" not in upper
    )
    if explicit_non_poe:
        number = 8 if "-8" in upper else 5
        return f"GS1200-{number} v2", False, 0

    if "GS1200-5HP" in upper:
        return "GS1200-5HP v2", True, 4
    if "GS1200-8HP" in upper:
        return "GS1200-8HP v2", True, 4

    # Some HP firmware reports only the generic family. Preserve the tested
    # 0.1.10 compatibility behaviour and infer the chassis from link data.
    if "GS1200" in upper:
        if port_count >= 8:
            return "GS1200-8HP v2", True, 4
        if port_count >= 5:
            return "GS1200-5HP v2", True, 4

    return model or "GS1200", "HP" in upper, min(4, port_count)


@dataclass(slots=True)
class Gs1900PoePort:
    """Parsed PoE web table row."""

    port: int
    enabled: bool
    power_w: float | None
    maximum_power_w: float | None


@dataclass(slots=True)
class Gs1900PoePage:
    """Parsed GS1900 PoE page."""

    xssid: str | None
    ports: dict[int, Gs1900PoePort]
    consumption_w: float | None
    budget_w: float | None
    threshold_percent: float | None


def _port_number(text: str) -> int | None:
    """Extract a physical port number from common labels."""
    numbers = re.findall(r"\d+", text)
    return int(numbers[-1]) if numbers else None


def parse_gs1900_poe_page(html: str) -> Gs1900PoePage:
    """Parse the GS1900 cmd=773 page tolerantly across firmware revisions."""
    soup = BeautifulSoup(html, "html.parser")
    xssid_tag = soup.find("input", attrs={"name": re.compile(r"^XSSID$", re.I)})
    xssid = xssid_tag.get("value") if xssid_tag else None

    ports: dict[int, Gs1900PoePort] = {}
    for row in soup.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td")]
        if len(cells) < 8:
            continue

        # Known GS1900 firmwares expose 12 or 13 cells. The port is the third
        # cell and the administrative state the fourth.
        if len(cells) in (12, 13):
            port = _port_number(cells[2])
            state_text = cells[3].lower()
            power_index = 8 if len(cells) == 13 else 7
            max_index = power_index + 1
        else:
            # Fallback: locate a cell that looks like a port and a nearby
            # enable/disable state, then find the last two power-like values.
            port = None
            state_text = ""
            power_index = -1
            max_index = -1
            for index, cell in enumerate(cells):
                if port is None and re.fullmatch(r"(?:port\s*)?\d+", cell, re.I):
                    port = _port_number(cell)
                if cell.lower() in {"enable", "enabled", "disable", "disabled"}:
                    state_text = cell.lower()
            numeric_indexes = [
                index
                for index, cell in enumerate(cells)
                if re.fullmatch(
                    r"\s*\d+(?:[.,]\d+)?\s*(?:mw|w)?\s*",
                    cell,
                    re.I,
                )
            ]
            if len(numeric_indexes) >= 2:
                power_index, max_index = numeric_indexes[-2:]

        if port is None or not state_text:
            continue
        enabled = state_text.startswith("enable")
        power = parse_float(cells[power_index]) if power_index >= 0 else None
        maximum = parse_float(cells[max_index]) if max_index >= 0 else None
        if power is not None and "mw" in cells[power_index].lower():
            power /= 1000
        elif power is not None and power > 500:
            # Known page values are expressed in milliwatts without a suffix.
            power /= 1000
        if maximum is not None and "mw" in cells[max_index].lower():
            maximum /= 1000
        elif maximum is not None and maximum > 500:
            maximum /= 1000
        ports[port] = Gs1900PoePort(port, enabled, power, maximum)

    page_text = soup.get_text(" ", strip=True)

    def labelled_number(pattern: str) -> float | None:
        match = re.search(pattern + r"[^\d]{0,30}(\d+(?:[.,]\d+)?)", page_text, re.I)
        return parse_float(match.group(1)) if match else None

    consumption = labelled_number(
        r"(?:total\s+)?(?:poe\s+)?(?:power\s+)?consum(?:ption|ed)"
    )
    budget = labelled_number(r"(?:total\s+)?(?:poe\s+)?power\s+budget")
    threshold = labelled_number(r"(?:poe\s+)?threshold")
    if consumption is None and ports:
        values = [port.power_w for port in ports.values() if port.power_w is not None]
        consumption = round(sum(values), 3) if values else None

    return Gs1900PoePage(xssid, ports, consumption, budget, threshold)
