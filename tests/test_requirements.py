"""Guard the library boundary: the client is vendored, not installed from PyPI."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "custom_components" / "frigidaire" / "manifest.json"
VENDOR_DIR = ROOT / "custom_components" / "frigidaire" / "vendor" / "frigidaire"
VENDORED_NOTES = VENDOR_DIR / "VENDORED.md"


def _requirements() -> list[str]:
    return json.loads(MANIFEST.read_text())["requirements"]


def test_manifest_does_not_install_frigidaire_from_pypi() -> None:
    """The PyPI build disables TLS verification (audit C1); the vendored copy replaces it."""
    assert not [r for r in _requirements() if r.startswith("frigidaire")]


def test_manifest_declares_what_the_vendored_client_imports() -> None:
    """Vendoring drops frigidaire's own dependency metadata, so requests must be declared here."""
    assert any(re.fullmatch(r"requests(>=[\d.]+)?", requirement) for requirement in _requirements())


def test_vendored_copy_is_present_and_records_its_upstream_version() -> None:
    for module in ("__init__.py", "rate_limit.py", "signature_generator.py"):
        assert (VENDOR_DIR / module).is_file()
    assert (VENDOR_DIR / "LICENSE").is_file()
    notes = VENDORED_NOTES.read_text()
    assert re.search(r"\*\*Vendored from version:\*\* \d+\.\d+\.\d+", notes)


def test_no_integration_module_imports_the_pypi_client() -> None:
    """The PyPI package is the one with verify=False; every import must go through vendor/."""
    for path in sorted((ROOT / "custom_components" / "frigidaire").glob("*.py")):
        assert not re.search(r"^(import frigidaire\b|from frigidaire import)", path.read_text(), re.M), path
