"""Independent entry point; install beside production adapter modules."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from slg_f95_worker import main

if __name__ == "__main__":
    raise SystemExit(main())
