"""Authenticated Streamable HTTP MCP endpoint for Octop expert agents."""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from octop.api.deps import current_user, get_server
from octop.infra.agent_mcp.protocol import handle_agent_mcp_request
from octop.infra.agent_mcp.service import AgentMcpGatewayService
from octop.infra.agent_mcp.stream_bridge import ExpertStreamService
from octop.infra.errors import ErrorCode, OctopError

router = APIRouter()

MCP_SESSION_HEADER = "mcp-session-id"
MCP_PROTOCOL_HEADER = "mcp-protocol-version"

# 这几个是 /stream 的核心参数，不能被当成 caller_meta 透传。
_STREAM_RESERVED_KEYS = frozenset({"expert_id", "message", "thread_id"})


def _service(server: Any) -> AgentMcpGatewayService:
    if server.app_runtime is None:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "server runtime is not ready")
    return AgentMcpGatewayService(
        agent_manager=server.app_runtime.agent_registry,
        gateway=server.app_runtime.gateway,
    )


def _stream_service(server: Any) -> ExpertStreamService:
    if server.app_runtime is None:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "server runtime is not ready")
    return ExpertStreamService(
        agent_manager=server.app_runtime.agent_registry,
        gateway=server.app_runtime.gateway,
    )


@router.post("/mcp/agents", summary="Call Octop experts through MCP")
async def agent_mcp_post(
    request: Request,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> Response:
    """Handle MCP initialize, tools/list, tools/call, notifications, and ping."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise OctopError(ErrorCode.SLASH_BAD_ARGS, "invalid JSON body") from exc
    if not isinstance(body, dict):
        raise OctopError(ErrorCode.SLASH_BAD_ARGS, "expected JSON object")

    method = str(body.get("method") or "")
    if method == "notifications/initialized":
        return Response(status_code=202)

    response = await handle_agent_mcp_request(
        service=_service(server),
        user=user,
        body=body,
    )
    headers = {"content-type": "application/json"}
    session_id = request.headers.get(MCP_SESSION_HEADER)
    if method == "initialize":
        session_id = secrets.token_urlsafe(16)
    if session_id:
        headers[MCP_SESSION_HEADER] = session_id
    protocol = request.headers.get(MCP_PROTOCOL_HEADER)
    if protocol:
        headers[MCP_PROTOCOL_HEADER] = protocol
    return JSONResponse(content=response, headers=headers)


@router.get("/mcp/agents", summary="Open the Octop Agent MCP event stream")
async def agent_mcp_get(
    request: Request,
    _user: Any = Depends(current_user),
) -> Response:
    """Open the optional SSE channel used by Streamable HTTP MCP clients."""
    if not request.headers.get(MCP_SESSION_HEADER):
        return JSONResponse(status_code=405, content={"error": "missing mcp-session-id"})

    async def _keepalive() -> Any:
        yield "event: message\ndata: \n\n"
        while True:
            await asyncio.sleep(3600)

    return StreamingResponse(
        _keepalive(),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "connection": "keep-alive"},
    )


@router.delete("/mcp/agents", status_code=204, summary="Close an Octop Agent MCP session")
async def agent_mcp_delete(_user: Any = Depends(current_user)) -> Response:
    """Acknowledge Streamable HTTP session cleanup."""
    return Response(status_code=204)


@router.post(
    "/mcp/agents/stream",
    summary="Stream an expert turn as Server-Sent Events",
)
async def agent_mcp_stream(
    request: Request,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> Response:
    """正文流式 + 工具状态（只到 start/end 粒度）+ 不含思考过程。

    请求体：
        {
          "expert_id": "xxx",            # 必填
          "message": "……",               # 必填
          "thread_id": "xxx",            # 可选，续聊时带上一轮 ack/done 里的值
          "itr_user_id": 1997            # 其余字段按白名单透传进 turn metadata
        }
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise OctopError(ErrorCode.SLASH_BAD_ARGS, "invalid JSON body") from exc
    if not isinstance(body, dict):
        raise OctopError(ErrorCode.SLASH_BAD_ARGS, "expected JSON object")

    expert_id = str(body.get("expert_id") or "").strip()
    if not expert_id:
        raise OctopError(ErrorCode.SLASH_BAD_ARGS, "expert_id is required")

    raw_thread = body.get("thread_id")
    thread_id = str(raw_thread).strip() if raw_thread else None

    # 除核心参数外，其余非空字段按白名单透传（防提权在 _merge_caller_meta 里）。
    caller_meta = {
        key: value
        for key, value in body.items()
        if key not in _STREAM_RESERVED_KEYS and value is not None
    }

    service = _stream_service(server)

    # 校验必须在开流之前全部做完：一旦返回 200 text/event-stream，
    # 就再也给不出 4xx 了，只能塞一个 error 帧进去。
    try:
        prepared = await service.prepare(
            user,
            expert_id=expert_id,
            message=body.get("message"),
            thread_id=thread_id,
            caller_meta=caller_meta,
        )
    except ValueError as exc:
        raise OctopError(ErrorCode.SLASH_BAD_ARGS, str(exc)) from exc

    return StreamingResponse(
        service.stream(
            prepared,
            user_id=user.id,
            is_disconnected=request.is_disconnected,
        ),
        media_type="text/event-stream",
        headers={
            "cache-control": "no-cache",
            "connection": "keep-alive",
            # 别删：nginx 及部分云 LB 会缓冲 SSE，删了就成了「全部生成完才一次性吐出」
            "x-accel-buffering": "no",
        },
    )
