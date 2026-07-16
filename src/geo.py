"""
Курированный список городов для настройки поиска через веб-интерфейс — без
этого пользователю пришлось бы руками лезть в `dictionaries` и искать числовой
id региона в JSON. Покрывает Москву/Питер и города-миллионники РФ плюс
"вся Россия"; для городов, которых здесь нет, на странице «Настройки» есть
отдельное поле "свой id" (см. settings.html) — HH и SuperJob используют разные
системы id одного и того же города, поэтому один пункт списка хранит оба сразу.
"""
from __future__ import annotations

CITIES = [
    {"name": "Москва", "hh_area": "1", "superjob_town": "4"},
    {"name": "Санкт-Петербург", "hh_area": "2", "superjob_town": "14"},
    {"name": "Новосибирск", "hh_area": "4", "superjob_town": "13"},
    {"name": "Екатеринбург", "hh_area": "3", "superjob_town": "33"},
    {"name": "Казань", "hh_area": "88", "superjob_town": "55"},
    {"name": "Нижний Новгород", "hh_area": "66", "superjob_town": "12"},
    {"name": "Челябинск", "hh_area": "104", "superjob_town": "106"},
    {"name": "Красноярск", "hh_area": "54", "superjob_town": "130"},
    {"name": "Самара", "hh_area": "78", "superjob_town": "5"},
    {"name": "Уфа", "hh_area": "99", "superjob_town": "173"},
    {"name": "Ростов-на-Дону", "hh_area": "76", "superjob_town": "73"},
    {"name": "Омск", "hh_area": "68", "superjob_town": "17"},
    {"name": "Краснодар", "hh_area": "53", "superjob_town": "25"},
    {"name": "Воронеж", "hh_area": "26", "superjob_town": "42"},
    {"name": "Пермь", "hh_area": "72", "superjob_town": "119"},
    {"name": "Волгоград", "hh_area": "24", "superjob_town": "89"},
    {"name": "Вся Россия", "hh_area": "113", "superjob_town": ""},
]


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
