"""
Проверка, есть ли в апстрим-репозитории версия новее задеплоенной.

Для open-source-распространения: у каждого инстанса локальный файл VERSION
(версия сборки, формат "ГГГГ-ММ-ДД" — дата релиза, при нескольких релизах в
день можно дописать ".1", ".2"), а в репозитории на GitHub — свой VERSION.
Если удалённая версия новее локальной — в веб-интерфейсе загорается баннер
«доступно обновление» и кнопка «Настройки» становится жирной с «(!)»
(см. base.html/settings.html), а номер сборки виден в футере.

Сравнение — по кортежу чисел (2026-07-28 → (2026,7,28)), так что работают и
даты, и старые целочисленные номера сборок, и суффиксы вроде "-28.1".

Результат кэшируется в settings-таблице на несколько часов, чтобы не ходить в
GitHub на каждый рендер страницы (и не блокировать страницу таймаутом, если
GitHub недоступен — кэшируется и неудачная попытка). Любая ошибка сети/парсинга
трактуется как «обновления нет»: фича чисто информационная, ничего не ломает.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import requests

log = logging.getLogger("updates")

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
_TTL_SECONDS = 6 * 3600
_CACHE_KEY = "update_check_cache"
# версия — только цифры, точки и дефисы (даты/номера сборок); всё прочее (напр.
# HTML-страница ошибки от CDN с кодом 200) не считаем валидной версией
_VERSION_RE = re.compile(r"^[0-9.\-]{1,32}$")


def local_version() -> str | None:
    try:
        v = _VERSION_FILE.read_text(encoding="utf-8").strip()
        return v or None
    except OSError:
        return None


def _parse(v: str | None) -> tuple[int, ...]:
    """Версию в кортеж чисел для сравнения: "2026-07-28" → (2026, 7, 28),
    "2026-07-28.1" → (2026, 7, 28, 1), старое целое "2" → (2,)."""
    if not v:
        return ()
    return tuple(int(x) for x in re.findall(r"\d+", v))


def check_for_update(storage, cfg: dict) -> dict:
    """{"available": bool, "local": str|None, "remote": str|None}. Кэшируется
    на _TTL_SECONDS; при ошибке сети возвращает последнее известное (или «нет»)."""
    local = local_version()
    url = (cfg.get("update_check") or {}).get("version_url")
    if not url or local is None:
        return {"available": False, "local": local, "remote": None}

    now = time.time()
    cache: dict = {}
    raw = storage.get_setting(_CACHE_KEY)
    if raw:
        try:
            cache = json.loads(raw)
        except json.JSONDecodeError:
            cache = {}

    if now - cache.get("ts", 0) >= _TTL_SECONDS:
        remote = cache.get("remote")  # на неудаче оставляем последнее известное
        try:
            resp = requests.get(url, timeout=6)
            resp.raise_for_status()
            candidate = resp.text.strip()
            remote = candidate if _VERSION_RE.match(candidate) else remote
        except requests.RequestException as e:
            log.info("Проверка обновлений не удалась: %s", e)
        cache = {"ts": now, "remote": remote}
        storage.set_setting(_CACHE_KEY, json.dumps(cache))

    remote = cache.get("remote")
    available = bool(remote) and _parse(remote) > _parse(local)
    return {"available": available, "local": local, "remote": remote}
