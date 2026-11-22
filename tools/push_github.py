"""Безопасный ``git push`` на GitHub без утечки токена.

Токен берётся из переменных окружения ``GITHUB_TOKEN`` / ``GH_TOKEN`` или из
``config/github_auth.json`` (файл в ``.gitignore``) и передаётся git только
через окружение (``GIT_CONFIG_*`` -> ``http.extraheader``). Поэтому секрет:

* не попадает в аргументы командной строки (виден в списке процессов),
* не пишется в ``.git/config`` (remote остаётся чистым URL),
* не выводится на экран — весь вывод git маскируется.

Примеры:
    python tools/push_github.py
    python tools/push_github.py --branch feature/x --remote origin
    python tools/push_github.py -- --force-with-lease

Проверка гигиены токена:
    git check-ignore -v config/github_auth.json
    git log --all -- config/github_auth.json
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AUTH_FILE = ROOT / "config" / "github_auth.json"


def load_token() -> str | None:
    """Токен из окружения или локального (игнорируемого git) файла."""
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    try:
        data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    token = data.get("token") if isinstance(data, dict) else None
    return token.strip() if isinstance(token, str) and token.strip() else None


def basic_auth(token: str) -> str:
    """base64 от ``x-access-token:<token>`` для заголовка Authorization."""
    return base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")


def mask(text: str, token: str | None) -> str:
    """Убирает токен и учётные данные из URL в произвольном тексте."""
    if not text:
        return text
    masked = str(text)
    if token:
        masked = masked.replace(token, "***").replace(basic_auth(token), "***")
    return re.sub(r"(https?://)[^/\s@]+@", r"\1***@", masked)


def git(args: list[str], token: str | None, with_auth: bool) -> tuple[int, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if with_auth and token:
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
        env["GIT_CONFIG_VALUE_0"] = "Authorization: Basic " + basic_auth(token)
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        return 1, f"Не удалось запустить git: {exc}"
    return proc.returncode, mask((proc.stdout or "").strip(), token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="git push с токеном вне .git/config")
    parser.add_argument("--remote", default="origin", help="имя remote (по умолчанию origin)")
    parser.add_argument("--branch", default=None, help="ветка; по умолчанию текущая")
    parser.add_argument("--dry-run", action="store_true", help="только показать команду")
    parser.add_argument("git_args", nargs="*", help="дополнительные аргументы git push")
    args = parser.parse_args(argv)

    token = load_token()
    if not token:
        print("Токен не найден: задайте GITHUB_TOKEN или создайте config/github_auth.json")
        return 2

    # Гигиена: убеждаемся, что в remote не сохранён секрет.
    code, remote_url = git(["remote", "get-url", args.remote], token, with_auth=False)
    if code != 0:
        print(mask(f"Не удалось прочитать remote {args.remote}: {remote_url}", token))
        return 1
    if "@" in remote_url.split("://", 1)[-1].split("/", 1)[0]:
        clean = re.sub(r"(https?://)[^/\s@]+@", r"\1", remote_url)
        print("ВНИМАНИЕ: в URL remote сохранены учётные данные. Исправьте:\n"
              f"  git remote set-url {args.remote} {clean}")

    branch = args.branch
    if not branch:
        code, out = git(["rev-parse", "--abbrev-ref", "HEAD"], token, with_auth=False)
        branch = out if code == 0 and out and out != "HEAD" else "main"

    push_args = ["push", args.remote, f"HEAD:{branch}", *args.git_args]
    if args.dry_run:
        print(f"git {' '.join(push_args)}  (токен передан бы через env, не в аргументах)")
        return 0

    code, out = git(push_args, token, with_auth=True)
    print(out or ("Push выполнен." if code == 0 else "git push завершился с ошибкой."))
    return code


if __name__ == "__main__":
    sys.exit(main())
