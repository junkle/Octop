"""Deliver produced artifacts to a downstream file service before they are emitted.

MCP 通道原本只把工作区相对下载地址放进产物帧，下游必须带登录态代理下载。
配置 sink 后，这里在推送帧之前把字节上传给下游平台，改用平台返回的公开地址
—— 与企微通道「读字节 → 上传平台 → 用平台凭证回复」同构。

未配置、读不到字节、超限、上传或解析失败一律原样返回帧：投递只是增强，
不允许影响本轮回答。
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import httpx

if TYPE_CHECKING:
    from octop_harness.backends.workspace import BackendWorkspace

    from octop.config import OctopConfig

_log = logging.getLogger(__name__)

# 只改写 Octop 自己的下载地址；已是 http(s) 绝对地址的帧说明上游投递过，保持原样。
_OCTOP_PREFIX = "/api/agents/"
_DEFAULT_MIME = "application/octet-stream"
_DEFAULT_FILENAME = "artifact"
# 平台响应形态各异：纯字符串、{"url": …}、{"data": "…"}、{"data": {"url": …}} 都能吃。
_URL_KEYS = ("url", "file_url", "fileUrl", "data", "result")


class ArtifactSink:
    """Upload artifact bytes to a downstream file service and return its URL."""

    def __init__(
        self,
        *,
        url: str | None,
        token: str | None = None,
        timeout_seconds: int = 15,
        max_bytes: int = 0,
    ) -> None:
        self._endpoint = (url or "").strip()
        self._token = (token or "").strip()
        self._timeout = float(timeout_seconds) if timeout_seconds > 0 else 15.0
        self._max_bytes = max(0, max_bytes)

    @classmethod
    def from_config(cls, config: OctopConfig | None) -> ArtifactSink:
        """Build a sink from config; a missing config simply disables delivery."""
        if config is None:
            return cls(url=None)
        return cls(
            url=config.artifact_sink_url,
            token=config.artifact_sink_token,
            timeout_seconds=config.artifact_sink_timeout_seconds,
            max_bytes=config.max_upload_bytes,
        )

    @property
    def enabled(self) -> bool:
        return bool(self._endpoint)

    async def deliver(
        self,
        frame: dict[str, Any],
        *,
        workspace: BackendWorkspace | None,
        agent_id: str = "",
    ) -> dict[str, Any]:
        """Return the frame with ``url``/``preview_url`` rewritten, or ``frame`` unchanged."""
        if not self.enabled:
            return frame
        source_url = frame.get("url")
        inline = _decode_inline(frame)
        if inline is None and not _is_octop_url(source_url):
            # 外部地址或既无 url 又无 data 的帧没有可投递的来源。
            return frame
        path = _workspace_path(frame, source_url)
        data = inline if inline is not None else await _read_workspace(workspace, path)
        if data is None:
            return _skip(agent_id, frame, path, "no-bytes")
        if self._max_bytes and len(data) > self._max_bytes:
            return _skip(agent_id, frame, path, f"oversize:{len(data)}")
        url = await self._upload(data, frame=frame, path=path, agent_id=agent_id)
        if not url:
            return frame
        delivered = dict(frame)
        if _is_octop_url(source_url):
            delivered["octop_url"] = str(source_url)
        delivered["url"] = url
        if "preview_url" in frame:
            delivered["preview_url"] = url
        return delivered

    async def _upload(
        self,
        data: bytes,
        *,
        frame: dict[str, Any],
        path: str,
        agent_id: str,
    ) -> str | None:
        mime = frame.get("mime_type")
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else None
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    self._endpoint,
                    headers=headers,
                    files={
                        "file": (
                            _upload_filename(frame, path),
                            data,
                            mime if isinstance(mime, str) and mime else _DEFAULT_MIME,
                        )
                    },
                )
        except Exception as exc:  # noqa: BLE001 - 投递失败不得中断本轮回答
            _warn(agent_id, frame, path, reason=exc.__class__.__name__)
            return None
        if not 200 <= response.status_code < 300:
            _warn(agent_id, frame, path, status=response.status_code)
            return None
        try:
            payload: Any = response.json()
        except ValueError:
            # 少数平台直接回纯文本地址。
            payload = response.text
        url = _extract_url(payload)
        if not url:
            _warn(agent_id, frame, path, reason="no-url")
        return url


def _skip(agent_id: str, frame: dict[str, Any], path: str, reason: str) -> dict[str, Any]:
    _log.debug(
        "artifact sink skipped: agent=%s kind=%s path=%s reason=%s",
        agent_id,
        frame.get("kind"),
        path,
        reason,
    )
    return frame


def _warn(
    agent_id: str,
    frame: dict[str, Any],
    path: str,
    *,
    status: int | None = None,
    reason: str | None = None,
) -> None:
    # 只记身份与状态：文件内容、token、响应体一律不出日志。
    _log.warning(
        "artifact sink delivery failed: agent=%s kind=%s path=%s status=%s reason=%s",
        agent_id,
        frame.get("kind"),
        path,
        status,
        reason,
    )


def _is_octop_url(url: object) -> bool:
    return isinstance(url, str) and url.startswith(_OCTOP_PREFIX)


def _workspace_path(frame: dict[str, Any], source_url: object) -> str:
    """取工作区相对路径：优先帧内 ``path``，否则从下载地址的 ``path`` 查询参数解出。"""
    path = frame.get("path")
    if isinstance(path, str) and path.strip():
        return path.strip()
    if not _is_octop_url(source_url):
        return ""
    values = parse_qs(urlparse(str(source_url)).query).get("path")
    return values[0].strip() if values else ""


def _decode_inline(frame: dict[str, Any]) -> bytes | None:
    raw = frame.get("data")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return base64.b64decode(raw)
    except ValueError:
        return None


async def _read_workspace(workspace: BackendWorkspace | None, path: str) -> bytes | None:
    if workspace is None or not path:
        return None
    try:
        return await workspace.adownload_bytes(path)
    except Exception:  # noqa: BLE001 - 读不到就降级回原地址
        _log.debug("artifact sink read failed: path=%s", path, exc_info=True)
        return None


def _upload_filename(frame: dict[str, Any], path: str) -> str:
    for candidate in (frame.get("filename"), path):
        if isinstance(candidate, str) and candidate.strip():
            name = candidate.strip().replace("\\", "/").rsplit("/", 1)[-1]
            if name:
                return name
    return _DEFAULT_FILENAME


def _extract_url(payload: Any) -> str:
    if isinstance(payload, str):
        candidate = payload.strip()
        return candidate if candidate.startswith(("http://", "https://")) else ""
    if isinstance(payload, dict):
        for key in _URL_KEYS:
            value = payload.get(key)
            if isinstance(value, (dict, str)):
                found = _extract_url(value)
                if found:
                    return found
    return ""
