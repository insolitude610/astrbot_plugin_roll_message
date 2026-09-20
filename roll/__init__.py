"""Dependency-free internals of the astrbot_plugin_roll plugin.

The modules in this package deliberately avoid importing AstrBot so the
regeneration and recall rules can be unit-tested with a bare Python
interpreter on any operating system.  ``main.py`` contains the thin AstrBot
wiring.
"""

from __future__ import annotations

__all__ = ["core", "history", "recall"]
