"""Тесты бота: bot.py — гонки ходов, уборка, экраны (с заглушкой Telegram)."""

import asyncio
import os
import shutil
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import bot as botmod  # noqa: E402
import game  # noqa: E402
import storage  # noqa: E402


class FakeBot:
    """Заглушка aiogram-бота: записывает, что показали пользователю."""

    def __init__(self):
        self.edits = []

    async def edit_message_text(self, text, chat_id=None, message_id=None,
                                reply_markup=None):
        self.edits.append(text)

    async def send_message(self, chat_id, text, reply_markup=None):
        self.edits.append(text)
        return SimpleNamespace(
            chat=SimpleNamespace(id=chat_id, type="private"),
            message_id=999,
        )


def ai_game(user_id=1):
    g = game.Game("ai", {
        "X": game.Player(user_id, "Человек"),
        "O": game.Player(None, game.AI_NAME),
    })
    g.views.append((1, 1, user_id))
    botmod.GAMES[g.id] = g
    return g


class RaceTests(unittest.TestCase):
    """Регрессии для гонок вокруг паузы «компьютер думает…»."""

    def tearDown(self):
        botmod.GAMES.clear()

    def test_restart_while_ai_thinking_does_not_freeze_game(self):
        async def scenario():
            fake = FakeBot()
            g = ai_game()
            g.move(4)                                   # ход человека
            first = asyncio.create_task(botmod.continue_game(g, fake))
            await asyncio.sleep(0.1)                    # «Заново» в паузу
            g.restart()
            second = asyncio.create_task(botmod.continue_game(g, fake))
            await asyncio.gather(first, second)
            return g

        g = asyncio.run(scenario())
        self.assertFalse(g.ai_turn, "игра зависла в очередь ИИ")
        self.assertEqual(g.current, "X", "ход игрока не вернулся")
        self.assertTrue(g.moves, "ИИ должен был сходить в новой партии")
        self.assertTrue(all(m.startswith("O") for m in g.moves),
                        f"сыграно за человека: {g.moves}")

    def test_double_tap_on_new_game_does_not_freeze(self):
        async def scenario():
            fake = FakeBot()
            g = ai_game()
            for cell in (0, 4, 1, 5, 2):                # человек выигрывает
                g.move(cell)
            g.restart()                                 # первое нажатие
            first = asyncio.create_task(botmod.continue_game(g, fake))
            await asyncio.sleep(0.1)                    # первый уснул в паузе
            g.restart()                                 # второе нажатие
            second = asyncio.create_task(botmod.continue_game(g, fake))
            await asyncio.gather(first, second)
            return g

        g = asyncio.run(scenario())
        self.assertEqual(g.current, "X")
        self.assertFalse(g.ai_turn)
        self.assertEqual(g.moves, [], "отложенная партия не должна ходить")

    def test_quit_while_ai_thinking_leaves_menu_on_screen(self):
        async def scenario():
            fake = FakeBot()
            g = ai_game()
            g.move(4)
            pending = asyncio.create_task(botmod.continue_game(g, fake))
            await asyncio.sleep(0.1)
            await botmod.close_game(g, fake)            # «⏹ Меню» в паузу
            before = len(fake.edits)
            await pending
            return fake.edits[before:], g.id in botmod.GAMES

        later_edits, still_registered = asyncio.run(scenario())
        self.assertFalse(still_registered, "партия должна быть закрыта")
        self.assertEqual(later_edits, [],
                         "доска вернулась поверх меню")


class FinalizeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ttt-bot-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        storage.init(os.path.join(self.dir, "game_data.json"))

    def tearDown(self):
        botmod.GAMES.clear()

    def test_result_is_recorded_exactly_once(self):
        async def scenario():
            fake = FakeBot()
            storage.touch(1, "Человек")
            g = ai_game()
            for cell in (0, 4, 1, 5, 2):
                g.move(cell)
            await botmod.finalize(g, fake)
            await botmod.finalize(g, fake)              # повторный вызов
            return g

        asyncio.run(scenario())
        self.assertEqual(len(storage.history_for(1)), 1)
        self.assertEqual(storage.stats_for(1)["games"], 1)
        self.assertEqual(storage.stats_for(1)["wins"], 1)


class ScreenTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ttt-bot-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        storage.init(os.path.join(self.dir, "game_data.json"))

    def test_stats_text_survives_corrupt_values(self):
        storage.touch(5, "A")
        storage._data["users"]["5"]["stats"] = {"games": "мусор", "wins": None}
        storage._save()
        text = botmod.stats_text(5, "A")
        self.assertIn("Пока нет ни одной партии", text)

    def test_stats_text_reports_winrate(self):
        storage.touch(5, "A")
        storage._data["users"]["5"]["stats"] = {
            "games": 4, "wins": 1, "losses": 2, "draws": 1}
        storage._save()
        text = botmod.stats_text(5, "A")
        self.assertIn("Сыграно партий: 4", text)
        self.assertIn("Винрейт: 25.0%", text)

    def test_history_screen_paginates_and_clamps(self):
        games = [{"id": i, "date": "2026-10-07T10:00:00", "mode": "ai",
                  "players": {"X": {"id": 5, "name": "A"},
                              "O": {"id": None, "name": "ИИ"}},
                  "result": "draw", "moves": []}
                 for i in range(1, 12)]
        text, kb = botmod.history_screen(games, 5, "A", 99)
        self.assertIn("#11  07.10 10:00", text)        # страница поджата
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertIn("3/3", labels)

        text, _ = botmod.history_screen(games, 5, "A", -3)
        self.assertIn("#1  07.10 10:00", text)         # первая страница

        empty_text, _ = botmod.history_screen([], 5, "A", 0)
        self.assertIn("Пока пусто", empty_text)

    def test_as_int(self):
        self.assertEqual(botmod.as_int("12"), 12)
        self.assertEqual(botmod.as_int(None), 0)
        self.assertEqual(botmod.as_int("мусор"), 0)
        self.assertEqual(botmod.as_int(-5), -5)


class JoinAndRoomsTests(unittest.TestCase):
    def setUp(self):
        botmod.JOIN_ATTEMPTS.clear()
        botmod.ROOMS.clear()

    def tearDown(self):
        botmod.JOIN_ATTEMPTS.clear()
        botmod.ROOMS.clear()

    def test_join_rate_limit(self):
        for _ in range(botmod.JOIN_MAX):
            self.assertTrue(botmod._join_allowed(1))
        self.assertFalse(botmod._join_allowed(1), "перебор кодов не ограничен")
        self.assertTrue(botmod._join_allowed(2), "лимит не должен быть общим")
        # попытки вне окна не учитываются
        botmod.JOIN_ATTEMPTS[1] = [time.time() - botmod.JOIN_WINDOW - 10]
        botmod._prune_join_limits()
        self.assertNotIn(1, botmod.JOIN_ATTEMPTS)
        self.assertTrue(botmod._join_allowed(1))

    def test_expired_rooms_are_purged(self):
        old = botmod.Room(code="OLD123", owner=game.Player(1, "A"),
                          chat_id=1, message_id=1, private=True,
                          created=time.time() - botmod.ROOM_TTL - 5)
        fresh = botmod.Room(code="NEW123", owner=game.Player(2, "B"),
                            chat_id=2, message_id=2, private=True)
        botmod.ROOMS.update({"OLD123": old, "NEW123": fresh})
        botmod._purge_rooms()
        self.assertEqual(list(botmod.ROOMS), ["NEW123"])

    def test_codes_are_six_safe_chars_and_unique(self):
        codes = {botmod._new_code() for _ in range(200)}
        self.assertEqual(len(codes), 200, "коды повторяются")
        for code in codes:
            self.assertEqual(len(code), 6)
            self.assertTrue(set(code) <= set(botmod.ROOM_ALPHABET), code)


class EditFallbackTests(unittest.TestCase):
    def test_none_message_returns_none(self):
        async def run():
            return await botmod.edit_or_send(None, "текст", botmod.back_kb())

        self.assertIsNone(asyncio.run(run()))

    def test_inaccessible_message_sends_new_one(self):
        try:
            from aiogram.types import Chat, InaccessibleMessage
            message = InaccessibleMessage(
                message_id=7, date=0,          # aiogram: недоступное = 0
                chat=Chat(id=42, type="private"))
        except Exception as exc:                      # pragma: no cover
            self.skipTest(f"aiogram не даёт создать InaccessibleMessage: {exc}")
        fake = FakeBot()

        async def run():
            return await botmod.edit_or_send(message, "текст",
                                             botmod.back_kb(), fake)

        shown = asyncio.run(run())
        self.assertIsNotNone(shown, "должно отправиться новое сообщение")
        self.assertEqual(fake.edits, ["текст"])


if __name__ == "__main__":
    unittest.main()
