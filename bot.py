"""Telegram-бот «Крестики-нолики» на aiogram 3.

Запуск:
    # Windows PowerShell
    $env:TELEGRAM_BOT_TOKEN = "123456:ABC-DEF..."
    python bot.py

Токен выдаёт @BotFather. Без токена бот печатает подсказку и выходит.
Игровая логика — в game.py, сохранность игроков и истории — в storage.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import sys
import time
from dataclasses import dataclass, field

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InaccessibleMessage,
    Message,
    User,
)

import game
import storage

log = logging.getLogger("ttt")
router = Router()

GAMES: dict[str, game.Game] = {}
ROOMS: dict[str, "Room"] = {}

ROOM_TTL = 3600            # сек — срок жизни кода приглашения
GAME_IDLE_TTL = 7200       # сек — простоя партии, после которого её закрывают
JOIN_WINDOW = 60           # окно для ограничения попыток /join
JOIN_MAX = 10              # максимум попыток ввода кода за окно
JOIN_ATTEMPTS: dict[int, list[float]] = {}
ROOM_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
PAGE_SIZE = 5              # партий на странице истории
NAME_LIMIT = 18
THINKING = "🤔 Компьютер думает…"

WELCOME = (
    "Привет, {name}! 👋\n"
    "Это «Крестики-нолики»: играй с компьютером или с другом.\n\n"
    "Выбирай режим 👇"
)
MENU_TEXT = "🎮 Главное меню\n\nВыбирай, во что поиграем 👇"
HELP_TEXT = (
    "ℹ️ Как играть\n\n"
    "🤖 Против компьютера — ИИ считает все варианты и не даёт\n"
    "   выиграть; среди равных ходов выбирает случайный, поэтому\n"
    "   партия чаще заканчивается вничью.\n"
    "👥 С другом — создаётся код приглашения, второй игрок\n"
    "   пишет /join КОД (в личке с ботом или прямо в группе).\n\n"
    "Ход — нажатие на свободную клетку (1–9). Счёт между вами\n"
    "сохраняется, а после партии кнопка «Ещё партия» начинает\n"
    "новую: первый ход достаётся сопернику.\n\n"
    "Команды:\n"
    "/menu — главное меню\n"
    "/join КОД — присоединиться к игре\n"
    "/help — эта справка"
)


# --------------------------------------------------------------------- #
#  Вспомогательное: имена, даты, клавиатуры, тексты экранов
# --------------------------------------------------------------------- #

@dataclass
class Room:
    """Приглашение на игру с другом, ждёт второго игрока."""

    code: str
    owner: game.Player
    chat_id: int
    message_id: int
    private: bool
    created: float = field(default_factory=time.time)


def button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def short(text: str | None, limit: int = NAME_LIMIT) -> str:
    text = (text or "?").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def display_name(user: User) -> str:
    return (user.full_name or user.first_name or "Игрок").strip()[:40]


def fmt_date(iso: str) -> str:
    """2026-10-06T23:07:52 → 06.10.2026 23:07."""
    try:
        date_part, time_part = iso.split("T")
        year, month, day = date_part.split("-")
        return f"{day}.{month}.{year} {time_part[:5]}"
    except (AttributeError, ValueError):
        return str(iso)


def fmt_stamp(iso: str) -> str:
    """2026-10-07T08:26:40 → 07.10 08:26."""
    try:
        date_part, time_part = iso.split("T")
        _, month, day = date_part.split("-")
        return f"{day}.{month} {time_part[:5]}"
    except (AttributeError, ValueError):
        return str(iso)


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("🤖 Против компьютера", "m:ai"),
         button("👥 С другом", "m:pvp")],
        [button("📊 Моя статистика", "m:stats"),
         button("🕘 История игр", "m:hist")],
        [button("ℹ️ Помощь", "m:help")],
    ])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("◀️ Назад", "m:menu")],
    ])


def board_text(g: game.Game, viewer: int | None = None,
               status: str | None = None) -> str:
    """Подпись над доской. `viewer` — для кого сообщение (личная подсказка)."""
    x, o = g.player("X"), g.player("O")
    icon = "🤖" if g.mode == "ai" else "👥"
    lines = [
        f"{icon} Партия {g.round_no} · крестики-нолики",
        f"{game.EMOJI['X']} {short(x.name)}   {game.EMOJI['O']} {short(o.name)}",
        f"Счёт {g.scores['X']}:{g.scores['O']} · ничьих {g.scores['draw']}",
        "",
    ]
    if status:
        lines.append(status)
    elif g.finished:
        if g.result == "draw":
            lines.append("🤝 Ничья!")
        else:
            winner = g.player(g.result)
            lines.append(f"🏆 Победа {game.EMOJI[g.result]} {short(winner.name)}!")
    else:
        current = g.current_player()
        if viewer is not None and viewer == current.id:
            lines.append("👉 Твой ход!")
        else:
            lines.append(f"Ходит {game.EMOJI[g.current]} {short(current.name)}")
    return "\n".join(lines)


def board_kb(g: game.Game) -> InlineKeyboardMarkup:
    """Сетка 3×3 из кнопок и служебная строка внизу."""
    rows = []
    for row in range(3):
        cells = []
        for col in range(3):
            index = row * 3 + col
            symbol = g.board[index]
            if symbol == " ":
                data = "noop" if g.finished else f"g:{g.id}:{index}"
                cells.append(button(str(index + 1), data))
            else:
                cells.append(button(game.EMOJI[symbol], "noop"))
        rows.append(cells)
    if g.finished:
        rows.append([button("🔁 Ещё партия", f"g:{g.id}:new"),
                     button("⏹ Меню", f"g:{g.id}:quit")])
    else:
        rows.append([button("🔄 Заново", f"g:{g.id}:new"),
                     button("⏹ Меню", f"g:{g.id}:quit")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def as_int(value) -> int:
    """Число из файла: пустое или испорченное значение не должно ронять экран."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def stats_text(user_id: int, name: str) -> str:
    stats = storage.stats_for(user_id)
    lines = ["📊 Моя статистика", "", f"Игрок: {name}"]
    created = storage.created_for(user_id)
    if created:
        lines.append(f"Аккаунт создан: {fmt_date(created)}")
    if not stats or not stats.get("games"):
        lines += ["", "Пока нет ни одной партии — самое время сыграть!"]
        return "\n".join(lines)
    games = as_int(stats.get("games"))
    wins = as_int(stats.get("wins"))
    if games <= 0:
        lines += ["", "Пока нет ни одной партии — самое время сыграть!"]
        return "\n".join(lines)
    lines += [
        "",
        f"Сыграно партий: {games}",
        f"🏆 Победы: {wins}   ❌ Поражения: {as_int(stats.get('losses'))}   "
        f"🤝 Ничьи: {as_int(stats.get('draws'))}",
        f"Винрейт: {wins / games * 100:.1f}%",
    ]
    return "\n".join(lines)


def history_row(entry: dict, user_id: int, name: str) -> list[str]:
    side = storage.side_of(entry, user_id, name)
    players = entry.get("players") or {}
    if entry.get("mode") == "ai":
        opponent = "ИИ"
    else:
        rival = "O" if side == "X" else "X"
        rival = players.get(rival) or {}
        opponent = str(rival.get("name") or "соперник")
    result = entry.get("result")
    if result == "draw":
        icon, verdict = "🤝", "НИЧЬЯ"
    elif side is not None and result == side:
        icon, verdict = "🏆", "ПОБЕДА"
    elif side is None:
        icon, verdict = "", "ИГРА"
    else:
        icon, verdict = "❌", "ПОРАЖЕНИЕ"
    stamp = fmt_stamp(str(entry.get("date", "")))
    moves = " ".join(entry.get("moves") or [])
    return [
        f"#{entry.get('id', 0)}  {stamp} · {icon} {verdict} vs {short(opponent, 14)}",
        f"     {moves}" if moves else "     —",
    ]


def history_screen(games: list[dict], user_id: int, name: str,
                   page: int) -> tuple[str, InlineKeyboardMarkup]:
    if not games:
        return "🕘 История игр\n\nПока пусто — сыграйте партию!", back_kb()
    pages = (len(games) + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, pages - 1))
    lines = [f"🕘 История игр · {len(games)}", ""]
    for entry in games[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        lines.extend(history_row(entry, user_id, name))
        lines.append("")
    text = "\n".join(lines).rstrip()
    rows = []
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(button("◀️", f"hist:{page - 1}"))
        nav.append(button(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(button("▶️", f"hist:{page + 1}"))
        rows.append(nav)
    rows.append([button("◀️ Назад", "m:menu")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def room_text(code: str) -> str:
    return (
        "👥 Игра с другом\n\n"
        f"Код приглашения: {code}\n\n"
        f"Второй игрок присоединяется командой:\n"
        f"    /join {code}\n\n"
        "💡 Друг может написать это в личке с ботом или прямо в группе — "
        "тогда доску увидят все.\n"
        "Код живёт 1 час."
    )


def finish_toast(g: game.Game, user_id: int) -> str | None:
    """Короткое сообщение-подсказка после завершающего хода."""
    if not g.finished:
        return None
    if g.result == "draw":
        return "🤝 Ничья"
    side = next((s for s, p in g.players.items() if p.id == user_id), None)
    if side == g.result:
        return "🏆 Победа!"
    return f"❌ Победа: {short(g.player(g.result).name)}"


# --------------------------------------------------------------------- #
#  Отрисовка: редактируем сообщения-доски
# --------------------------------------------------------------------- #

async def edit_or_send(message, text: str, kb: InlineKeyboardMarkup,
                       bot: Bot | None = None):
    """Заменить содержимое сообщения с кнопками; при неудаче прислать новое.

    ``message`` бывает недоступным (сообщение старше 48 часов) — тогда
    отправляем новое в тот же чат, иначе кнопки молча «умрут».
    """
    if isinstance(message, InaccessibleMessage):
        if bot is None:
            return None
        try:
            return await bot.send_message(message.chat.id, text, reply_markup=kb)
        except TelegramAPIError:
            return None
    if not isinstance(message, Message):
        return None
    try:
        return await message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return message
    except TelegramAPIError:
        pass
    try:
        return await message.answer(text, reply_markup=kb)
    except TelegramAPIError:
        return None


async def redraw(g: game.Game, bot: Bot, status: str | None = None) -> None:
    """Обновить все сообщения-доски партии; недоступные убрать из списка.

    Временные сбои (обрыв связи, флуд-контроль Telegram) не считаем
    «умершей» доской — иначе одна сетевая ошибка убивала бы партию.
    """
    dead = []
    for chat_id, message_id, viewer in list(g.views):
        try:
            await bot.edit_message_text(
                board_text(g, viewer, status),
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=board_kb(g),
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                dead.append((chat_id, message_id))
        except (TelegramRetryAfter, TelegramNetworkError):
            log.debug("Временный сбой Telegram при обновлении доски %s — "
                      "оставляем сообщение", message_id)
        except TelegramAPIError:
            dead.append((chat_id, message_id))
    for view in dead:
        try:
            g.views.remove(view)
        except ValueError:
            pass
    if not g.views:
        GAMES.pop(g.id, None)


async def close_game(g: game.Game, bot: Bot) -> None:
    """Закрыть партию и превратить её доски в главное меню."""
    for chat_id, message_id, _viewer in list(g.views):
        try:
            await bot.edit_message_text(
                MENU_TEXT, chat_id=chat_id, message_id=message_id,
                reply_markup=main_menu_kb(),
            )
        except TelegramAPIError:
            pass
    GAMES.pop(g.id, None)


async def finalize(g: game.Game, bot: Bot) -> None:
    """Записать результат партии и показать итог на доске."""
    if not g.recorded:
        g.recorded = True
        try:
            entry_id = storage.finish_game(
                g.mode, g.round_no, g.players, g.result, g.moves)
            log.info("Партия #%s сохранена: %s", entry_id, g.result)
        except OSError:
            log.exception("Не удалось сохранить историю партий")
    await redraw(g, bot)


async def continue_game(g: game.Game, bot: Bot) -> None:
    """Обновить доску; если ходит компьютер — доиграть его ход.

    Пока компьютер «думает», игрок может нажать «Заново» или «Меню».
    Снимок ``(id, round_no)`` до паузы позволяет после неё понять, что
    партия уже другая (или закрыта), и не сыграть чужой ход.
    """
    if not g.ai_turn:
        await redraw(g, bot)
        return
    token = (g.id, g.round_no)
    await redraw(g, bot, status=THINKING)
    await asyncio.sleep(0.6)
    if token != (g.id, g.round_no) or g.id not in GAMES:
        return                                   # партию перезапустили/закрыли
    await asyncio.to_thread(g.ai_move)
    if token != (g.id, g.round_no) or g.id not in GAMES:
        return
    if g.finished:
        await finalize(g, bot)
    else:
        await redraw(g, bot)


# --------------------------------------------------------------------- #
#  Команды
# --------------------------------------------------------------------- #

@router.message(CommandStart())
async def cmd_start(msg: Message):
    user = storage.touch(msg.from_user.id, display_name(msg.from_user))
    await msg.answer(WELCOME.format(name=user["name"]), reply_markup=main_menu_kb())


@router.message(Command("menu"))
async def cmd_menu(msg: Message):
    await msg.answer(MENU_TEXT, reply_markup=main_menu_kb())


@router.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(HELP_TEXT, reply_markup=main_menu_kb())


@router.message(Command("join"))
async def cmd_join(msg: Message, bot: Bot):
    user = storage.touch(msg.from_user.id, display_name(msg.from_user))
    tokens = (msg.text or "").split()
    if len(tokens) < 2:
        await msg.answer("Укажи код игры: /join КОД")
        return
    if not _join_allowed(msg.from_user.id):
        await msg.answer("Слишком много попыток — подожди минуту 🕐")
        return
    code = tokens[1].upper()                  # только код, без лишних слов
    room = ROOMS.get(code)
    if room is None:
        await msg.answer("Код не найден 😕\nСоздай новую игру: /menu → 👥 С другом")
        return
    if time.time() - room.created > ROOM_TTL:
        ROOMS.pop(code, None)
        await msg.answer("Этот код устарел — попроси друга создать новую игру.")
        return
    if room.owner.id == msg.from_user.id:
        await msg.answer("Ты уже в этой партии — ждём второго игрока 👀")
        return

    g = game.Game("pvp", {
        "X": room.owner,
        "O": game.Player(msg.from_user.id, user["name"]),
    })
    GAMES[g.id] = g
    ROOMS.pop(code, None)
    # Доска показывается и в чате создателя, и у присоединившегося игрока.
    g.views.append((room.chat_id, room.message_id,
                    room.owner.id if room.private else None))
    if msg.chat.id != room.chat_id:
        try:
            sent = await msg.answer(board_text(g, msg.from_user.id),
                                    reply_markup=board_kb(g))
            g.views.append((sent.chat.id, sent.message_id, msg.from_user.id))
        except TelegramAPIError:
            log.warning("Не удалось отправить доску в чат %s", msg.chat.id)
    await redraw(g, bot)
    log.info("Партия %s: %s vs %s", g.id, room.owner.name, user["name"])


@router.message(F.text)
async def any_text(msg: Message):
    """Любой обычный текст — подсказка (в группах молчим, чтобы не мешать)."""
    if msg.chat.type != "private":
        return
    await msg.answer("Я понимаю только кнопки и команды 🙂\n"
                     "/menu — главное меню, /help — как играть")


# --------------------------------------------------------------------- #
#  Кнопки меню
# --------------------------------------------------------------------- #

@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


@router.callback_query(F.data == "m:ai")
async def cb_ai(cb: CallbackQuery, bot: Bot):
    user = storage.touch(cb.from_user.id, display_name(cb.from_user))
    g = game.Game("ai", {
        "X": game.Player(user["id"], user["name"]),
        "O": game.Player(None, game.AI_NAME),
    })
    GAMES[g.id] = g
    shown = await edit_or_send(cb.message, board_text(g, cb.from_user.id),
                               board_kb(g), bot)
    if shown is None:
        GAMES.pop(g.id, None)
        return await cb.answer("Не удалось показать доску", show_alert=True)
    g.views.append((shown.chat.id, shown.message_id, cb.from_user.id))
    await cb.answer("Ходи! ✊")


@router.callback_query(F.data == "m:pvp")
async def cb_pvp(cb: CallbackQuery, bot: Bot):
    user = storage.touch(cb.from_user.id, display_name(cb.from_user))
    _purge_rooms()
    # Вторая игра в этом чате закрывает предыдущее приглашение.
    for code, room in list(ROOMS.items()):
        if room.owner.id == cb.from_user.id and (
                cb.message is not None
                and room.chat_id == cb.message.chat.id):
            ROOMS.pop(code, None)
    code = _new_code()
    shown = await edit_or_send(cb.message, room_text(code),
                               InlineKeyboardMarkup(inline_keyboard=[
                                   [button("⏹ Отмена", f"room:{code}:cancel")]]),
                               bot)
    if shown is None:
        return await cb.answer("Не удалось создать игру", show_alert=True)
    ROOMS[code] = Room(
        code=code,
        owner=game.Player(cb.from_user.id, user["name"]),
        chat_id=shown.chat.id,
        message_id=shown.message_id,
        private=shown.chat.type == "private",
    )
    log.info("Комната %s создана (%s)", code, user["name"])
    await cb.answer(f"Код: {code}")


@router.callback_query(F.data == "m:stats")
async def cb_stats(cb: CallbackQuery, bot: Bot):
    user = storage.touch(cb.from_user.id, display_name(cb.from_user))
    await edit_or_send(cb.message, stats_text(user["id"], user["name"]),
                       back_kb(), bot)
    await cb.answer()


@router.callback_query(F.data == "m:hist")
async def cb_history(cb: CallbackQuery, bot: Bot):
    await _show_history(cb, bot, 0)


@router.callback_query(F.data.startswith("hist:"))
async def cb_history_page(cb: CallbackQuery, bot: Bot):
    try:
        page = int(cb.data.split(":", 1)[1])
    except ValueError:
        page = 0
    await _show_history(cb, bot, page)


@router.callback_query(F.data == "m:help")
async def cb_help(cb: CallbackQuery, bot: Bot):
    await edit_or_send(cb.message, HELP_TEXT, back_kb(), bot)
    await cb.answer()


@router.callback_query(F.data == "m:menu")
async def cb_menu(cb: CallbackQuery, bot: Bot):
    await edit_or_send(cb.message, MENU_TEXT, main_menu_kb(), bot)
    await cb.answer()


@router.callback_query(F.data.startswith("room:"))
async def cb_room(cb: CallbackQuery, bot: Bot):
    parts = cb.data.split(":")
    code = parts[1] if len(parts) > 1 else ""
    room = ROOMS.get(code)
    if room is None:
        return await cb.answer("Это приглашение уже закрыто")
    if room.owner.id != cb.from_user.id:
        return await cb.answer("Отменить приглашение может только создатель")
    ROOMS.pop(code, None)
    await edit_or_send(cb.message, "⏹ Приглашение отменено.",
                       main_menu_kb(), bot)
    await cb.answer("Отменено")


# --------------------------------------------------------------------- #
#  Ходы в игре
# --------------------------------------------------------------------- #

@router.callback_query(F.data.startswith("g:"))
async def cb_game(cb: CallbackQuery, bot: Bot):
    parts = cb.data.split(":")
    if len(parts) != 3:
        return await cb.answer()
    g = GAMES.get(parts[1])
    if g is None:
        return await cb.answer("Эта партия уже закрыта — /menu", show_alert=True)
    action = parts[2]

    if not g.is_player(cb.from_user.id):
        return await cb.answer("В этой партии играют другие игроки")
    g.updated = time.time()      # активность — чтобы уборка не закрыла партию

    if action == "quit":
        await cb.answer("До встречи! 👋")
        await close_game(g, bot)
        return

    if action == "new":
        g.restart()
        await cb.answer("Новая партия! Первый ход меняется")
        await continue_game(g, bot)
        return

    # --- обычный ход: вся проверка и запись синхронны, без await,
    # --- чтобы два параллельных нажатия не выполнились подряд ---
    if not action.isdigit():
        return await cb.answer()
    if g.finished:
        return await cb.answer("Партия окончена — нажми «Ещё партия»")
    if g.ai_turn:
        return await cb.answer(THINKING)
    if g.current_player().id != cb.from_user.id:
        return await cb.answer(f"Сейчас ходит {short(g.current_player().name)}")
    error = g.move(int(action))
    if error:
        return await cb.answer(error)

    await cb.answer(finish_toast(g, cb.from_user.id))
    if g.finished:
        await finalize(g, bot)
    else:
        await continue_game(g, bot)


# --------------------------------------------------------------------- #
#  Служебное
# --------------------------------------------------------------------- #

async def _show_history(cb: CallbackQuery, bot: Bot, page: int):
    name = display_name(cb.from_user)
    games = storage.history_for(cb.from_user.id, name)
    text, kb = history_screen(games, cb.from_user.id, name, page)
    await edit_or_send(cb.message, text, kb, bot)
    await cb.answer()


def _purge_rooms() -> None:
    now = time.time()
    for code in [c for c, r in ROOMS.items() if now - r.created > ROOM_TTL]:
        ROOMS.pop(code, None)


def _new_code() -> str:
    while True:
        code = "".join(secrets.choice(ROOM_ALPHABET) for _ in range(6))
        if code not in ROOMS:
            return code


def _join_allowed(user_id: int) -> bool:
    """Ограничить перебор кодов приглашения: не чаще JOIN_MAX раз за минуту."""
    now = time.time()
    stamps = [t for t in JOIN_ATTEMPTS.get(user_id, []) if now - t < JOIN_WINDOW]
    if len(stamps) >= JOIN_MAX:
        JOIN_ATTEMPTS[user_id] = stamps
        return False
    stamps.append(now)
    JOIN_ATTEMPTS[user_id] = stamps
    return True


def _prune_join_limits() -> None:
    now = time.time()
    for user_id in list(JOIN_ATTEMPTS):
        fresh = [t for t in JOIN_ATTEMPTS[user_id] if now - t < JOIN_WINDOW]
        if fresh:
            JOIN_ATTEMPTS[user_id] = fresh
        else:
            JOIN_ATTEMPTS.pop(user_id, None)


async def _reaper(bot: Bot) -> None:
    """Фоновая уборка: протухшие комнаты и брошенные партии.

    Без неё GAMES/ROOMS росли бы бесконечно: бот не получает события
    о том, что игрок просто закрыл чат и никогда не вернётся.
    """
    while True:
        await asyncio.sleep(60)
        try:
            _purge_rooms()
            _prune_join_limits()
            now = time.time()
            for g in list(GAMES.values()):
                if now - g.updated > GAME_IDLE_TTL:
                    log.info("Партия %s закрыта по простояну (%ss)",
                             g.id, int(now - g.updated))
                    await close_game(g, bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Фоновая уборка: ошибка проигнорирована")


def read_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token and len(sys.argv) > 1:
        token = sys.argv[1].strip()
        print("Внимание: токен передан аргументом командной строки — он виден "
              "в истории shell и в списке процессов.\n"
              "Надёжнее: $env:TELEGRAM_BOT_TOKEN = \"<токен>\"", file=sys.stderr)
    return token


async def main() -> None:
    token = read_token()
    if not token:
        print(
            "Не задан токен бота.\n"
            "1. Создай бота у @BotFather и скопируй токен.\n"
            "2. Задай переменную окружения:\n"
            "     PowerShell:  $env:TELEGRAM_BOT_TOKEN = \"<токен>\"\n"
            "     cmd:         set TELEGRAM_BOT_TOKEN=<токен>\n"
            "3. Запусти:  python bot.py   (или python bot.py <токен>)",
            file=sys.stderr,
        )
        raise SystemExit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        storage.init()
    except OSError:
        log.exception("Не удалось открыть файл данных — проверь права доступа")
        raise SystemExit(1)

    bot = Bot(token=token)
    dp = Dispatcher()
    dp.include_router(router)
    reaper = asyncio.create_task(_reaper(bot))
    try:
        await bot.set_my_commands([
            BotCommand(command="menu", description="Главное меню"),
            BotCommand(command="join", description="Присоединиться: /join КОД"),
            BotCommand(command="help", description="Как играть"),
        ])
        log.info("Бот запускается, игр в памяти: %s", len(GAMES))
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except TelegramUnauthorizedError:
        log.error("Токен не принят сервером Telegram — проверь TELEGRAM_BOT_TOKEN")
        raise SystemExit(1)
    except TelegramNetworkError:
        log.error("Нет связи с api.telegram.org — проверь интернет или прокси")
        raise SystemExit(1)
    finally:
        reaper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reaper
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nПока!")
