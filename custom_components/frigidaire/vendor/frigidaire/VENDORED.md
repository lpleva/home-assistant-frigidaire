# Vendored `frigidaire` client

**Upstream:** https://github.com/bm1549/frigidaire (PyPI `frigidaire`)
**Vendored from version:** 0.18.53
**Vendored on:** 2026-09-07
**License:** MIT (`LICENSE`, copied from the 0.18.53 wheel).
`signature_generator.py` is a port of https://github.com/SAP/gigya-android-sdk,
Apache 2.0; that notice stays at the top of the file.

## Why it is vendored

The published 0.18.53 wheel passes `verify=False` on every HTTP request,
including the POST that carries the account password, and it takes the host for
that POST out of a response fetched over the same unverified connection. Anyone
in a network-middle position could read the password in cleartext. That is
finding **C1** in the audit this fork answers. Fixing it needs a change to the
library, not the integration, so the package is copied in-tree and imported as
`custom_components.frigidaire.vendor.frigidaire`. `manifest.json` no longer
requires `frigidaire` from PyPI; it lists `requests`, which the vendored code
imports directly.

## Changes made to the upstream 0.18.53 source

Each entry names the audit finding it answers.

- **C1 — TLS verification is on everywhere.** `verify=False` is gone from
  `get_request`, `post_request`, and `put_request`; `requests` uses the system
  trust store. The wrapper in `rate_limit.py` rejects any call that tries to
  pass `verify=False` (or a `cert`/`verify` override that weakens TLS), so the
  flag cannot come back through a keyword argument.
- **C1 — identity and API hosts are checked against an allowlist.** The
  `domain`, `apiKey`, and `httpRegionalBaseUrl` values taken from the
  `identity-providers` response are validated before the credential POST:
  the identity domain must sit under a known Electrolux/Gigya domain
  (`_ALLOWED_IDENTITY_DOMAIN_SUFFIXES`), and the regional base URL must be
  `https://` on a known Electrolux host (`_ALLOWED_API_HOST_SUFFIXES`). A value
  outside the allowlist raises `FrigidaireException` and no credentials are
  sent. Every request URL is re-checked at the point it is built
  (`_validate_request_url`), so a redirected or malformed base URL cannot leak
  the bearer token either.
- **H1 — no process-wide warning suppression.** The import-time
  `urllib3.disable_warnings(InsecureRequestWarning)` call and the comment that
  justified it are deleted, along with the now-unused `urllib3` import.
- **H6 — a malformed appliance record is skipped, not fatal.**
  `Appliance.__init__` uses `.get()` for `applianceData`, `modelName`, and
  `applianceName`, raising `ValueError` only when `applianceId` is missing, and
  `get_appliances()` logs and skips a record it cannot parse instead of letting
  one bad record kill the whole list.
- **M3 — module logger.** Every `logging.<level>()` call in `__init__.py` and
  `signature_generator.py` goes through `_LOGGER = logging.getLogger(__name__)`,
  so `logger:` config in Home Assistant can control this library's verbosity.
- **M4 — no response bodies in exception messages.** The two `authenticate()`
  failures report the response's key names and `errorCode` rather than the body,
  which on those endpoints carries session tokens. `authenticate()` also raises
  with `status_code=401` and `error_code="invalid_credentials"` when Gigya
  reports a login failure, so callers can classify it without string matching.
- **M9 — the rate limiter no longer sleeps while holding its lock.**
  `RateLimiter.wait()` computes the delay under the lock and sleeps outside it.
- **M10 — scope keys are hashed and the session can be closed.** The shared
  limiter and re-auth lock are keyed by a SHA-256 prefix of the account
  identifier rather than by the email address itself, and `Frigidaire.close()`
  releases the `requests.Session` so an integration reload does not leak its
  connection pool.
- **L3 — no `print()`.** `signature_generator.get_signature` logs the failure
  through `_LOGGER.exception` instead of printing to stdout.
- **H3 support — no password, no login attempt.** `authenticate()` raises
  `FrigidaireException(..., error_code="reauth_required")` when the cached
  session is gone and no password is held, instead of POSTing an empty
  password. This is what lets the integration keep the password out of the
  config entry and prompt for it only when the session actually dies.

## Keeping it current

Upstream releases do not land here automatically. To take a new upstream
version: copy the three modules over this directory, re-apply the changes above
(they are small and self-contained), bump "Vendored from version", and run
`.venv/bin/python -m pytest -q` — `tests/test_vendored_client.py` covers the TLS
and allowlist behaviour and will fail loudly if a fix is dropped.
