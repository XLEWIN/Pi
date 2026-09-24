"""Tagging module exceptions — user-facing errors vs control flow."""

from __future__ import annotations


class TaggingError(Exception):
    """Base for errors that should be shown to the user as a card/line."""


class NotGroupError(TaggingError):
    """Command used outside a group/supergroup."""


class NotAdminError(TaggingError):
    """Caller is not a chat administrator."""


class NoReplyError(TaggingError):
    """/all was used without replying to a message."""


class AlreadyRunningError(TaggingError):
    """A tagging session already exists for this chat."""


class NoActiveSessionError(TaggingError):
    """/tagabort (or similar) found no running session."""


class NobodyToTagError(TaggingError):
    """Candidate set was empty after filters/exclusions."""


class AdminFetchError(TaggingError):
    """getChatAdministrators failed — cannot exclude admins safely."""


class FloodTooLongError(TaggingError):
    """FloodWait exceeded the acceptable pause — session ends."""

    def __init__(self, seconds: int):
        self.seconds = int(seconds)
        super().__init__(f"Flood wait of {self.seconds}s is too long")


class SessionCancelled(Exception):
    """Control flow: the session's cancel token was triggered.

    Not user-facing — sender.py converts it into the stop card and the
    exact MSG_STOPPED_FMT reply.
    """
