"""Тесты хранилища: storage.py — целостность данных, битый файл, блокировки."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import storage  # noqa: E402
import game  # noqa: E402

STATS_ZERO = {"games": 0, "wins": 0, "losses": 0, "draws": 0}


class StorageTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ttt-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "game_data.json")
        storage.init(self.path)

    def read(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    def write(self, data):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)

    # ------------------------------------------------------------- инициация

    def test_init_without_file_starts_clean(self):
        self.assertEqual(storage._data["users"], {})
        self.assertEqual(storage._data["history"], [])
        self.assertEqual(storage._data["next_id"], 1)

    def test_corrupt_file_is_quarantined_not_wiped(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"users": {"1": {"name": "A"')
        storage.init(self.path)
        self.assertEqual(storage._data["users"], {})
        backups = [f for f in os.listdir(self.dir) if ".corrupt-" in f]
        self.assertEqual(len(backups), 1, "битый файл должен быть отложен")
        with open(os.path.join(self.dir, backups[0]), encoding="utf-8") as fh:
            self.assertIn('"users"', fh.read())   # данные не уничтожены

    def test_wrong_structure_is_quarantined(self):
        self.write({"users": ["not", "a", "dict"], "history": [], "next_id": 1})
        storage.init(self.path)
        self.assertEqual(storage._data["users"], {})
        self.assertTrue(any(".corrupt-" in f for f in os.listdir(self.dir)))

    def test_missing_keys_are_filled(self):
        self.write({"users": {"7": {"id": 7, "name": "A"}}})
        storage.init(self.path)
        self.assertEqual(storage._data["history"], [])
        self.assertEqual(storage._data["next_id"], 1)
        self.assertIn("7", storage._data["users"])

    # --------------------------------------------------------------- игроки

    def test_touch_creates_and_updates_user(self):
        user = storage.touch(100, "Артём")
        self.assertEqual(user["stats"], STATS_ZERO)
        self.assertEqual(storage.stats_for(100), STATS_ZERO)
        self.assertIsNotNone(storage.created_for(100))
        storage.touch(100, "Артём Павлов")
        self.assertEqual(storage.stats_for(100), STATS_ZERO)
        self.assertEqual(self.read()["users"]["100"]["name"], "Артём Павлов")

    def test_unknown_user(self):
        self.assertIsNone(storage.stats_for(5))
        self.assertIsNone(storage.created_for(5))

    # --------------------------------------------------- старые консольные

    def test_legacy_claim_moves_stats_and_drops_credentials(self):
        storage._data["users"]["артём"] = {
            "name": "Артём", "salt": "aa", "hash": "bb",
            "stats": {"games": 50, "wins": 40, "losses": 5, "draws": 5},
        }
        storage._data["history"] = [{
            "id": 1, "date": "2026-01-01T00:00:00", "mode": "pvp", "round": 1,
            "players": {"X": {"id": None, "name": "Артём"},
                        "O": {"id": None, "name": "Алиса"}},
            "result": "X", "moves": [],
        }]
        storage._save()

        user = storage.touch(999, "Артём")
        self.assertEqual(user["stats"]["wins"], 40)
        legacy = self.read()["users"]["артём"]
        self.assertTrue(legacy["claimed"])
        self.assertEqual(legacy["claimed_by"], 999)
        self.assertNotIn("salt", legacy)
        self.assertNotIn("hash", legacy)
        # партия привязана к новому владельцу — её больше не найдёт по имени
        self.assertEqual(
            storage._data["history"][0]["players"]["X"]["id"], 999)
        self.assertEqual([e["id"] for e in storage.history_for(999)], [1])
        self.assertEqual(storage.history_for(888, "Артём"), [])

    def test_legacy_claim_is_one_shot(self):
        storage._data["users"]["алиса"] = {
            "name": "Алиса", "stats": {"games": 3, "wins": 3,
                                       "losses": 0, "draws": 0},
        }
        storage._save()
        first = storage.touch(1, "Алиса")
        second = storage.touch(2, "Алиса")
        self.assertEqual(first["stats"]["wins"], 3)
        self.assertEqual(second["stats"]["wins"], 0)

    # ------------------------------------------------------------- история

    def test_finish_game_updates_stats_and_history(self):
        storage.touch(1, "X-игрок")
        storage.touch(2, "O-игрок")
        players = {"X": game.Player(1, "X-игрок"),
                   "O": game.Player(2, "O-игрок")}
        entry_id = storage.finish_game("pvp", 1, players, "X", ["X5"])
        self.assertEqual(entry_id, 1)
        self.assertEqual(self.read()["next_id"], 2)
        self.assertEqual(storage.stats_for(1)["wins"], 1)
        self.assertEqual(storage.stats_for(1)["games"], 1)
        self.assertEqual(storage.stats_for(2)["losses"], 1)
        games = storage.history_for(1)
        self.assertEqual(len(games), 1)
        self.assertEqual(storage.side_of(games[0], 1, "X-игрок"), "X")

    def test_ai_has_no_stats_but_appears_in_history(self):
        storage.touch(1, "Человек")
        players = {"X": game.Player(1, "Человек"),
                   "O": game.Player(None, "ИИ")}
        storage.finish_game("ai", 1, players, "draw", [])
        self.assertEqual(storage.stats_for(1)["games"], 1)
        self.assertEqual(storage.stats_for(1)["draws"], 1)

    def test_history_is_capped(self):
        for i in range(storage.MAX_HISTORY + 10):
            entry = {"id": i, "date": "2026-01-01T00:00:00", "mode": "ai",
                     "round": 1, "players": {}, "result": "draw", "moves": []}
            storage._data["history"].append(entry)
        storage._save()
        storage.finish_game("ai", 1, {}, "draw", [])
        self.assertEqual(len(self.read()["history"]), storage.MAX_HISTORY)

    # --------------------------------------------------- внешние правки и лок

    def test_external_edit_is_picked_up_instead_of_overwritten(self):
        storage.touch(1, "Первый")
        data = self.read()
        data["users"]["2"] = {"id": 2, "name": "Внешний", "stats": STATS_ZERO}
        time.sleep(0.01)
        self.write(data)                      # правка «снаружи»
        storage.touch(3, "Третий")
        users = self.read()["users"]
        self.assertIn("1", users)
        self.assertIn("2", users, "внешняя запись затёрта")
        self.assertIn("3", users)

    def test_save_is_atomic_and_leaves_no_tmp(self):
        storage.touch(1, "A")
        self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        self.assertEqual(self.read()["users"]["1"]["name"], "A")

    def test_lock_is_released_after_mutation(self):
        storage.touch(1, "A")
        self.assertIsNone(storage._lock_fh, "блокировка должна быть снята")
        self.assertEqual(storage._lock_depth, 0)

    @unittest.skipUnless(os.name == "nt", "межпроцессная блокировка на Windows")
    def test_second_process_cannot_write_behind_our_back(self):
        """Пока один процесс меняет данные, второй обязан ждать блокировку."""
        storage.touch(1, "Первый")
        ready = os.path.join(self.dir, "ready")
        child = r'''
import json, msvcrt, os, sys, time
path, ready = sys.argv[1], sys.argv[2]
fh = open(path + ".lock", "a+b")
if fh.seek(0, 2) == 0:
    fh.write(b"\0"); fh.flush()
fh.seek(0)
msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
open(ready, "w").write("ok")
time.sleep(1.0)
data = json.load(open(path, encoding="utf-8"))
data["users"]["424242"] = {"id": 424242, "name": "Второй процесс",
                           "stats": {"games": 0, "wins": 0, "losses": 0,
                                     "draws": 0}}
with open(path, "w", encoding="utf-8") as out:
    json.dump(data, out, ensure_ascii=False)
fh.seek(0)
msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
fh.close()
'''
        proc = subprocess.Popen([sys.executable, "-c", child, self.path, ready])
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        for _ in range(100):                  # ждём, пока дочерний возьмёт лок
            if os.path.exists(ready):
                break
            time.sleep(0.05)
        self.assertTrue(os.path.exists(ready), "дочерний процесс не стартовал")

        started = time.time()
        storage.touch(3, "Третий")
        waited = time.time() - started
        proc.wait(timeout=15)

        self.assertGreaterEqual(waited, 0.5, "запись не дождалась блокировки")
        users = self.read()["users"]
        for key in ("1", "3", "424242"):
            self.assertIn(key, users, f"запись {key} потеряна")


if __name__ == "__main__":
    unittest.main()
