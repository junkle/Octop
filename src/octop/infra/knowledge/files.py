"""Filesystem layout and safe document persistence for knowledge bases."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from octop.infra.utils.paths import PathLayout

_ASSET_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_ASSET_SUFFIXES = {
    b"\x89PNG": ".png",
    b"\xff\xd8": ".jpg",
    b"GIF8": ".gif",
    b"BM": ".bmp",
}


def knowledge_base_dir(kb_id: str) -> Path:
    return PathLayout.from_env().knowledge_dir / kb_id


def documents_dir(kb_id: str) -> Path:
    path = knowledge_base_dir(kb_id) / "docs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def document_path(kb_id: str, doc_id: str, filename: str) -> Path:
    suffix = Path(filename).suffix.lower()
    return documents_dir(kb_id) / f"{doc_id}{suffix}"


def write_document(kb_id: str, doc_id: str, filename: str, content: bytes) -> Path:
    path = document_path(kb_id, doc_id, filename)
    path.write_bytes(content)
    return path


def delete_document_file(kb_id: str, doc_id: str, filename: str) -> None:
    document_path(kb_id, doc_id, filename).unlink(missing_ok=True)


def document_assets_dir(kb_id: str, doc_id: str) -> Path:
    return knowledge_base_dir(kb_id) / "assets" / doc_id


def replace_document_assets(kb_id: str, doc_id: str, images: dict[str, bytes]) -> None:
    """Replace one document's extracted rasters. An empty mapping clears them."""
    directory = document_assets_dir(kb_id, doc_id)
    shutil.rmtree(directory, ignore_errors=True)
    if not images:
        return
    directory.mkdir(parents=True, exist_ok=True)
    for image_id, data in images.items():
        if not _ASSET_ID_RE.fullmatch(image_id):
            continue
        (directory / f"{image_id}{_asset_suffix(data)}").write_bytes(data)


def document_asset_file(kb_id: str, doc_id: str, image_id: str) -> Path | None:
    if not _ASSET_ID_RE.fullmatch(image_id):
        return None
    directory = document_assets_dir(kb_id, doc_id)
    if not directory.is_dir():
        return None
    matches = [path for path in directory.glob(f"{image_id}.*") if path.is_file()]
    if len(matches) != 1:
        return None
    return matches[0]


def delete_document_assets(kb_id: str, doc_id: str) -> None:
    shutil.rmtree(document_assets_dir(kb_id, doc_id), ignore_errors=True)


def delete_knowledge_base_files(kb_id: str) -> None:
    shutil.rmtree(knowledge_base_dir(kb_id), ignore_errors=True)


def _asset_suffix(data: bytes) -> str:
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    for magic, suffix in _ASSET_SUFFIXES.items():
        if data.startswith(magic):
            return suffix
    return ".bin"
