"""Graceful Rich-Message sending shared by /start, /blacklist and friends.

Rich Messages are a strict upgrade, never a requirement: if the API
server rejects the block, refuses the 10.3 ``style`` enum on the buttons,
or the builder itself blows up, the caller's HTML path must run exactly
as it did before.  ``bot/rich.py`` deliberately raises on every failure —
this module is where those failures are caught and turned into "use the
old message".

``bot/modules/help.py`` keeps its own, more elaborate copy of this ladder
(nav keyboards, group-DM variants, three distinct HTML fallbacks); it was
already shipped and tested against that shape, so it is not folded in
here.  New rich surfaces should use these two helpers instead of growing
a third copy.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from bot import rich as R
from bot.logger import logger

#: Process-wide kill switch.  Flip to ``False`` to force the HTML path
#: without touching every call site (handy if a Bot API rollout goes
#: wrong and rich needs to be off for a while).
RICH_ENABLED = True

#: A zero-argument coroutine returning whatever the HTML path returns.
Fallback = Callable[[], Awaitable[Any]]


def build_blocks(
    fn: Callable[..., Sequence[Dict[str, Any]]],
    *args: Any,
) -> Optional[List[Dict[str, Any]]]:
    """Run a block builder, returning ``None`` instead of raising.

    ``None`` means "the HTML path is the only path": either rich is
    switched off, the builder raised, or it produced something that
    fails :func:`bot.rich.validate` locally.
    """
    if not RICH_ENABLED:
        return None
    try:
        blocks = list(fn(*args))
        R.validate(blocks)
        return blocks
    except Exception as e:
        logger.warning(
            "rich: %s failed to build blocks — falling back to HTML: %s",
            getattr(fn, "__name__", fn), e,
        )
        return None


def _attempts(blocks: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Styled blocks first, then the same message without button styles.

    Button styling is a 10.3 addition.  A server that accepts the rich
    body but rejects the style enum is worth one retry rather than an
    immediate trip back to HTML — the colours are cosmetic, the layout
    is not.
    """
    first = list(blocks)
    if R.has_styled_buttons(first):
        return [first, R.unstyle(first)]
    return [first]


async def send_with_fallback(
    bot,
    chat_id: Any,
    blocks: Optional[Sequence[Dict[str, Any]]],
    *,
    rich_markup=None,
    fallback: Fallback,
) -> Any:
    """Send *blocks* rich (styled → unstyled → HTML).

    Returns the result of whichever path won: the sent :class:`Message`
    from rich, or whatever *fallback* returns.  Never raises for a rich
    failure — that is the whole point.
    """
    if blocks:
        for attempt in _attempts(blocks):
            try:
                return await R.send_rich(
                    bot, chat_id, attempt, reply_markup=rich_markup
                )
            except Exception as e:
                logger.debug("rich send rejected (%s) — next attempt", e)
    return await fallback()


async def edit_with_fallback(
    bot,
    chat_id: Any,
    message_id: int,
    blocks: Optional[Sequence[Dict[str, Any]]],
    *,
    rich_markup=None,
    fallback: Fallback,
) -> Any:
    """Rewrite an existing message as rich, else hand it to *fallback*.

    Returns ``True`` when the rich path landed, so callers that also
    need to answer a callback query only on the HTML route can tell the
    two apart.
    """
    if blocks:
        for attempt in _attempts(blocks):
            try:
                await R.edit_rich(
                    bot, chat_id, message_id, attempt,
                    reply_markup=rich_markup,
                )
                return True
            except Exception as e:
                logger.debug("rich edit rejected (%s) — next attempt", e)
    return await fallback()
