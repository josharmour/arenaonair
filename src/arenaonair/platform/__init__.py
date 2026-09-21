"""Per-OS platform adapters for ArenaOnAir.

Everything OS-specific hides behind this package boundary (DESIGN.md §2,
constraint #6): no ``sys.platform`` special cases outside ``platform/``.

Modules:
    logpath  -- dispatches Player.log discovery to the right OS adapter.
    windows / macos / linux -- one discovery routine each.
"""

from .logpath import default_player_log_path

__all__ = ["default_player_log_path"]
