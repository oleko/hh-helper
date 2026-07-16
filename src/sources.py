"""
Общая точка входа для мультиисточниковости (hh.ru + SuperJob): по строке
`source` из БД выбирает нужный клиент, а get_full_vacancy/get_vacancy_status
скрывают разницу в API от остального кода (main.py, webapp.py) — им нужно
только "дай вакансию по этому id", не зная, у кого она физически.

parse_vacancy_url() — для инструмента "вставь ссылку на вакансию": достаёт
(source, native_id) из ссылки на hh.ru или superjob.ru.
"""
from __future__ import annotations

import re

from .hh_client import HHClient
from .superjob_client import SuperJobClient
from .superjob_client import native_id as sj_native_id

_HH_URL_RE = re.compile(r"hh\.ru/vacancy/(\d+)")
# ссылка superjob.ru всегда оканчивается на "...-<id>.html"
_SJ_URL_RE = re.compile(r"superjob\.ru/vakansii/.*-(\d+)\.html")


def parse_vacancy_url(url: str) -> tuple[str, str] | None:
    """Возвращает (source, native_id) по ссылке на вакансию hh.ru/superjob.ru,
    либо None, если ссылка не распознана ни одним из источников."""
    m = _HH_URL_RE.search(url)
    if m:
        return "hh", m.group(1)
    m = _SJ_URL_RE.search(url)
    if m:
        return "superjob", m.group(1)
    return None


def get_full_vacancy(hh: HHClient, sj: SuperJobClient, vacancy_id: str, source: str | None) -> dict:
    if source == "superjob":
        return sj.get_vacancy(sj_native_id(vacancy_id))
    return hh.get_vacancy(vacancy_id)


def get_vacancy_status(hh: HHClient, sj: SuperJobClient, vacancy_id: str, source: str | None) -> dict:
    if source == "superjob":
        return sj.get_vacancy_status(sj_native_id(vacancy_id))
    return hh.get_vacancy_status(vacancy_id)
