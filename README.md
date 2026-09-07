## About this fork

This is an audited fork of [bm1549/home-assistant-frigidaire](https://github.com/bm1549/home-assistant-frigidaire), taken at upstream version 0.1.47; this fork is version 0.2.1. It exists because, in the Home Assistant install it serves, every integration that holds a login or can act on the home gets a line-by-line audit before it runs, and the fixes live here rather than upstream.

**Why it was forked.** The integration holds a Frigidaire account login and runs inside Home Assistant with full privileges, and the library it depended on turned TLS certificate verification off for every request, including the one carrying the password.

**What is different.** The `frigidaire` client library is vendored into the integration with TLS verification on, its hosts allowlisted, redirects refused and timeouts everywhere, so the login can no longer be intercepted or redirected. The password is no longer stored in the config entry (only the username; a dead session prompts re-authentication), the session token is kept owner-readable under `.storage`, response bodies stay out of exception messages and logs, a reauth flow was added, entity names follow current Home Assistant conventions, the dehumidifier write paths (target humidity, modes) were fixed and tested, and the test suite grew from 133 to 235. An independent review then added three fixes, including refusing HTTP redirects and never validating a typed password against a cached session. Fixes are not sent upstream; upstream is unchanged by this fork.

**How it is kept current.** A weekly job merges upstream's new commits onto a branch, runs this fork's tests, reviews the diff, and only then pushes; a merge conflict or a failing test stops it. The fork is installed through HACS as a custom repository, so Home Assistant offers each new version as an update.

**Where the detail is.** CHANGELOG.md record every change by audit finding.

---

# Home Assistant Custom Component for Frigidaire

[![Latest Release](https://img.shields.io/github/release/bm1549/home-assistant-frigidaire/all.svg?style=for-the-badge)](https://github.com/bm1549/home-assistant-frigidaire/releases)
[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge)](https://github.com/custom-components/hacs)
[![License](https://img.shields.io/github/license/bm1549/home-assistant-frigidaire?style=for-the-badge)](LICENSE)
[![Maintainer](https://img.shields.io/badge/MAINTAINER-%40bm154969-red?style=for-the-badge)](https://github.com/bm1549)
[![Community Forum](https://img.shields.io/badge/COMMUNITY-FORUM-success?style=for-the-badge)](https://community.home-assistant.io)

A Home Assistant integration for Frigidaire WiFi-connected appliances, using the Frigidaire 2.0 (Electrolux) cloud API.

## Supported Devices

- **Air Conditioners** — window, portable, and inverter models
- **Dehumidifiers**

## Features

### Air Conditioner

- HVAC modes: Cool, Auto (Eco), Fan Only, Dry, Off
- Fan speed: Auto, Low, Medium, High
- Target temperature control (°F and °C)
- Preset modes: Sleep
- Swing modes: Vertical, Off
- ON/OFF timer control (30-minute increments, up to 24 hours)
- Extra state attributes: `check_filter`, `reported_fan_speed`, and `active_alerts`;
	the legacy `current_fan_speed` alias is retained for existing templates
- `hvac_action` reflects the mode the appliance reports it is *actually* running, so an
	Eco/Auto unit shows `cooling` vs `fan` as it cycles rather than the requested mode

### Dehumidifier

- Modes: Normal (Dry), Boost (Continuous), Auto, Sleep, plus Smart and Fan on the models
	that report them (they appear once the appliance is in one)
- Target humidity control (35-85%, 5% steps). Setting a target switches the unit to Dry
	only when the current mode ignores the setpoint, so a unit in Continuous stays there
- Fan speed control via the `frigidaire.set_fan_mode` service: `low`, `medium`, `high`
- Extra state attributes: `current_humidity`, `check_filter`, `fan_mode`, `bin_full`

### Automatic Entities

These are created automatically, but only for appliances that actually report the
underlying value — no extra API polling is involved, since all of it arrives in the same
cloud response the climate and dehumidifier entities already use.

| Entity | Type | Description |
|---|---|---|
| Humidity | Humidity sensor | Room relative humidity, on appliances with a humidity sensor (including some ACs) |
| PM2.5 | PM2.5 sensor | Particulate concentration in µg/m³, on appliances with an air-quality sensor |
| Wi-Fi Signal | Signal strength sensor | RSSI in dBm plus a `link_quality` attribute. Diagnostic and **disabled by default** — enable it from the device page when troubleshooting |
| Connectivity | Connectivity binary sensor | Whether the cloud can currently reach the appliance. Worth alerting on: a disconnected appliance keeps serving its last-known values, so every other entity looks healthy while its data silently goes stale |

`pm10` is deliberately **not** exposed. On the appliances observed so far it alternates
between a fixed placeholder value and a value identical to `pm25`, so it carries no
information `pm25` does not already provide.

### Optional Entities

During setup — or at any time via **Configure** — you can enable additional entities per device:

| Entity | Type | Description |
|---|---|---|
| Ionizer (Clean Air Mode) | Switch | Toggles the ionizer/clean air feature |
| Display Light | Switch | Toggles the unit's display panel light |
| Child Lock | Switch | Locks the physical controls on the unit |
| Check Filter | Problem binary sensor | On for `CLEAN`, `CHANGE`, or `BUY`; exposes `filter_state` for notification automations |
| Bucket Status | Binary sensor | Full/Empty for the water bucket; dehumidifiers only, unavailable on models that report no bucket signal |
| Filter Runtime | Duration sensor | Cumulative filter runtime reported by the appliance in native seconds; Home Assistant handles display-unit conversion |
| Compressor Estimate | Running binary sensor | Opt-in temperature-based estimate for air conditioners; also refines `hvac_action` while enabled |

Each device is configured independently, so a home with both an AC and a dehumidifier can have different entities enabled for each.
Filter runtime and the raw diagnostic attributes reuse the appliance platform's normal cloud response and do not add API polling.

### Optional Compressor Estimate

The Frigidaire API does not expose physical compressor telemetry. For air conditioners, **Enable compressor estimate**
adds a diagnostic binary sensor that estimates compressor activity from operating mode, ambient temperature, and target
temperature. It does not use `fanSpeedState`, which can retain its last value while the appliance is off.

The option is disabled by default. While disabled, `hvac_action` retains the integration's standard behavior. While enabled,
the same coordinator-owned estimate changes `hvac_action` from cooling or drying to idle after the configured temperature
deadband and off delay are satisfied. On appliances that report `modeState`, that real telemetry is used for `hvac_action`
instead and enabling the option only adds the Compressor Estimate sensor. Above the upper deadband boundary the estimate
is running, below the lower boundary the off-delay timer runs, and between the boundaries the previous estimate is
retained. It reuses the normal coordinator response and does not add cloud polling.

If a standalone threshold signal is sufficient and changing `hvac_action` is not needed, Home Assistant's Threshold helper
and a template binary sensor with `delay_off` can implement that externally instead. Helpers create separate entities; they
cannot override the Frigidaire climate entity's own `hvac_action` property.

## Installing

### HACS (Recommended)

1. Open HACS in Home Assistant.
2. Go to **Integrations** and search for **Frigidaire**.
3. Click **Download** and restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration** and search for **Frigidaire**.
5. Enter your Frigidaire account email and password.

### Manual

1. Clone or download this repo.
2. Copy the `custom_components/frigidaire/` folder into `/config/custom_components/frigidaire/` on your HA instance.
3. Restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration** and search for **Frigidaire**.
5. Enter your Frigidaire account email and password.

## Reconfiguring Optional Entities

Go to **Settings → Devices & Services → Frigidaire → Configure** to change which optional entities are enabled for each device.

## Upgrading to 0.2.0

0.2.0 is a security release. Three things change that you can see:

1. **Your Frigidaire password is no longer stored in Home Assistant.** Older versions kept
	it in cleartext in `.storage/core.config_entries`, and therefore in every backup. The
	integration now runs on the session key it already had, and the password is removed from
	the config entry the first time 0.2.0 starts. If the session ever stops working, Home
	Assistant shows a **Re-authenticate** card and asks for the password then, instead of
	retrying silently forever the way it used to.
2. **The session key moves to `.storage/frigidaire-<entry_id>.json`, mode 0600.** It used to
	sit next to `configuration.yaml`, world-readable. The old files are read once (so you keep
	your session) and then deleted. The key is still inside a full Home Assistant backup:
	treat a backup as something that can drive your appliance.
3. **New entities get device-prefixed names and IDs.** The diagnostic entities used to be
	called just "Connectivity" or "Bucket Status"; they are now
	"Bedroom AC Connectivity" and so on, so a second appliance no longer produces
	`binary_sensor.connectivity_2`. Entities already in your registry keep the IDs they have —
	nothing to fix — but a fresh install, or an appliance you add now, uses the new form. Check
	any automation you write against the ID shown on the device page.

The client library is also vendored into `custom_components/frigidaire/vendor/frigidaire/`
rather than installed from PyPI, because the published build disabled TLS certificate
verification on every request, including the one carrying your password. See
`vendor/frigidaire/VENDORED.md` for the full list of changes.

## Upgrading from <=0.1.26

The 0.1.27 release introduces device grouping, per-device switch configuration, and sleep mode as a preset on AC entities. After upgrading:

1. Copy the new files and restart Home Assistant — your existing climate and dehumidifier entities will continue to work without any reconfiguration.
2. To enable the new switch entities, go to **Settings → Devices & Services → Frigidaire → Configure** and select the switches you want for each device.

## If something goes wrong

- **Integration doesn't show up in the list?** Restart HA one more time. Also double-check the folder path — it should be `/config/custom_components/frigidaire/`, not nested deeper.
- **Login keeps failing?** Make sure you're using the same email and password as the Frigidaire mobile app. No extra spaces.
- **No devices after a successful login?** Open the Frigidaire app and confirm your appliances are online there. If the app can't see them, HA won't either.

Found a bug or have an idea? Open an [issue](https://github.com/bm1549/home-assistant-frigidaire/issues). PRs are welcome too.
