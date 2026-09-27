"""Compatibility entry point; the builder now ships in the package.

    python -m tools.build_carddb [--out PATH]   ==   arenaonair build-carddb [--out PATH]
"""
import sys

from arenaonair import build_carddb as _impl

if __name__ == "__main__":
    raise SystemExit(_impl.main())
sys.modules[__name__] = _impl
