"""Тесты чистой игровой логики: game.py (без сети и файлов)."""

import os
import random
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import game  # noqa: E402


def empty_board():
    return [" "] * 9


def ai_game():
    return game.Game("ai", {
        "X": game.Player(1, "Человек"),
        "O": game.Player(None, game.AI_NAME),
    })


class WinnerTests(unittest.TestCase):
    def test_every_win_line_is_detected(self):
        for line in game.WIN_LINES:
            board = empty_board()
            for i in line:
                board[i] = "X"
            self.assertEqual(game.winner_of(board), ("X", line))

    def test_empty_and_mixed_board_have_no_winner(self):
        self.assertEqual(game.winner_of(empty_board()), (None, None))
        board = empty_board()
        board[0], board[1] = "X", "O"
        self.assertEqual(game.winner_of(board), (None, None))

    def test_board_full(self):
        self.assertFalse(game.board_full(empty_board()))
        self.assertTrue(game.board_full(list("XOXOXOXOX")))

    def test_other_symbol(self):
        self.assertEqual(game.other("X"), "O")
        self.assertEqual(game.other("O"), "X")


class MoveTests(unittest.TestCase):
    def test_valid_move_switches_turn(self):
        g = ai_game()
        self.assertIsNone(g.move(4))
        self.assertEqual(g.board[4], "X")
        self.assertEqual(g.current, "O")
        self.assertEqual(g.moves, ["X5"])

    def test_move_rejects_bad_cells(self):
        g = ai_game()
        self.assertIsNotNone(g.move(9))          # вне поля
        self.assertIsNotNone(g.move(-1))
        self.assertIsNone(g.move(4))             # X в центр
        self.assertIsNotNone(g.move(4))          # клетка занята
        self.assertEqual(g.current, "O")         # очередь не сдвинулась
        for cell in (0, 8, 1, 7):                # O0, X8, O1, X7
            self.assertIsNone(g.move(cell))
        self.assertIsNone(g.move(2))             # O добирает (0,1,2)
        self.assertTrue(g.finished)
        self.assertEqual(g.result, "O")
        self.assertEqual(g.scores["O"], 1)
        self.assertIsNotNone(g.move(3))          # после конца ходить нельзя

    def test_draw_is_counted(self):
        g = ai_game()
        for cell in (0, 1, 2, 4, 3, 5, 7, 6, 8):   # равный исход
            self.assertIsNone(g.move(cell))
        self.assertTrue(g.finished)
        self.assertEqual(g.result, "draw")
        self.assertEqual(g.scores["draw"], 1)

    def test_restart_flips_first_move_and_keeps_score(self):
        g = ai_game()
        for cell in (0, 4, 1, 5, 2):               # X берёт (0,1,2)
            g.move(cell)
        self.assertEqual(g.result, "X")
        g.restart()
        self.assertEqual(g.round_no, 2)
        self.assertEqual(g.first, "O")
        self.assertEqual(g.current, "O")
        self.assertEqual(g.board, empty_board())
        self.assertEqual(g.moves, [])
        self.assertIsNone(g.result)
        self.assertFalse(g.recorded)
        self.assertEqual(g.scores["X"], 1)         # счёт между партиями живёт


class AiTests(unittest.TestCase):
    def test_ai_turn_property(self):
        g = ai_game()
        self.assertFalse(g.ai_turn)     # первым ходит человек (X)
        g.move(0)
        self.assertTrue(g.ai_turn)      # очередь компьютера (O)
        g.current = "X"
        self.assertFalse(g.ai_turn)

    def test_ai_move_does_nothing_for_the_human(self):
        """Регрессия: отложенный ход ИИ не должен играть за человека."""
        g = ai_game()
        g.current = "X"                 # чужая очередь — ходить нельзя
        before = list(g.board)
        g.ai_move()
        self.assertEqual(g.board, before)
        self.assertEqual(g.moves, [])

    def test_ai_move_does_nothing_after_finish(self):
        g = ai_game()
        g.move(4)
        g.result = "draw"
        before = list(g.board)
        g.ai_move()
        self.assertEqual(g.board, before)
        self.assertEqual(g.moves, ["X5"])

    def test_ai_move_plays_on_its_turn(self):
        g = ai_game()
        g.move(4)
        g.ai_move()
        self.assertEqual(g.current, "X")       # ход ИИ сделан, очередь человека
        self.assertTrue(g.moves[-1].startswith("O"))

    def test_ai_never_loses_to_random_play(self):
        rnd = random.Random(20261007)
        for _ in range(30):
            g = ai_game()
            while not g.finished:
                if g.ai_turn:
                    g.ai_move()
                else:
                    free = [i for i, c in enumerate(g.board) if c == " "]
                    g.move(rnd.choice(free))
            # X — человек, O — ИИ: ИИ обязан остаться непобеждённым
            self.assertNotEqual(g.result, "X", f"ИИ проиграл: {g.moves}")

    def test_ai_plays_perfectly_against_itself(self):
        for _ in range(20):
            g = ai_game()
            while not g.finished:
                index = game.best_move(g.board, g.current)
                g.move(index)
            self.assertNotIn(g.result, ("X", "O"),
                             f"кто-то выиграл при идеальной игре: {g.moves}")

    def test_best_move_on_full_board_is_none(self):
        self.assertIsNone(game.best_move(list("XOXOXOXOX"), "X"))

    def test_is_player(self):
        g = ai_game()
        self.assertTrue(g.is_player(1))
        self.assertFalse(g.is_player(2))
        self.assertFalse(g.is_player(None))


if __name__ == "__main__":
    unittest.main()
