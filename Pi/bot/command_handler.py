"""Multi-prefix command support — / ! . # $ % & ? all run commands.

Why this exists
---------------
python-telegram-bot 21's ``CommandHandler`` matches a message only when
Telegram attached a ``BOT_COMMAND`` entity to it, and Telegram only
creates that entity for messages starting with ``/``.  Prefixes such as
``!help`` or ``.all`` therefore never reach PTB's handler — there is no
``prefixes=`` option in this PTB line either.

This module provides two drop-in replacements:

``CommandHandler``
    Same constructor, callback contract and ``context.args`` behavior as
    ``telegram.ext.CommandHandler``.  ``check_update`` parses the message
    text against ``COMMAND_PREFIXES`` instead of trusting the entity.

``COMMAND``
    Filter, drop-in for ``telegram.ext.filters.COMMAND``.  Use it (almost
    always as ``~COMMAND``) anywhere the bot excludes commands from text
    pipelines — tracking, stats, blocklist, filters, shield — so that
    ``!help`` is treated exactly like ``/help``.

Both are built on one parser (``parse_command``), so dispatch and the
filters can never disagree about what counts as a command.

Parsing mirrors PTB's entity behavior: the command is the leading run of
``[A-Za-z0-9_]`` after the prefix (optionally ``@BotName``), so
``/help!`` or ``!help-me`` trigger ``help`` just like PTB would, while
plain text, bare prefixes and mid-message punctuation do not.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from telegram import Message, Update
from telegram.ext import CommandHandler as _PTBCommandHandler
from telegram.ext import filters as filters_module

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
    is checked by :class:`CommandHandler`.
    """
    if not text or text[0] not in _PREFIX_SET:
        return None
    parts = text.split(None, 1)
    m = _LEADING_CMD_RE.match(parts[0][1:])  # [0][0] is the prefix char
    if not m:
        return None
    args = parts[1].split() if len(parts) > 1 else []
    return m.group(0), args


class MultiPrefixCommand(filters_module.MessageFilter):
    """Syntactic command filter: message *starts* with a prefixed command.

    Drop-in for ``telegram.ext.filters.COMMAND`` (multi-prefix aware).
    """

    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("COMMAND")

    def filter(self, message: Message) -> bool:
        return parse_command(message.text) is not None


#: Drop-in for ``telegram.ext.filters.COMMAND`` — combine with ``~`` to
#: exclude every prefix-command from text pipelines.
COMMAND = MultiPrefixCommand()


class CommandHandler(_PTBCommandHandler):
    """``telegram.ext.CommandHandler`` that understands COMMAND_PREFIXES.

    Constructor, ``context.args`` and filters behave exactly like PTB's;
    only ``check_update`` differs (text parsing instead of entities).
    """

    __slots__ = ()

    def check_update(self, update: object):
        if not isinstance(update, Update) or not update.effective_message:
            return None
        message = update.effective_message
        parsed = parse_command(message.text)
        if parsed is None:
            return None
        command, args = parsed

        # @BotName bookkeeping — mirror PTB: the suffix (if any) must
        # name this bot, otherwise another bot's @-command would fire us.
        parts = command.split("@")
        try:
            username = message.get_bot().username
        except (RuntimeError, AttributeError):
            username = None
        if username is None:
            # Bot identity unknown (never happens in dispatch): only
            # accept bare commands — an @target cannot be verified.
            if len(parts) > 1 or parts[0].lower() not in self.commands:
                return None
        else:
            parts.append(username)
            if not (
                parts[0].lower() in self.commands
                and parts[1].lower() == username.lower()
            ):
                return None

        if not self._check_correct_args(args):
            return None
        filter_result = self.filters.check_update(update)
        if filter_result:
            return args, filter_result
        return False


__all__ = [
    "COMMAND_PREFIXES",
    "PREFIXES",
    "COMMAND",
    "CommandHandler",
    "MultiPrefixCommand",
    "parse_command",
]
