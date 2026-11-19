"""Package-local launcher for both a source checkout and an installed wheel."""

from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from local_memory.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
