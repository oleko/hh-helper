"""
Клиент для SuperJob API — второй источник вакансий вдобавок к hh.ru.

Публичный поиск вакансий не требует OAuth/логина работодателя — достаточно
заголовка X-Api-App-Id с секретным ключом приложения (см. https://api.superjob.ru/,
раздел «Программный интерфейс»). Проверено вживую реальным ключом перед
написанием этого файла: GET /2.0/vacancies/ и GET /2.0/vacancies/{id}/
работают с одним этим заголовком, без токена доступа.

normalize() приводит сырой ответ SuperJob к той же форме словаря, что и HH API
(name/employer/area/salary/snippet/address.metro/key_skills/description) —
благодаря этому scorer.vacancy_to_text/get_metro, docx_export и хранение в БД
работают для обоих источников без единой строчки условной логики на разницу
форматов. Единственное, чего SuperJob не даёт (в отличие от HH) — название
линии метро (только station + внутренний id_metro_line без имени) и
структурированный список ключевых навыков — это ограничения API, не наши.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

log = logging.getLogger("superjob_client")

BASE_URL = "https://api.superjob.ru/2.0"
ID_PREFIX = "sj"


class SuperJobApiError(RuntimeError):
    pass


@dataclass
class SuperJobConfig:
    secret_key: str
    town: int | None = None  # id города в справочнике SuperJob, см. config.yaml


def prefixed_id(raw_id: int | str) -> str:
    """Наш внутренний id вакансии в БД — с префиксом, чтобы не столкнуться
    с числовым id вакансии hh.ru (у обоих источников id — просто целые числа
    в пересекающихся диапазонах)."""
    return f"{ID_PREFIX}{raw_id}"


def is_superjob_id(vacancy_id: str) -> bool:
    return vacancy_id.startswith(ID_PREFIX)


def native_id(vacancy_id: str) -> str:
    """Числовой id SuperJob без нашего префикса — то, что реально нужно передавать в API."""
    return vacancy_id[len(ID_PREFIX):] if is_superjob_id(vacancy_id) else vacancy_id


_CURRENCY_MAP = {"rub": "RUR", "uah": "UAH", "uzs": "UZS"}


def normalize(raw: dict) -> dict:
    town = raw.get("town") or {}
    metro_list = raw.get("metro") or []
    metro_title = metro_list[0].get("title") if metro_list else None
    payment_from = raw.get("payment_from") or None
    payment_to = raw.get("payment_to") or None
    published_at = None
    if raw.get("date_published"):
        published_at = datetime.fromtimestamp(raw["date_published"], tz=timezone.utc).isoformat()
    description = "\n\n".join(filter(None, [raw.get("work"), raw.get("candidat"), raw.get("compensation")]))
    return {
        "id": prefixed_id(raw["id"]),
        "name": raw.get("profession"),
        "employer": {"name": raw.get("firm_name")},
        "area": {"name": town.get("title")},
        "salary": (
            {
                "from": payment_from,
                "to": payment_to,
                "currency": _CURRENCY_MAP.get(raw.get("currency"), (raw.get("currency") or "").upper()),
                "gross": True,
            }
            if (payment_from or payment_to)
            else None
        ),
        "url": raw.get("link"),
        "alternate_url": raw.get("link"),
        "published_at": published_at,
        "snippet": {"requirement": raw.get("candidat"), "responsibility": raw.get("work")},
        "address": {"metro": {"station_name": metro_title, "line_name": None}} if metro_title else {},
        "schedule": {"name": (raw.get("type_of_work") or {}).get("title")},
        "experience": {"name": (raw.get("experience") or {}).get("title")},
        # структурированных ключевых навыков SuperJob не отдаёт (в отличие от HH) —
        # ATS-ключевые слова для карточки вакансии теперь в любом случае выделяет
        # модель при скоринге (см. scorer.py: ats_keywords), а не это поле.
        "key_skills": [],
        "description": description or None,
        "archived": bool(raw.get("is_closed")),
    }


class SuperJobClient:
    def __init__(self, cfg: SuperJobConfig):
        self.cfg = cfg
        self._session = requests.Session()
        self._session.headers.update({"X-Api-App-Id": cfg.secret_key})

    def _get(self, path: str, params: dict[str, Any] | None = None) -> tuple[dict, int]:
        resp = self._session.get(f"{BASE_URL}{path}", params=params, timeout=20)
        if resp.status_code >= 400 and resp.status_code != 404:
            raise SuperJobApiError(f"SuperJob API {resp.status_code} на {path}: {resp.text[:300]}")
        return resp.json(), resp.status_code

    def search_vacancies(self, **params) -> list[dict]:
        """Поиск, постраничный проход. Возвращает НОРМАЛИЗОВАННЫЕ (в форме HH) вакансии."""
        max_pages = params.pop("max_pages", 4)
        results: list[dict] = []
        page = 0
        while page < max_pages:
            data, _ = self._get("/vacancies/", {**params, "page": page})
            items = data.get("objects", [])
            results.extend(normalize(v) for v in items)
            log.info("  страница %s, найдено на странице: %s", page + 1, len(items))
            page += 1
            if not data.get("more"):
                break
            time.sleep(0.3)
        return results

    def get_vacancy(self, vacancy_native_id: str) -> dict:
        """Полная карточка вакансии по числовому id SuperJob (без префикса sj)."""
        data, status = self._get(f"/vacancies/{vacancy_native_id}/")
        if status == 404:
            raise SuperJobApiError(f"SuperJob API 404 на /vacancies/{vacancy_native_id}/: вакансия не найдена")
        return normalize(data)

    def get_vacancy_status(self, vacancy_native_id: str) -> dict:
        """Проверка актуальности — тот же формат, что и HHClient.get_vacancy_status."""
        data, status = self._get(f"/vacancies/{vacancy_native_id}/")
        if status == 404:
            return {"found": False, "archived": True}
        return {"found": True, "archived": bool(data.get("is_closed"))}

    def get_towns(self) -> list[dict]:
        """Полный справочник городов SuperJob (id, title) — для страницы поиска
        id города в веб-интерфейсе (см. /tool/areas). `all=1` отдаёт весь список
        одним запросом, без постраничного прохода."""
        data, _ = self._get("/towns/", {"all": 1})
        return data.get("objects", [])
