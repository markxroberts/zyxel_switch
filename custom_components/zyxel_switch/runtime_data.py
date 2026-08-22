"""Runtime data stored on the config entry."""

from __future__ import annotations

from dataclasses import dataclass

from .api import ZyxelApiClient
from .coordinator import ZyxelSwitchCoordinator
from .session_store import ZyxelSessionStore


@dataclass(slots=True)
class ZyxelRuntimeData:
    """Objects owned by one loaded config entry."""

    client: ZyxelApiClient
    coordinator: ZyxelSwitchCoordinator
    session_store: ZyxelSessionStore
