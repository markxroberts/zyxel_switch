"""Zyxel Switch integration."""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import ZyxelApiClient, create_client
from .const import (
    CONF_FAMILY,
    CONF_POLL_INTERVAL,
    CONF_SNMP_COMMUNITY,
    CONF_SNMP_PORT,
    CONF_USE_HTTPS,
    CONF_VERIFY_SSL,
    DEFAULT_HTTP_PORT,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_SNMP_COMMUNITY,
    DEFAULT_SNMP_PORT,
    DEFAULT_USERNAME,
    DOMAIN,
    FAMILY_AUTO,
    FAMILY_GS1200,
    FAMILY_GS1900,
    LEGACY_SESSION_KEYS,
    PLATFORMS,
)
from .coordinator import ZyxelSwitchCoordinator
from .entity import _physical_device_id
from .entity_migration import entry_matches_expected, expected_entities
from .runtime_data import ZyxelRuntimeData
from .session_store import ZyxelSessionStore

_LOGGER = logging.getLogger(__name__)


def _new_http_session(hass: HomeAssistant) -> aiohttp.ClientSession:
    """Create an HA-managed session with an isolated, manually managed cookie jar."""
    return async_create_clientsession(
        hass,
        cookie_jar=aiohttp.DummyCookieJar(),
    )


async def _async_first_refresh(
    hass: HomeAssistant,
    entry: ConfigEntry[ZyxelRuntimeData],
    session_store: ZyxelSessionStore,
    recovery_cookies: dict[str, str],
) -> tuple[ZyxelApiClient, ZyxelSwitchCoordinator]:
    """Create the family client and complete the coordinator's first refresh."""
    configured_family = str(entry.data.get(CONF_FAMILY, FAMILY_AUTO))
    families = (
        (FAMILY_GS1200, FAMILY_GS1900)
        if configured_family == FAMILY_AUTO
        else (configured_family,)
    )
    poll_interval = int(
        entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
    )
    failures: list[Exception] = []
    session = _new_http_session(hass)

    for family in families:
        client = create_client(
            session,
            entry.data,
            family=family,
            initial_cookies=recovery_cookies,
            session_changed=session_store.async_save,
        )
        coordinator = ZyxelSwitchCoordinator(hass, entry, client, poll_interval)
        try:
            await coordinator.async_config_entry_first_refresh()
        except (ConfigEntryAuthFailed, ConfigEntryNotReady) as err:
            failures.append(err)
            await coordinator.async_shutdown()
            # A failed candidate has attempted recovery/logout and cleared its
            # private store. Do not feed the same stale cookie to another family.
            recovery_cookies = {}
            if configured_family != FAMILY_AUTO:
                session.detach()
                raise
            continue

        if configured_family == FAMILY_AUTO:
            hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, CONF_FAMILY: family},
            )
        return client, coordinator

    # No client owns this HA connector wrapper when every candidate fails.
    session.detach()

    auth_failure = next(
        (error for error in failures if isinstance(error, ConfigEntryAuthFailed)),
        None,
    )
    if auth_failure is not None:
        raise auth_failure
    if failures:
        raise failures[-1]
    raise ConfigEntryNotReady("Unable to determine the Zyxel switch family")


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry[ZyxelRuntimeData]
) -> bool:
    """Set up a Zyxel switch from a modern config entry."""
    session_store = ZyxelSessionStore(hass, entry.entry_id)
    recovery_cookies = await session_store.async_load()
    client, coordinator = await _async_first_refresh(
        hass,
        entry,
        session_store,
        recovery_cookies,
    )
    entry.runtime_data = ZyxelRuntimeData(client, coordinator, session_store)

    await _async_migrate_entity_registry(hass, entry, coordinator)
    await _async_migrate_device_registry(hass, entry, coordinator)
    await _async_cleanup_non_poe_registry_entries(hass, entry, coordinator)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    async def _async_on_hass_stop(_event: Event) -> None:
        await coordinator.async_shutdown()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_on_hass_stop)
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: ConfigEntry[ZyxelRuntimeData]
) -> bool:
    """Unload platforms and close the embedded web session."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.coordinator.async_shutdown()
    return unloaded


async def async_remove_entry(
    hass: HomeAssistant, entry: ConfigEntry[ZyxelRuntimeData]
) -> None:
    """Remove private session-recovery storage with the config entry."""
    await ZyxelSessionStore(hass, entry.entry_id).async_remove()


async def _async_reload_entry(
    hass: HomeAssistant, entry: ConfigEntry[ZyxelRuntimeData]
) -> None:
    """Reload after options or reconfiguration changes."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate all 0.1.x config entries to the 0.2 runtime schema."""
    if entry.version > 2 or (entry.version == 2 and entry.minor_version > 1):
        return False

    data = dict(entry.data)
    options = dict(entry.options)

    raw_host = str(data.get(CONF_HOST, "")).strip().rstrip("/")
    if "://" in raw_host:
        parsed = urlsplit(raw_host)
        data[CONF_HOST] = parsed.hostname or raw_host
        data[CONF_USE_HTTPS] = parsed.scheme.lower() == "https"
        if parsed.port:
            data[CONF_PORT] = parsed.port
    else:
        data[CONF_HOST] = raw_host.strip("[]")

    data.setdefault(CONF_USERNAME, DEFAULT_USERNAME)
    data.setdefault(CONF_PASSWORD, "")
    data.setdefault(CONF_FAMILY, data.pop("switch_family", FAMILY_AUTO))
    data.setdefault(CONF_USE_HTTPS, bool(data.pop("ssl", False)))
    data.setdefault(CONF_VERIFY_SSL, bool(data.pop("verify_ssl", False)))
    data.setdefault(
        CONF_PORT,
        443 if data[CONF_USE_HTTPS] else DEFAULT_HTTP_PORT,
    )
    data.setdefault(CONF_SNMP_COMMUNITY, DEFAULT_SNMP_COMMUNITY)
    data.setdefault(CONF_SNMP_PORT, DEFAULT_SNMP_PORT)

    old_interval = data.pop("scan_interval", None)
    if old_interval is not None and CONF_POLL_INTERVAL not in options:
        options[CONF_POLL_INTERVAL] = int(old_interval)
    options.setdefault(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)

    recovery_token: str | None = None
    for key in LEGACY_SESSION_KEYS:
        value = data.pop(key, None)
        if value and recovery_token is None:
            recovery_token = str(value)
    if recovery_token:
        await ZyxelSessionStore(hass, entry.entry_id).async_save(
            {"token": recovery_token}
        )

    hass.config_entries.async_update_entry(
        entry,
        data=data,
        options=options,
        version=2,
        minor_version=1,
    )
    _LOGGER.info("Migrated Zyxel Switch config entry %s to version 2.1", entry.title)
    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: ConfigEntry[ZyxelRuntimeData],
    device_entry: dr.DeviceEntry,
) -> bool:
    """Prevent deleting the integration's only device independently."""
    return False


async def _async_migrate_entity_registry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: ZyxelSwitchCoordinator,
) -> None:
    """Merge 0.1.x and 0.2.0 duplicate entities without changing old entity IDs."""
    registry = er.async_get(hass)
    registry_entries = [
        registry_entry
        for registry_entry in er.async_entries_for_config_entry(
            registry, entry.entry_id
        )
        if registry_entry.platform == DOMAIN
    ]
    unique_base = entry.unique_id or entry.entry_id
    migrated = 0
    removed = 0

    for expected in expected_entities(coordinator.data):
        new_unique_id = f"{unique_base}-{expected.suffix}"
        candidates = [
            registry_entry
            for registry_entry in registry_entries
            if entry_matches_expected(registry_entry, expected)
        ]
        if not candidates:
            continue

        # The oldest registry entry is the pre-upgrade entity in normal use.
        # Retaining it preserves entity_id, user customisations and recorder history.
        canonical = min(
            candidates,
            key=lambda registry_entry: (
                registry_entry.created_at,
                registry_entry.entity_id,
            ),
        )
        duplicates = [
            registry_entry
            for registry_entry in candidates
            if registry_entry.entity_id != canonical.entity_id
        ]

        # A newer 0.2.0 entity can already own the target unique ID. Remove it
        # before moving that unique ID to the retained pre-upgrade entity.
        for duplicate in duplicates:
            registry.async_remove(duplicate.entity_id)
            registry_entries.remove(duplicate)
            removed += 1

        if canonical.unique_id != new_unique_id:
            registry.async_update_entity(
                canonical.entity_id,
                new_unique_id=new_unique_id,
            )
            migrated += 1

    if migrated or removed:
        _LOGGER.info(
            "Reconciled Zyxel entity registry for %s: migrated %d, "
            "removed %d duplicate(s)",
            entry.title,
            migrated,
            removed,
        )


async def _async_migrate_device_registry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: ZyxelSwitchCoordinator,
) -> None:
    """Restore the 0.1.x MAC device identity and merge 0.2.x duplicates.

    Version 0.2.0 changed the DeviceInfo identifier to the config-entry ID.
    If GS1900 MAC discovery was temporarily unavailable, Home Assistant could not
    match that new identifier back to the existing MAC-identified device and a
    second device registry record was created. Preserve the oldest physical-device
    record, reattach registry entities to it, then remove only the duplicate.
    """
    physical_id = _physical_device_id(entry, coordinator.data.mac)
    if physical_id is None:
        return

    registry = dr.async_get(hass)
    devices = dr.async_entries_for_config_entry(registry, entry.entry_id)
    if not devices:
        return

    physical_identifier = (DOMAIN, physical_id)
    mac_connection = (dr.CONNECTION_NETWORK_MAC, physical_id)

    physical_candidates = [
        device
        for device in devices
        if physical_identifier in device.identifiers
        or mac_connection in device.connections
    ]
    if physical_candidates:
        canonical = min(
            physical_candidates,
            key=lambda device: (device.created_at, device.id),
        )
    else:
        canonical = min(devices, key=lambda device: (device.created_at, device.id))

    duplicates = [device for device in devices if device.id != canonical.id]
    moved = 0
    if duplicates:
        entity_registry = er.async_get(hass)
        duplicate_ids = {device.id for device in duplicates}
        for registry_entry in er.async_entries_for_config_entry(
            entity_registry, entry.entry_id
        ):
            if registry_entry.device_id not in duplicate_ids:
                continue
            entity_registry.async_update_entity(
                registry_entry.entity_id,
                device_id=canonical.id,
            )
            moved += 1

        # Remove conflicting 0.2.x device keys before retargeting the retained
        # record, avoiding identifier/connection collision checks in HA's registry.
        for duplicate in duplicates:
            registry.async_remove_device(duplicate.id)

    # Retarget the canonical record to the stable hardware identity captured in
    # the config entry (the 0.1.x MAC for upgraded installations).
    if (
        canonical.identifiers != {physical_identifier}
        or mac_connection not in canonical.connections
    ):
        registry.async_update_device(
            canonical.id,
            new_identifiers={physical_identifier},
            merge_connections={mac_connection},
        )

    if duplicates:
        _LOGGER.info(
            "Reconciled Zyxel device registry for %s: kept %s, removed %d "
            "duplicate device(s), moved %d entity registry entr%s",
            entry.title,
            canonical.id,
            len(duplicates),
            moved,
            "y" if moved == 1 else "ies",
        )


async def _async_cleanup_non_poe_registry_entries(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: ZyxelSwitchCoordinator,
) -> None:
    """Remove stale PoE entities left by older builds on non-PoE GS1200s."""
    if coordinator.data.has_poe:
        return
    registry = er.async_get(hass)
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        unique_id = registry_entry.unique_id.lower()
        entity_domain = registry_entry.entity_id.split(".", 1)[0]
        stale = False
        if entity_domain == "switch" and "led" not in unique_id:
            stale = True
        elif entity_domain == "sensor" and (
            "poe_power" in unique_id
            or unique_id.endswith("-power")
            or "port_power" in unique_id
        ):
            stale = True
        if stale:
            registry.async_remove(registry_entry.entity_id)
