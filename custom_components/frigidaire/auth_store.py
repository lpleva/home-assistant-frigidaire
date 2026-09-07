"""Persistence for the Frigidaire session key.

Kept free of Home Assistant imports so the persistence + migration logic can be
unit-tested without a full HA environment.

The session key is a long-lived bearer token: on its own it is enough to drive the
appliance, and no password change revokes it. So it is written owner-only (0600), atomically,
and inside ``.storage/`` rather than next to ``configuration.yaml`` — the config root is the
directory people zip up and paste into forum threads. It is still inside the Home Assistant
backup, which is unavoidable for a token that has to survive a restart; the file permissions
and the location are what keep it out of casual sharing.

Each config entry stores its key in its own ``frigidaire-<entry_id>.json``. A single shared
``frigidaire.json`` was used before that; when multiple accounts were configured they
clobbered each other's keys, forcing repeated re-authentication (each mints a new
server-side session and trips Frigidaire's active-session cap, cas_3403). Both the old
shared file and the old config-root location are migrated on first run, then deleted.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile

_LOGGER = logging.getLogger(__name__)

# Legacy shared auth file, pre-dating per-entry scoping.
AUTH_FILE = "frigidaire.json"
STORAGE_SUBDIR = ".storage"


def load_auth(auth_path: str) -> tuple[str | None, str | None]:
    """Read a stored session key; a missing, empty or unreadable file means no cached session.

    Reading never creates the file. The previous version did, which is why it also needed a
    zero-length guard.
    """
    if not os.path.exists(auth_path) or os.path.getsize(auth_path) == 0:
        return None, None
    try:
        with open(auth_path) as f:
            obj: dict = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        _LOGGER.warning("Ignoring unreadable Frigidaire session file %s", auth_path)
        return None, None
    if not isinstance(obj, dict):
        return None, None
    return obj.get("session_key"), obj.get("regional_base_url")


def save_auth(auth_path: str, session_key: str, regional_base_url: str | None) -> None:
    """Write the session key atomically, readable only by the owner."""
    directory = os.path.dirname(auth_path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".frigidaire-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(
                {"session_key": session_key, "regional_base_url": regional_base_url},
                f,
                ensure_ascii=False,
                indent=4,
            )
        # Replace rather than truncate-and-write, so a crash mid-write cannot leave a
        # half-written (or empty) file behind.
        os.replace(tmp_path, auth_path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def storage_dir(config_dir: str) -> str:
    """Home Assistant's .storage directory."""
    return os.path.join(config_dir, STORAGE_SUBDIR)


def per_entry_auth_path(config_dir: str, entry_id: str) -> str:
    """The auth file dedicated to a single config entry."""
    return os.path.join(storage_dir(config_dir), f"frigidaire-{entry_id}.json")


def shared_auth_path(config_dir: str) -> str:
    """Where the config flow stages a key before an entry_id exists."""
    return os.path.join(storage_dir(config_dir), AUTH_FILE)


def legacy_auth_paths(config_dir: str, entry_id: str) -> list[str]:
    """Pre-0.2.0 locations, in the order they should be tried."""
    return [
        os.path.join(config_dir, f"frigidaire-{entry_id}.json"),
        os.path.join(config_dir, AUTH_FILE),
    ]


def resolve_initial_auth_path(config_dir: str, entry_id: str) -> str:
    """Where to load an entry's initial session key from.

    Prefer the entry's own file. When it does not exist yet, fall back in turn to the
    staged shared file and then to the two pre-0.2.0 config-root locations, so an existing
    user keeps their cached session key instead of re-authenticating. When none exists,
    return the per-entry path (creating no stray legacy file).
    """
    per_entry = per_entry_auth_path(config_dir, entry_id)
    if os.path.exists(per_entry):
        return per_entry
    for candidate in [shared_auth_path(config_dir), *legacy_auth_paths(config_dir, entry_id)]:
        if os.path.exists(candidate):
            return candidate
    return per_entry


def remove_auth(config_dir: str, entry_id: str) -> None:
    """Delete an entry's session file. Called when the config entry itself is removed.

    The key stays valid server-side for a long time after the entry is gone, so leaving
    the file behind leaves a live token in .storage (and in every backup) belonging to an
    integration the user has deleted.
    """
    _unlink(per_entry_auth_path(config_dir, entry_id))


def purge_legacy_auth(config_dir: str, entry_id: str) -> None:
    """Delete auth files this entry no longer reads, once its own file exists.

    The pre-0.2.0 files are world-readable and sit next to configuration.yaml, which is the
    exposure the move to .storage fixes; leaving them behind would leave the token where it
    always was. The staged shared file goes too: it is the config flow's handoff to setup,
    and once the entry has its own copy it is a second live token nothing reads.
    """
    stale = list(legacy_auth_paths(config_dir, entry_id))
    if os.path.exists(per_entry_auth_path(config_dir, entry_id)):
        stale.append(shared_auth_path(config_dir))
    for path in stale:
        _unlink(path)


def _unlink(path: str) -> None:
    """Remove a session file, tolerating one that is already gone."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    except OSError as err:
        _LOGGER.warning("Could not remove the Frigidaire session file %s: %s", path, err)
    else:
        _LOGGER.debug("Removed Frigidaire session file %s", path)
