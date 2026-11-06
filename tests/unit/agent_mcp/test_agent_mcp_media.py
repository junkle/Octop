"""Agent MCP gateway media: inbound attachments and outbound artifact delivery."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from octop_gateway.models import (
    FileContent,
    ImageContent,
    InboundMessage,
    TextContent,
)

from octop.config import OctopConfig, _defaults_for_file
from octop.infra.agent_mcp.artifact_sink import ArtifactSink
from octop.infra.agent_mcp.attachments import (
    MAX_MCP_ATTACHMENTS,
    MAX_MESSAGE_CHARS,
    TURN_RESERVED_ARGS,
    build_content_parts,
    is_safe_inbound_path,
    resolve_media_type,
    validate_attachments,
    validate_turn,
)
from octop.infra.agent_mcp.protocol import handle_agent_mcp_request
from octop.infra.agent_mcp.stream_bridge import ExpertStreamService
from octop.infra.gateway.threads import ThreadRegistry
from octop.infra.users.identity import User

# ─── 入站附件：校验与内容块映射 ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "inbound/a.png",
        "inbound/wecom/01M63ECAGCT0JQCVC13A2HA584/1793859489_ab7207ff.png",
    ],
)
def test_is_safe_inbound_path_accepts_workspace_relative(path: str) -> None:
    assert is_safe_inbound_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "",
        "inbound/",
        "/inbound/a.png",
        "../SOUL.md",
        "inbound/../SOUL.md",
        "inbound/../../SOUL.md",
        "inbound/a\\b.png",
        "file://inbound/a.png",
        "C:/inbound/a.png",
        "outbound/a.png",
        "generated/a.png",
    ],
)
def test_is_safe_inbound_path_rejects_escape(path: str) -> None:
    assert is_safe_inbound_path(path) is False


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"media_type": "image/png"}, "image/png"),
        ({"mime_type": "image/jpeg"}, "image/jpeg"),
        ({"mimeType": "application/pdf"}, "application/pdf"),
        ({"media_type": " ", "mime_type": "text/csv"}, "text/csv"),
        ({}, "application/octet-stream"),
    ],
)
def test_resolve_media_type_prefers_known_keys(item: dict, expected: str) -> None:
    assert resolve_media_type(item) == expected


def test_validate_attachments_returns_empty_for_missing() -> None:
    assert validate_attachments(None) == []
    assert validate_attachments([]) == []


def test_validate_attachments_normalizes_fields() -> None:
    normalized = validate_attachments(
        [
            {"path": "inbound/chart.png", "media_type": "image/png"},
            {
                "workspace_path": "inbound/report.xlsx",
                "filename": "report.xlsx",
                "mime_type": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            },
        ]
    )

    assert normalized == [
        {"path": "inbound/chart.png", "filename": "chart.png", "media_type": "image/png"},
        {
            "path": "inbound/report.xlsx",
            "filename": "report.xlsx",
            "media_type": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        },
    ]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("inbound/a.png", "must be an array"),
        (["inbound/a.png"], "must be an object"),
        ([{"filename": "a.png"}], "path is required"),
        ([{"path": "inbound/../SOUL.md"}], "workspace-relative"),
        ([{"path": "/inbound/a.png"}], "workspace-relative"),
    ],
)
def test_validate_attachments_rejects_malformed(raw: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_attachments(raw)


def test_validate_attachments_rejects_too_many() -> None:
    raw = [{"path": f"inbound/{index}.png"} for index in range(MAX_MCP_ATTACHMENTS + 1)]
    with pytest.raises(ValueError, match="at most"):
        validate_attachments(raw)


def test_validate_turn_allows_attachment_only_message() -> None:
    attachments = validate_attachments([{"path": "inbound/a.png", "media_type": "image/png"}])

    assert validate_turn(None, attachments) == ""
    assert validate_turn("   ", attachments) == ""


def test_validate_turn_requires_text_or_attachment() -> None:
    with pytest.raises(ValueError, match="message is required"):
        validate_turn("", [])


def test_validate_turn_rejects_non_string_and_oversized() -> None:
    with pytest.raises(ValueError, match="must be a string"):
        validate_turn(7, [])
    with pytest.raises(ValueError, match="exceeds"):
        validate_turn("x" * (MAX_MESSAGE_CHARS + 1), [])


def test_build_content_parts_routes_images_and_files() -> None:
    attachments = validate_attachments(
        [
            {"path": "inbound/chart.png", "media_type": "image/png"},
            {"path": "inbound/shot.png", "media_type": "application/octet-stream"},
            {"path": "inbound/report.xlsx", "media_type": "application/vnd.ms-excel"},
        ]
    )

    parts = build_content_parts(attachments)

    assert [type(part) for part in parts] == [ImageContent, ImageContent, FileContent]
    assert parts[0].local_path == "inbound/chart.png"
    assert parts[0].mime_type == "image/png"
    assert parts[0].alt_text == "chart.png"
    # MIME 缺失但路径是图片时仍按图片处理
    assert parts[1].mime_type == "image/png"
    assert isinstance(parts[2], FileContent)
    assert parts[2].local_path == "inbound/report.xlsx"
    assert parts[2].filename == "report.xlsx"
    assert parts[2].mime_type == "application/vnd.ms-excel"


def test_build_content_parts_keeps_order_and_reads_no_bytes() -> None:
    attachments = validate_attachments(
        [
            {"path": "inbound/a.png", "media_type": "image/png"},
            {"path": "inbound/b.pdf", "media_type": "application/pdf"},
        ]
    )

    parts = build_content_parts(attachments)

    assert [part.local_path for part in parts] == ["inbound/a.png", "inbound/b.pdf"]
    # 路径引用模式：不携带内联字节，模型调用前才由中间件读取
    assert all(not getattr(part, "data", None) for part in parts)


# ─── 接线测试：校验装配真的接进两条 MCP 入口，防回退 ──────────────────────────


def _user() -> User:
    return User(id=7, username="alice", role="user", display_name="Alice")


def _expert_service() -> ExpertStreamService:
    manager = MagicMock()
    manager.get_row.return_value = SimpleNamespace(
        agent_id="expert-1",
        kind="expert",
        enabled=True,
        user_id=7,
        is_shared=False,
    )
    gateway = MagicMock()
    gateway.thread_registry.get_thread.return_value = SimpleNamespace(
        thread_id="thr-1",
        agent_id="expert-1",
        user_id=7,
        channel_type=ThreadRegistry.CHANNEL_MCP,
        session_key="session-1",
    )
    return ExpertStreamService(agent_manager=manager, gateway=gateway)


def _events(frames: list[str]) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for frame in frames:
        lines = frame.strip().splitlines()
        event = next(line for line in lines if line.startswith("event: ")).split(": ", 1)[1]
        data = next(line for line in lines if line.startswith("data: ")).split(": ", 1)[1]
        events.append((event, json.loads(data)))
    return events


def test_turn_reserved_args_keep_attachments_out_of_caller_meta() -> None:
    # 若被当成 caller_meta，会被白名单静默丢弃，prepare 收不到附件
    assert "attachments" in TURN_RESERVED_ARGS


@pytest.mark.asyncio
async def test_prepare_builds_text_and_media_parts() -> None:
    service = _expert_service()

    prepared = await service.prepare(
        _user(),
        expert_id="expert-1",
        message="这张图正常吗",
        thread_id="thr-1",
        attachments=[
            {"path": "inbound/chart.png", "media_type": "image/png"},
            {
                "path": "inbound/report.xlsx",
                "media_type": "application/vnd.ms-excel",
            },
        ],
    )

    content = prepared.inbound.content
    assert isinstance(prepared.inbound, InboundMessage)
    assert [type(part) for part in content] == [TextContent, ImageContent, FileContent]
    assert content[1].local_path == "inbound/chart.png"
    assert content[2].local_path == "inbound/report.xlsx"


@pytest.mark.asyncio
async def test_prepare_allows_attachment_only_turn() -> None:
    service = _expert_service()

    prepared = await service.prepare(
        _user(),
        expert_id="expert-1",
        message=None,
        thread_id="thr-1",
        attachments=[{"path": "inbound/chart.png", "media_type": "image/png"}],
    )

    assert [type(part) for part in prepared.inbound.content] == [ImageContent]


@pytest.mark.asyncio
async def test_prepare_rejects_path_escape_before_streaming() -> None:
    service = _expert_service()

    with pytest.raises(ValueError, match="workspace-relative"):
        await service.prepare(
            _user(),
            expert_id="expert-1",
            message="读一下",
            thread_id="thr-1",
            attachments=[{"path": "inbound/../SOUL.md"}],
        )


@pytest.mark.asyncio
async def test_stream_emits_artifact_and_done_summary() -> None:
    service = _expert_service()

    async def _chunks(_inbound: InboundMessage) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "token", "content": "已生成"}
        yield {
            "type": "attachment",
            "kind": "image",
            "url": "/api/agents/expert-1/workspace/download?path=outbound%2Fa.png",
            "filename": "a.png",
        }

    service._gateway.processor.iter_turn_chunks = _chunks
    prepared = SimpleNamespace(
        expert_id="expert-1",
        thread_id="thr-1",
        session_key="session-1",
        inbound=MagicMock(),
    )

    frames = [frame async for frame in service.stream(prepared, user_id=7)]
    events = _events(frames)

    artifact = next(data for event, data in events if event == "artifact")
    assert artifact == {
        "kind": "image",
        "url": "/api/agents/expert-1/workspace/download?path=outbound%2Fa.png",
        "filename": "a.png",
    }
    done = next(data for event, data in events if event == "done")
    assert done["artifacts"] == [artifact]
    assert done["response"] == "已生成"


@pytest.mark.asyncio
async def test_collect_result_collects_artifacts_for_jsonrpc() -> None:
    async def _chunks() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "token", "content": "ok"}
        yield {"type": "attachment", "kind": "file", "filename": "b.xlsx"}

    service = _expert_service()
    result = await service._collect_result(_chunks(), expert_id="expert-1", thread_id="thr-1")

    assert result["artifacts"] == [{"kind": "file", "filename": "b.xlsx"}]
    assert result["response"] == "ok"


@pytest.mark.asyncio
async def test_call_tool_passes_attachments_outside_caller_meta() -> None:
    service = MagicMock()
    service.call_expert = AsyncMock(return_value={"status": "completed"})
    attachments = [{"path": "inbound/chart.png", "media_type": "image/png"}]

    await handle_agent_mcp_request(
        service=service,
        user=_user(),
        body={
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "call_octop_expert",
                "arguments": {
                    "expert_id": "expert-1",
                    "attachments": attachments,
                    "itr_user_id": 1997,
                },
            },
        },
    )

    kwargs = service.call_expert.await_args.kwargs
    assert kwargs["attachments"] == attachments
    assert kwargs["caller_meta"] == {"itr_user_id": 1997}


# ─── 出站产物投递：上传后改写地址，任何失败都降级 ─────────────────────────────

_DELIVERED_URL = "http://itr.example/admin-api/infra/file/12/get/report.pdf"
_DOWNLOAD_URL = "/api/agents/expert-1/workspace/download?path=generated%2Freport.pdf"
_FRAME: dict[str, Any] = {
    "kind": "file",
    "url": _DOWNLOAD_URL,
    "preview_url": _DOWNLOAD_URL,
    "filename": "report.pdf",
    "mime_type": "application/pdf",
    "path": "generated/report.pdf",
}


class _Response:
    def __init__(self, status_code: int = 200, payload: Any = None, *, text: str = "") -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Client:
    """Minimal stand-in for ``httpx.AsyncClient`` used as an async context manager."""

    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def post(self, url: str, **kwargs: Any) -> Any:
        self.calls.append({"url": url, **kwargs})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class _Workspace:
    def __init__(self, data: bytes | None) -> None:
        self.data = data
        self.paths: list[str] = []

    async def adownload_bytes(self, path: str) -> bytes | None:
        self.paths.append(path)
        return self.data


def _sink(**overrides: Any) -> ArtifactSink:
    kwargs: dict[str, Any] = {
        "url": "http://itr.example/admin-api/infra/file/upload",
        "token": "sink-token",
        "timeout_seconds": 15,
        "max_bytes": 1024,
    }
    kwargs.update(overrides)
    return ArtifactSink(**kwargs)


def _patch_client(client: _Client) -> Any:
    return patch(
        "octop.infra.agent_mcp.artifact_sink.httpx.AsyncClient",
        return_value=client,
    )


def test_defaults_for_file_never_persists_the_token() -> None:
    defaults = _defaults_for_file()

    assert "artifact_sink_token" not in defaults
    assert defaults["artifact_sink_url"] is None


def test_from_config_reads_sink_settings_and_upload_limit() -> None:
    config = OctopConfig(
        artifact_sink_url="http://itr.example/upload",
        artifact_sink_token="t",
        artifact_sink_timeout_seconds=7,
        max_upload_mb=2,
    )

    sink = ArtifactSink.from_config(config)

    assert sink.enabled
    assert sink._endpoint == "http://itr.example/upload"
    assert sink._token == "t"
    assert sink._timeout == 7.0
    assert sink._max_bytes == 2 * 1024 * 1024


def test_from_config_without_settings_is_disabled() -> None:
    assert not ArtifactSink.from_config(None).enabled
    assert not ArtifactSink.from_config(OctopConfig()).enabled


@pytest.mark.asyncio
async def test_disabled_sink_returns_frame_untouched() -> None:
    client = _Client(_Response(payload={"data": _DELIVERED_URL}))
    workspace = _Workspace(b"%PDF-1.4")

    with _patch_client(client):
        delivered = await ArtifactSink.from_config(None).deliver(
            dict(_FRAME), workspace=workspace, agent_id="expert-1"
        )

    assert delivered == _FRAME
    assert client.calls == []
    assert workspace.paths == []


@pytest.mark.asyncio
async def test_delivery_rewrites_url_and_keeps_octop_url() -> None:
    client = _Client(_Response(payload={"code": 0, "data": _DELIVERED_URL}))
    workspace = _Workspace(b"%PDF-1.4")

    with _patch_client(client):
        delivered = await _sink().deliver(dict(_FRAME), workspace=workspace, agent_id="expert-1")

    assert delivered["url"] == _DELIVERED_URL
    assert delivered["preview_url"] == _DELIVERED_URL
    assert delivered["octop_url"] == _DOWNLOAD_URL
    assert delivered["path"] == "generated/report.pdf"
    assert delivered["filename"] == "report.pdf"
    assert workspace.paths == ["generated/report.pdf"]
    call = client.calls[0]
    assert call["url"] == "http://itr.example/admin-api/infra/file/upload"
    assert call["headers"] == {"Authorization": "Bearer sink-token"}
    name, data, mime = call["files"]["file"]
    assert (name, data, mime) == ("report.pdf", b"%PDF-1.4", "application/pdf")


@pytest.mark.asyncio
async def test_http_failure_degrades_to_the_original_frame(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _Client(_Response(status_code=502))

    with _patch_client(client), caplog.at_level(logging.WARNING):
        delivered = await _sink().deliver(
            dict(_FRAME), workspace=_Workspace(b"%PDF"), agent_id="expert-1"
        )

    assert delivered == _FRAME
    assert "artifact sink delivery failed" in caplog.text
    assert "status=502" in caplog.text
    assert "sink-token" not in caplog.text


@pytest.mark.asyncio
async def test_connection_error_degrades_without_raising() -> None:
    client = _Client(httpx.ConnectError("boom"))

    with _patch_client(client):
        delivered = await _sink().deliver(
            dict(_FRAME), workspace=_Workspace(b"%PDF"), agent_id="expert-1"
        )

    assert delivered == _FRAME


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        _Response(payload=ValueError("not json")),
        _Response(payload={"code": 0, "data": "not-a-url"}),
        _Response(payload={"code": 500, "msg": "failed"}),
    ],
)
async def test_unusable_response_degrades(response: _Response) -> None:
    with _patch_client(_Client(response)):
        delivered = await _sink().deliver(
            dict(_FRAME), workspace=_Workspace(b"%PDF"), agent_id="expert-1"
        )

    assert delivered == _FRAME


@pytest.mark.asyncio
async def test_plain_text_response_is_accepted() -> None:
    response = _Response(payload=ValueError("not json"), text=_DELIVERED_URL)

    with _patch_client(_Client(response)):
        delivered = await _sink().deliver(
            dict(_FRAME), workspace=_Workspace(b"%PDF"), agent_id="expert-1"
        )

    assert delivered["url"] == _DELIVERED_URL


@pytest.mark.asyncio
async def test_oversize_file_is_skipped_without_a_request() -> None:
    client = _Client(_Response(payload={"data": _DELIVERED_URL}))
    workspace = _Workspace(b"x" * 8)

    with _patch_client(client):
        delivered = await _sink(max_bytes=4).deliver(
            dict(_FRAME), workspace=workspace, agent_id="expert-1"
        )

    assert delivered == _FRAME
    assert client.calls == []


@pytest.mark.asyncio
async def test_path_falls_back_to_the_download_url_query() -> None:
    frame = {k: v for k, v in _FRAME.items() if k != "path"}
    frame["url"] = frame["preview_url"] = (
        "/api/agents/expert-1/workspace/download?path=generated%2F%E5%9B%BE.pdf"
    )
    workspace = _Workspace(b"%PDF")

    with _patch_client(_Client(_Response(payload={"data": _DELIVERED_URL}))):
        await _sink().deliver(frame, workspace=workspace, agent_id="expert-1")

    assert workspace.paths == ["generated/图.pdf"]


@pytest.mark.asyncio
async def test_missing_workspace_or_bytes_keeps_the_frame() -> None:
    client = _Client(_Response(payload={"data": _DELIVERED_URL}))

    with _patch_client(client):
        without_workspace = await _sink().deliver(dict(_FRAME), workspace=None, agent_id="e")
        missing_bytes = await _sink().deliver(
            dict(_FRAME), workspace=_Workspace(None), agent_id="e"
        )

    assert without_workspace == _FRAME
    assert missing_bytes == _FRAME
    assert client.calls == []


@pytest.mark.asyncio
async def test_external_url_is_never_reuploaded() -> None:
    client = _Client(_Response(payload={"data": _DELIVERED_URL}))
    frame = {**_FRAME, "url": "https://cdn.example/already.pdf"}

    with _patch_client(client):
        delivered = await _sink().deliver(frame, workspace=_Workspace(b"%PDF"), agent_id="e")

    assert delivered == frame
    assert client.calls == []


@pytest.mark.asyncio
async def test_inline_base64_frame_is_uploaded_without_workspace() -> None:
    frame = {
        "kind": "image",
        "mime_type": "image/png",
        "data": base64.b64encode(b"PNGDATA").decode("ascii"),
    }

    with _patch_client(_Client(_Response(payload={"data": _DELIVERED_URL}))):
        delivered = await _sink().deliver(frame, workspace=None, agent_id="expert-1")

    assert delivered["url"] == _DELIVERED_URL
    assert "preview_url" not in delivered
    assert "octop_url" not in delivered
    assert delivered["data"] == frame["data"]


def _stream_service(sink: ArtifactSink, chunks: Any) -> ExpertStreamService:
    manager = SimpleNamespace()
    manager.get_agent = lambda _agent_id: SimpleNamespace(workspace=_Workspace(b"%PDF"))
    gateway = SimpleNamespace(processor=SimpleNamespace(iter_turn_chunks=chunks))
    return ExpertStreamService(agent_manager=manager, gateway=gateway, artifact_sink=sink)


@pytest.mark.asyncio
async def test_stream_emits_delivered_artifact_and_matching_done_summary() -> None:
    async def _chunks(_inbound: Any) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "token", "content": "已生成"}
        yield {"type": "attachment", **_FRAME}

    service = _stream_service(_sink(), _chunks)
    prepared = SimpleNamespace(
        expert_id="expert-1",
        thread_id="thr-1",
        session_key="session-1",
        inbound=object(),
    )

    with _patch_client(_Client(_Response(payload={"data": _DELIVERED_URL}))):
        frames = [frame async for frame in service.stream(prepared, user_id=7)]

    events = _events(frames)
    artifact = next(data for event, data in events if event == "artifact")
    done = next(data for event, data in events if event == "done")
    assert artifact["url"] == _DELIVERED_URL
    assert artifact["octop_url"] == _DOWNLOAD_URL
    assert done["artifacts"] == [artifact]


@pytest.mark.asyncio
async def test_collect_result_uses_the_same_delivery_path() -> None:
    async def _chunks() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "attachment", **_FRAME}

    service = _stream_service(_sink(), _chunks)

    with _patch_client(_Client(_Response(payload={"data": _DELIVERED_URL}))):
        result = await service._collect_result(_chunks(), expert_id="expert-1", thread_id="thr-1")

    assert result["artifacts"][0]["url"] == _DELIVERED_URL
    assert result["artifacts"][0]["octop_url"] == _DOWNLOAD_URL
