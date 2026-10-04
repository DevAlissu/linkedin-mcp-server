"""Only the tools that write to LinkedIn ask Claude Code for the user's approval.

``tests/test_server.py`` checks that the served tool list equals the recorded
contract; this checks the rule the contract must follow: a tool carries
``_meta["anthropic/requiresUserInteraction"] = true`` exactly when it is tagged
``actions``, so a new write tool cannot ship without it and a read tool does not
interrupt the user for nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

from linkedin_mcp_server.tools.approval import (
    REQUIRES_USER_INTERACTION_KEY,
    requires_user_interaction,
)

_TOOL_CONTRACT = Path(__file__).parent / "fixtures" / "tool-contract" / "tools.json"


def _tools() -> list[dict]:
    return json.loads(_TOOL_CONTRACT.read_text(encoding="utf-8"))


def _tags(tool: dict) -> set[str]:
    return set(tool.get("_meta", {}).get("fastmcp", {}).get("tags", []))


def test_every_write_tool_and_only_those_require_user_interaction() -> None:
    tools = _tools()
    writes = {t["name"] for t in tools if "actions" in _tags(t)}
    marked = {
        t["name"]
        for t in tools
        if t.get("_meta", {}).get(REQUIRES_USER_INTERACTION_KEY) is True
    }

    assert writes == {"apply_profile_changes", "connect_with_person", "send_message"}
    assert marked == writes


def test_no_tool_marked_for_approval_claims_to_be_read_only() -> None:
    for tool in _tools():
        if tool.get("_meta", {}).get(REQUIRES_USER_INTERACTION_KEY):
            assert tool.get("annotations", {}).get("readOnlyHint") is not True, tool[
                "name"
            ]


def test_each_tool_gets_its_own_meta_mapping() -> None:
    first, second = requires_user_interaction(), requires_user_interaction()

    assert first == {REQUIRES_USER_INTERACTION_KEY: True}
    assert first is not second
