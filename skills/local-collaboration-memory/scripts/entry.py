"""Portable launcher; works with absolute Python/script paths without installation."""
from local_memory.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
