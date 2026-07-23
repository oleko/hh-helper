"""
Курированный список городов для настройки поиска через веб-интерфейс — без
этого пользователю пришлось бы руками лезть в `dictionaries` и искать числовой
id региона в JSON. Покрывает Москву/Питер и города-миллионники РФ плюс
"вся Россия"; для городов, которых здесь нет, на странице «Настройки» есть
отдельное поле "свой id" (см. settings.html) — hh.ru, SuperJob и Хабр Карьера
используют разные системы id одного и того же города, поэтому один пункт списка
хранит все сразу.

habr_city_id: числовой id из career.habr.com/api/frontend/cities?term=<город>
(поле value вида "c_678" → "678"). Заполнены проверенные вживую; где пусто —
поиск по Хабру идёт без фильтра города (по всей России), это рабочий фолбэк,
можно дозаполнить позже тем же эндпоинтом.
"""
from __future__ import annotations

CITIES = [
    {"name": "Москва", "hh_area": "1", "superjob_town": "4", "habr_city_id": "678"},
    {"name": "Санкт-Петербург", "hh_area": "2", "superjob_town": "14", "habr_city_id": "679"},
    {"name": "Новосибирск", "hh_area": "4", "superjob_town": "13", "habr_city_id": "717"},
    {"name": "Екатеринбург", "hh_area": "3", "superjob_town": "33", "habr_city_id": "693"},
    {"name": "Казань", "hh_area": "88", "superjob_town": "55", "habr_city_id": "698"},
    {"name": "Нижний Новгород", "hh_area": "66", "superjob_town": "12", "habr_city_id": ""},
    {"name": "Челябинск", "hh_area": "104", "superjob_town": "106", "habr_city_id": ""},
    {"name": "Красноярск", "hh_area": "54", "superjob_town": "130", "habr_city_id": ""},
    {"name": "Самара", "hh_area": "78", "superjob_town": "5", "habr_city_id": ""},
    {"name": "Уфа", "hh_area": "99", "superjob_town": "173", "habr_city_id": ""},
    {"name": "Ростов-на-Дону", "hh_area": "76", "superjob_town": "73", "habr_city_id": ""},
    {"name": "Омск", "hh_area": "68", "superjob_town": "17", "habr_city_id": ""},
    {"name": "Краснодар", "hh_area": "53", "superjob_town": "25", "habr_city_id": ""},
    {"name": "Воронеж", "hh_area": "26", "superjob_town": "42", "habr_city_id": ""},
    {"name": "Пермь", "hh_area": "72", "superjob_town": "119", "habr_city_id": ""},
    {"name": "Волгоград", "hh_area": "24", "superjob_town": "89", "habr_city_id": ""},
    {"name": "Вся Россия", "hh_area": "113", "superjob_town": "", "habr_city_id": ""},
]


def habr_city_for_area(hh_area: str) -> str:
    """habr_city_id для города, выбранного через hh_area в /settings; пусто —
    поиск по Хабру без фильтра города (см. cmd_fetch)."""
    c = find_by_area(hh_area)
    return (c or {}).get("habr_city_id", "") if c else ""


def find_by_area(hh_area: str) -> dict | None:
    return next((c for c in CITIES if c["hh_area"] == hh_area), None)


def flatten_hh_areas(nodes: list[dict], parent_name: str | None = None, depth: int = 0) -> list[dict]:
    """HH отдаёт дерево страна → регион → город (глубже 2 уровней редко нужно
    для поиска id). depth 0-1 (страны и их прямые дети — регионы/федеральные
    города) показываем без родителя; глубже — дописываем родителя в скобках
    для однозначности (в одной области может быть несколько похожих названий)."""
    result = []
    for n in nodes:
        label = n["name"] if depth <= 1 else f"{n['name']} ({parent_name})"
        result.append({"id": n["id"], "name": label})
        result.extend(flatten_hh_areas(n.get("areas", []), parent_name=n["name"], depth=depth + 1))
    return result


def flatten_superjob_towns(objects: list[dict]) -> list[dict]:
    return [{"id": str(o["id"]), "name": o["title"]} for o in objects]
