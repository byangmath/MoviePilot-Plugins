from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
PLUGIN_ROOT = ROOT / "plugins.v2"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
