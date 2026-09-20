"""Asynchronous device clients for Zyxel GS1200 and GS1900 switches."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
import math
import random
import re
import string
from typing import Any, Protocol

import aiohttp

from .const import FAMILY_GS1200, FAMILY_GS1900, FULL_FETCH_RETRIES
from .exceptions import (
    ZyxelActionError,
    ZyxelCannotConnect,
    ZyxelInvalidAuth,
    ZyxelSessionBusy,
    ZyxelSnmpError,
    ZyxelUnsupported,
)
from .http import SessionChangedCallback, ZyxelHttpClient
from .models import PortData, SwitchData
from .parsers import (
    bit_values,
    bools_to_mask,
    canonical_gs1200_model,
    js_array,
    js_value,
    normalise_speed,
    parse_float,
    parse_gs1900_poe_page,
    parse_int,
)
from .snmp import Gs1900SnmpClient

_LOGGER = logging.getLogger(__name__)


class ZyxelApiClient(Protocol):
    """Protocol implemented by both switch families."""

    family: str
    host: str

    async def async_fetch_data(self) -> SwitchData:
        """Fetch all data needed by coordinator entities."""

    async def async_set_poe(self, port: int, enabled: bool) -> None:
        """Set PoE state on one port."""

    async def async_set_led_eco(self, enabled: bool) -> None:
        """Set GS1200 LED Eco state."""

    async def async_logout(self) -> None:
        """Close the server-side web session."""

    async def async_prepare(self) -> None:
        """Recover and close a session left by an earlier process."""


class _BaseZyxelWebClient(ZyxelHttpClient):
    """Common web-session handling for the two embedded web interfaces."""

    family: str

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        use_https: bool,
        verify_ssl: bool,
        *,
        initial_cookies: Mapping[str, str] | None = None,
        session_changed: SessionChangedCallback | None = None,
    ) -> None:
        super().__init__(
            session,
            host,
            port,
            use_https,
            verify_ssl,
            initial_cookies=initial_cookies,
            session_changed=session_changed,
        )
        self._prepared = False
        self._logged_out = False

    async def async_prepare(self) -> None:
        """Close a recoverable session before the first new login."""
        if self._prepared:
            return
        self._prepared = True
        if not self.has_cookies:
            return
        _LOGGER.debug("Attempting recovery logout for %s", self.host)
        try:
            await self._async_logout_request()
        except ZyxelCannotConnect:
            # Preserve the cookie so another restart can retry recovery.
            self._prepared = False
            raise
        await self.async_clear_cookies()
        self._logged_out = True

    async def async_logout(self) -> None:
        """Idempotently close a server-side session owned by this client."""
        if not self.has_cookies:
            self._logged_out = True
            return
        try:
            await self._async_logout_request()
        finally:
            await self.async_clear_cookies()
            self._logged_out = True

    async def _async_logout_request(self) -> None:
        raise NotImplementedError

    async def async_set_led_eco(self, enabled: bool) -> None:
        raise ZyxelActionError(f"LED Eco control is not supported by {self.family}")


def _encode_gs1200_password(password: str) -> str:
    """Encode a GS1200 password using the switch's JavaScript algorithm."""
    output: list[str] = []
    alphabet = string.ascii_letters + string.digits
    length = len(password)
    for character in password:
        output.append(random.choice(alphabet))
        output.append(chr(ord(character) - length))
    output.append(random.choice(alphabet))
    return "".join(output)


def _encode_gs1900_password(password: str) -> str:
    """Encode a GS1900 password using its fixed 321-character form."""
    alphabet = string.ascii_letters + string.digits
    output: list[str] = []
    remaining = len(password)
    original_length = remaining
    for index in range(1, 322 - original_length):
        if index % 5 == 0 and remaining > 0:
            remaining -= 1
            output.append(password[remaining])
        elif index == 123:
            output.append(
                "0"
                if original_length < 10
                else str(math.floor(original_length / 10))
            )
        elif index == 289:
            output.append(str(original_length % 10))
        else:
            output.append(random.choice(alphabet))
    return "".join(output)


def _looks_like_login_page(text: str) -> bool:
    """Return whether a page is an unauthenticated login form."""
    lower = text.lower()
    return (
        'action="login.cgi"' in lower
        or "action='login.cgi'" in lower
        or ("name=\"username\"" in lower and "name=\"password\"" in lower)
        or ("name='username'" in lower and "name='password'" in lower)
    )


class Gs1200Client(_BaseZyxelWebClient):
    """Fully asynchronous GS1200 web client."""

    family = FAMILY_GS1200

    _POWER_PATHS = (
        "/poe_data.js",
        "/poe_power_data.js",
        "/poe_port_data.js",
        "/poe_status_data.js",
    )

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        use_https: bool,
        verify_ssl: bool,
        password: str,
        *,
        initial_cookies: Mapping[str, str] | None = None,
        session_changed: SessionChangedCallback | None = None,
    ) -> None:
        super().__init__(
            session,
            host,
            port,
            use_https,
            verify_ssl,
            initial_cookies=initial_cookies,
            session_changed=session_changed,
        )
        self._password = password
        self._last_data: SwitchData | None = None
        self._poe_override: list[bool] | None = None
        self._compatibility_warning_logged = False
        self._power_path: str | None = None

    async def _async_logout_request(self) -> None:
        await self.async_request(
            "GET",
            "/logout.html",
            allow_redirects=False,
            expected_statuses={200, 301, 302, 303},
            retry=False,
        )

    async def _async_force_tokenless_logout(self) -> None:
        """Ask the switch to drop an orphaned session when no token survived."""
        saved = dict(self.cookies)
        await self.async_clear_cookies()
        try:
            await self.async_request(
                "GET",
                "/logout.html",
                allow_redirects=False,
                expected_statuses={200, 301, 302, 303},
                retry=False,
            )
        finally:
            if saved:
                await self.async_set_cookies(saved)

    async def _async_login(self, *, allow_busy_recovery: bool = True) -> None:
        await self.async_prepare()
        if self.has_cookies:
            return
        response = await self.async_request(
            "POST",
            "/login.cgi",
            data={"password": _encode_gs1200_password(self._password)},
            allow_redirects=False,
            expected_statuses={200, 301, 302, 303},
        )
        if self.has_cookies:
            self._logged_out = False
            return
        lower = response.text.lower()
        if "logged in already" in lower:
            if allow_busy_recovery:
                await self._async_force_tokenless_logout()
                await self._async_login(allow_busy_recovery=False)
                return
            raise ZyxelSessionBusy(
                f"The GS1200 web interface on {self.host} is already in use"
            )
        if "invalid" in lower or "incorrect" in lower:
            raise ZyxelInvalidAuth(f"Invalid password for {self.host}")
        raise ZyxelInvalidAuth(
            f"The GS1200 login on {self.host} did not return a session token"
        )

    async def _async_authenticated_get(
        self, path: str, *, optional: bool = False
    ) -> str | None:
        """Fetch a page, reauthenticating once if the session expired."""
        await self._async_login()
        try:
            response = await self.async_request(
                "GET",
                path,
                expected_statuses={200, 404} if optional else {200},
                retry=not optional,
            )
        except ZyxelCannotConnect:
            if optional:
                return None
            raise
        if response.status == 404:
            return None
        if _looks_like_login_page(response.text):
            await self.async_clear_cookies()
            await self._async_login()
            response = await self.async_request("GET", path, expected_statuses={200})
            if _looks_like_login_page(response.text):
                raise ZyxelInvalidAuth(f"GS1200 session expired on {self.host}")
        return response.text

    async def _async_fetch_once(self) -> SwitchData:
        system_text = await self._async_authenticated_get("/system_data.js")
        link_text = await self._async_authenticated_get("/link_data.js")
        assert system_text is not None and link_text is not None

        statuses = js_array(link_text, "portstatus", "portStatus")
        speeds = js_array(link_text, "speed", "portSpeed")
        raw_model = js_value(system_text, "model_name") or "GS1200"
        port_count = max(len(statuses), len(speeds))
        if not port_count:
            model_match = re.search(r"GS1200-(5|8)", raw_model, re.I)
            port_count = int(model_match.group(1)) if model_match else 0
        if port_count not in {5, 8}:
            count_description = port_count or "unknown"
            raise ZyxelUnsupported(
                f"Unsupported GS1200 port count {count_description} "
                f"on {self.host}"
            )

        model, has_poe, poe_port_count = canonical_gs1200_model(raw_model, port_count)
        poe_values: list[bool] = []
        powers: list[str] = []
        if has_poe:
            state_text = await self._async_authenticated_get("/port_state_data.js")
            assert state_text is not None
            raw_mask = js_value(state_text, "portPoE")
            if raw_mask is not None:
                mask = parse_int(raw_mask, 0) or 0
                poe_values = bit_values(mask, poe_port_count)
                self._poe_override = None
            else:
                # Compatibility mode for firmware exposing only portState.
                if self._poe_override is None:
                    fallback_mask = parse_int(js_value(state_text, "portState"), 0) or 0
                    self._poe_override = bit_values(fallback_mask, poe_port_count)
                poe_values = list(self._poe_override)
                if not self._compatibility_warning_logged:
                    _LOGGER.warning(
                        "GS1200 %s omits portPoE; using compatibility state caching",
                        self.host,
                    )
                    self._compatibility_warning_logged = True

            power_text: str | None = None
            paths = (self._power_path,) if self._power_path else self._POWER_PATHS
            for path in paths:
                if path is None:
                    continue
                power_text = await self._async_authenticated_get(path, optional=True)
                if power_text is not None:
                    self._power_path = path
                    break
            if power_text:
                powers = js_array(power_text, "port_power", "portPower", "poe_power")

        ports: dict[int, PortData] = {}
        for zero_index in range(port_count):
            number = zero_index + 1
            status = (
                statuses[zero_index].strip().lower()
                if zero_index < len(statuses)
                else ""
            )
            link_up = True if status == "up" else False if status else None
            speed = (
                normalise_speed(speeds[zero_index])
                if zero_index < len(speeds)
                else None
            )
            poe_capable = has_poe and zero_index < poe_port_count
            poe_enabled = (
                poe_values[zero_index]
                if poe_capable and zero_index < len(poe_values)
                else None
            )
            power = (
                parse_float(powers[zero_index])
                if poe_capable and zero_index < len(powers)
                else None
            )
            ports[number] = PortData(
                number=number,
                name=f"Port {number}",
                link_up=link_up,
                speed_mbps=speed,
                poe_capable=poe_capable,
                poe_enabled=poe_enabled,
                poe_power_w=power,
            )

        mac = js_value(system_text, "sys_MAC")
        firmware = js_value(system_text, "sys_fmw_ver")
        name = js_value(system_text, "sys_dev_name") or self.host
        uptime = parse_int(js_value(system_text, "system_uptime"))
        led_raw = parse_int(js_value(system_text, "sys_led_state"))
        led_eco = bool(led_raw) if led_raw is not None else None
        consumption_values = [
            port.poe_power_w for port in ports.values() if port.poe_power_w is not None
        ]
        data = SwitchData(
            family=self.family,
            host=self.host,
            model=model,
            name=name,
            mac=mac,
            firmware=firmware,
            uptime_seconds=uptime,
            led_eco=led_eco,
            poe_consumption_w=(
                round(sum(consumption_values), 3) if consumption_values else None
            ),
            ports=ports,
        )
        self._last_data = data
        return data

    async def async_fetch_data(self) -> SwitchData:
        """Fetch GS1200 data with one complete-fetch retry."""
        last_error: Exception | None = None
        for attempt in range(FULL_FETCH_RETRIES):
            try:
                return await self._async_fetch_once()
            except ZyxelInvalidAuth:
                raise
            except (ZyxelCannotConnect, ZyxelSessionBusy, ZyxelUnsupported) as err:
                last_error = err
                if attempt + 1 < FULL_FETCH_RETRIES:
                    await asyncio.sleep(2)
        assert last_error is not None
        raise last_error

    async def async_set_poe(self, port: int, enabled: bool) -> None:
        """Set one GS1200 PoE port while preserving the complete bit mask."""
        if self._last_data is None:
            await self.async_fetch_data()
        assert self._last_data is not None
        poe_ports = [
            item for item in self._last_data.ports.values() if item.poe_capable
        ]
        target = self._last_data.ports.get(port)
        if target is None or not target.poe_capable:
            raise ZyxelActionError(f"Port {port} is not PoE capable")
        values = [bool(item.poe_enabled) for item in poe_ports]
        target_index = next(
            index for index, item in enumerate(poe_ports) if item.number == port
        )
        values[target_index] = enabled
        physical_count = len(self._last_data.ports)
        payload: dict[str, int] = {
            "g_port_flwcl": 0,
            "g_port_poe": bools_to_mask(values),
            "g_port_state": (1 << physical_count) - 1,
        }
        for index in range(physical_count):
            payload[f"g_port_speed{index}"] = 0

        await self._async_login()
        response = await self.async_request(
            "POST",
            "/port_state_set.cgi",
            data=payload,
            expected_statuses={200, 301, 302, 303},
            allow_redirects=False,
        )
        if _looks_like_login_page(response.text):
            await self.async_clear_cookies()
            await self._async_login()
            response = await self.async_request(
                "POST",
                "/port_state_set.cgi",
                data=payload,
                expected_statuses={200, 301, 302, 303},
                allow_redirects=False,
            )
        if response.status >= 400:
            raise ZyxelActionError(f"Failed to change PoE state on port {port}")
        if self._poe_override is not None:
            self._poe_override = values

    async def async_set_led_eco(self, enabled: bool) -> None:
        """Set GS1200 LED Eco mode."""
        await self._async_login()
        response = await self.async_request(
            "POST",
            "/led_cfg.cgi",
            data={"led_state_f": 1 if enabled else 0},
            expected_statuses={200, 301, 302, 303},
            allow_redirects=False,
        )
        if response.status >= 400:
            raise ZyxelActionError("Failed to change LED Eco state")


class Gs1900Client(_BaseZyxelWebClient):
    """Asynchronous GS1900 HTTP control plus read-only async SNMP monitoring."""

    family = FAMILY_GS1900

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        use_https: bool,
        verify_ssl: bool,
        username: str,
        password: str,
        snmp_port: int,
        snmp_community: str,
        *,
        initial_cookies: Mapping[str, str] | None = None,
        session_changed: SessionChangedCallback | None = None,
    ) -> None:
        super().__init__(
            session,
            host,
            port,
            use_https,
            verify_ssl,
            initial_cookies=initial_cookies,
            session_changed=session_changed,
        )
        self._username = username
        self._password = password
        self._snmp = Gs1900SnmpClient(host, snmp_port, snmp_community)
        self._last_data: SwitchData | None = None

    async def _async_logout_request(self) -> None:
        await self.async_request(
            "GET",
            "/cgi-bin/dispatcher.cgi",
            params={"cmd": "3"},
            expected_statuses={200, 301, 302, 303},
            allow_redirects=False,
            retry=False,
        )

    async def _async_login(self, *, force: bool = False) -> None:
        await self.async_prepare()
        if self.has_cookies and not force:
            return
        if force:
            await self.async_clear_cookies()
        first = await self.async_request(
            "POST",
            "/cgi-bin/dispatcher.cgi",
            data={
                "username": self._username,
                "password": _encode_gs1900_password(self._password),
                "login": "true;",
            },
            expected_statuses={200},
        )
        auth_id = first.text.strip()
        if not auth_id or "<html" in auth_id.lower():
            lower = auth_id.lower()
            if "already" in lower or "in use" in lower:
                raise ZyxelSessionBusy(
                    f"The GS1900 web interface on {self.host} is already in use"
                )
            raise ZyxelInvalidAuth(f"Invalid GS1900 credentials for {self.host}")
        await asyncio.sleep(1)
        second = await self.async_request(
            "POST",
            "/cgi-bin/dispatcher.cgi",
            data={"authId": auth_id, "login_chk": "true"},
            expected_statuses={200},
        )
        if "OK" not in second.text:
            raise ZyxelInvalidAuth(f"Invalid GS1900 credentials for {self.host}")
        self._logged_out = False

    async def _async_poe_page(self, *, retry_auth: bool = True) -> str:
        await self._async_login()
        response = await self.async_request(
            "GET",
            "/cgi-bin/dispatcher.cgi",
            params={"cmd": "773"},
            expected_statuses={200},
        )
        if _looks_like_login_page(response.text):
            if not retry_auth:
                raise ZyxelInvalidAuth(f"GS1900 session expired on {self.host}")
            await self._async_login(force=True)
            return await self._async_poe_page(retry_auth=False)
        return response.text

    async def async_fetch_data(self) -> SwitchData:
        """Combine SNMP interface monitoring and HTTP PoE data."""
        try:
            snmp = await self._snmp.async_fetch_data()
        except ZyxelSnmpError as err:
            raise ZyxelCannotConnect(str(err)) from err
        if (
            "GS1900" not in snmp.model.upper()
            and "GS1900" not in snmp.description.upper()
        ):
            raise ZyxelUnsupported(
                f"SNMP device at {self.host} is not identified as a GS1900"
            )

        page_html = await self._async_poe_page()
        page = parse_gs1900_poe_page(page_html)
        ports: dict[int, PortData] = {}
        for number, interface in snmp.ports.items():
            poe = page.ports.get(number)
            alias = interface.alias.strip()
            name = alias or interface.name or f"Port {number}"
            ports[number] = PortData(
                number=number,
                name=name,
                link_up=interface.link_up,
                speed_mbps=interface.speed_mbps,
                poe_capable=poe is not None,
                poe_enabled=poe.enabled if poe else None,
                poe_power_w=poe.power_w if poe else None,
                rx_bytes=interface.rx_bytes,
                tx_bytes=interface.tx_bytes,
            )

        data = SwitchData(
            family=self.family,
            host=self.host,
            model=snmp.model,
            name=snmp.name,
            mac=snmp.mac,
            firmware=snmp.firmware,
            uptime_seconds=snmp.uptime_seconds,
            poe_consumption_w=(
                snmp.poe_consumption_w
                if snmp.poe_consumption_w is not None
                else page.consumption_w
            ),
            poe_budget_w=(
                snmp.poe_budget_w if snmp.poe_budget_w is not None else page.budget_w
            ),
            poe_threshold_percent=(
                snmp.poe_threshold_percent
                if snmp.poe_threshold_percent is not None
                else page.threshold_percent
            ),
            ports=ports,
        )
        self._last_data = data
        return data

    async def async_set_poe(self, port: int, enabled: bool) -> None:
        """Control PoE through the authenticated web interface, never SNMP SET."""
        page_html = await self._async_poe_page()
        page = parse_gs1900_poe_page(page_html)
        if port not in page.ports:
            raise ZyxelActionError(f"Port {port} is not PoE capable")
        if not page.xssid:
            raise ZyxelActionError("GS1900 PoE page did not contain XSSID")
        payload: dict[str, Any] = {
            "XSSID": page.xssid,
            "portlist": str(port),
            "state": 1 if enabled else 0,
            "portPriority": 2,
            "portPowerMode": 3,
            "portRangeDetection": 0,
            "portLimitMode": 0,
            "poeTimeRange": 20,
            "cmd": 775,
            "sysSubmit": "Apply",
        }
        response = await self.async_request(
            "POST",
            "/cgi-bin/dispatcher.cgi",
            data=payload,
            expected_statuses={200},
        )
        if "window.location.replace" not in response.text:
            await self._async_login(force=True)
            page = parse_gs1900_poe_page(await self._async_poe_page())
            if not page.xssid:
                raise ZyxelActionError("GS1900 PoE page did not contain XSSID")
            payload["XSSID"] = page.xssid
            response = await self.async_request(
                "POST",
                "/cgi-bin/dispatcher.cgi",
                data=payload,
                expected_statuses={200},
            )
            if "window.location.replace" not in response.text:
                raise ZyxelActionError(f"Failed to change PoE state on port {port}")


def create_client(
    session: aiohttp.ClientSession,
    config: Mapping[str, Any],
    *,
    family: str | None = None,
    initial_cookies: Mapping[str, str] | None = None,
    session_changed: SessionChangedCallback | None = None,
) -> ZyxelApiClient:
    """Create a family-specific client from config-entry shaped data."""
    from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME

    from .const import (
        CONF_FAMILY,
        CONF_SNMP_COMMUNITY,
        CONF_SNMP_PORT,
        CONF_USE_HTTPS,
        CONF_VERIFY_SSL,
        DEFAULT_SNMP_PORT,
        FAMILY_GS1200,
        FAMILY_GS1900,
    )

    selected = family or str(config[CONF_FAMILY])
    common = {
        "session": session,
        "host": str(config[CONF_HOST]),
        "port": int(config[CONF_PORT]),
        "use_https": bool(config.get(CONF_USE_HTTPS, False)),
        "verify_ssl": bool(config.get(CONF_VERIFY_SSL, False)),
        "initial_cookies": initial_cookies,
        "session_changed": session_changed,
    }
    if selected == FAMILY_GS1200:
        return Gs1200Client(
            password=str(config[CONF_PASSWORD]),
            **common,
        )
    if selected == FAMILY_GS1900:
        return Gs1900Client(
            username=str(config[CONF_USERNAME]),
            password=str(config[CONF_PASSWORD]),
            snmp_port=int(config.get(CONF_SNMP_PORT, DEFAULT_SNMP_PORT)),
            snmp_community=str(config.get(CONF_SNMP_COMMUNITY, "")),
            **common,
        )
    raise ZyxelUnsupported(f"Unsupported switch family: {selected}")
