"""
Клиент для HH API.

Использует client_credentials grant — токен приложения, БЕЗ входа
в личный аккаунт на hh.ru. Этого достаточно для публичного поиска
вакансий (GET /vacancies). Отклики (/negotiations) сюда сознательно
не включены: это требует персональной OAuth-авторизации и решения
человека, а не скрипта.
"""
from __future__ import annotations

import json
import time
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("hh_client")

BASE_URL = "https://api.hh.ru"


class HHApiError(RuntimeError):
    pass


@dataclass
class HHConfig:
    client_id: str
    client_secret: str
    user_agent: str
    # каждый запуск CLI — новый процесс, поэтому токен кэшируется на диск между
    # запусками. HH ограничивает частоту выдачи НОВОГО токена приложения
    # ("app token refresh too early") — без кэша fetch → score впритык друг за
    # другом гарантированно ловит эту ошибку на каждой вакансии.
    token_cache_path: str = ".hh_token_cache.json"


class HHClient:
    def __init__(self, cfg: HHConfig):
        self.cfg = cfg
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._session = requests.Session()
        self._session.headers.update({"HH-User-Agent": cfg.user_agent})
        self._load_cached_token()

    # ---- аутентификация ----

    def _load_cached_token(self) -> None:
        path = Path(self.cfg.token_cache_path)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("expires_at", 0) > time.time():
                self._token = data["access_token"]
                self._token_expires_at = data["expires_at"]
        except (json.JSONDecodeError, KeyError, OSError):
            pass  # повреждённый/неполный кэш — просто получим новый токен

    def _save_cached_token(self) -> None:
        path = Path(self.cfg.token_cache_path)
        try:
            path.write_text(
                json.dumps({"access_token": self._token, "expires_at": self._token_expires_at}),
                encoding="utf-8",
            )
        except OSError as e:
            log.warning("Не удалось сохранить кэш токена HH в %s: %s", path, e)

    def _fetch_app_token(self) -> None:
        resp = self._session.post(
            f"{BASE_URL}/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.cfg.client_id,
                "client_secret": self.cfg.client_secret,
            },
            timeout=20,
        )
        if resp.status_code != 200:
            raise HHApiError(
                f"Не удалось получить токен приложения ({resp.status_code}): {resp.text[:300]}\n"
                "Проверь client_id/client_secret в config.yaml (см. https://dev.hh.ru/admin)."
            )
        data = resp.json()
        self._token = data["access_token"]
        # HH обычно не отдаёт expires_in для client_credentials — подстрахуемся коротким TTL
        self._token_expires_at = time.time() + data.get("expires_in", 3600) - 60
        self._save_cached_token()

    def _ensure_token(self) -> None:
        if not self._token or time.time() >= self._token_expires_at:
            self._fetch_app_token()
        self._session.headers["Authorization"] = f"Bearer {self._token}"

    # ---- запросы ----

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        self._ensure_token()
        resp = self._session.get(f"{BASE_URL}{path}", params=params, timeout=20)
        if resp.status_code == 403:
            raise HHApiError(
                "403 от HH API. Обычно значит: неверный/просроченный токен, "
                "либо не передан обязательный заголовок HH-User-Agent."
            )
        if resp.status_code >= 400:
            raise HHApiError(f"HH API {resp.status_code} на {path}: {resp.text[:300]}")
        return resp.json()

    def search_vacancies(self, **params) -> list[dict]:
        """Поиск по одному запросу с постраничным проходом. Возвращает список 'сырых' вакансий."""
        max_pages = params.pop("max_pages", 4)
        results: list[dict] = []
        page = 0
        while page < max_pages:
            data = self._get("/vacancies", {**params, "page": page})
            items = data.get("items", [])
            results.extend(items)
            pages_total = data.get("pages", 1)
            log.info("  страница %s/%s, найдено на странице: %s", page + 1, pages_total, len(items))
            page += 1
            if page >= pages_total:
                break
            time.sleep(0.3)  # вежливая пауза между страницами
        return results

    def get_vacancy(self, vacancy_id: str) -> dict:
        """Полная карточка вакансии, включая description — это то, что нужно скорингу и тейлору."""
        return self._get(f"/vacancies/{vacancy_id}")

    def get_vacancy_status(self, vacancy_id: str) -> dict:
        """Проверка актуальности без выброса ошибки на 404 (вакансия удалена совсем).

        Возвращает {"found": bool, "archived": bool}. archived=True и для 404
        (вакансии совсем нет — точно не откликнуться), и для archived=true в
        ответе HH (формально ещё существует, но снята с публикации)."""
        self._ensure_token()
        resp = self._session.get(f"{BASE_URL}/vacancies/{vacancy_id}", timeout=20)
        if resp.status_code == 404:
            return {"found": False, "archived": True}
        if resp.status_code >= 400:
            raise HHApiError(f"HH API {resp.status_code} на /vacancies/{vacancy_id}: {resp.text[:300]}")
        data = resp.json()
        return {"found": True, "archived": bool(data.get("archived"))}

    def get_dictionaries(self) -> dict:
        return self._get("/dictionaries")

    def get_areas(self) -> list[dict]:
        return self._get("/areas")

    def get_professional_roles(self) -> dict:
        return self._get("/professional_roles")
