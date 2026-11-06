"""Inbound attachments for the Octop Agent MCP gateway.

MCP 通道原本只传纯文本，图片即使已上传到工作区也不会进入视觉链路。
本模块只做「校验 + 映射」：把调用方声明的附件转成 ``ImageContent`` /
``FileContent`` 内容块，交由下游既有的
:func:`octop.infra.gateway.media.attachment_hints.materialize_image_part`
在模型调用前读取字节并内联 —— 与企微 / dashboard 通道共用同一条流水线。

这里不读字节、不做 IO，也不做视觉数量截断（视觉配额由下游
``VISION_MAX_COUNT`` 统一控制）。
"""

from __future__ import annotations

from typing import Any

from octop_gateway.models import ContentPart, FileContent, ImageContent

from octop.infra.gateway.media.attachment_hints import is_vision_attachment

# 单条消息附件总数上限：防止一次调用塞入过多文件（视觉配额另由下游控制）。
MAX_MCP_ATTACHMENTS = 8
# 与 MCP 入口的文本上限一致；文本长度统一由本模块校验。
MAX_MESSAGE_CHARS = 100_000

# turn 参数不是调用方身份，不得进入 caller_meta；attachments 单独解码成内容块。
TURN_RESERVED_ARGS = frozenset({"expert_id", "message", "thread_id", "attachments"})

DEFAULT_MEDIA_TYPE = "application/octet-stream"
_INBOUND_PREFIX = "inbound/"
_MEDIA_TYPE_KEYS = ("media_type", "mime_type", "mimeType")


def _as_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def resolve_media_type(item: dict[str, Any]) -> str:
    """MIME 容错：media_type / mime_type / mimeType 任一存在即可。"""
    for key in _MEDIA_TYPE_KEYS:
        media_type = _as_text(item.get(key))
        if media_type:
            return media_type
    return DEFAULT_MEDIA_TYPE


def is_safe_inbound_path(path: str) -> bool:
    """只允许工作区 ``inbound/`` 下的相对路径。

    不复用 ``inbound_rel_path``：后者只做前缀补全，``inbound/../x`` 会原样通过。
    """
    if not path.startswith(_INBOUND_PREFIX):
        return False
    if ".." in path or "\\" in path or "://" in path:
        return False
    # Windows 盘符（C:/...）即使带了 inbound/ 前缀也不接受
    if len(path) >= 2 and path[1] == ":":
        return False
    return bool(path[len(_INBOUND_PREFIX) :].strip("/"))


def validate_attachments(raw: object) -> list[dict[str, Any]]:
    """校验并归一化入站附件；格式或路径非法时抛 ``ValueError``（开流前 4xx）。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("attachments must be an array")
    if len(raw) > MAX_MCP_ATTACHMENTS:
        raise ValueError(f"attachments must contain at most {MAX_MCP_ATTACHMENTS} items")

    normalized: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each attachment must be an object")
        path = _as_text(item.get("path")) or _as_text(item.get("workspace_path"))
        if not path:
            raise ValueError("attachment path is required")
        if not is_safe_inbound_path(path):
            raise ValueError(
                f"attachment path must be a workspace-relative path under {_INBOUND_PREFIX!r}: {path!r}"
            )
        filename = _as_text(item.get("filename")) or path.rsplit("/", 1)[-1]
        normalized.append(
            {"path": path, "filename": filename, "media_type": resolve_media_type(item)}
        )
    return normalized


def validate_turn(message: object, attachments: list[dict[str, Any]]) -> str:
    """返回去空白后的文本；文本与附件同时为空、或文本超长时抛 ``ValueError``。

    放宽的是「有附件可以没有文字」，空文案占位由下游
    ``harness_request.attachment_empty_image()`` 负责，这里不生成任何文案。
    """
    if message is not None and not isinstance(message, str):
        raise ValueError("message must be a string")
    text = str(message or "").strip()
    if not text and not attachments:
        raise ValueError("message is required")
    if len(text) > MAX_MESSAGE_CHARS:
        raise ValueError(f"message exceeds {MAX_MESSAGE_CHARS} characters")
    return text


def build_content_parts(attachments: list[dict[str, Any]]) -> list[ContentPart]:
    """把已校验的附件映射为内容块；不读字节、不做数量截断。"""
    parts: list[ContentPart] = []
    for item in attachments:
        path = str(item["path"])
        media_type = str(item["media_type"])
        filename = str(item["filename"])
        if is_vision_attachment(media_type=media_type, path=path):
            parts.append(
                ImageContent(
                    local_path=path,
                    mime_type=media_type if media_type.startswith("image/") else "image/png",
                    alt_text=filename,
                )
            )
        else:
            parts.append(FileContent(local_path=path, filename=filename, mime_type=media_type))
    return parts
