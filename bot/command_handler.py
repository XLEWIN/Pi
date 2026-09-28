"""Multi-prefix command support — / ! . # $ % & ? all run commands.

Why this exists
---------------
aiogram's built-in ``Command`` filter matches the ``/`` prefix only (via
BOT_COMMAND entities).  This module keeps the Pi bot's original
multi-prefix contract from the python-telegram-bot days:

``parse_command``
    One parser used by both dispatch and the filters, so they can never
    disagree about what counts as a command.

``COMMAND``
    Syntactic filter (drop-in for ``filters.COMMAND``) — combine with
    ``~COMMAND`` to exclude every prefixed command from text pipelines.

``cmd("name", ...)`` / ``CommandFilter``
    Filter for a specific set of commands.  On match it injects
    ``args`` (list of whitespace-separated arguments) into the handler
    data — the aiogram replacement for ``context.args``.

Parsing mirrors PTB's entity behavior: the command is the leading run of
``[A-Za-z0-9_]`` after the prefix (optionally ``@BotName``), so
``/help!`` or ``!help-me`` trigger ``help``, while plain text, bare
prefixes and mid-message punctuation do not.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple, Union

from aiogram.filters import Filter

#: Prefixes that may start a command, in the order the user requested.
COMMAND_PREFIXES: Tuple[str, ...] = ("/", "!", ".", "#", "$", "%", "&", "?")

#: All prefixes as one string (``"".join(COMMAND_PREFIXES)``).
PREFIXES: str = "".join(COMMAND_PREFIXES)

_PREFIX_SET = frozenset(COMMAND_PREFIXES)

# Leading command run after the prefix: word chars, optional @BotName.
# Mirrors where Telegram ends the BOT_COMMAND entity.
_LEADING_CMD_RE = re.compile(r"[A-Za-z0-9_]+(?:@[A-Za-z0-9_]+)?")


def parse_command(text: Optional[str]) -> Optional[Tuple[str, List[str]]]:
    """Parse ``text`` as a prefixed command.

    Returns ``(command, args)`` — ``command`` excludes the prefix but may
    keep an optional ``@BotName`` suffix — or ``None`` when ``text`` is
    not a command.  Purely syntactic: whether the command is *registered*
    is checked by :class:`CommandFilter`.
    """
    if not text or text[0] not in _PREFIX_SET:
        return None
    parts = text.split(None, 1)
    m = _LEADING_CMD_RE.match(parts[0][1:])  # [0][0] is the prefix char
    if not m:
        return None
    args = parts[1].split() if len(parts) > 1 else []
    return m.group(0), args


class MultiPrefixCommand(Filter):
    """Syntactic command filter: message *starts* with a prefixed command.

    Drop-in for ``telegram.ext.filters.COMMAND`` (multi-prefix aware).
    """

    async def __call__(self, message, **kwargs) -> bool:  # noqa: ANN001
        return parse_command(getattr(message, "text", None)) is not None


#: Drop-in for PTB ``filters.COMMAND`` — use ``~COMMAND`` to exclude
#: every prefix-command from text pipelines.
COMMAND = MultiPrefixCommand()


class CommandFilter(Filter):
    """Match messages invoking one of ``commands``; injects ``args``."""

    def __init__(self, *names: Union[str, Iterable[str]]) -> None:
        flat: List[str] = []
        for n in names:
            if isinstance(n, str):
                flat.append(n)
            else:
                flat.extend(n)
        if not flat:
            raise ValueError("CommandFilter requires at least one command")
        self.commands = frozenset(c.lower() for c in flat)

    async def __call__(self, message, bot=None, **kwargs):  # noqa: ANN001
        parsed = parse_command(getattr(message, "text", None))
        if parsed is None:
            return False
        command, args = parsed
        parts = command.split("@")
        base = parts[0].lower()
        if base not in self.commands:
            return False
        if len(parts) > 1:
            # @-suffix must name this bot (mirror PTB CommandHandler).
            username = getattr(bot, "username", None)
            if username is None or parts[1].lower() != username.lower():
                return False
        return {"args": args}

    def __repr__(self) -> str:
        return f"<CommandFilter {sorted(self.commands)}>"


#: PTB-era alias kept so ``isinstance(f, PiCommandHandler)`` style tests
#: keep working against the filter object.
CommandHandler = CommandFilter


def cmd(*names: Union[str, Iterable[str]]) -> CommandFilter:
    """``cmd("x")`` or ``cmd("a", "b")`` or ``cmd(["a", "b"])``."""
    return CommandFilter(*names)


__all__ = [
    "COMMAND_PREFIXES",
    "PREFIXES",
    "COMMAND",
    "CommandFilter",
    "CommandHandler",
    "MultiPrefixCommand",
    "cmd",
    "parse_command",
]
