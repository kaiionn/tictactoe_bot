"""Хранение игроков, статистики и истории партий в game_data.json.

Игроки идентифицируются по Telegram ID — пароли больше не нужны.
Старые консольные аккаунты (ключи по имени с salt/hash) в файле сохраняются:
при первом входе нового пользователя с совпадающим именем их статистика
переходит ему, запись помечается как занятая (`claimed`, `claimed_by`),
связанные партии истории привязываются к его Telegram ID, а неиспользуемые
salt/hash удаляются.

Надёжность записи:
  * атомарная запись — временный файл + ``os.replace`` + ``fsync``;
  * межпроцессная блокировка на всё время изменения данных: два экземпляра
    бота (или ручная правка) больше не затирают записи друг друга;
  * правка файла «снаружи» подхватывается перед следующим изменением;
  * битый файл не затирается молча, а откладывается в ``*.corrupt-<время>``
    и о нём пишется ошибка в лог.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
from contextlib import contextmanager

log = logging.getLogger("ttt.storage")

DATA_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "game_data.json")
MAX_HISTORY = 200

_data: dict | None = None

# Внутрипроцессная блокировка (вложенные вызовы разрешены).
_state_lock = threading.RLock()
_lock_depth = 0
# Дескриптор межпроцессной блокировки на ``DATA_FILE + ".lock"``.
_lock_fh = None
_lock_path: str | None = None
# (st_mtime_ns, st_size) файла, который мы последними читали/писали.
_saved_sig: tuple | None = None


def _fresh_stats() -> dict:
    return {"games": 0, "wins": 0, "losses": 0, "draws": 0}


def _norm(text) -> str:
    if not isinstance(text, str):
        return ""
    return text.strip().casefold()


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _file_sig() -> tuple | None:
    try:
        st = os.stat(DATA_FILE)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _is_valid(raw) -> bool:
    """Структура файла пригодна для работы (ключи отсутствуют — это норма)."""
    if not isinstance(raw, dict):
        return False
    if "users" in raw and not isinstance(raw["users"], dict):
        return False
    if "history" in raw and not isinstance(raw["history"], list):
        return False
    if "next_id" in raw and not isinstance(raw["next_id"], int):
        return False
    return True


def _normalize(raw: dict) -> dict:
    if not isinstance(raw.get("users"), dict):
        raw["users"] = {}
    if not isinstance(raw.get("history"), list):
        raw["history"] = []
    if not isinstance(raw.get("next_id"), int):
        raw["next_id"] = 1
    return raw


# --------------------------------------------------------------------- #
#  Блокировки
# --------------------------------------------------------------------- #

def _lock_acquire(path: str):
    """Взять экскюзивную блокировку; вернуть дескриптор либо None."""
    try:
        fh = open(path, "a+b")
    except OSError:
        log.exception("Не удалось открыть файл блокировки %s", path)
        return None
    try:
        if fh.seek(0, os.SEEK_END) == 0:      # нужен байт для диапазона
            fh.write(b"\0")
            fh.flush()
        fh.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX)
    except ImportError:
        # Платформа без нужного модуля — работаем без межпроцессной блокировки.
        log.debug("Межпроцессная блокировка недоступна на этой платформе")
    except OSError:
        log.exception("Не удалось заблокировать %s", path)
        fh.close()
        return None
    return fh


def _lock_release(fh) -> None:
    if fh is None:
        return
    try:
        fh.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_UN)
    except (OSError, ImportError):
        pass
    finally:
        try:
            fh.close()
        except OSError:
            pass


@contextmanager
def _mutation():
    """Единственное место, где меняются данные: блокировка + синхронизация."""
    global _lock_depth, _lock_fh, _lock_path
    with _state_lock:
        _lock_depth += 1
        try:
            if _lock_depth == 1:
                path = DATA_FILE + ".lock"
                if _lock_path != path:
                    _lock_release(_lock_fh)
                    _lock_fh, _lock_path = None, path
                if _lock_fh is None:
                    _lock_fh = _lock_acquire(path)
                _sync_external()
            yield
        finally:
            _lock_depth -= 1
            if _lock_depth == 0:
                _lock_release(_lock_fh)
                _lock_fh = None


def _sync_external() -> None:
    """Подхватить изменения файла, сделанные вне этого процесса."""
    global _saved_sig
    if _data is None or _saved_sig is None:
        return
    sig = _file_sig()
    if sig == _saved_sig:
        return
    try:
        with open(DATA_FILE, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        _saved_sig = None
        return
    except (OSError, ValueError):
        log.warning("%s сейчас не читается — оставляем данные памяти", DATA_FILE)
        return
    if not _is_valid(raw):
        log.warning("%s повреждён извне — игнорируем, свои данные сохраняем",
                    DATA_FILE)
        return
    _data.clear()
    _data.update(_normalize(raw))
    _saved_sig = sig
    log.info("Подхвачены внешние изменения %s", DATA_FILE)


# --------------------------------------------------------------------- #
#  Чтение и запись файла
# --------------------------------------------------------------------- #

def _quarantine() -> str | None:
    """Отложить битый файл в сторону, чтобы не затереть его пустым."""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = f"{DATA_FILE}.corrupt-{stamp}"
    try:
        os.replace(DATA_FILE, target)
    except OSError:
        log.exception("Не удалось отложить битый файл в %s", target)
        return None
    return target


def _read_or_recover() -> dict:
    """Прочитать файл; при повреждении — отложить его и начать заново."""
    try:
        with open(DATA_FILE, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return {}
    except ValueError:
        backup = _quarantine()
        log.error("game_data.json повреждён — отложен в %s, "
                  "начинаем с пустого файла", backup or "(не удалось)")
        return {}
    except OSError:
        log.exception("Не удалось прочитать %s — остановка", DATA_FILE)
        raise
    if not _is_valid(raw):
        backup = _quarantine()
        log.error("game_data.json имеет неправильную структуру — отложен в %s",
                  backup or "(не удалось)")
        return {}
    return raw


def _save() -> None:
    """Атомарная запись. Вызывать только под блокировкой ``_mutation()``."""
    global _saved_sig
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(_data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, DATA_FILE)
    _saved_sig = _file_sig()


def _store() -> None:
    """Как ``_save``, но не роняет обработчик бота при ошибке записи."""
    try:
        _save()
    except OSError:
        log.exception("Не удалось сохранить %s — изменения только в памяти",
                      DATA_FILE)


def _ensure() -> dict:
    if _data is None:
        init()
    return _data


# --------------------------------------------------------------------- #
#  Инициализация
# --------------------------------------------------------------------- #

def init(path: str | None = None) -> dict:
    """Загрузить файл (или начать с чистого) и привести его к новому формату.

    ``path`` — переопределить расположение файла (удобно в тестах).
    """
    global DATA_FILE, _data, _saved_sig
    if path is not None and path != DATA_FILE:
        DATA_FILE = path
        _saved_sig = None
    with _mutation():
        _data = _normalize(_read_or_recover())
        _saved_sig = _file_sig()
        _migrate()
    return _data


def _migrate() -> None:
    """Консольный формат истории: имена игроков → пары «id, имя»."""
    changed = False
    for entry in _data["history"]:
        players = entry.get("players")
        if not isinstance(players, dict):
            continue
        for symbol, value in list(players.items()):
            if isinstance(value, str):
                players[symbol] = {"id": None, "name": value}
                changed = True
    if changed:
        _save()


# --------------------------------------------------------------------- #
#  Игроки
# --------------------------------------------------------------------- #

def _bind_legacy_history(old_name: str, user_id: int) -> None:
    """Привязать старые консольные партии к Telegram ID нового владельца.

    После привязки эти записи больше не находятся по имени — их не увидит
    посторонний, случайно совпавший по имени.
    """
    target = _norm(old_name)
    if not target:
        return
    for entry in _data["history"]:
        players = entry.get("players")
        if not isinstance(players, dict):
            continue
        for player in players.values():
            if (isinstance(player, dict)
                    and player.get("id") is None
                    and _norm(player.get("name")) == target):
                player["id"] = int(user_id)


def _claim_legacy(name: str, user_id: int):
    """Найти старый консольный аккаунт с таким же именем и занять его."""
    target = _norm(name)
    if not target:
        return None
    for key, user in _data["users"].items():
        if key.isdigit() or not isinstance(user, dict):
            continue
        if user.get("claimed"):
            continue
        if _norm(user.get("name")) != target:
            continue
        user["claimed"] = True
        user["claimed_by"] = int(user_id)
        user.pop("salt", None)
        user.pop("hash", None)          # мёртвые креды: паролей больше нет
        _bind_legacy_history(user.get("name"), user_id)
        log.info("Старый аккаунт %r унаследован пользователем %s",
                 user.get("name"), user_id)
        return user
    return None


def touch(user_id: int, name: str) -> dict:
    """Создать запись игрока (или обновить отображаемое имя) по его Telegram ID."""
    with _mutation():
        _ensure()
        key = str(user_id)
        user = _data["users"].get(key)
        if user is None:
            stats = _fresh_stats()
            legacy = _claim_legacy(name, user_id)
            if legacy:
                for stat in stats:
                    try:
                        stats[stat] += int((legacy.get("stats") or {}).get(stat, 0))
                    except (TypeError, ValueError):
                        pass
            user = {"id": int(user_id), "name": name,
                    "created": _now(), "stats": stats}
            _data["users"][key] = user
            _store()
        elif user.get("name") != name:
            user["name"] = name
            _store()
        return user


def stats_for(user_id: int) -> dict | None:
    _ensure()
    user = _data["users"].get(str(user_id))
    if not user:
        return None
    stats = _fresh_stats()
    stats.update(user.get("stats") or {})
    return stats


def created_for(user_id: int) -> str | None:
    _ensure()
    user = _data["users"].get(str(user_id))
    return user.get("created") if user else None


# --------------------------------------------------------------------- #
#  История партий
# --------------------------------------------------------------------- #

def finish_game(mode: str, round_no: int, players: dict, result: str,
                moves: list[str]) -> int:
    """Записать итоги партии в статистику и историю.

    `players` — {"X": объект с атрибутами id и name, "O": ...};
    у компьютера id равен None, поэтому его статистика не ведётся.
    """
    with _mutation():
        _ensure()
        for symbol, player in players.items():
            user_id = getattr(player, "id", None)
            if user_id is None:
                continue
            user = _data["users"].get(str(user_id))
            if not user:
                continue
            stats = user.setdefault("stats", _fresh_stats())
            for stat in _fresh_stats():
                stats.setdefault(stat, 0)
            stats["games"] += 1
            if result == "draw":
                stats["draws"] += 1
            elif result == symbol:
                stats["wins"] += 1
            else:
                stats["losses"] += 1

        entry_id = _data["next_id"]
        _data["history"].append({
            "id": entry_id,
            "date": _now(),
            "mode": mode,
            "round": round_no,
            "players": {
                symbol: {
                    "id": getattr(player, "id", None),
                    "name": getattr(player, "name", ""),
                }
                for symbol, player in players.items()
            },
            "result": result,
            "moves": list(moves),
        })
        _data["next_id"] += 1
        overflow = len(_data["history"]) - MAX_HISTORY
        if overflow > 0:
            del _data["history"][:overflow]
        _save()
    return entry_id


def side_of(entry: dict, user_id: int, name: str | None = None) -> str | None:
    """За какую сторону играл пользователь в записи истории."""
    _ensure()
    target = _norm(name)
    for symbol, player in (entry.get("players") or {}).items():
        if isinstance(player, dict):
            player_id = player.get("id")
            if player_id is not None and player_id == user_id:
                return symbol
            if player_id is None and target and _norm(player.get("name")) == target:
                return symbol
        elif target and _norm(str(player)) == target:
            return symbol
    return None


def history_for(user_id: int, name: str | None = None) -> list[dict]:
    """Партии пользователя, свежие вперёд. Старые записи ищем ещё и по имени."""
    _ensure()
    target = _norm(name)
    games = []
    for entry in reversed(_data["history"]):
        players = entry.get("players")
        if not isinstance(players, dict):
            continue
        for symbol, player in players.items():
            if isinstance(player, dict):
                player_id = player.get("id")
                if player_id is not None and player_id == user_id:
                    games.append(entry)
                    break
                if player_id is None and target and _norm(player.get("name")) == target:
                    games.append(entry)
                    break
            elif target and _norm(str(player)) == target:
                games.append(entry)
                break
    return games
