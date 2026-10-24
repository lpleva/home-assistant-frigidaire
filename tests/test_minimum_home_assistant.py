"""Guard the hacs.json promise: the code imports nothing newer than the minimum Home Assistant.

Exercises ``scripts/check_minimum_home_assistant.py``. The static tests need only the
standard library: they run the check on a throwaway repo layout under ``tmp_path`` and on
this repo. The last test runs the script's ``--import`` mode as a subprocess and is skipped
unless the installed Home Assistant is the minimum's release, since only then does
importing prove anything.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_minimum_home_assistant.py"
HACS_JSON = ROOT / "hacs.json"

spec = importlib.util.spec_from_file_location("check_minimum_home_assistant", SCRIPT)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def _fake_repo(tmp_path: Path, minimum: str, sources: dict[str, str]) -> tuple[Path, Path]:
    """A repo-shaped directory: hacs.json at the root, the integration's modules below it."""
    hacs_json = tmp_path / "hacs.json"
    hacs_json.write_text(json.dumps({"homeassistant": minimum}))
    integration = tmp_path / "custom_components" / "frigidaire"
    integration.mkdir(parents=True)
    for name, source in sources.items():
        (integration / name).write_text(source)
    return hacs_json, integration


def test_parse_version_orders_releases_numerically() -> None:
    assert check.parse_version("2026.10.0") > check.parse_version("2026.9.0")
    assert check.parse_version("2026.9") == check.parse_version("2026.9.0")
    assert check.parse_version("2025.8.0") < check.parse_version("2026.9.0")


def test_parse_version_tolerates_a_prerelease() -> None:
    """A development build of Home Assistant must compare (and be skipped), not raise."""
    assert check.parse_version("2026.11.0.dev0") == (2026, 11, 0)
    assert check.parse_version("2026.11.0b1") == (2026, 11, 0)
    assert check.parse_version("2026.11.0rc2") == (2026, 11, 0)
    assert check.parse_version("2026.11.0.dev0") > check.parse_version("2026.10.3")


def test_finds_names_imported_directly_and_through_a_module() -> None:
    source = (
        "from homeassistant import config_entries\n"
        "from homeassistant.const import PERCENTAGE, UnitOfDensity as Density\n"
        "import homeassistant.helpers.entity\n"
        "class Flow(config_entries.OptionsFlowWithReload):\n"
        "    pass\n"
    )
    assert check.homeassistant_imports(source) == {
        "homeassistant.config_entries",
        "homeassistant.config_entries.OptionsFlowWithReload",
        "homeassistant.const.PERCENTAGE",
        "homeassistant.const.UnitOfDensity",
        "homeassistant.helpers.entity",
    }


def test_a_minimum_older_than_an_imported_name_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The 0.2.x situation: UnitOfDensity imported, hacs.json still at 2025.8.0."""
    hacs_json, integration = _fake_repo(
        tmp_path, "2025.8.0", {"sensor.py": "from homeassistant.const import UnitOfDensity\n"}
    )
    assert check.static_check(hacs_json, integration) == 1
    out = capsys.readouterr().out
    assert "sensor.py imports homeassistant.const.UnitOfDensity, added in Home Assistant 2026.7.0" in out


def test_a_minimum_at_or_above_every_imported_name_passes(tmp_path: Path) -> None:
    sources = {
        "sensor.py": "from homeassistant.const import UnitOfDensity\n",
        "config_flow.py": "from homeassistant import config_entries\nx = config_entries.OptionsFlowWithReload\n",
    }
    hacs_json, integration = _fake_repo(tmp_path, "2026.9.0", sources)
    assert check.static_check(hacs_json, integration) == 0
    hacs_json.write_text(json.dumps({"homeassistant": "2026.10.2"}))
    assert check.static_check(hacs_json, integration) == 0


def test_a_name_the_table_does_not_know_is_not_a_failure(tmp_path: Path) -> None:
    """The static check only knows the names in FIRST_RELEASE; the import check covers the rest."""
    hacs_json, integration = _fake_repo(
        tmp_path, "2025.8.0", {"sensor.py": "from homeassistant.const import PERCENTAGE\n"}
    )
    assert check.static_check(hacs_json, integration) == 0


def test_this_repo_imports_nothing_newer_than_its_hacs_json_minimum() -> None:
    assert check.static_check() == 0


def test_the_hacs_json_minimum_is_a_release_version() -> None:
    minimum = json.loads(HACS_JSON.read_text())["homeassistant"]
    assert len(minimum.split(".")) == 3, "hacs.json wants year.month.patch"
    newest_needed = max(check.FIRST_RELEASE.values(), key=check.parse_version)
    assert check.parse_version(minimum) >= check.parse_version(
        newest_needed
    ), f"the code imports a name added in Home Assistant {newest_needed}"


def test_every_module_imports_on_the_minimum_release() -> None:
    """The script's --import mode, run the way CI would: a fresh interpreter, exit code checked."""
    installed = check.installed_homeassistant()
    minimum = check.minimum_version()
    if installed is None or check.parse_version(installed)[:2] != check.parse_version(minimum)[:2]:
        pytest.skip(f"needs Home Assistant {minimum}'s release installed, found {installed}")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--import"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Every module imports on Home Assistant {installed}." in result.stdout
