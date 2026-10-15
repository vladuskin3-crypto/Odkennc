"""
Telegram-рассылка: выбор групп -> текст -> интервал (кд) -> старт.

pip install "aiogram>=3.7"
set BOT_TOKEN=123:ABC
set ADMIN_IDS=11111111,22222222
python broadcast_bot.py

Группы добавляются автоматически, когда бота добавляют в чат/канал
(в канале бот должен быть админом). Если бот уже сидит в группе —
напиши в ней /reg (от админа бота).
"""
import asyncio
import json
import logging
import os
import re
import uuid
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ---------------- config ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")  # FILL: токен от @BotFather
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}  # FILL: твой tg id (@userinfobot)
kDataFile = Path("data.json")
kSendDelay = 0.6  # пауза между чатами внутри одного круга, сек (лимиты telegram)
kIntervals = [  # (подпись, секунды); 0 = один раз
    ("Один раз", 0),
    ("5 мин", 300),
    ("15 мин", 900),
    ("30 мин", 1800),
    ("1 час", 3600),
    ("3 часа", 10800),
    ("6 часов", 21600),
    ("12 часов", 43200),
    ("24 часа", 86400),
]
kMinInterval = 30  # минимальный кастомный интервал, сек

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("broadcast")


# ---------------- storage ----------------
class Store:
    def __init__(self, path: Path):
        self.path = path
        self.groups: dict[str, str] = {}
        self.jobs: dict[str, dict] = {}
        if path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            self.groups = d.get("groups", {})
            self.jobs = d.get("jobs", {})

    def save(self):
        self.path.write_text(
            json.dumps({"groups": self.groups, "jobs": self.jobs}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


store = Store(kDataFile)
tasks: dict[str, asyncio.Task] = {}


# ---------------- helpers ----------------
def fmt_interval(sec: int) -> str:
    if sec == 0:
        return "один раз"
    if sec % 86400 == 0:
        return f"каждые {sec // 86400} д"
    if sec % 3600 == 0:
        return f"каждые {sec // 3600} ч"
    if sec % 60 == 0:
        return f"каждые {sec // 60} мин"
    return f"каждые {sec} с"


def parse_interval(s: str) -> int | None:
    m = re.fullmatch(r"\s*(\d+)\s*([smhdсмчд]?)\s*", s.lower())
    if not m:
        return None
    n, u = int(m.group(1)), m.group(2) or "m"
    mult = {"s": 1, "с": 1, "m": 60, "м": 60, "h": 3600, "ч": 3600, "d": 86400, "д": 86400}[u]
    return n * mult


def short(text: str, n: int = 40) -> str:
    t = re.sub(r"<[^>]+>", "", text).replace("\n", " ")
    return t if len(t) <= n else t[: n - 1] + "…"


# ---------------- job runner ----------------
async def send_round(bot: Bot, job: dict):
    for cid in list(job["chats"]):
        try:
            await bot.send_message(int(cid), job["text"], parse_mode=ParseMode.HTML)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.send_message(int(cid), job["text"], parse_mode=ParseMode.HTML)
            except Exception as e2:
                log.warning("retry fail %s: %s", cid, e2)
        except TelegramForbiddenError:
            log.warning("kicked from %s, removing", cid)
            store.groups.pop(str(cid), None)
            job["chats"].remove(cid)
            store.save()
        except TelegramBadRequest as e:
            log.warning("bad request %s: %s", cid, e)
        except Exception as e:
            log.warning("send fail %s: %s", cid, e)
        await asyncio.sleep(kSendDelay)


async def run_job(bot: Bot, jid: str):
    try:
        while jid in store.jobs:
            job = store.jobs[jid]
            await send_round(bot, job)
            if job["interval"] == 0 or not job["chats"]:
                break
            await asyncio.sleep(job["interval"])
    except asyncio.CancelledError:
        pass
    finally:
        store.jobs.pop(jid, None)
        tasks.pop(jid, None)
        store.save()


def start_job(bot: Bot, jid: str):
    tasks[jid] = asyncio.create_task(run_job(bot, jid))


def stop_job(jid: str):
    t = tasks.get(jid)
    if t:
        t.cancel()
    store.jobs.pop(jid, None)
    store.save()


# ---------------- FSM ----------------
class Form(StatesGroup):
    groups = State()
    text = State()
    interval = State()
    custom = State()
    confirm = State()


router = Router()
router.message.filter(F.chat.type == "private", F.from_user.id.in_(ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

groups_router = Router()  # без админ-фильтра: события чатов


def main_kb():
    b = InlineKeyboardBuilder()
    b.button(text="➕ Новая рассылка", callback_data="new")
    b.button(text="📋 Активные рассылки", callback_data="jobs")
    b.adjust(1)
    return b.as_markup()


def groups_kb(selected: list[str]):
    b = InlineKeyboardBuilder()
    for cid, title in store.groups.items():
        mark = "✅" if cid in selected else "▫️"
        b.button(text=f"{mark} {title}", callback_data=f"g:{cid}")
    b.adjust(1)
    b2 = InlineKeyboardBuilder()
    b2.button(text="Выбрать все", callback_data="g_all")
    b2.button(text="Сбросить", callback_data="g_none")
    b2.button(text="Далее ➡️", callback_data="g_done")
    b2.button(text="✖️ Отмена", callback_data="cancel")
    b2.adjust(2, 1, 1)
    b.attach(b2)
    return b.as_markup()


def interval_kb():
    b = InlineKeyboardBuilder()
    for label, sec in kIntervals:
        b.button(text=label, callback_data=f"i:{sec}")
    b.adjust(3)
    b2 = InlineKeyboardBuilder()
    b2.button(text="✏️ Свой интервал", callback_data="i_custom")
    b2.button(text="✖️ Отмена", callback_data="cancel")
    b2.adjust(1)
    b.attach(b2)
    return b.as_markup()


def confirm_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🚀 Запустить", callback_data="go")
    b.button(text="✖️ Отмена", callback_data="cancel")
    b.adjust(2)
    return b.as_markup()


@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("меню", reply_markup=main_kb())


@router.callback_query(F.data == "cancel")
async def cb_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("отменено", reply_markup=main_kb())
    await c.answer()


@router.callback_query(F.data == "new")
async def cb_new(c: CallbackQuery, state: FSMContext):
    if not store.groups:
        await c.message.edit_text(
            "групп нет. добавь бота в группу/канал (в канале — админом) "
            "или напиши в уже существующей группе /reg",
            reply_markup=main_kb(),
        )
        return await c.answer()
    await state.set_state(Form.groups)
    await state.update_data(selected=[])
    await c.message.edit_text("выбери группы:", reply_markup=groups_kb([]))
    await c.answer()


@router.callback_query(Form.groups, F.data.startswith("g:"))
async def cb_toggle(c: CallbackQuery, state: FSMContext):
    cid = c.data[2:]
    sel = (await state.get_data()).get("selected", [])
    sel.remove(cid) if cid in sel else sel.append(cid)
    await state.update_data(selected=sel)
    await c.message.edit_reply_markup(reply_markup=groups_kb(sel))
    await c.answer()


@router.callback_query(Form.groups, F.data.in_({"g_all", "g_none"}))
async def cb_all(c: CallbackQuery, state: FSMContext):
    sel = list(store.groups) if c.data == "g_all" else []
    await state.update_data(selected=sel)
    await c.message.edit_reply_markup(reply_markup=groups_kb(sel))
    await c.answer()


@router.callback_query(Form.groups, F.data == "g_done")
async def cb_groups_done(c: CallbackQuery, state: FSMContext):
    sel = (await state.get_data()).get("selected", [])
    if not sel:
        return await c.answer("выбери хотя бы одну", show_alert=True)
    await state.set_state(Form.text)
    await c.message.edit_text("пришли текст рассылки (форматирование telegram сохранится):")
    await c.answer()


@router.message(Form.text, F.text)
async def msg_text(m: Message, state: FSMContext):
    await state.update_data(text=m.html_text)
    await state.set_state(Form.interval)
    await m.answer("как часто слать?", reply_markup=interval_kb())


@router.callback_query(Form.interval, F.data.startswith("i:"))
async def cb_interval(c: CallbackQuery, state: FSMContext):
    await state.update_data(interval=int(c.data[2:]))
    await show_confirm(c.message, state, edit=True)
    await c.answer()


@router.callback_query(Form.interval, F.data == "i_custom")
async def cb_custom(c: CallbackQuery, state: FSMContext):
    await state.set_state(Form.custom)
    await c.message.edit_text(
        f"напиши интервал: 90s, 45m, 2h, 1d (число без буквы = минуты). минимум {kMinInterval} с"
    )
    await c.answer()


@router.message(Form.custom, F.text)
async def msg_custom(m: Message, state: FSMContext):
    sec = parse_interval(m.text)
    if sec is None or sec < kMinInterval:
        return await m.answer(f"не понял или слишком мало (мин {kMinInterval} с). ещё раз:")
    await state.update_data(interval=sec)
    await show_confirm(m, state, edit=False)


async def show_confirm(msg: Message, state: FSMContext, edit: bool):
    d = await state.get_data()
    await state.set_state(Form.confirm)
    names = ", ".join(store.groups.get(c, c) for c in d["selected"])
    txt = (
        f"<b>группы:</b> {names}\n"
        f"<b>интервал:</b> {fmt_interval(d['interval'])}\n"
        f"<b>текст:</b>\n{d['text']}"
    )
    if edit:
        await msg.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=confirm_kb())
    else:
        await msg.answer(txt, parse_mode=ParseMode.HTML, reply_markup=confirm_kb())


@router.callback_query(Form.confirm, F.data == "go")
async def cb_go(c: CallbackQuery, state: FSMContext, bot: Bot):
    d = await state.get_data()
    jid = uuid.uuid4().hex[:8]
    store.jobs[jid] = {
        "chats": d["selected"],
        "text": d["text"],
        "interval": d["interval"],
    }
    store.save()
    start_job(bot, jid)
    await state.clear()
    await c.message.edit_text(f"запущено ({fmt_interval(d['interval'])})", reply_markup=main_kb())
    await c.answer()


@router.callback_query(F.data == "jobs")
async def cb_jobs(c: CallbackQuery):
    if not store.jobs:
        await c.message.edit_text("активных рассылок нет", reply_markup=main_kb())
        return await c.answer()
    b = InlineKeyboardBuilder()
    lines = []
    for jid, j in store.jobs.items():
        lines.append(f"• {fmt_interval(j['interval'])} → {len(j['chats'])} чат(ов): {short(j['text'])}")
        b.button(text=f"⏹ Стоп: {short(j['text'], 25)}", callback_data=f"stop:{jid}")
    b.button(text="⬅️ Назад", callback_data="back")
    b.adjust(1)
    await c.message.edit_text("\n".join(lines), reply_markup=b.as_markup())
    await c.answer()


@router.callback_query(F.data.startswith("stop:"))
async def cb_stop(c: CallbackQuery):
    stop_job(c.data[5:])
    await c.answer("остановлено")
    await cb_jobs(c)


@router.callback_query(F.data == "back")
async def cb_back(c: CallbackQuery):
    await c.message.edit_text("меню", reply_markup=main_kb())
    await c.answer()


# ---------------- регистрация групп ----------------
@groups_router.my_chat_member()
async def on_my_chat_member(e: ChatMemberUpdated):
    if e.chat.type not in ("group", "supergroup", "channel"):
        return
    cid = str(e.chat.id)
    st = e.new_chat_member.status
    if st in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        store.groups[cid] = e.chat.title or cid
    elif st in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        store.groups.pop(cid, None)
    store.save()


@groups_router.message(Command("reg"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_reg(m: Message):
    if m.from_user and m.from_user.id in ADMIN_IDS:
        store.groups[str(m.chat.id)] = m.chat.title or str(m.chat.id)
        store.save()
        await m.reply("ок, группа в списке")


# ---------------- main ----------------
async def main():
    if not BOT_TOKEN or not ADMIN_IDS:
        raise SystemExit("задай BOT_TOKEN и ADMIN_IDS")
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    dp.include_router(groups_router)
    for jid in list(store.jobs):
        start_job(bot, jid)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "my_chat_member"])


if __name__ == "__main__":
    asyncio.run(main())
