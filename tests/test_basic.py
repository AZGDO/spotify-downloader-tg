from pathlib import Path


def test_placeholder() -> None:
    assert Path("bot.py").exists()
