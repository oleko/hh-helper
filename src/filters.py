"""
Общие фильтры поиска (опыт/занятость/график) для обоих источников. hh.ru и
SuperJob используют разные параметры и разные системы id для одних и тех же
понятий, поэтому у каждого варианта фильтра сразу два сопоставления — hh.ru
принимает несколько значений одного параметра через повторение ключа в
query-string (?experience=between1And3&experience=between3And6, задокументированное
поведение их API для мультивыбора); SuperJob такой возможности не документирует,
поэтому туда уходит только первое выбранное значение в каждой категории.

Schedule — единственная категория, где сама структура категорий у провайдеров
не совпадает: у HH это один параметр `schedule`, а у SuperJob "удалённая
работа" и "вахта" относятся к РАЗНЫМ параметрам (`place_of_work` и
`type_of_work` соответственно — второй тот же параметр, что и у "сменного
графика", но с другим значением). Поэтому у вариантов schedule есть свой
sj_param; у вариантов без чёткого соответствия на SuperJob (полный день,
гибкий график) sj_param/sj = None — фильтр в этом случае подставляется
только на hh.ru, на SuperJob эта категория для таких вариантов не сужает поиск.
"""
from __future__ import annotations

EXPERIENCE_OPTIONS = [
    {"key": "no_experience", "label": "Без опыта", "hh": "noExperience", "sj": 1},
    {"key": "1_3", "label": "1–3 года", "hh": "between1And3", "sj": 2},
    {"key": "3_6", "label": "3–6 лет", "hh": "between3And6", "sj": 3},
    {"key": "6_plus", "label": "Более 6 лет", "hh": "moreThan6", "sj": 4},
]

EMPLOYMENT_OPTIONS = [
    {"key": "full", "label": "Полная занятость", "hh": "full", "sj": 6},
    {"key": "part", "label": "Частичная занятость", "hh": "part", "sj": 13},
    {"key": "project", "label": "Проектная работа", "hh": "project", "sj": None},
    {"key": "probation", "label": "Стажировка", "hh": "probation", "sj": None},
]

SCHEDULE_OPTIONS = [
    {"key": "full_day", "label": "Полный день", "hh": "fullDay", "sj_param": None, "sj": None},
    {"key": "shift", "label": "Сменный график", "hh": "shift", "sj_param": "type_of_work", "sj": 12},
    {"key": "flexible", "label": "Гибкий график", "hh": "flexible", "sj_param": None, "sj": None},
    {"key": "remote", "label": "Удалённая работа", "hh": "remote", "sj_param": "place_of_work", "sj": 2},
    {"key": "fly_in_fly_out", "label": "Вахта", "hh": "flyInFlyOut", "sj_param": "type_of_work", "sj": 9},
]


def hh_values(options: list[dict], selected_keys: list[str]) -> list[str]:
    return [o["hh"] for o in options if o["key"] in selected_keys]


def sj_experience_or_employment(options: list[dict], selected_keys: list[str]) -> int | None:
    """SuperJob не поддерживает мультивыбор в этих категориях — берём первый
    выбранный вариант, у которого вообще есть sj-соответствие."""
    for o in options:
        if o["key"] in selected_keys and o.get("sj") is not None:
            return o["sj"]
    return None


def sj_schedule_params(selected_keys: list[str]) -> dict[str, int]:
    """У schedule на SuperJob разные варианты бьют в разные параметры — собираем
    все применимые в один dict (например {"type_of_work": 12, "place_of_work": 2},
    если выбраны и "сменный график", и "удалённая работа" одновременно)."""
    result: dict[str, int] = {}
    for o in SCHEDULE_OPTIONS:
        if o["key"] in selected_keys and o.get("sj_param") and o.get("sj") is not None:
            result[o["sj_param"]] = o["sj"]
    return result
