"""
Проверка, есть ли в апстрим-репозитории версия новее задеплоенной.

Для open-source-распространения: у каждого инстанса локальный файл VERSION
(целое число сборки), а в репозитории на GitHub — свой VERSION. Если удалённый
номер больше локального — в веб-интерфейсе загорается баннер «доступно
обновление» и кнопка «Настройки» становится жирной с «(!)» (см. base.html/
settings.html). Номер сборки бампается вручную при релизе (одна строка).

Результат кэшируется в settings-таблице на несколько часов, чтобы не ходить в
GitHub на каждый рендер страницы (и не блокировать страницу таймаутом, если
GitHub недоступен — кэшируется и неудачная попытка). Любая ошибка сети/парсинга
трактуется как «обновления нет»: фича чисто информационная, ничего не ломает.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import requests

log = logging.getLogger("updates")

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
_TTL_SECONDS = 6 * 3600
_CACHE_KEY = "update_check_cache"


def local_version() -> int | None:
    try:
        return int(_VERSION_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def check_for_update(storage, cfg: dict) -> dict:
    """{"available": bool, "local": int|None, "remote": int|None}. Кэшируется
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
            remote = int(resp.text.strip())
        except (requests.RequestException, ValueError) as e:
            log.info("Проверка обновлений не удалась: %s", e)
        cache = {"ts": now, "remote": remote}
        storage.set_setting(_CACHE_KEY, json.dumps(cache))

    remote = cache.get("remote")
    return {"available": bool(remote and local is not None and remote > local), "local": local, "remote": remote}
