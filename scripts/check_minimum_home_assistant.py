"""Hold the code to the minimum Home Assistant that ``hacs.json`` promises.

HACS offers a release to every install at or above the ``homeassistant`` version in
``hacs.json`` and turns older installs away, so that field is a promise: the integration
loads on that release. Nothing enforced it. 0.2.x started importing ``UnitOfDensity``,
which core only gained in 2026.9, while ``hacs.json`` still said 2025.8.0, so an install
in between would have been offered a release that fails at import time.

Two checks, both run from the repo root:

    python scripts/check_minimum_home_assistant.py            # the static check
    python scripts/check_minimum_home_assistant.py --import   # plus the import check

The static check needs only the standard library: it reads the minimum from ``hacs.json``,
walks every ``from homeassistant... import`` in the integration with ``ast``, and fails when
a name in ``FIRST_RELEASE`` (names core added recently, with the release that added them)
needs a newer release than the minimum. It runs without Home Assistant installed, so the
test suite runs it on every change. Its blind spot is a name missing from the table.

The import check closes that blind spot the way upstream's CI does (#177), by importing
every module of the integration on the minimum release itself. It only means something
against that release, so it refuses to run unless the installed Home Assistant is the
same year.month as the minimum (a patch release never adds a constant). Exits 0 when every
check passes, 1 otherwise, 2 when the import check cannot run.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import importlib.util
import json
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "frigidaire"
HACS_JSON = REPO_ROOT / "hacs.json"

# Names this integration imports from Home Assistant that core added recently, with the
# first release that has them. Add a row when a change starts using something new; the
# test suite then fails until hacs.json's minimum is at least that release.
FIRST_RELEASE: dict[str, str] = {
    "homeassistant.config_entries.OptionsFlowWithReload": "2025.8.0",
    "homeassistant.const.UnitOfDensity": "2026.9.0",
}


def parse_version(version: str) -> tuple[int, ...]:
    """``"2026.9.0"`` -> ``(2026, 9, 0)``; a missing patch counts as 0, so ``2026.9`` == ``2026.9.0``."""
    parts = tuple(int(piece) for piece in version.split("."))
    return parts + (0,) * (3 - len(parts))


def minimum_version(hacs_json: Path = HACS_JSON) -> str:
    return json.loads(hacs_json.read_text())["homeassistant"]


def integration_files(integration_dir: Path = INTEGRATION_DIR) -> list[Path]:
    """The integration's own modules; the vendored client never imports Home Assistant."""
    return sorted(path for path in integration_dir.glob("*.py"))


def homeassistant_imports(source: str) -> set[str]:
    """Every dotted name imported from Home Assistant in ``source``.

    ``from homeassistant.const import UnitOfDensity`` yields ``homeassistant.const.UnitOfDensity``;
    ``from homeassistant import config_entries`` yields ``homeassistant.config_entries``. A name
    reached through attribute access, like ``config_entries.OptionsFlowWithReload``, is found
    by walking the attribute chain back to an imported module.
    """
    tree = ast.parse(source)
    names: set[str] = set()
    modules: dict[str, str] = {}  # local alias -> dotted module, for attribute lookups
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "homeassistant":
            for alias in node.names:
                dotted = f"{node.module}.{alias.name}"
                names.add(dotted)
                modules[alias.asname or alias.name] = dotted
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "homeassistant":
                    names.add(alias.name)
                    modules[alias.asname or alias.name] = alias.name
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            chain = _attribute_chain(node)
            if chain and chain[0] in modules:
                names.add(".".join([modules[chain[0]], *chain[1:]]))
    return names


def _attribute_chain(node: ast.expr) -> list[str] | None:
    """``a.b.c`` -> ``["a", "b", "c"]``; anything that is not a plain dotted name -> None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return parts[::-1]


def too_new_for(minimum: str, files: list[Path], first_release: dict[str, str] = FIRST_RELEASE) -> list[str]:
    """Problems with each file that imports a name newer than ``minimum``; empty when none."""
    floor = parse_version(minimum)
    problems: list[str] = []
    for path in files:
        for name in sorted(homeassistant_imports(path.read_text())):
            needed = first_release.get(name)
            if needed and parse_version(needed) > floor:
                problems.append(f"{path.name} imports {name}, added in Home Assistant {needed}")
    return problems


def static_check(hacs_json: Path = HACS_JSON, integration_dir: Path = INTEGRATION_DIR) -> int:
    minimum = minimum_version(hacs_json)
    problems = too_new_for(minimum, integration_files(integration_dir))
    for problem in problems:
        print(f"FAIL {problem}")
    if problems:
        print(
            f"\nhacs.json promises Home Assistant {minimum}, which lacks {len(problems)} name(s) the code imports. "
            "Raise the minimum to the release that has them, or stop using them."
        )
        return 1
    print(f"ok   every known-version import fits the hacs.json minimum, Home Assistant {minimum}")
    return 0


def installed_homeassistant() -> str | None:
    if importlib.util.find_spec("homeassistant") is None:
        return None
    from homeassistant.const import __version__

    return __version__


def integration_modules(integration_dir: Path = INTEGRATION_DIR) -> list[str]:
    package = ".".join(integration_dir.relative_to(REPO_ROOT).parts)
    submodules = [path.stem for path in integration_files(integration_dir) if path.stem != "__init__"]
    return [package] + [f"{package}.{name}" for name in submodules]


def import_check(hacs_json: Path = HACS_JSON, integration_dir: Path = INTEGRATION_DIR) -> int:
    minimum = minimum_version(hacs_json)
    installed = installed_homeassistant()
    if installed is None:
        print("Home Assistant is not installed; the import check needs the minimum release installed")
        return 2
    if parse_version(installed)[:2] != parse_version(minimum)[:2]:
        print(
            f"hacs.json promises Home Assistant {minimum}, but {installed} is installed; "
            "importing on a different release proves nothing about the minimum"
        )
        return 2

    sys.path.insert(0, str(REPO_ROOT))
    failed = 0
    for module in integration_modules(integration_dir):
        try:
            importlib.import_module(module)
        except Exception:
            failed += 1
            print(f"FAIL {module}\n{traceback.format_exc()}")
        else:
            print(f"ok   {module}")
    if failed:
        print(f"\n{failed} module(s) do not import on Home Assistant {installed}, the release hacs.json promises.")
        return 1
    print(f"\nEvery module imports on Home Assistant {installed}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--import",
        dest="do_import",
        action="store_true",
        help="also import every module of the integration on the installed Home Assistant, "
        "which must be the minimum's release",
    )
    args = parser.parse_args(argv)
    status = static_check()
    if args.do_import:
        status = max(status, import_check())
    return status


if __name__ == "__main__":
    sys.exit(main())
