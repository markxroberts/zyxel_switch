"""Shared asynchronous HTTP support for Zyxel embedded web servers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import logging
import re
from typing import Any

import aiohttp

from .const import HTTP_RETRIES, HTTP_RETRY_DELAY, HTTP_TIMEOUT
from .exceptions import ZyxelCannotConnect, ZyxelInvalidAuth

_LOGGER = logging.getLogger(__name__)

SessionChangedCallback = Callable[[Mapping[str, str]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ZyxelHttpResponse:
    """Small response object detached from aiohttp's context manager."""

    status: int
    text: str
    headers: Mapping[str, str]


class ZyxelHttpClient:
    """HTTP client using Home Assistant's injected aiohttp session."""

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
        self._session = session
        self.host = host
        self.port = port
        self.use_https = use_https
        self.verify_ssl = verify_ssl
        self._cookies: dict[str, str] = dict(initial_cookies or {})
        self._session_changed = session_changed
        scheme = "https" if use_https else "http"
        display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        self.base_url = f"{scheme}://{display_host}:{port}"

    @property
    def cookies(self) -> Mapping[str, str]:
        """Return a copy of current cookies."""
        return dict(self._cookies)

    @property
    def has_cookies(self) -> bool:
        """Return whether the client has a possible authenticated session."""
        return bool(self._cookies)

    async def async_set_cookies(self, cookies: Mapping[str, str]) -> None:
        """Replace and persist the current cookies."""
        self._cookies = {
            str(key): str(value)
            for key, value in cookies.items()
            if value
        }
        if self._session_changed is not None:
            await self._session_changed(self._cookies)

    async def async_clear_cookies(self) -> None:
        """Clear and persist the current cookies."""
        if not self._cookies:
            return
        self._cookies.clear()
        if self._session_changed is not None:
            await self._session_changed({})

    async def _async_capture_cookies(self, response: aiohttp.ClientResponse) -> None:
        """Capture normal and malformed embedded-server Set-Cookie headers."""
        changed = False
        for key, morsel in response.cookies.items():
            if morsel.value and self._cookies.get(key) != morsel.value:
                self._cookies[key] = morsel.value
                changed = True

        for header in response.headers.getall("Set-Cookie", []):
            # Only take the first name/value pair; ignore malformed attributes.
            match = re.match(r"\s*([^=;,\s]+)=([^;,\s]*)", header)
            if (
                match
                and match.group(2)
                and self._cookies.get(match.group(1)) != match.group(2)
            ):
                self._cookies[match.group(1)] = match.group(2)
                changed = True

        if changed and self._session_changed is not None:
            await self._session_changed(self._cookies)

    async def async_request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        allow_redirects: bool = True,
        expected_statuses: set[int] | None = None,
        retry: bool = True,
    ) -> ZyxelHttpResponse:
        """Perform a bounded, retrying asynchronous HTTP request."""
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        attempts = HTTP_RETRIES if retry else 1
        expected = expected_statuses or set(range(200, 400))
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            if attempt > 1:
                await asyncio.sleep(HTTP_RETRY_DELAY)
            try:
                timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
                async with self._session.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    cookies=self._cookies or None,
                    allow_redirects=allow_redirects,
                    ssl=None if self.verify_ssl else False,
                    timeout=timeout,
                ) as response:
                    text = await response.text(errors="replace")
                    await self._async_capture_cookies(response)
                    if response.status in {401, 403}:
                        raise ZyxelInvalidAuth(
                            f"Authentication rejected by {self.host}"
                        )
                    if response.status not in expected:
                        error = ZyxelCannotConnect(
                            f"The switch returned HTTP {response.status} for {path}"
                        )
                        # Unsupported resources and client errors do not
                        # improve on retry.
                        if response.status < 500:
                            raise error
                        last_error = error
                        continue
                    return ZyxelHttpResponse(
                        response.status,
                        text,
                        dict(response.headers),
                    )
            except ZyxelInvalidAuth:
                raise
            except ZyxelCannotConnect as err:
                last_error = err
                if attempt == attempts:
                    raise
            except (TimeoutError, asyncio.TimeoutError, aiohttp.ClientError) as err:
                last_error = err
                _LOGGER.debug(
                    "HTTP %s %s failed for %s on attempt %s/%s: %s",
                    method,
                    path,
                    self.host,
                    attempt,
                    attempts,
                    type(err).__name__,
                )

        raise ZyxelCannotConnect(
            f"Unable to communicate with {self.host} while requesting "
            f"{path}: {last_error}"
        ) from last_error
