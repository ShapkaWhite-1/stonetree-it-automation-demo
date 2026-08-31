"""Run the project without installing it first."""

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from stonetree_it_automation.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())

