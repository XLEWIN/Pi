"""Media module exceptions — user-facing + internal.

Class names keep the ``IG*`` prefix (stable for imports across the
codebase) but messages are platform-neutral: the module now serves
YouTube, TikTok and Instagram alike.
"""

from __future__ import annotations


class IGError(Exception):
    """Base error; `user` is safe to show in Telegram cards."""

    def __init__(self, user: str, *, code: str = "error", retryable: bool = False):
        super().__init__(user)
        self.user = user
        self.code = code
        self.retryable = retryable


class IGDisabled(IGError):
    def __init__(self) -> None:
        super().__init__("Media downloader is disabled.", code="disabled")


class IGInvalidUrl(IGError):
    def __init__(self) -> None:
        super().__init__(
            "That does not look like a supported media link.",
            code="invalid_url",
        )


class IGPlaylist(IGError):
    def __init__(self) -> None:
        super().__init__(
            "Playlists are not supported — please send a direct video link.",
            code="playlist",
        )


class IGPrivateMedia(IGError):
    def __init__(self) -> None:
        super().__init__(
            "This media is private or requires login — cannot download.",
            code="private",
        )


class IGMediaGone(IGError):
    def __init__(self) -> None:
        super().__init__("Media not found — it may be deleted or expired.", code="gone")


class IGRateLimited(IGError):
    def __init__(self) -> None:
        super().__init__(
            "The source is rate-limiting us. Try again in a moment.",
            code="rate_limit",
            retryable=True,
        )


class IGTooLarge(IGError):
    def __init__(self, size: int | None = None) -> None:
        detail = f" ({size / (1024 * 1024):.0f} MB)" if size else ""
        super().__init__(
            f"File is too large for Telegram{detail} — pick a smaller "
            "quality in /mediasettings.",
            code="too_large",
        )
        self.size = size


class IGBusy(IGError):
    def __init__(self) -> None:
        super().__init__(
            "Already processing this link — one moment…",
            code="busy",
            retryable=True,
        )


class IGRateRejected(IGError):
    def __init__(self) -> None:
        super().__init__(
            "Too many download requests — wait a moment and try again.",
            code="rate_rejected",
            retryable=True,
        )


class IGResolveFailed(IGError):
    def __init__(self, detail: str = "Could not extract media.") -> None:
        super().__init__(detail, code="resolve", retryable=True)


class IGDownloadFailed(IGError):
    def __init__(self) -> None:
        super().__init__("Download failed. Try again shortly.", code="download", retryable=True)
