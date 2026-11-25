"""Classify logged files so the Files screen knows how to preview them."""

from __future__ import annotations

from pathlib import PurePosixPath

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac"}
TEXT_EXTENSIONS = {".json", ".txt"}
TEXT_MIMETYPES = {"application/json"}  # plus anything text/*

MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_AUDIO_BYTES = 200 * 1024 * 1024
MAX_TEXT_BYTES = 5 * 1024 * 1024


def file_kind(name: str, mimetype: str = "") -> str:
    """'image', 'audio', 'text' (json, txt and other text/*) or 'other'."""
    if mimetype.startswith("image/"):
        return "image"
    if mimetype.startswith("audio/"):
        return "audio"
    ext = PurePosixPath(name).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in TEXT_EXTENSIONS or mimetype.startswith("text/") or mimetype in TEXT_MIMETYPES:
        return "text"
    return "other"
