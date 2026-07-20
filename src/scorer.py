"""
Оценка вакансий: сравниваем текст вакансии с карьерной базой и
получаем структурированную оценку (JSON) — насколько подходит,
какой трек (A/B), красные флаги, рекомендация.

Модель просят вернуть ТОЛЬКО JSON, без преамбулы — это парсится
напрямую, без хрупких regex по markdown-ограде.
"""
from __future__ import annotations

import json
import logging
import re

from .llm_provider import LLMProvider

log = logging.getLogger("scorer")

SYSTEM_PROMPT = """Ты — карьерный консультант. Тебе даны:
1) карьерная база кандидата (позиционирование, треки, зоны роста, дилбрейкеры, зарплатные ожидания);
2) текст вакансии.

Оцени, насколько вакансия подходит кандидату. Верни ТОЛЬКО валидный JSON, без пояснений
до или после, без markdown-ограды, по следующей схеме:

{
  "fit_score": <0-100 целое>,
  "track": "A" | "B",
  "salary_fit": "выше ожиданий" | "в рамках" | "ниже пола" | "не указана",
  "red_flags": [<список строк, может быть пустым>],
  "rationale": "<2-3 предложения, почему такая оценка>",
  "recommend": "respond" | "consider" | "skip",
  "ats_keywords": [<список строк, 5-12 штук>]
}

Критерии:
- track: "A" только если явно совпадает с доменом трека A из карьерной базы
  (международная фарма/медпроизводители). "B" — трек "деньги/масштаб" из базы
  И одновременно катч-олл на все остальные случаи (вакансия не подходит ни
  под один явный трек кандидата) — отдельного значения "other" не существует,
  всё, что не A, — это B.
- fit_score учитывает: домен (медтех/IVD/фарма — сильный плюс), фокус на Product+Promotion
  (не Price/Place-тяжёлые роли), масштаб и уровень (не generic исполнительские позиции),
  культуру (описание вакансии намекает на микроменеджмент — минус), зарплату относительно пола.
- red_flags: например, "зарплата ниже пола", "явный микроменеджмент в тексте",
  "роль слишком операционная/не бренд-фокус", "коммерческий директор с P&L-ядром — не по профилю".
- recommend "respond" только если fit_score >= 70 и нет серьёзных red_flags.
- recommend "consider" для 50-69 или при спорных флагах.
- recommend "skip" для < 50 или явного дилбрейкера (микроменеджмент, зарплата сильно ниже пола).
- Метро: если в тексте вакансии указано "приоритетная линия для кандидата" —
  это небольшой плюс к fit_score (не решающий фактор). Если метро не указано
  вообще — это нейтрально, не штрафуй за это.
- Если перед текстом вакансии дан блок "ПРОШЛЫЕ РАСХОЖДЕНИЯ" — это реальные
  случаи, где кандидат не согласился с твоей же прошлой оценкой похожей
  вакансии. Это самый сильный сигнал о его настоящих предпочтениях, сильнее
  общих критериев выше — используй его, чтобы поправить оценку в сторону
  того, что кандидат реально выбирает, а не только формальных признаков.

ats_keywords — 5-12 конкретных слов/словосочетаний ИЗ ТЕКСТА ВАКАНСИИ (дословно,
не перефразируя и не переводя), по которым HR или ATS-система (Huntflow, Поток,
E-Staff и т.п.) реально фильтрует резюме при поиске: названия инструментов,
методологий, систем, сертификатов, доменных терминов, аббревиатур (CJM, ROMI,
P&L, B2G и т.п.). Не включай общие слова вроде "ответственность" или
"коммуникабельность" — только то, что похоже на поисковый запрос рекрутёра.
Если в тексте явно есть поле "Ключевые навыки (по данным HH)" — обязательно
включи все слова оттуда плюс то, что заметишь в остальном тексте вакансии.
"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    # на случай если модель всё же обернула в ```json ... ```
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    return json.loads(text)


_DECISION_LABEL = {"fit": "подходит (кандидат отметил 👍)", "not_fit": "не подходит (кандидат отметил 👎)"}


def build_corrections_note(rows: list) -> str:
    """Собирает блок "ПРОШЛЫЕ РАСХОЖДЕНИЯ" из vacancies, где storage.disagreements()
    нашла разницу между решением кандидата и рекомендацией модели. Пустая строка,
    если расхождений ещё не было — тогда блок в промпт просто не добавляется."""
    if not rows:
        return ""
    lines = ["ПРОШЛЫЕ РАСХОЖДЕНИЯ (реальные решения кандидата, которые разошлись с твоей оценкой):"]
    for r in rows:
        actual = _DECISION_LABEL.get(r["decision"], r["decision"])
        line = (
            f"- «{r['name']}» ({r['employer'] or '—'}), трек {r['track'] or '?'}: "
            f"ты порекомендовал «{r['recommend']}» ({r['rationale'] or 'без обоснования'}), "
            f"а кандидат решил: {actual}"
        )
        if r["decision_reason"]:
            line += f" — его причина: «{r['decision_reason']}»"
        lines.append(line + ".")
    return "\n".join(lines)


def score_vacancy(
    provider: LLMProvider, career_base_md: str, vacancy_text: str, corrections_note: str = ""
) -> dict:
    user_content = (
        f"КАРЬЕРНАЯ БАЗА КАНДИДАТА:\n{career_base_md}\n\n"
        + (f"---\n\n{corrections_note}\n\n" if corrections_note else "")
        + f"---\n\nТЕКСТ ВАКАНСИИ:\n{vacancy_text}\n\n"
        "Верни JSON по схеме из системного промпта."
    )
    raw = provider.complete(SYSTEM_PROMPT, user_content, max_tokens=800, temperature=0.2)
    try:
        return _extract_json(raw)
    except json.JSONDecodeError as e:
        log.warning("Не удалось распарсить JSON от модели: %s\nОтвет: %.300s", e, raw)
        return {
            "fit_score": None,
            "track": "B",
            "salary_fit": "не указана",
            "red_flags": ["не удалось оценить автоматически — проверь вручную"],
            "rationale": "Ошибка парсинга ответа модели.",
            "recommend": "consider",
            "ats_keywords": [],
        }


def get_metro(raw: dict) -> tuple[str | None, str | None]:
    """(station_name, line_name) из адреса вакансии, либо (None, None), если метро не указано."""
    metro = ((raw.get("address") or {}).get("metro")) or {}
    return metro.get("station_name"), metro.get("line_name")


def vacancy_to_text(raw: dict, priority_metro_lines: list[str] | None = None) -> str:
    """Собирает читаемый текст вакансии из сырого JSON HH API для промпта."""
    parts = [f"Должность: {raw.get('name')}"]
    employer = (raw.get("employer") or {}).get("name")
    if employer:
        parts.append(f"Компания: {employer}")
    area = (raw.get("area") or {}).get("name")
    if area:
        parts.append(f"Регион: {area}")
    station, line = get_metro(raw)
    if station:
        note = f"Метро: {station} ({line})" if line else f"Метро: {station}"
        if priority_metro_lines and line in priority_metro_lines:
            note += " — приоритетная линия для кандидата"
        parts.append(note)
    else:
        parts.append("Метро: не указано")
    salary = raw.get("salary") or {}
    if salary:
        parts.append(
            f"Зарплата: {salary.get('from')}-{salary.get('to')} {salary.get('currency')} "
            f"({'гросс' if not salary.get('gross') else 'на руки' if salary.get('gross') is False else ''})"
        )
    schedule = (raw.get("schedule") or {}).get("name")
    if schedule:
        parts.append(f"График: {schedule}")
    experience = (raw.get("experience") or {}).get("name")
    if experience:
        parts.append(f"Опыт: {experience}")
    snippet = raw.get("snippet") or {}
    if snippet.get("requirement"):
        parts.append(f"Требования (сниппет): {snippet['requirement']}")
    if snippet.get("responsibility"):
        parts.append(f"Обязанности (сниппет): {snippet['responsibility']}")
    key_skills = [s.get("name") for s in (raw.get("key_skills") or []) if s.get("name")]
    if key_skills:
        # ключевые навыки, которые сам HH выделил для вакансии — по ним и матчат
        # рекрутёры, и ATS-системы (Huntflow, Поток и т.п.). Важно вплетать эти
        # же слова в резюме дословно, где это правда, а не перефразировать.
        parts.append(f"Ключевые навыки (по данным HH): {', '.join(key_skills)}")
    description = raw.get("description")
    if description:
        parts.append(f"Полное описание: {description}")
    return "\n".join(parts)
