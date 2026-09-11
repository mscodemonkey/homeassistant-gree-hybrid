"""Load the protocol package without importing Home Assistant integration setup."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

PROTOCOL = Path(__file__).parents[1] / "custom_components" / "gree_hybrid" / "protocol"

package = ModuleType("gree_hybrid_protocol")
package.__path__ = [str(PROTOCOL)]
sys.modules.setdefault("gree_hybrid_protocol", package)
