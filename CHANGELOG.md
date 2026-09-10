# Changelog

## 0.2.2

- Session keys turn out to live 12 hours, not "a long time", so the no-password design of 0.2.0 asked for the password twice a day. The client now keeps the refresh token Electrolux issues with each session key (stored beside it, owner-only) and uses it to mint the next key itself, the way Electrolux's own app does; the reauth prompt is only for a refresh Electrolux refuses. Auth files from before this load with no refresh token and get one on the next login. Three tests.

## 0.2.1

- Config flow: mark username and password as required in the form schema, so an empty password is refused by the form instead of crashing validation with a KeyError ("Could not parse the Frigidaire appliance list: 'password'").

## 0.2.0 — 2026-09-07

A security and correctness release answering a full audit of the integration and the
`frigidaire` PyPI client it used. Every finding is listed with its audit ID.

Start here if you are upgrading: **your Frigidaire password is no longer stored in Home
Assistant**, the session key moves to `.storage/` with mode 0600, and new entities get
device-prefixed names. The README's "Upgrading to 0.2.0" section covers what you might
notice.

### Critical

- **C1 — TLS certificate verification was disabled on every request, including the POST
  carrying the account email and password, and the host for that POST was taken from a
  response fetched over the same unverified connection.** The client is now vendored into
  `custom_components/frigidaire/vendor/frigidaire/` (upstream 0.18.53, MIT, changes
  recorded in `vendor/frigidaire/VENDORED.md`) and `manifest.json` no longer installs it
  from PyPI. In the vendored copy: `verify=False` is gone from all three request methods,
  the session wrapper rejects any attempt to reintroduce it, and the Gigya identity domain
  and the regional API base URL are validated against an allowlist of Electrolux and Gigya
  hosts before any credential is sent. Every request URL is re-checked at send time, which
  also covers a tampered base URL restored from the caller's own cache, and redirects are
  not followed — the allowlist checks the URL this client builds, and `requests` replays
  the body of a 307/308, which for the login call is the password.

### High

- **H1** — the import-time `urllib3.disable_warnings(InsecureRequestWarning)` call, which
  silenced insecure-request warnings for every other integration in the process, is gone.
- **H2** — authentication failures raise `ConfigEntryAuthFailed` from both setup and the
  coordinator, and the config flow gained `reauth` / `reauth_confirm` steps. A password
  change now shows a "re-enter your credentials" card instead of retrying forever with no
  way to fix it from the UI.
- **H3** — the config entry stores only the username. The session key is the credential the
  integration runs on; the password is asked for only when that key stops working. Entries
  created by older versions are migrated to config-entry version 2, which drops the stored
  password. Adding an account (or re-authenticating) always mints a fresh session rather
  than reusing a cached one, so the password you type is actually checked.
- **H4** — the session key is written atomically, mode 0600, into `.storage/` instead of
  world-readable next to `configuration.yaml`. The old files are migrated once and deleted,
  the staged copy the config flow writes is removed once the entry has its own, and
  deleting the integration deletes the key with it.
- **H5** — `set_humidity` switches the appliance to Dry only when the active mode ignores
  the setpoint, and refreshes once instead of three times. A unit in Continuous stays in
  Continuous when the humidity slider moves.
- **H6** — a cloud record missing `applianceData` no longer raises `KeyError` and takes the
  whole appliance list (and setup) with it: the record is skipped with a warning, and setup
  treats any unexpected exception as a retryable condition.

### Medium

- **M1** — `climate.temperature_unit` falls back to Fahrenheit instead of raising
  `KeyError` when the appliance omits `temperatureRepresentation`.
- **M2** — an unmapped mode warns once per distinct value rather than on every poll, and
  the dehumidifier's `FANONLY` is mapped rather than warned about.
- **M3** — the vendored client logs through a module logger, so `logger:` configuration in
  Home Assistant can control its verbosity.
- **M4** — the two `authenticate()` failures report the response's key names and error code
  instead of interpolating a body that carries session tokens.
- **M5** — the reserved `diagnostics.py` filename now holds a real diagnostics platform
  (redacted entry, coordinator state, and reported properties). The value parsers that used
  to occupy it moved to `parsers.py`.
- **M6** — every entity sets `has_entity_name`, so names and entity IDs carry the device and
  a second appliance no longer produces `binary_sensor.connectivity_2`.
- **M7** — the dehumidifier's write paths are tested: rounding, mode preservation, power
  commands, the delayed-start case, the mode round trip, and the warn-once behaviour.
- **M8** — auth failures are classified on `status_code` / `error_code` rather than by
  matching English text in a library message, with the string kept only as a fallback.
- **M9** — the rate limiter no longer sleeps while holding its lock, and setup uses a 30s
  timeout with one in-library retry so a hung endpoint cannot pin an executor thread for
  minutes.
- **M10** — the process-global limiter and re-auth lock are keyed by a hash rather than the
  account's email address, and unloading the entry closes the `requests.Session` that every
  reload used to leak.

### Low

- **L1** — the commented-out `fan_modes` block is replaced by the reason fan speed lives on
  the custom service.
- **L2** — six copies of the enum normalizer collapse into `helpers.normalize_enum_value`.
- **L3** — `signature_generator` logs its failure instead of `print()`ing it to stdout.
- **L4** — Smart gets its own Home Assistant mode, so selecting the displayed mode no longer
  switches the appliance out of Smart.
- **L5** — the switch platform's dead early return on empty options is gone.
- **L6** — the config flow names the parse failures it expects and keeps the exception chain
  (which can carry a response body) out of the log.
- **L7** — the options flow aborts with a message instead of raising `KeyError` when the
  entry is not loaded.
- **L8** — `DELAYED_START` no longer falls through both power checks: `set_mode` leaves a
  pending start timer alone instead of cancelling it.

### Also fixed, from the review of these fixes

- Redirects are no longer followed on any request (see C1 above).
- An unexpected setup failure is logged with its traceback before it becomes a retry, so a
  bug in the integration cannot hide behind a silent retry loop.
- Adding an account no longer validates the typed password against a cached session key,
  which accepted a wrong password and could reuse another account's session.
- `Smart` and `Fan` stay in the dehumidifier's mode list once the unit has been seen in
  them, instead of vanishing as soon as you switch away.
- The deprecated `CONCENTRATION_MICROGRAMS_PER_CUBIC_METER` constant (which logged a
  warning naming this integration on every start) is replaced by `UnitOfDensity`.
- A retried appliance command carries the newly minted session key rather than the dead one.
- Response bodies are kept out of every client exception message, not just the two in the
  login flow.
- Diagnostics redact an appliance nickname that is really the appliance id.

### Testing

133 tests before, 234 after. New coverage: the vendored client's TLS, host allowlist and
response parsing; the reauth flow and the session-cap case that must not trigger it; the
auth file's permissions and migration; the dehumidifier write paths; and the diagnostics
platform's redaction; the user config flow, which had no tests at all; and the entry
migration and removal paths.

The PyPI `frigidaire` wheel is uninstalled from the development venv and a test asserts it
is not importable, so the suite proves the vendored copy is the one in use rather than
quietly falling back to the package with `verify=False`.
