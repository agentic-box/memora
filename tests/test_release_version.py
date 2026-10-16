"""Release prep: the version is pinned consistently, and a four-component version (0.5.7.1) is accepted
everywhere the version is parsed or compared.

Where it is parsed or compared: pyproject.toml (setuptools, PEP 440), memora/__init__.py (_get_version: package
metadata, else the pyproject regex), /health (memora.__version__), and scripts/deploy-memora-all.sh, which derives the
expected /health version from the tag with ${TAG#v} and compares the two STRINGS. None of them splits on dots or
assumes three components.
"""
import re
import subprocess
from pathlib import Path

import pytest
from packaging.version import Version

import memora

ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    return re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)


def test_every_pin_of_the_version_agrees():
    v = _pyproject_version()
    assert re.search(rf'^version: "{re.escape(v)}"', (ROOT / "agent.yaml").read_text(), re.M)
    deploy = (ROOT / "scripts" / "deploy-memora-all.sh").read_text()
    assert f'TAG="${{DEPLOY_TAG:-v{v}}}"' in deploy                      # the default tag
    assert f"\nTAG|DEPLOY_TAG|v{v}\n" in deploy                         # the DEFAULTS guard table
    assert f"to v{v}: fetch" in deploy and f"checkout the v{v} tag" in deploy and f"reports version {v} (proves" in deploy
    assert re.search(rf"^## {re.escape(v)} — \d{{4}}-\d\d-\d\d$", (ROOT / "CHANGELOG.md").read_text(), re.M)


def test_a_four_component_version_is_valid_and_ordered_between_its_neighbours():
    v = Version(_pyproject_version())
    assert str(v) == _pyproject_version()                                 # PEP 440 does not normalise it away
    assert v.release == tuple(int(x) for x in _pyproject_version().split("."))
    assert Version("0.5.7") < v < Version("0.5.8")


def test_the_version_fallback_reads_four_components(monkeypatch):
    """memora._get_version without installed metadata reads pyproject.toml with a regex that must keep every component."""
    def not_found(_name):
        raise memora._PNF(_name)
    monkeypatch.setattr(memora, "_meta_version", not_found)
    assert memora._get_version() == _pyproject_version()


@pytest.mark.parametrize("tag,expected", [("v0.5.7.1", "0.5.7.1"), ("v0.5.7", "0.5.7"), ("v1.2.3.4.5", "1.2.3.4.5")])
def test_the_deploys_expected_health_version_is_the_tag_without_the_v(tag, expected):
    """scripts/deploy-memora-all.sh passes "${TAG#v}" as EXPECTED_VERSION and compares it to /health's string."""
    out = subprocess.run(["bash", "-c", 'TAG="$1"; printf %s "${TAG#v}"', "x", tag], capture_output=True, text=True)
    assert out.stdout == expected
