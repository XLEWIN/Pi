"""Instagram module exceptions — user-facing + internal."""

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
        super().__init__("Instagram downloader is disabled.", code="disabled")


class IGInvalidUrl(IGError):
    def __init__(self) -> None:
        super().__init__(
            "That does not look like a supported Instagram post/reel link.",
            code="invalid_url",
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
            "Instagram rate-limited us. Try again in a moment.",
            code="rate_limit",
            retryable=True,
        )


class IGTooLarge(IGError):
    def __init__(self, size: int | None = None) -> None:
        detail = f" ({size} bytes)" if size else ""
        super().__init__(
            f"File is too large for Telegram{detail}.",
            code="too_large",
        )


class IGBusy(IGError):
    def __init__(self) -> None:
        super().__init__(
            "Already processing this link — one moment…",
            code="busy",
            retryable=True,
        )


class IGResolveFailed(IGError):
    def __init__(self, detail: str = "Could not extract media.") -> None:
        super().__init__(detail, code="resolve", retryable=True)


class IGDownloadFailed(IGError):
    def __init__(self) -> None:
        super().__init__("Download failed. Try again shortly.", code="download", retryable=True)
