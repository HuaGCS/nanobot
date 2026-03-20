from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from nanobot.session.manager import SessionManager


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_save_appends_only_new_messages(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("qq:test")
    session.add_message("user", "hello")
    session.add_message("assistant", "hi")
    manager.save(session)

    path = manager._get_session_path(session.key)
    original_text = path.read_text(encoding="utf-8")

    session.add_message("user", "next")
    manager.save(session)

    lines = _read_jsonl(path)
    assert path.read_text(encoding="utf-8").startswith(original_text)
    assert sum(1 for line in lines if line.get("_type") == "metadata") == 1
    assert [line["content"] for line in lines if line.get("role")] == ["hello", "hi", "next"]


def test_save_appends_metadata_checkpoint_without_rewriting_history(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("qq:test")
    session.add_message("user", "hello")
    session.add_message("assistant", "hi")
    manager.save(session)

    path = manager._get_session_path(session.key)
    original_text = path.read_text(encoding="utf-8")

    session.last_consolidated = 2
    manager.save(session)

    lines = _read_jsonl(path)
    assert path.read_text(encoding="utf-8").startswith(original_text)
    assert sum(1 for line in lines if line.get("_type") == "metadata") == 2
    assert lines[-1]["_type"] == "metadata"
    assert lines[-1]["last_consolidated"] == 2

    manager.invalidate(session.key)
    reloaded = manager.get_or_create("qq:test")
    assert reloaded.last_consolidated == 2
    assert [message["content"] for message in reloaded.messages] == ["hello", "hi"]


def test_clear_rewrites_session_file(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("qq:test")
    session.add_message("user", "hello")
    session.add_message("assistant", "hi")
    manager.save(session)

    path = manager._get_session_path(session.key)
    session.clear()
    manager.save(session)

    lines = _read_jsonl(path)
    assert len(lines) == 1
    assert lines[0]["_type"] == "metadata"
    assert lines[0]["last_consolidated"] == 0

    manager.invalidate(session.key)
    reloaded = manager.get_or_create("qq:test")
    assert reloaded.messages == []
    assert reloaded.last_consolidated == 0


def test_list_sessions_uses_file_mtime_for_append_only_updates(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("qq:test")
    session.add_message("user", "hello")
    manager.save(session)

    path = manager._get_session_path(session.key)
    stale_time = time.time() - 3600
    os.utime(path, (stale_time, stale_time))

    before = datetime.fromisoformat(manager.list_sessions()[0]["updated_at"])
    assert before.timestamp() < time.time() - 3000

    session.add_message("assistant", "hi")
    manager.save(session)

    after = datetime.fromisoformat(manager.list_sessions()[0]["updated_at"])
    assert after > before

