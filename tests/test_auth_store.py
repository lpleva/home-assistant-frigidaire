"""Tests for the auth persistence, file permissions, and the per-entry migration."""

import json
import os
import stat

import auth_store


def test_save_then_load_round_trips(tmp_path) -> None:
    path = str(tmp_path / ".storage" / "frigidaire-abc.json")
    auth_store.save_auth(path, "the-key", "https://api.us.example")
    assert auth_store.load_auth(path) == ("the-key", "https://api.us.example")


def test_saved_file_is_owner_only(tmp_path) -> None:
    """The session key alone controls the appliance, so no group or world access."""
    path = str(tmp_path / ".storage" / "frigidaire-abc.json")
    auth_store.save_auth(path, "the-key", None)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_save_leaves_no_temporary_files_behind(tmp_path) -> None:
    path = str(tmp_path / ".storage" / "frigidaire-abc.json")
    auth_store.save_auth(path, "one", None)
    auth_store.save_auth(path, "two", None)
    assert sorted(p.name for p in (tmp_path / ".storage").iterdir()) == ["frigidaire-abc.json"]
    assert json.loads((tmp_path / ".storage" / "frigidaire-abc.json").read_text())["session_key"] == "two"


def test_load_missing_file_returns_none_pair_and_creates_nothing(tmp_path) -> None:
    missing = tmp_path / "does-not-exist.json"
    assert auth_store.load_auth(str(missing)) == (None, None)
    assert not missing.exists()


def test_load_ignores_a_corrupt_file(tmp_path) -> None:
    path = tmp_path / "frigidaire-abc.json"
    path.write_text("{not json")
    assert auth_store.load_auth(str(path)) == (None, None)


def test_per_entry_auth_path_is_scoped_by_entry_id_and_lives_in_storage(tmp_path) -> None:
    path = auth_store.per_entry_auth_path(str(tmp_path), "entry-123")
    assert path == str(tmp_path / ".storage" / "frigidaire-entry-123.json")


def test_resolve_prefers_per_entry_file_when_present(tmp_path) -> None:
    """Once an entry has its own file, every fallback is ignored."""
    (tmp_path / ".storage").mkdir()
    (tmp_path / ".storage" / "frigidaire-e1.json").write_text("{}")
    (tmp_path / ".storage" / "frigidaire.json").write_text("{}")
    (tmp_path / "frigidaire.json").write_text("{}")  # pre-0.2.0 location too
    assert auth_store.resolve_initial_auth_path(str(tmp_path), "e1") == str(
        tmp_path / ".storage" / "frigidaire-e1.json"
    )


def test_resolve_falls_back_to_the_staged_shared_file(tmp_path) -> None:
    """First setup after the config flow: the key the flow staged is picked up."""
    (tmp_path / ".storage").mkdir()
    (tmp_path / ".storage" / "frigidaire.json").write_text("{}")
    assert auth_store.resolve_initial_auth_path(str(tmp_path), "e1") == str(tmp_path / ".storage" / "frigidaire.json")


def test_resolve_migrates_a_pre_020_per_entry_file_from_the_config_root(tmp_path) -> None:
    (tmp_path / "frigidaire-e1.json").write_text("{}")
    assert auth_store.resolve_initial_auth_path(str(tmp_path), "e1") == str(tmp_path / "frigidaire-e1.json")


def test_resolve_migrates_the_pre_020_shared_file_from_the_config_root(tmp_path) -> None:
    (tmp_path / "frigidaire.json").write_text("{}")
    assert auth_store.resolve_initial_auth_path(str(tmp_path), "e1") == str(tmp_path / "frigidaire.json")


def test_resolve_uses_per_entry_when_nothing_exists(tmp_path) -> None:
    """Fresh install: use the per-entry path, creating no legacy litter."""
    assert auth_store.resolve_initial_auth_path(str(tmp_path), "e1") == str(
        tmp_path / ".storage" / "frigidaire-e1.json"
    )


def test_purge_removes_the_world_readable_config_root_copies(tmp_path) -> None:
    (tmp_path / "frigidaire.json").write_text("{}")
    (tmp_path / "frigidaire-e1.json").write_text("{}")
    (tmp_path / ".storage").mkdir()
    (tmp_path / ".storage" / "frigidaire-e1.json").write_text("{}")

    auth_store.purge_legacy_auth(str(tmp_path), "e1")

    assert not (tmp_path / "frigidaire.json").exists()
    assert not (tmp_path / "frigidaire-e1.json").exists()
    assert (tmp_path / ".storage" / "frigidaire-e1.json").exists()


def test_purge_is_a_no_op_when_there_is_nothing_to_remove(tmp_path) -> None:
    auth_store.purge_legacy_auth(str(tmp_path), "e1")


def test_purge_also_removes_the_staged_shared_file_once_the_entry_has_its_own(tmp_path) -> None:
    """The staged file is the flow's handoff to setup; after that it is a second live token."""
    (tmp_path / ".storage").mkdir()
    (tmp_path / ".storage" / "frigidaire.json").write_text("{}")
    (tmp_path / ".storage" / "frigidaire-e1.json").write_text("{}")

    auth_store.purge_legacy_auth(str(tmp_path), "e1")

    assert not (tmp_path / ".storage" / "frigidaire.json").exists()
    assert (tmp_path / ".storage" / "frigidaire-e1.json").exists()


def test_purge_keeps_the_staged_file_when_the_entry_has_no_file_yet(tmp_path) -> None:
    """Setup failed before writing its own copy; the staged key is still the only one."""
    (tmp_path / ".storage").mkdir()
    (tmp_path / ".storage" / "frigidaire.json").write_text("{}")

    auth_store.purge_legacy_auth(str(tmp_path), "e1")

    assert (tmp_path / ".storage" / "frigidaire.json").exists()


def test_remove_auth_deletes_only_that_entrys_file(tmp_path) -> None:
    (tmp_path / ".storage").mkdir()
    (tmp_path / ".storage" / "frigidaire-e1.json").write_text("{}")
    (tmp_path / ".storage" / "frigidaire-e2.json").write_text("{}")

    auth_store.remove_auth(str(tmp_path), "e1")

    assert not (tmp_path / ".storage" / "frigidaire-e1.json").exists()
    assert (tmp_path / ".storage" / "frigidaire-e2.json").exists()


def test_remove_auth_is_a_no_op_when_the_file_is_already_gone(tmp_path) -> None:
    auth_store.remove_auth(str(tmp_path), "e1")
