"""GitHub integration and authentication service for CodeLane."""
import base64
import json
import os
import re
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List


class GitHubService:
    """Manages GitHub authentication, personal access tokens, and repository operations."""
    
    _instance = None

    def __init__(self, config_dir: Optional[Path] = None):
        if config_dir is None:
            self.config_dir = Path(__file__).resolve().parent.parent / "config"
        else:
            self.config_dir = Path(config_dir)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.auth_file = self.config_dir / "github_auth.json"
        self._user_data: Optional[Dict[str, Any]] = None
        self._load_auth()

    @classmethod
    def get_instance(cls) -> "GitHubService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _load_auth(self):
        if self.auth_file.exists():
            try:
                self._user_data = json.loads(self.auth_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._user_data = None
        else:
            self._user_data = None

    def _save_auth(self, data: Dict[str, Any]):
        self._user_data = data
        try:
            self.auth_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def is_authenticated(self) -> bool:
        return bool(self._user_data and self._user_data.get("token"))

    def get_token(self) -> Optional[str]:
        return self._user_data.get("token") if self._user_data else None

    def get_user_info(self) -> Optional[Dict[str, Any]]:
        return self._user_data

    def logout(self):
        self._user_data = None
        if self.auth_file.exists():
            try:
                self.auth_file.unlink()
            except OSError:
                pass

    def authenticate_with_token(self, token: str) -> Tuple[bool, Optional[Dict[str, Any]], str]:
        """Verify token against GitHub API and persist user profile."""
        token = token.strip()
        if not token:
            return False, None, "Введите Personal Access Token."

        url = "https://api.github.com/user"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "CodeLane-IDE-Client"
            }
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    user_info = {
                        "token": token,
                        "login": data.get("login", ""),
                        "name": data.get("name") or data.get("login", ""),
                        "avatar_url": data.get("avatar_url", ""),
                        "html_url": data.get("html_url", ""),
                        "email": data.get("email", ""),
                        "public_repos": data.get("public_repos", 0),
                        "total_private_repos": data.get("total_private_repos", 0)
                    }
                    self._save_auth(user_info)
                    return True, user_info, f"Вы успешно вошли как @{user_info['login']}!"
                return False, None, f"Ошибка ответа GitHub: {resp.status}"
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return False, None, "Неверный токен (401 Unauthorized). Проверьте правильность токена и его права (scopes: repo)."
            if e.code == 403:
                return False, None, "Доступ ограничен или превышен лимит запросов GitHub API (403 Forbidden)."
            return False, None, f"Ошибка GitHub API: HTTP {e.code}"
        except urllib.error.URLError as e:
            return False, None, f"Не удалось подключиться к GitHub: {e.reason}"
        except Exception as e:
            return False, None, f"Неожиданная ошибка авторизации: {e}"

    def list_user_repos(self) -> Tuple[bool, List[Dict[str, Any]], str]:
        """Fetch current user's repositories from GitHub."""
        token = self.get_token()
        if not token:
            return False, [], "Требуется авторизация на GitHub."

        url = "https://api.github.com/user/repos?sort=updated&per_page=100&affiliation=owner,collaborator"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "CodeLane-IDE-Client"
            }
        )

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status == 200:
                    repos = json.loads(resp.read().decode("utf-8"))
                    result = []
                    for r in repos:
                        result.append({
                            "name": r.get("name", ""),
                            "full_name": r.get("full_name", ""),
                            "html_url": r.get("html_url", ""),
                            "clone_url": r.get("clone_url", ""),
                            "private": r.get("private", False),
                            "description": r.get("description") or "Без описания",
                            "default_branch": r.get("default_branch", "main"),
                            "updated_at": r.get("updated_at", "")[:10]
                        })
                    return True, result, f"Загружено {len(result)} репозиториев."
                return False, [], f"Ошибка получения репозиториев: HTTP {resp.status}"
        except Exception as e:
            return False, [], f"Ошибка загрузки репозиториев: {e}"

    def create_repository(self, name: str, description: str = "", is_private: bool = False) -> Tuple[bool, Optional[Dict[str, Any]], str]:
        """Create a new repository on GitHub."""
        token = self.get_token()
        if not token:
            return False, None, "Требуется авторизация на GitHub."

        name = name.strip()
        if not name:
            return False, None, "Введите имя репозитория."

        url = "https://api.github.com/user/repos"
        payload = json.dumps({
            "name": name,
            "description": description.strip(),
            "private": bool(is_private),
            "auto_init": False
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"token {token}",
                "Content-Type": "application/json",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "CodeLane-IDE-Client"
            },
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status in {200, 201}:
                    data = json.loads(resp.read().decode("utf-8"))
                    return True, data, f"Репозиторий {name} успешно создан на GitHub!"
                return False, None, f"Ошибка создания репозитория: HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            err_msg = ""
            try:
                err_data = json.loads(e.read().decode("utf-8"))
                err_msg = err_data.get("message", "")
            except Exception:
                pass
            return False, None, f"Не удалось создать репозиторий: {err_msg or e.code}"
        except Exception as e:
            return False, None, f"Ошибка: {e}"

    def get_auth_git_env(self) -> Dict[str, str]:
        """Environment for git: no interactive prompts, token passed out of band.

        Токен передаётся только через переменные окружения (GIT_CONFIG_* →
        http.extraheader), поэтому он не попадает ни в аргументы процесса,
        ни в `.git/config`, ни в вывод git. GITHUB_TOKEN/GH_TOKEN оставлены
        для совместимости с gh CLI.
        """
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        token = self.get_token()
        if not token:
            return env
        env["GITHUB_TOKEN"] = token
        env["GH_TOKEN"] = token
        # git >= 2.31 читает конфиг из окружения: секрет не пишется в файл.
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
        env["GIT_CONFIG_VALUE_0"] = "Authorization: Basic " + self._basic_auth_value(token)
        return env

    @staticmethod
    def _basic_auth_value(token: str) -> str:
        """base64 от 'x-access-token:<token>' для заголовка Authorization."""
        return base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")

    def mask_secrets(self, text: str) -> str:
        """Маскирует токен в произвольном тексте (вывод git, ошибки, логи)."""
        if not text:
            return text
        masked = str(text)
        token = self.get_token()
        if token:
            masked = masked.replace(token, "***")
            masked = masked.replace(self._basic_auth_value(token), "***")
        # https://<user>:<pass>@host и https://<token>@host
        return re.sub(r"(https?://)[^/\s@]+@", r"\1***@", masked)
