"""Clip oversized MCP tool results out of the model context.

Heavy MCP servers (e.g. a CRM connector) can return page dumps of tens of KB
per tool call. Every byte of a ``ToolMessage.content`` rides the model context
on all subsequent turns of the thread, and one 4000-line customer-list JSON is
enough to poison a thread: later turns slow to a crawl or the model gives up
entirely (empty completions, hung streams).

Model-request converters serialize ``message.content`` but skip
``ToolMessage.artifact`` — the same seam ``octop_ui`` offload uses. So for
oversized results we:

1. move the full text into ``artifact`` (dashboard / history keep it),
2. replace ``content`` with a slim stub: what was returned, how big, and a
   head excerpt, plus instructions to re-request with pagination/grep when
   the details are actually needed.

The clip happens at ``wrap_tool_call`` time (result fresh, before checkpoint),
so history stays complete in ``artifact`` and the model context only ever
carries the stub.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

logger = logging.getLogger(__name__)

#: Results at or above this size get clipped (chars ≈ bytes for ASCII; CJK
#: counts ~3 bytes/char so effective threshold for Chinese-heavy payloads is
#: ~1/3 of this in bytes).
CLIP_MIN_CHARS = 12_000
#: How much of the original content stays visible as an excerpt.
EXCERPT_CHARS = 1_500


def _clip_content(text: str) -> str:
    lines = text.count("\n") + 1
    return (
        "[结果过大已截断] 完整结果共 "
        f"{len(text)} 字符 / {lines} 行，已移出上下文。"
        "如需完整数据：1) 用分页参数（pageSize/offset）重新查询，每次不超过 50 条；"
        "2) 或缩小查询范围/筛选条件后重试。\n"
        "--- 开头节选 ---\n"
        f"{text[:EXCERPT_CHARS]}\n--- 节选结束 ---"
    )


class ToolResultClipMiddleware(AgentMiddleware[Any, Any]):
    """Clip oversized ``ToolMessage.content`` into ``artifact`` + slim stub."""

    def __init__(self, *, clip_min_chars: int = CLIP_MIN_CHARS) -> None:
        super().__init__()
        self._min_chars = clip_min_chars

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        result = handler(request)
        self._try_clip(result)
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        result = await handler(request)
        self._try_clip(result)
        return result

    def _try_clip(self, result: ToolMessage | Command[Any]) -> None:
        try:
            self._clip(result)
        except Exception:
            # Clipping must never break a tool call.
            logger.warning("tool result clip failed", exc_info=True)

    def _clip(self, result: ToolMessage | Command[Any]) -> None:
        if not isinstance(result, ToolMessage):
            return
        content = result.content
        if not isinstance(content, str) or len(content) < self._min_chars:
            return
        if result.artifact is not None:
            # Something already lives on the artifact slot — leave it alone.
            return
        result.artifact = content
        result.content = _clip_content(content)
        logger.info(
            "tool result clipped: %d chars moved to artifact (tool_call=%s)",
            len(content),
            getattr(result, "tool_call_id", "?"),
        )


__all__ = ["ToolResultClipMiddleware", "CLIP_MIN_CHARS"]
