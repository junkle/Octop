"""SSE bridge for expert turns: streaming text, coarse tool status, no reasoning.

只做三件事：
  1. token chunk  → SSE `token` 帧（正文真增量）
  2. tool_call_chunk / tool_result → 折叠成 `tool` 帧，仅暴露 start/end，不带工具名
  3. reasoning chunk 直接丢弃（思考过程永不出网）
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from octop_gateway.models import ChannelSubject, InboundMessage, TextContent

from octop.infra.agent_mcp.service import (
    MCP_CHANNEL_ID,
    AgentMcpGatewayService,
    _merge_caller_meta,
)
from octop.infra.gateway.hitl.coordinator import HitlStreamContext
from octop.infra.gateway.hitl.format import normalize_hitl_request
from octop.infra.gateway.threads import ThreadRegistry

_log = logging.getLogger(__name__)

# 心跳间隔。反代/网关的空闲超时一般是 60s，留出 4 倍余量。
HEARTBEAT_SECONDS = 15.0


def sse_frame(event: str, data: dict[str, Any]) -> str:
    """单行长 JSON + event 名，前端按 event 分派即可。"""
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _safe_disconnected(probe: Callable[[], Awaitable[bool]]) -> bool:
    try:
        return bool(await probe())
    except Exception:  # noqa: BLE001 - 探测失败就当还连着
        return False


@dataclass(slots=True)
class PreparedTurn:
    """开流前的全部校验结果。拿到它之后再返回 200 就安全了。"""

    expert_id: str
    thread_id: str
    session_key: str
    inbound: InboundMessage


class _ToolTracker:
    """把 tool_call_chunk / tool_result 折叠成 start / end，不保留工具名。

    同一 index 的 name/args 分片可能来很多次，start 只发一次；
    tool_result 不带 index，按先来先配（本地工具是串行的，够用）。
    """

    __slots__ = ("_open", "_seq")

    def __init__(self) -> None:
        self._open: dict[int, str] = {}
        self._seq = 0

    def on_call_chunk(self, chunk: dict[str, Any]) -> str | None:
        raw = chunk.get("index", 0)
        try:
            idx = int(raw)
        except (TypeError, ValueError):
            idx = 0
        if idx in self._open:
            return None
        self._seq += 1
        call_id = f"c{self._seq}"
        self._open[idx] = call_id
        return call_id

    def on_result(self) -> str | None:
        if not self._open:
            return None
        idx = next(iter(self._open))
        return self._open.pop(idx)


class ExpertStreamService(AgentMcpGatewayService):
    """在 AgentMcpGatewayService 基础上加 prepare / stream，父类方法一个不动。"""

    async def prepare(
        self,
        user: Any,
        *,
        expert_id: str,
        message: Any,
        thread_id: str | None = None,
        caller_meta: dict[str, Any] | None = None,
    ) -> PreparedTurn:
        """开流前的全部校验：专家可见性、消息长度、thread 归属、身份白名单。

        任何一项不过都抛异常，调用方还能回 4xx —— 一旦返回了
        `text/event-stream` 就没法再改 HTTP 状态码了。
        """
        row = self._require_expert(expert_id, user)
        text = self._validate_message(message)
        thread, session_key = await self._resolve_thread(
            expert_id=row.agent_id,
            user=user,
            thread_id=thread_id,
        )
        meta: dict[str, Any] = {
            "session_key": session_key,
            "thread_id": thread,
            "user_is_admin": user.is_admin,
        }
        # 只放行白名单 key；session_key、thread_id、user_is_admin 不可被外部覆盖。
        _merge_caller_meta(meta, caller_meta)
        inbound = InboundMessage(
            channel_id=MCP_CHANNEL_ID,
            channel_type=ThreadRegistry.CHANNEL_MCP,
            tenant_id=row.agent_id,
            channel_subject=ChannelSubject(subject_id=str(user.id)),
            channel_session_id=session_key,
            content=[TextContent(text=text)],
            metadata=meta,
        )
        return PreparedTurn(
            expert_id=row.agent_id,
            thread_id=thread,
            session_key=session_key,
            inbound=inbound,
        )

    async def stream(
        self,
        prepared: PreparedTurn,
        *,
        user_id: int,
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    ) -> AsyncIterator[str]:
        expert_id = prepared.expert_id
        thread_id = prepared.thread_id
        parts: list[str] = []
        usage: dict[str, Any] | None = None
        tracker = _ToolTracker()
        status = "completed"
        error: dict[str, Any] | None = None
        approval: dict[str, Any] | None = None

        # 首帧就把 thread_id 交出去：第一轮也能拿到，续聊和审批恢复都靠它。
        yield sse_frame(
            "ack",
            {"expert_id": expert_id, "thread_id": thread_id, "ts": _now_ms()},
        )

        # 不能把每次 __anext__ 丢进独立 Task：hitl_thread_scope 的 ContextVar
        # Token 会在「set 的 Task」与「reset 的 Task」不一致时炸
        # 「Token was created in a different Context」。
        # 整轮 async for 固定在同一个 pump Task；外层只做心跳超时读队列。
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        async def _pump() -> None:
            try:
                async for chunk in self._gateway.processor.iter_turn_chunks(
                    prepared.inbound
                ):
                    await queue.put(("chunk", chunk))
                await queue.put(("end", None))
            except asyncio.CancelledError:
                await queue.put(("end", None))
                raise
            except Exception as exc:  # noqa: BLE001
                await queue.put(("pump_error", exc))

        pump_task = asyncio.create_task(_pump(), name="octop-mcp-stream-pump")
        try:
            while True:
                try:
                    kind, payload = await asyncio.wait_for(
                        queue.get(), timeout=HEARTBEAT_SECONDS
                    )
                except asyncio.TimeoutError:
                    yield sse_frame("heartbeat", {})
                    if is_disconnected is not None and await _safe_disconnected(
                        is_disconnected
                    ):
                        break
                    continue

                if kind == "end":
                    break
                if kind == "pump_error":
                    exc = payload
                    _log.exception(
                        "expert stream failed: expert=%s thread=%s",
                        expert_id,
                        thread_id,
                    )
                    status = "failed"
                    error = {
                        "message": str(exc) or exc.__class__.__name__
                    }
                    yield sse_frame("error", error)
                    break

                chunk = payload
                if not isinstance(chunk, dict):
                    continue
                ctype = str(chunk.get("type") or "")

                if ctype in ("token", "delta"):
                    text = chunk.get("content", chunk.get("text", ""))
                    if text:
                        parts.append(str(text))
                        yield sse_frame("token", {"text": str(text)})

                elif ctype == "tool_call_chunk":
                    call_id = tracker.on_call_chunk(chunk)
                    if call_id is not None:
                        yield sse_frame(
                            "tool", {"call_id": call_id, "phase": "start"}
                        )

                elif ctype == "tool_result":
                    call_id = tracker.on_result()
                    if call_id is not None:
                        yield sse_frame(
                            "tool", {"call_id": call_id, "phase": "end"}
                        )

                elif ctype == "usage":
                    if isinstance(chunk.get("usage"), dict):
                        usage = dict(chunk["usage"])

                elif ctype == "hitl_required":
                    request = chunk.get("request")
                    pending_id = chunk.get("pending_id")
                    if isinstance(request, dict):
                        request = normalize_hitl_request(request)
                        record = (
                            self._gateway.processor.hitl_coordinator
                            .register_from_request(
                                request,
                                ctx=HitlStreamContext(
                                    thread_id=thread_id,
                                    agent_id=expert_id,
                                    user_id=user_id,
                                    session_key=prepared.session_key,
                                    channel_type=ThreadRegistry.CHANNEL_MCP,
                                ),
                            )
                        )
                        pending_id = record.pending_id
                        request = {**request, "pending_id": pending_id}
                    approval = {"pending_id": pending_id, "request": request}
                    status = "approval_required"
                    yield sse_frame("hitl", approval)
                    break

                elif ctype == "error":
                    error = {
                        "message": str(
                            chunk.get("message") or "expert call failed"
                        )
                    }
                    if chunk.get("error_code"):
                        error["code"] = str(chunk["error_code"])
                    status = "failed"
                    yield sse_frame("error", error)
                    break

                # reasoning → 故意丢弃（思考过程不展示）

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _log.exception(
                "expert stream failed: expert=%s thread=%s", expert_id, thread_id
            )
            status = "failed"
            error = {"message": str(exc) or exc.__class__.__name__}
            yield sse_frame("error", error)
        finally:
            if not pump_task.done():
                pump_task.cancel()
                try:
                    await pump_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

        done: dict[str, Any] = {
            "status": status,
            "expert_id": expert_id,
            "thread_id": thread_id,
            "response": "".join(parts),
        }
        if error:
            done["error"] = error
        if approval:
            done["approval"] = approval
        if usage:
            done["usage"] = usage
        yield sse_frame("done", done)
