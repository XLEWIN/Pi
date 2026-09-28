"""Shared aiogram-era fakes for the Pi test suite.

Importable as ``from aiofakes import ...`` (unittest discover puts this
directory on sys.path).  All fakes are duck-typed — no aiogram session,
no network.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

BOT_USERNAME = "PiModulerBot"


class FakeBot:
    """Records outbound calls; configurable lookups."""

    def __init__(self, username: str = BOT_USERNAME, user_id: int = 1) -> None:
        self.username = username
        self.id = user_id
        self.sent: list = []
        self.chat_members: dict = {}
        self.chats: dict = {}
        self.admins: dict = {}
        self.member_count: dict = {}
        self.me = SimpleNamespace(
            id=user_id, username=username, full_name="Pi", is_bot=True,
            can_read_all_group_messages=True,
        )

    async def get_me(self):
        return self.me

    async def send_message(self, chat_id, text, **kw):
        self.sent.append({"chat_id": chat_id, "text": text, **kw})
        return SimpleNamespace(message_id=len(self.sent), chat=SimpleNamespace(id=chat_id))

    async def send_photo(self, chat_id, photo, **kw):
        self.sent.append({"chat_id": chat_id, "photo": photo, **kw})
        return SimpleNamespace(message_id=len(self.sent))

    async def send_document(self, chat_id, document, **kw):
        self.sent.append({"chat_id": chat_id, "document": document, **kw})
        return SimpleNamespace(message_id=len(self.sent))

    async def send_animation(self, chat_id, animation, **kw):
        self.sent.append({"chat_id": chat_id, "animation": animation, **kw})
        return SimpleNamespace(message_id=len(self.sent))

    async def send_audio(self, chat_id, audio, **kw):
        self.sent.append({"chat_id": chat_id, "audio": audio, **kw})
        return SimpleNamespace(message_id=len(self.sent))

    async def send_voice(self, chat_id, voice, **kw):
        self.sent.append({"chat_id": chat_id, "voice": voice, **kw})
        return SimpleNamespace(message_id=len(self.sent))

    async def send_video(self, chat_id, video, **kw):
        self.sent.append({"chat_id": chat_id, "video": video, **kw})
        return SimpleNamespace(message_id=len(self.sent))

    async def send_video_note(self, chat_id, video_note, **kw):
        self.sent.append({"chat_id": chat_id, "video_note": video_note, **kw})
        return SimpleNamespace(message_id=len(self.sent))

    async def send_sticker(self, chat_id, sticker, **kw):
        self.sent.append({"chat_id": chat_id, "sticker": sticker, **kw})
        return SimpleNamespace(message_id=len(self.sent))

    async def get_chat_member(self, chat_id, user_id):
        return self.chat_members.get(
            (chat_id, user_id),
            SimpleNamespace(
                user=SimpleNamespace(id=user_id, is_bot=False, first_name="U"),
                status="member",
            ),
        )

    async def get_chat(self, chat_id):
        return self.chats.get(
            chat_id, SimpleNamespace(id=chat_id, type="supergroup", title="T")
        )

    async def get_chat_administrators(self, chat_id):
        return self.admins.get(chat_id, [])

    async def get_chat_member_count(self, chat_id):
        return self.member_count.get(chat_id, 1)

    async def delete_message(self, chat_id, message_id):
        self.sent.append({"delete": (chat_id, message_id)})

    async def get_file(self, file_id):
        return SimpleNamespace(file_id=file_id, file_path=f"path/{file_id}")

    async def download_file(self, file_path, destination=None, *a, **k):
        if destination:
            with open(destination, "wb") as f:
                f.write(b"")
        return b""


class FakeMessage:
    """Duck-typed aiogram Message — records sends/edits/deletes."""

    def __init__(self, text="hi", *, chat_id=-100777001, chat_type="supergroup",
                 user_id=42, message_id=1, username="tester", is_bot=False,
                 first_name="Tester", title="T", **kw):
        self.text = text
        self.message_id = message_id
        self.chat = SimpleNamespace(id=chat_id, type=chat_type, title=title)
        self.from_user = SimpleNamespace(
            id=user_id, is_bot=is_bot, first_name=first_name, username=username
        )
        self.caption = kw.pop("caption", None)
        self.reply_to_message = kw.pop("reply_to_message", None)
        for k, v in kw.items():
            setattr(self, k, v)
        self.calls: list = []

    # recording send shortcuts
    async def answer(self, text, **kw):
        self.calls.append(("answer", text, kw))
        return SimpleNamespace(message_id=self.message_id, chat=self.chat)

    async def reply(self, text, **kw):
        self.calls.append(("reply", text, kw))
        return SimpleNamespace(message_id=self.message_id, chat=self.chat)

    def _media(self, kind):
        async def _send(media, *a, **kw):
            self.calls.append((kind, media, kw))
            return SimpleNamespace(message_id=self.message_id, chat=self.chat)
        return _send

    reply_photo = answer_photo = property(lambda s: s._media("answer_photo"))
    reply_document = answer_document = property(lambda s: s._media("answer_document"))
    reply_animation = answer_animation = property(lambda s: s._media("answer_animation"))
    reply_video = answer_video = property(lambda s: s._media("answer_video"))
    reply_sticker = answer_sticker = property(lambda s: s._media("answer_sticker"))
    reply_voice = answer_voice = property(lambda s: s._media("answer_voice"))
    reply_audio = answer_audio = property(lambda s: s._media("answer_audio"))

    async def edit_text(self, text, **kw):
        self.calls.append(("edit_text", text, kw))
        return self

    async def edit_caption(self, caption, **kw):
        self.calls.append(("edit_caption", caption, kw))
        return self

    async def edit_reply_markup(self, reply_markup=None, **kw):
        self.calls.append(("edit_reply_markup", reply_markup, kw))
        return self

    async def delete(self, **kw):
        self.calls.append(("delete", None, kw))

    async def pin(self, **kw):
        self.calls.append(("pin", None, kw))

    async def unpin(self, **kw):
        self.calls.append(("unpin", None, kw))

    @property
    def sent_texts(self):
        return [t for (k, t, _) in self.calls if k in ("answer", "reply")]

    @property
    def last(self):
        return self.calls[-1] if self.calls else None


def make_message(*args, **kw) -> FakeMessage:
    return FakeMessage(*args, **kw)


def make_callback(data="x:1", *, message=None, user_id=42, chat_id=-100777001,
                  chat_type="supergroup", username="tester"):
    msg = message if message is not None else FakeMessage(
        text="trigger", chat_id=chat_id, chat_type=chat_type, user_id=user_id
    )
    cb = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, is_bot=False, first_name="T",
                                  username=username),
        message=msg,
        answers=[],
    )

    async def _answer(text=None, show_alert=False, **kw):
        cb.answers.append({"text": text, "show_alert": show_alert, **kw})

    cb.answer = _answer
    return cb


async def call(fn, event, *, bot=None, args=None, chat_data=None, bot_data=None,
               **extra):
    """Await ``fn(event, ...)`` passing only the deps its signature declares."""
    params = inspect.signature(fn).parameters
    first = next(iter(params), None)
    deps = {
        "bot": bot if bot is not None else FakeBot(),
        "args": args if args is not None else [],
        "chat_data": chat_data if chat_data is not None else {},
        "bot_data": bot_data if bot_data is not None else {},
        **extra,
    }
    kwargs = {k: v for k, v in deps.items() if k in params and k != first}
    return await fn(event, **kwargs)


def command_filters(flt):
    """All CommandFilter instances inside a (possibly composite) filter.

    Walks bare CommandFilters, aiogram logic composites (``_AndFilter`` /
    ``_OrFilter`` hold ``.targets`` of FilterObject; ``_InvertFilter`` holds
    ``.target``), and legacy ``.filters`` lists.  MagicFilter nodes are
    terminal (their auto-attribute ``__getattr__`` would recurse forever).
    """
    from bot.command_handler import CommandFilter
    if flt is None:
        return []
    if "magic_filter" in type(flt).__module__:
        return []
    if isinstance(flt, CommandFilter):
        return [flt]
    out = []
    targets = getattr(flt, "targets", None)
    if isinstance(targets, (list, tuple)):
        for t in targets:
            out.extend(command_filters(getattr(t, "callback", t)))
    single = getattr(flt, "target", None)
    if single is not None:
        out.extend(command_filters(getattr(single, "callback", single)))
    subs = getattr(flt, "filters", None)
    if isinstance(subs, (list, tuple)):
        for s in subs:
            out.extend(command_filters(s))
    return out
