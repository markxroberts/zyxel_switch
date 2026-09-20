# Changelog

## 0.2.4

- Restores stable MAC-based device registry identity, preferring the hardware identity already stored in the config entry, and automatically merges duplicate GS1900 device records created by the 0.2.x identifier change.
- Reattaches existing entity-registry entries to the retained device before removing the duplicate, preserving entity IDs and history.
- Restores read-only POWER-ETHERNET-MIB main-PSE scalar polling for GS1900 PoE budget, consumption and threshold.
- Keeps the 0.2.3 raw numeric OID approach, so PySNMP performs no runtime MIB loading on Home Assistant's event loop.
- Falls back to the authenticated web PoE page when a main-PSE scalar is unavailable.
- Fixes a duplicated comprehension in the entity-registry reconciliation helper.

## 0.2.3

- Fixes Home Assistant blocking-I/O warnings from PySNMP MIB loading on the event loop.
- Uses raw numeric OID/value varbinds for all GS1900 GET operations so `lookupMib=False` remains effective.
- Replaces PySNMP `bulk_walk_cmd` with an asynchronous `bulk_cmd` subtree loop because `bulk_walk_cmd` always initialises a MIB view.
- Keeps all SNMP network I/O asynchronous and read-only; no SNMP SET support is introduced.
- Does not change entity unique IDs, names or registry migration behaviour.

## 0.2.2

- Fixes missing icons on PoE port switch entities.
- Replaces the invalid `mdi:power-ethernet` reference with modern Home Assistant icon translations.
- Uses `mdi:ethernet` while PoE is enabled and `mdi:ethernet-off` while disabled.
- Moves the LED Eco icon to the same `icons.json` architecture.

## 0.2.1

- Migrates legacy GS1200 entity registry entries in place to prevent duplicate entities after upgrading from 0.1.10.
- Preserves existing entity IDs, custom names, visibility settings and history where matching legacy entries exist.

## 0.2.0

Architectural rewrite based on 0.1.10:

- Uses modern config-entry setup and UI `ConfigFlow`, including options, reconfigure and reauthentication flows.
- Stores per-entry runtime objects in `ConfigEntry.runtime_data`.
- Centralises all polling and entity updates in a `DataUpdateCoordinator`.
- Uses `async`/`await` throughout network and control paths.
- Uses Home Assistant-managed, isolated `aiohttp` sessions for every HTTP/HTTPS request.
- Uses asynchronous read-only PySNMP for GS1900 monitoring.
- Adds explicit manifest `version`, `dependencies` and `requirements` keys.
- Adds config-entry migration for 0.1.x data and legacy session tokens.
- Preserves GS1200 restart recovery, session handoff, non-PoE detection and ambiguous PoE-state caching.
- Preserves GS1900 IF-MIB port aliases and web-interface PoE control.
- Adds diagnostics with credential and session redaction.
- Adds private session storage cleanup when a config entry is removed.
- Includes local Zyxel brand images for light and dark UI modes.
- Aligns dependency versions with Home Assistant 2026.7.4.
