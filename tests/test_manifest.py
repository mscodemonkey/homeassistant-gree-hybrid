"""Static checks for the custom integration scaffold."""

import json
import struct
from pathlib import Path

INTEGRATION = Path(__file__).parents[1] / "custom_components" / "gree_hybrid"
REPOSITORY = Path(__file__).parents[1]


def _png_size(path: Path) -> tuple[int, int]:
    """Read a PNG's dimensions without adding an image-library dependency."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def test_manifest_identifies_gree_hybrid() -> None:
    """The manifest and package directory use the hybrid domain."""
    manifest = json.loads((INTEGRATION / "manifest.json").read_text())

    assert manifest["domain"] == "gree_hybrid"
    assert manifest["name"] == "Gree Hybrid"
    assert all("greeclimate" not in requirement for requirement in manifest["requirements"])
    assert (INTEGRATION / "__init__.py").is_file()


def test_config_flow_and_translations_are_present() -> None:
    """The integration can be discovered and configured by Home Assistant."""
    assert (INTEGRATION / "config_flow.py").is_file()
    assert (INTEGRATION / "strings.json").is_file()
    assert (INTEGRATION / "translations" / "en.json").is_file()


def test_home_assistant_and_hacs_brand_icons_are_present() -> None:
    """Both consumers receive correctly sized project-owned PNG assets."""
    for root in (INTEGRATION / "brand", REPOSITORY / "brand"):
        assert _png_size(root / "icon.png") == (256, 256)
        assert _png_size(root / "icon@2x.png") == (512, 512)
