"""Service validation tests for the Octop Agent MCP Gateway."""

from __future__ import annotations

import pytest

from octop.infra.agent_mcp.service import AgentMcpGatewayService


@pytest.mark.parametrize(
    ("decision", "message"),
    [
        ({"type": "edit"}, "edited_action"),
        ({"type": "respond", "message": " "}, "non-empty"),
        ({"type": "reject", "message": 7}, "must be a string"),
    ],
)
def test_validate_decisions_rejects_malformed_payloads(
    decision: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        AgentMcpGatewayService._validate_decisions([decision])


def test_validate_decisions_accepts_supported_payloads() -> None:
    decisions = [
        {"type": "approve"},
        {"type": "edit", "edited_action": {"name": "safe_tool"}},
        {"type": "reject", "message": "not allowed"},
        {"type": "respond", "message": "use the staging environment"},
    ]
    assert AgentMcpGatewayService._validate_decisions(decisions) == decisions


def test_merge_caller_meta_whitelists_keys() -> None:
    from octop.infra.agent_mcp.service import _merge_caller_meta

    meta = {"session_key": "sk", "thread_id": "t1", "user_is_admin": False}
    _merge_caller_meta(
        meta,
        {
            "itr_user_id": "001326",
            "wecom_user_id": "001326",
            "business_context": "itr",
        },
    )
    assert meta["itr_user_id"] == "001326"
    assert meta["wecom_user_id"] == "001326"
    assert meta["business_context"] == "itr"


def test_merge_caller_meta_blocks_privilege_escalation() -> None:
    from octop.infra.agent_mcp.service import _merge_caller_meta

    meta = {"session_key": "sk", "thread_id": "t1", "user_is_admin": False}
    _merge_caller_meta(
        meta,
        {
            "user_is_admin": True,  # 提权尝试：必须被忽略
            "session_key": "evil",
            "thread_id": "evil",
            "role": "admin",
            "itr_user_id": None,  # None 值也不透传
        },
    )
    assert meta["user_is_admin"] is False
    assert meta["session_key"] == "sk"
    assert meta["thread_id"] == "t1"
    assert "role" not in meta
    assert "itr_user_id" not in meta
