import pytest


def test_unauthenticated_model_error_says_what_to_do(tmp_path, monkeypatch):
    """The first wall a new user hits: actions, not a file path."""
    from simantic import _replx
    from simantic.mcu import SimError

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SIMANTIC_HOME", str(tmp_path))
    monkeypatch.delenv("SIMANTIC_MCU_LIB", raising=False)
    with pytest.raises(SimError) as caught:
        _replx.model_replx("ESP32-C3")

    message = str(caught.value)
    assert "ESP32-C3 needs an account" in message
    assert "simantic auth" in message
    # The path to ~/.sim_id is a detail the reader cannot act on.
    assert ".sim_id" not in message
