"""ToolResultClipMiddleware — oversized tool results stay out of model context."""

from __future__ import annotations

from langchain_core.messages import ToolMessage

from octop.infra.agents.middleware.tool_result_clip import (
    CLIP_MIN_CHARS,
    ToolResultClipMiddleware,
)


def _tool_result(content: str) -> ToolMessage:
    return ToolMessage(content=content, tool_call_id="call_1", name="entity-query")


def _run(mw: ToolResultClipMiddleware, msg: ToolMessage) -> ToolMessage:
    def handler(_request: object) -> ToolMessage:
        return msg

    class _Req:
        pass

    out = mw.wrap_tool_call(_Req(), handler)  # type: ignore[arg-type]
    assert isinstance(out, ToolMessage)
    return out


def test_small_result_untouched() -> None:
    mw = ToolResultClipMiddleware()
    msg = _run(mw, _tool_result('{"customers": [1, 2, 3]}'))
    assert msg.content == '{"customers": [1, 2, 3]}'
    assert msg.artifact is None


def test_oversized_result_clipped() -> None:
    mw = ToolResultClipMiddleware()
    big = '{"rows": [' + ",".join(str(i) for i in range(4000)) + "]}"
    assert len(big) >= CLIP_MIN_CHARS
    msg = _run(mw, _tool_result(big))
    # Full payload preserved on the artifact (history / dashboard keep it).
    assert msg.artifact == big
    # Model context only carries the slim stub.
    assert len(msg.content) < 2000
    assert "结果过大已截断" in msg.content
    assert "分页" in msg.content
    # Stub shows the original head so the model knows what it got.
    assert '{"rows": [0,1,2' in msg.content.replace(", ", ",")


def test_custom_threshold() -> None:
    mw = ToolResultClipMiddleware(clip_min_chars=100)
    msg = _run(mw, _tool_result("x" * 150))
    assert msg.artifact == "x" * 150
    assert "x" * 150 != msg.content


def test_existing_artifact_not_overwritten() -> None:
    mw = ToolResultClipMiddleware()
    original = ToolMessage(
        content="x" * 20_000,
        tool_call_id="call_1",
        name="t",
        artifact={"already": "here"},
    )
    msg = _run(mw, original)
    assert msg.artifact == {"already": "here"}
    assert msg.content == "x" * 20_000


def test_non_string_content_untouched() -> None:
    mw = ToolResultClipMiddleware()
    original = ToolMessage(
        content=[{"type": "text", "text": "x" * 20_000}],
        tool_call_id="call_1",
        name="t",
    )
    msg = _run(mw, original)
    assert msg.content == [{"type": "text", "text": "x" * 20_000}]
    assert msg.artifact is None
