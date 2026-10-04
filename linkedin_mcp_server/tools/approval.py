"""Client-side approval marker for the tools that write to LinkedIn.

Claude Code asks the user before every call of a tool whose ``tools/list``
entry carries ``_meta["anthropic/requiresUserInteraction"] = true``, even in
its bypass-permissions mode. Other clients ignore the key, so it adds a human
check where it is honoured and never replaces the server's own safeguards
(the write flag, ``confirm`` and change sets).

Every tool tagged ``actions`` writes to LinkedIn and carries it; no other tool
does (``tests/test_tool_approval.py``).
"""

from __future__ import annotations

REQUIRES_USER_INTERACTION_KEY = "anthropic/requiresUserInteraction"


def requires_user_interaction() -> dict[str, bool]:
    """A fresh ``meta`` mapping for ``@mcp.tool``; never shared between tools."""
    return {REQUIRES_USER_INTERACTION_KEY: True}
