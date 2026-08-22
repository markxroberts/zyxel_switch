# Zyxel Switch for Home Assistant

Version **0.2.3** is based on the architectural rewrite of the 0.1.10 test release for Home Assistant 2026.7 or later.

## Supported hardware

- GS1200-5 v2 and GS1200-8 v2 (non-PoE)
- GS1200-5HP v2
- GS1200-8HP v2
- GS1900-10HP
- GS1900-24EP

Other closely related GS1200 and GS1900 models may work, but have not been validated.

## Architecture

- UI-only setup through `config_flow.py`; no YAML or legacy platform setup.
- One `DataUpdateCoordinator` per config entry provides a coherent snapshot to every entity.
- Runtime objects are stored in `ConfigEntry.runtime_data`.
- All HTTP and HTTPS traffic uses asynchronous `aiohttp` sessions created by Home Assistant.
- GS1900 monitoring uses asynchronous, read-only SNMPv2c through PySNMP.
- Numeric OID varbinds are used directly so PySNMP does not perform runtime MIB-file loading on Home Assistant's event loop.
- PoE changes use the switch web interface. The integration performs no SNMP SET operations.
- Reconfigure and reauthentication flows hand the switch web session from the loaded coordinator to a temporary validation client, then reload or resume safely.
- Opaque web-session cookies are stored in Home Assistant private storage solely to recover and close a session orphaned by an interrupted restart.
- Includes local Zyxel light- and dark-mode brand images for the Home Assistant UI.

## Entities

Depending on model and available telemetry, the integration creates:

- Link binary sensors for physical ports
- Negotiated-speed sensors
- PoE enable switches for PoE-capable ports
- Per-port PoE power sensors
- Received and transmitted byte counters on GS1900, disabled by default
- Uptime
- Total PoE consumption, budget and threshold when exposed
- GS1200 LED Eco control

GS1900 port labels are read primarily from IF-MIB `ifAlias`, including non-PoE ports 13–24 on the GS1900-24EP.

## Installation

1. Extract the release archive into the Home Assistant configuration directory so that the integration is at:
   `/config/custom_components/zyxel_switch/`
2. Restart Home Assistant.
3. Open **Settings → Devices & services → Add integration**.
4. Search for **Zyxel Switch**.
5. Add one config entry per physical switch.

For an upgrade from 0.1.10, replace the existing integration directory and restart Home Assistant. Existing config entries are migrated automatically. Do not remove and recreate them unless migration fails.

## Configuration

The config flow asks for:

- Management host or IP address
- Web-interface port and HTTP/HTTPS choice
- Web-interface username and password
- Switch family, normally **Automatic detection**
- HTTPS certificate verification
- SNMPv2c community and port for GS1900 monitoring

The polling interval is available under the integration's **Configure** options. The supported range is 30–300 seconds; the default is 60 seconds.

Secret fields are never prefilled during reconfiguration. Leaving a password or SNMP community blank preserves the stored value.

## Session behaviour

Some GS1200 firmware permits only one web session. The integration logs out during unload, graceful restart, reconfiguration and reauthentication. It also retains the opaque session token privately so that a subsequent start can close a session left behind by an interrupted restart.

A browser session can still block access. Log out of the switch web interface before setup or reconfiguration when Home Assistant reports that the session is busy.

## Logging

Enable debug logging when investigating a problem:

```yaml
logger:
  logs:
    custom_components.zyxel_switch: debug
```

Do not include passwords, SNMP communities or cookie values in issue reports.

## Validation status

The 0.2.3 archive has been syntax-checked, JSON-validated, architecture-checked and exercised with parser unit tests. It cannot be hardware-tested in this build environment, so testing on the listed GS1200 and GS1900 switches remains necessary.
