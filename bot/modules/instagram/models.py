"""Data models for resolved Instagram media."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class MediaKind(str, Enum):
    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"
    ANIMATION = "animation"  # GIF-like


class PostType(str, Enum):
    POST = "post"
    REEL = "reel"
    STORY = "story"
    PROFILE = "profile"
    HIGHLIGHT = "highlight"
    UNKNOWN = "unknown"


@dataclass
class MediaAsset:
    """One downloadable file extracted from a post (carousel item or single)."""

    url: str
    kind: MediaKind
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[float] = None
    filesize: Optional[int] = None
    ext: str = "mp4"
    codec: Optional[str] = None
    note: str = ""


@dataclass
class ResolvedPost:
    """Normalized result of a resolve pass."""

    canonical_url: str
    post_type: PostType
    media_id: str
    title: str = ""
    uploader: str = ""
    caption: str = ""
    webpage_url: str = ""
    assets: List[MediaAsset] = field(default_factory=list)
    is_sidecar: bool = False
    thumbnail: Optional[str] = None
    resolve_ms: int = 0
    resolver: str = "ytdlp"

    @property
    def cache_key(self) -> str:
        return f"{self.media_id}:{len(self.assets)}"


@dataclass
class UploadedRef:
    """A successfully sent Telegram media reference (for file_id cache)."""

    cache_key: str
    kind: MediaKind
    file_id: Optional[str] = None
    message_id: Optional[int] = None
