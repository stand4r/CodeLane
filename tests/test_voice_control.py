"""Voice commands must require an exact spoken instruction."""
import pytest

from services.voice_control import command_for


@pytest.mark.parametrize(("phrase", "action"), [
    ("Джарвис, открой настройки", "action_open_settings"),
    ("Запусти файл!", "action_run_file"),
    ("покажи терминал", "toggle_terminal"),
    ("  новый   файл  ", "action_new_file"),
])
def test_explicit_voice_commands(phrase, action):
    assert command_for(phrase) == action


@pytest.mark.parametrize("phrase", [
    "объясни этот код", "не запускай файл", "удали файл", "",
])
def test_other_speech_does_not_trigger_an_ide_command(phrase):
    assert command_for(phrase) is None
