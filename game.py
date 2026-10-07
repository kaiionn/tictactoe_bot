"""Чистая игровая логика «Крестики-нолики»: поле, правила и ИИ.

Модуль ничего не печатает и ничего не спрашивает — им пользуется
Telegram-бот (см. bot.py).
"""

from __future__ import annotations

import random
import secrets
import time
from dataclasses import dataclass

WIN_LINES = (
    (0, 1, 2), (3, 4, 5), (6, 7, 8),
    (0, 3, 6), (1, 4, 7), (2, 5, 8),
    (0, 4, 8), (2, 4, 6),
)
EMOJI = {"X": "❌", "O": "⭕"}
AI_NAME = "ИИ"


def other(symbol: str) -> str:
    return "O" if symbol == "X" else "X"


def winner_of(board):
    """Победитель и линия из трёх клеток либо (None, None)."""
    for a, b, c in WIN_LINES:
        if board[a] != " " and board[a] == board[b] == board[c]:
            return board[a], (a, b, c)
    return None, None


def board_full(board) -> bool:
    return all(cell != " " for cell in board)


def score_position(board, turn, me, opp, depth, alpha, beta) -> int:
    """Минимакс с альфа-бета отсечением: оценка позиции глазами `me`."""
    winner, _ = winner_of(board)
    if winner == me:
        return 10 - depth
    if winner == opp:
        return depth - 10
    if board_full(board):
        return 0
    if turn == me:
        value = -100
        for i in range(9):
            if board[i] == " ":
                board[i] = turn
                value = max(value, score_position(
                    board, opp, me, opp, depth + 1, alpha, beta))
                board[i] = " "
                alpha = max(alpha, value)
                if alpha >= beta:
                    break
        return value
    value = 100
    for i in range(9):
        if board[i] == " ":
            board[i] = turn
            value = min(value, score_position(
                board, me, me, opp, depth + 1, alpha, beta))
            board[i] = " "
            beta = min(beta, value)
            if alpha >= beta:
                break
    return value


def best_move(board, symbol):
    """Лучший ход; среди равных по оценке — случайный (чтобы не повторяться)."""
    opp = other(symbol)
    best_score = -100
    moves = []
    for i in range(9):
        if board[i] == " ":
            board[i] = symbol
            value = score_position(board, opp, symbol, opp, 0, -100, 100)
            board[i] = " "
            if value > best_score:
                best_score = value
                moves = [i]
            elif value == best_score:
                moves.append(i)
    return random.choice(moves) if moves else None


@dataclass(frozen=True)
class Player:
    """Участник партии: id в Telegram (None — компьютер)."""

    id: int | None
    name: str


class Game:
    """Партия (или серия партий между теми же игроками).

    `views` — адреса сообщений-досок: (chat_id, message_id, viewer_id),
    где viewer_id — игрок, чьё сообщение это (None для групповых чатов).
    """

    def __init__(self, mode: str, players: dict[str, Player], first: str = "X"):
        self.id = secrets.token_hex(4)
        self.mode = mode                      # "ai" | "pvp"
        self.players = dict(players)
        self.first = first
        self.board = [" "] * 9
        self.current = first
        self.round_no = 1
        self.scores = {"X": 0, "O": 0, "draw": 0}
        self.result: str | None = None        # "X" | "O" | "draw"
        self.win_line: tuple[int, int, int] | None = None
        self.moves: list[str] = []
        self.recorded = False                 # итог уже записан в историю
        self.views: list[tuple[int, int, int | None]] = []
        self.updated = time.time()            # последняя активность (для уборки)

    # ------------------------------------------------------------------ #

    @property
    def finished(self) -> bool:
        return self.result is not None

    @property
    def ai_turn(self) -> bool:
        return self.mode == "ai" and self.current == "O"

    def player(self, symbol: str) -> Player:
        return self.players[symbol]

    def current_player(self) -> Player:
        return self.players[self.current]

    def is_player(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        return any(p.id == user_id for p in self.players.values())

    # ------------------------------------------------------------------ #

    def move(self, index: int) -> str | None:
        """Ход текущего игрока. None — успех, иначе текст ошибки.

        Метод синхронный: в async-коде его вызывают до любовых await,
        чтобы два параллельных нажатия не сошлись в одну очередь.
        """
        self.updated = time.time()
        if self.finished:
            return "Партия уже закончилась"
        if not 0 <= index < 9:
            return "Такой клетки нет"
        if self.board[index] != " ":
            return "Клетка уже занята — выбери другую"
        symbol = self.current
        self.board[index] = symbol
        self.moves.append(f"{symbol}{index + 1}")
        winner, line = winner_of(self.board)
        if winner:
            self.result, self.win_line = winner, line
        elif board_full(self.board):
            self.result = "draw"
        else:
            self.current = other(symbol)
        if self.result is not None:
            self.scores[self.result] += 1
        return None

    def ai_move(self) -> None:
        """Ход компьютера.

        Ничего не делает, если сейчас не ход ИИ: вызов «вхолостую» возможен,
        когда партия перезапущена или закрыта, пока компьютер «думал».
        """
        if self.finished or not self.ai_turn:
            return
        index = best_move(self.board, self.current)
        if index is not None:
            self.move(index)

    def restart(self) -> None:
        """Новая партия между теми же игроками: счёт сохраняется."""
        self.updated = time.time()
        self.board = [" "] * 9
        self.round_no += 1
        self.first = other(self.first)
        self.current = self.first
        self.result = None
        self.win_line = None
        self.moves = []
        self.recorded = False
