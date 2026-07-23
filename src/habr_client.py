"""
Клиент для Хабр Карьеры (career.habr.com) — третий источник вакансий вдобавок
к hh.ru и SuperJob.

Авторизация не нужна: у Хабра есть открытый frontend-JSON API поиска
(GET /api/frontend/vacancies?q=...&city_id=...&page=...), отдающий вакансии
списком с зарплатой/навыками/компанией. Единственное требование — обычный
браузерный User-Agent (без него отдаёт не-JSON). Проверено вживую перед
написанием файла.

Полное описание вакансии frontend-API в списке не отдаёт, поэтому get_vacancy()
берёт страницу /vacancies/{id} и достаёт из неё блок schema.org JobPosting
(<script type="application/ld+json">) — там лежит description (HTML). В этом
описании Хабр сам перечисляет навыки/квалификацию/специализацию текстом, так
что для скоринга его достаточно.

normalize() приводит ответ к той же форме словаря, что и HH API — как и
superjob_client.normalize() — чтобы scorer/storage/docx работали для всех трёх
источников без source-специфичных веток.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger("habr_client")

BASE_URL = "https://career.habr.com"
ID_PREFIX = "hc"
# frontend-API без нормального UA отдаёт HTML/403 вместо JSON
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"

_LD_JSON_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
_CURRENCY_MAP = {"rur": "RUR", "rub": "RUR", "usd": "USD", "eur": "EUR"}


class HabrApiError(RuntimeError):
    pass


@dataclass
class HabrConfig:
    # у Хабра нет ключей — конфиг оставлен для единообразия с hh/superjob и на
    # случай, если позже понадобится переопределить город/UA
    city_id: str | None = None


def prefixed_id(raw_id: int | str) -> str:
    return f"{ID_PREFIX}{raw_id}"


def is_habr_id(vacancy_id: str) -> bool:
    return vacancy_id.startswith(ID_PREFIX)


def native_id(vacancy_id: str) -> str:
    """Числовой id Хабра без нашего префикса — то, что реально нужно в URL."""
    return vacancy_id[len(ID_PREFIX):] if is_habr_id(vacancy_id) else vacancy_id


def _salary(raw_salary: dict | None) -> dict | None:
    raw_salary = raw_salary or {}
    lo, hi = raw_salary.get("from"), raw_salary.get("to")
    if not lo and not hi:
        return None
    cur = (raw_salary.get("currency") or "").lower()
    return {"from": lo, "to": hi, "currency": _CURRENCY_MAP.get(cur, cur.upper()), "gross": True}


def normalize(raw: dict) -> dict:
    """Элемент списка frontend-API → форма HH. description здесь пустой — его
    добирает get_vacancy() со страницы вакансии (см. модуль-docstring)."""
    company = raw.get("company") or {}
    locations = raw.get("locations") or []
    skills = raw.get("skills") or []
    href = raw.get("href") or f"/vacancies/{raw.get('id')}"
    published_at = (raw.get("publishedDate") or {}).get("date")
    skill_names = [s.get("title") for s in skills if s.get("title")]
    snippet = raw.get("title") or ""
    if skill_names:
        snippet = f"{snippet}. Навыки: {', '.join(skill_names)}"
    return {
        "id": prefixed_id(raw["id"]),
        "name": raw.get("title"),
        "employer": {"name": company.get("title")},
        "area": {"name": locations[0].get("title")} if locations else {},
        "salary": _salary(raw.get("salary")),
        "url": BASE_URL + href,
        "alternate_url": BASE_URL + href,
        "published_at": published_at,
        "snippet": {"requirement": snippet, "responsibility": None},
        # у Хабра нет метро — как и у части вакансий HH; get_metro вернёт (None, None)
        "address": {},
        # remoteWork — единственный «график», который Хабр отдаёт в списке
        "schedule": {"name": "Можно удалённо"} if raw.get("remoteWork") else {},
        # квалификация Хабра (Junior/Middle/Senior) — ближайший аналог «опыта»
        "experience": {"name": (raw.get("salaryQualification") or {}).get("title")},
        "key_skills": [{"name": n} for n in skill_names],
        "description": None,
        "archived": bool(raw.get("archived")),
    }


def _normalize_detail(native: str, ld: dict) -> dict:
    """schema.org JobPosting со страницы вакансии → форма HH (для get_vacancy).
    В ld+json Хабра гарантированно есть title/description/datePosted; остальное —
    best-effort, недостающие поля просто отсутствуют (vacancy_to_text через .get)."""
    org = ld.get("hiringOrganization") or {}
    base = (ld.get("baseSalary") or {}).get("value") or {}
    lo, hi = base.get("minValue"), base.get("maxValue")
    salary = None
    if lo or hi:
        cur = ((ld.get("baseSalary") or {}).get("currency") or "RUR")
        salary = {"from": lo, "to": hi, "currency": _CURRENCY_MAP.get(str(cur).lower(), str(cur).upper()), "gross": True}
    loc = ld.get("jobLocation") or {}
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    # schema.org допускает address как объектом PostalAddress, так и просто строкой
    addr = loc.get("address") if isinstance(loc, dict) else None
    if isinstance(addr, dict):
        locality = addr.get("addressLocality")
    elif isinstance(addr, str):
        locality = addr
    else:
        locality = None
    return {
        "id": prefixed_id(native),
        "name": ld.get("title"),
        "employer": {"name": org.get("name")},
        "area": {"name": locality} if locality else {},
        "salary": salary,
        "url": f"{BASE_URL}/vacancies/{native}",
        "alternate_url": f"{BASE_URL}/vacancies/{native}",
        "published_at": ld.get("datePosted"),
        "snippet": {"requirement": None, "responsibility": None},
        "address": {},
        "schedule": {},
        "experience": {},
        "key_skills": [],
        "description": ld.get("description"),
        "archived": False,
    }


class HabrClient:
    def __init__(self, cfg: HabrConfig | None = None):
        self.cfg = cfg or HabrConfig()
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _USER_AGENT})

    def search_vacancies(self, query: str, city_id: str | None = None, max_pages: int = 4) -> list[dict]:
        """Поиск по одному запросу, постраничный проход. Возвращает
        НОРМАЛИЗОВАННЫЕ (в форме HH) вакансии."""
        results: list[dict] = []
        page = 1
        while page <= max_pages:
            params: dict[str, Any] = {"q": query, "page": page}
            if city_id:
                params["city_id"] = city_id
            resp = self._session.get(f"{BASE_URL}/api/frontend/vacancies", params=params, timeout=20)
            if resp.status_code >= 400:
                raise HabrApiError(f"Хабр Карьера API {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            items = data.get("list", [])
            results.extend(normalize(v) for v in items)
            meta = data.get("meta") or {}
            log.info("  страница %s/%s, найдено на странице: %s", page, meta.get("totalPages", "?"), len(items))
            if page >= meta.get("totalPages", page):
                break
            page += 1
            time.sleep(0.3)
        return results

    def _fetch_ld_json(self, native: str) -> tuple[dict | None, int]:
        resp = self._session.get(f"{BASE_URL}/vacancies/{native}", timeout=20)
        if resp.status_code == 404:
            return None, 404
        if resp.status_code >= 400:
            raise HabrApiError(f"Хабр Карьера {resp.status_code} на /vacancies/{native}: {resp.text[:200]}")
        m = _LD_JSON_RE.search(resp.text)
        if not m:
            return None, resp.status_code
        try:
            return json.loads(m.group(1)), resp.status_code
        except json.JSONDecodeError:
            return None, resp.status_code

    def get_vacancy(self, vacancy_native_id: str) -> dict:
        """Полная карточка по числовому id Хабра (без префикса hc) — description
        из schema.org JobPosting на странице вакансии."""
        ld, status = self._fetch_ld_json(vacancy_native_id)
        if status == 404:
            raise HabrApiError(f"Хабр Карьера 404 на /vacancies/{vacancy_native_id}: вакансия не найдена")
        if ld is None:
            raise HabrApiError(f"На странице /vacancies/{vacancy_native_id} не найден блок JobPosting")
        return _normalize_detail(vacancy_native_id, ld)

    def get_vacancy_status(self, vacancy_native_id: str) -> dict:
        """Проверка актуальности — тот же формат, что и у HHClient. У Хабра нет
        флага архива в ld+json; закрытые/снятые вакансии отдают 404."""
        resp = self._session.get(f"{BASE_URL}/vacancies/{vacancy_native_id}", timeout=20)
        if resp.status_code == 404:
            return {"found": False, "archived": True}
        if resp.status_code >= 400:
            raise HabrApiError(f"Хабр Карьера {resp.status_code} на /vacancies/{vacancy_native_id}")
        return {"found": True, "archived": False}
