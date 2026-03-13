from pathlib import Path

from nanobot.bus.queue import MessageBus
from nanobot.channels.matrix import MatrixChannel
from nanobot.channels.mochat import MochatChannel
from nanobot.config.schema import MatrixConfig, MatrixInstanceConfig, MochatConfig, MochatInstanceConfig


def test_matrix_default_store_path_unchanged(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("nanobot.channels.matrix.get_data_dir", lambda: tmp_path)
    channel = MatrixChannel(
        MatrixConfig(
            enabled=True,
            homeserver="https://matrix.example.com",
            access_token="token",
            user_id="@bot:example.com",
            allow_from=["*"],
        ),
        MessageBus(),
    )

    assert channel._get_store_path() == tmp_path / "matrix-store"


def test_matrix_instance_store_path_isolated(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("nanobot.channels.matrix.get_data_dir", lambda: tmp_path)
    channel = MatrixChannel(
        MatrixInstanceConfig(
            name="ops",
            enabled=True,
            homeserver="https://matrix.example.com",
            access_token="token",
            user_id="@bot:example.com",
            allow_from=["*"],
        ),
        MessageBus(),
    )

    assert channel._get_store_path() == tmp_path / "matrix-store" / "ops"


def test_mochat_default_state_dir_unchanged(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("nanobot.channels.mochat.get_runtime_subdir", lambda _: tmp_path / "mochat")
    channel = MochatChannel(
        MochatConfig(enabled=True, claw_token="token", agent_user_id="agent-1", allow_from=["*"]),
        MessageBus(),
    )

    assert channel._state_dir == tmp_path / "mochat"
    assert channel._cursor_path == tmp_path / "mochat" / "session_cursors.json"


def test_mochat_instance_state_dir_isolated(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("nanobot.channels.mochat.get_runtime_subdir", lambda _: tmp_path / "mochat")
    channel = MochatChannel(
        MochatInstanceConfig(
            name="sales",
            enabled=True,
            claw_token="token",
            agent_user_id="agent-1",
            allow_from=["*"],
        ),
        MessageBus(),
    )

    assert channel._state_dir == tmp_path / "mochat" / "sales"
    assert channel._cursor_path == tmp_path / "mochat" / "sales" / "session_cursors.json"
