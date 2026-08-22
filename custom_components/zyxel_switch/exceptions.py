"""Exceptions raised by the Zyxel Switch integration."""

from __future__ import annotations


class ZyxelError(Exception):
    """Base exception for Zyxel switch failures."""


class ZyxelCannotConnect(ZyxelError):
    """The switch could not be reached or returned an unusable response."""


class ZyxelInvalidAuth(ZyxelError):
    """Authentication was rejected."""


class ZyxelSessionBusy(ZyxelError):
    """The embedded web server reports that another session is active."""


class ZyxelUnsupported(ZyxelError):
    """The device is not a supported Zyxel switch family."""


class ZyxelActionError(ZyxelError):
    """A requested switch action failed."""


class ZyxelSnmpError(ZyxelError):
    """An SNMP request failed."""
