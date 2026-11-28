"""Offline push-to-talk speech input and explicit CodeLane voice commands."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path


COMMANDS = {
    "открой настройки": "action_open_settings",
    "открыть настройки": "action_open_settings",
    "запусти файл": "action_run_file",
    "запустить файл": "action_run_file",
    "сохрани файл": "action_save_file",
    "сохранить файл": "action_save_file",
    "новый файл": "action_new_file",
    "создай файл": "action_new_file",
    "открой файл": "action_quick_open",
    "найди файл": "action_quick_open",
    "палитра команд": "show_command_palette",
    "покажи терминал": "toggle_terminal",
    "скрой терминал": "toggle_terminal",
}


def command_for(text: str) -> str | None:
    """Only exact, explicit phrases control the workbench; other speech goes to chat."""
    normalized = re.sub(r"\s+", " ", text.strip().lower().strip(".!? "))
    normalized = re.sub(r"^(?:джарвис|jarvis)[, ]+", "", normalized)
    return COMMANDS.get(normalized)


MODEL_NAME = "vosk-model-small-ru-0.22"


def model_path() -> Path:
    """The native Vosk library needs a model path without Cyrillic characters."""
    override = os.environ.get("CODELANE_VOSK_MODEL")
    if override:
        return Path(override)
    program_data = Path(os.environ.get("PROGRAMDATA", "C:/ProgramData"))
    return program_data / "CodeLane" / "models" / MODEL_NAME


def recognize_once(cancelled=None) -> str:
    """Listen to one utterance from the default microphone, for at most 8 s."""
    try:
        import sounddevice as sd
        import vosk
    except ImportError as exc:
        raise RuntimeError("Установите зависимости голосового ввода: vosk и sounddevice.") from exc

    path = model_path()
    if not path.is_dir():
        raise RuntimeError(f"Русская модель распознавания не найдена: {path}")
    vosk.SetLogLevel(-1)
    try:
        recognizer = vosk.KaldiRecognizer(vosk.Model(str(path)), 16000)
        with sd.RawInputStream(samplerate=16000, blocksize=4000, channels=1, dtype="int16") as stream:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if cancelled is not None and cancelled():
                    return ""
                data, _overflow = stream.read(4000)
                if recognizer.AcceptWaveform(bytes(data)):
                    phrase = json.loads(recognizer.Result()).get("text", "").strip()
                    if phrase:
                        return phrase
            return json.loads(recognizer.FinalResult()).get("text", "").strip()
    except Exception as exc:
        raise RuntimeError(f"Не удалось записать или распознать речь: {exc}") from exc
