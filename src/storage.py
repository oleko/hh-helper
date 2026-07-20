"""
Локальное хранилище вакансий на SQLite. Без внешних сервисов —
одна файловая база, которая живёт рядом со скриптом на VPS.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS vacancies (
    id              TEXT PRIMARY KEY,
    name            TEXT,
    employer        TEXT,
    area            TEXT,
    salary_from     INTEGER,
    salary_to       INTEGER,
    currency        TEXT,
    url             TEXT,
    alternate_url   TEXT,
    published_at    TEXT,
    snippet         TEXT,
    raw_json        TEXT,
    fetched_at      TEXT,
    -- поля скоринга, заполняются позже
    score           INTEGER,
    track           TEXT,       -- 'A' (межд. фарма) / 'B' (крупная РФ-корп + катч-олл "другое")
    salary_fit      TEXT,
    red_flags       TEXT,
    rationale       TEXT,
    recommend       TEXT,       -- 'respond' / 'consider' / 'skip'
    scored_at       TEXT,
    -- статус по решению человека
    status          TEXT DEFAULT 'new',  -- new -> digested -> interested/skip -> applied
    -- метро вакансии (заполняется вместе со скорингом)
    metro_station   TEXT,
    metro_line      TEXT,
    metro_priority  INTEGER DEFAULT 0,   -- 1, если metro_line — приоритетная линия кандидата
    -- отметка "по душе" — независима от status, пока не влияет на скоринг
    liked           INTEGER DEFAULT 0,
    -- актуальность на hh.ru: заполняется при ручной/автопроверке, не при fetch/score
    archived            INTEGER DEFAULT 0,  -- 1 = снята с публикации/в архиве по данным HH
    archive_checked_at  TEXT,               -- когда последний раз проверяли актуальность
    -- решение человека "подходит/не подходит" — отдельно от status/liked.
    -- NULL у всех новых вакансий. 'not_fit' по умолчанию прячет вакансию из
    -- списков (дальше не отслеживаем), 'fit' — просто положительная метка.
    decision            TEXT,
    decision_reason     TEXT,  -- необязательное объяснение решения, для промпта скоринга
    -- источник вакансии: 'hh' (по умолчанию, для обратной совместимости со
    -- старыми строками) или 'superjob'. id вакансий SuperJob хранится с
    -- префиксом "sj", чтобы не столкнуться с числовыми id hh.ru.
    source              TEXT DEFAULT 'hh',
    -- 'search' — обычный сбор по фильтрам (fetch); 'manual_url' — добавлена
    -- через инструмент "вакансия по ссылке". manual_url НЕ попадает в три
    -- основных списка (Разбор/Подходит/Архив), чтобы не путать сбор с
    -- разовой проверкой чужой вакансии.
    origin              TEXT DEFAULT 'search',
    -- ключевые слова для HR/ATS-систем, которые выделила модель при скоринге
    -- (см. scorer.py) — JSON-список строк, показывается чипами на карточке
    ats_keywords        TEXT
);

CREATE TABLE IF NOT EXISTS daily_comments (
    day         TEXT PRIMARY KEY,  -- YYYY-MM-DD
    comment     TEXT,
    created_at  TEXT
);

-- простое key-value хранилище для настроек, которые меняются из веб-интерфейса
-- (число хранимых бэкапов, пауза сбора новых вакансий) — не трогаем config.yaml
-- программно, чтобы не потерять комментарии в нём при перезаписи.
CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

-- лог токенов LLM-запросов (score/tailor/digest) — по одной строке на вызов
-- provider.complete(), для счётчика в статистике. day хранится отдельной
-- колонкой (не парсим created_at на каждый SELECT), группировка по дням —
-- как у count_by_day().
CREATE TABLE IF NOT EXISTS token_usage (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at         TEXT,
    day                TEXT,
    provider           TEXT,   -- 'yandex' | 'gigachat'
    task               TEXT,   -- 'score' | 'tailor' | 'digest'
    prompt_tokens      INTEGER,
    completion_tokens  INTEGER,
    total_tokens       INTEGER
);
"""

# Колонки, добавленные после первого релиза схемы — для уже существующих БД
# (например, на проде) CREATE TABLE IF NOT EXISTS их не добавит, поэтому
# _init_schema() докатывает недостающие через ALTER TABLE.
_MIGRATION_COLUMNS = {
    "metro_station": "TEXT",
    "metro_line": "TEXT",
    "metro_priority": "INTEGER DEFAULT 0",
    "liked": "INTEGER DEFAULT 0",
    "archived": "INTEGER DEFAULT 0",
    "archive_checked_at": "TEXT",
    "decision": "TEXT",
    "decision_reason": "TEXT",
    "source": "TEXT DEFAULT 'hh'",
    "origin": "TEXT DEFAULT 'search'",
    "ats_keywords": "TEXT",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(vacancies)")}
            for col, coltype in _MIGRATION_COLUMNS.items():
                if col not in existing:
                    conn.execute(f"ALTER TABLE vacancies ADD COLUMN {col} {coltype}")

    def upsert_vacancy(self, v: dict[str, Any], source: str = "hh", origin: str = "search") -> bool:
        """Возвращает True, если это новая вакансия (не было в базе).

        `source`: 'hh' | 'superjob' — кто отдал данные (v уже нормализован под
        форму HH, см. superjob_client.normalize). `origin`: 'search' (обычный
        fetch) | 'manual_url' (добавлена через инструмент "вакансия по ссылке")."""
        salary = v.get("salary") or {}
        employer = (v.get("employer") or {}).get("name")
        area = (v.get("area") or {}).get("name")
        snippet = " ".join(
            filter(None, [
                (v.get("snippet") or {}).get("requirement"),
                (v.get("snippet") or {}).get("responsibility"),
            ])
        )
        with self._conn() as conn:
            existing = conn.execute("SELECT id FROM vacancies WHERE id = ?", (v["id"],)).fetchone()
            conn.execute(
                """
                INSERT INTO vacancies (id, name, employer, area, salary_from, salary_to, currency,
                                        url, alternate_url, published_at, snippet, raw_json, fetched_at,
                                        source, origin)
                VALUES (:id, :name, :employer, :area, :salary_from, :salary_to, :currency,
                        :url, :alternate_url, :published_at, :snippet, :raw_json, :fetched_at,
                        :source, :origin)
                ON CONFLICT(id) DO NOTHING
                """,
                {
                    "id": v["id"],
                    "name": v.get("name"),
                    "employer": employer,
                    "area": area,
                    "salary_from": salary.get("from"),
                    "salary_to": salary.get("to"),
                    "currency": salary.get("currency"),
                    "url": v.get("url"),
                    "alternate_url": v.get("alternate_url"),
                    "published_at": v.get("published_at"),
                    "snippet": snippet,
                    "raw_json": json.dumps(v, ensure_ascii=False),
                    "fetched_at": now_iso(),
                    "source": source,
                    "origin": origin,
                },
            )
        return existing is None

    def unscored(self) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM vacancies WHERE score IS NULL ORDER BY published_at DESC"
            ).fetchall()

    def save_score(self, vacancy_id: str, score: dict[str, Any], metro: dict[str, Any] | None = None) -> None:
        metro = metro or {}
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE vacancies SET score=?, track=?, salary_fit=?, red_flags=?,
                                      rationale=?, recommend=?, scored_at=?,
                                      metro_station=?, metro_line=?, metro_priority=?,
                                      ats_keywords=?
                WHERE id=?
                """,
                (
                    score.get("fit_score"),
                    score.get("track"),
                    score.get("salary_fit"),
                    json.dumps(score.get("red_flags", []), ensure_ascii=False),
                    score.get("rationale"),
                    score.get("recommend"),
                    now_iso(),
                    metro.get("station"),
                    metro.get("line"),
                    int(bool(metro.get("priority"))),
                    json.dumps(score.get("ats_keywords", []), ensure_ascii=False),
                    vacancy_id,
                ),
            )

    def top_for_digest(self, min_score: int, limit: int) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                """
                SELECT * FROM vacancies
                WHERE score IS NOT NULL AND score >= ? AND status IN ('new', 'digested')
                ORDER BY score DESC
                LIMIT ?
                """,
                (min_score, limit),
            ).fetchall()

    SORT_OPTIONS = {
        "score": "score DESC",
        "fit_first": "(CASE WHEN decision = 'fit' THEN 0 ELSE 1 END), score DESC",
        "new_first": "fetched_at DESC",
        "old_unreviewed": "(CASE WHEN decision IS NULL THEN 0 ELSE 1 END), fetched_at ASC",
    }

    def _scored_where(
        self, status: str | None, min_score: int | None, decision: str | None
    ) -> tuple[str, list[Any]]:
        """Общий WHERE для list_scored/count_scored — держим условие в одном месте,
        чтобы счётчик страниц никогда не разъехался с самим списком."""
        where = "WHERE score IS NOT NULL AND origin != 'manual_url'"
        params: list[Any] = []
        if status:
            where += " AND status = ?"
            params.append(status)
        if min_score is not None:
            where += " AND score >= ?"
            params.append(min_score)
        if decision == "unsorted":
            where += " AND decision IS NULL"
        elif decision in ("fit", "not_fit"):
            where += " AND decision = ?"
            params.append(decision)
        return where, params

    def list_scored(
        self,
        status: str | None = None,
        min_score: int | None = None,
        decision: str | None = None,
        sort: str = "score",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        """Для веб-интерфейса: все оценённые вакансии, в любом статусе (в отличие от
        top_for_digest, которая намеренно ограничена статусами new/digested).
        `decision`: None — без фильтра; "unsorted" — ещё не разобранные
        (decision IS NULL, это и есть главная страница-очередь); "fit"/"not_fit" —
        точное значение (страницы «Подходит»/«Архив»).

        Вакансии с origin='manual_url' (добавлены через инструмент "вакансия
        по ссылке") сюда никогда не попадают — это разовая проверка, не часть
        очереди разбора.

        `limit`/`offset` — пагинация для веб-списков; None (по умолчанию) —
        без ограничения, как раньше (используется там, где нужен весь список)."""
        where, params = self._scored_where(status, min_score, decision)
        query = f"SELECT * FROM vacancies {where} ORDER BY " + self.SORT_OPTIONS.get(
            sort, self.SORT_OPTIONS["score"]
        )
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            params = params + [limit, offset]
        with self._conn() as conn:
            return conn.execute(query, params).fetchall()

    def count_scored(
        self, status: str | None = None, min_score: int | None = None, decision: str | None = None
    ) -> int:
        """То же условие, что и list_scored, но COUNT(*) — для пагинации (сколько всего
        страниц) без вытягивания всех строк целиком."""
        where, params = self._scored_where(status, min_score, decision)
        with self._conn() as conn:
            return conn.execute(f"SELECT COUNT(*) AS cnt FROM vacancies {where}", params).fetchone()["cnt"]

    def count_by_decision(self) -> dict[str, int]:
        """Для навигации: сколько вакансий не разобрано / подходит / не подходит."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT decision, COUNT(*) AS cnt FROM vacancies "
                "WHERE score IS NOT NULL AND origin != 'manual_url' GROUP BY decision"
            ).fetchall()
        counts = {"unsorted": 0, "fit": 0, "not_fit": 0}
        for r in rows:
            key = r["decision"] if r["decision"] in ("fit", "not_fit") else "unsorted"
            counts[key] = counts.get(key, 0) + r["cnt"]
        return counts

    def record_token_usage(self, provider: str, task: str, usage: dict | None) -> None:
        """usage — LLMProvider.last_usage после provider.complete() (None, если
        вызов упал до получения ответа — тогда просто ничего не пишем)."""
        if not usage:
            return
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO token_usage (created_at, day, provider, task, "
                "prompt_tokens, completion_tokens, total_tokens) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    now_iso(),
                    now_iso()[:10],
                    provider,
                    task,
                    usage.get("prompt_tokens", 0),
                    usage.get("completion_tokens", 0),
                    usage.get("total_tokens", 0),
                ),
            )

    def token_usage_by_day(self) -> list[sqlite3.Row]:
        """Суммарные токены по дням — для графика в /stats."""
        with self._conn() as conn:
            return conn.execute(
                """
                SELECT day,
                       SUM(total_tokens) AS total,
                       SUM(prompt_tokens) AS prompt,
                       SUM(completion_tokens) AS completion
                FROM token_usage
                GROUP BY day
                ORDER BY day
                """
            ).fetchall()

    def token_usage_totals(self) -> list[sqlite3.Row]:
        """Итого токенов по провайдеру — для сводной строки над графиком."""
        with self._conn() as conn:
            return conn.execute(
                "SELECT provider, SUM(total_tokens) AS total FROM token_usage GROUP BY provider"
            ).fetchall()

    def count_by_day(self) -> list[sqlite3.Row]:
        """Сколько вакансий было стянуто (fetch) в каждый день — для графика."""
        with self._conn() as conn:
            return conn.execute(
                """
                SELECT substr(fetched_at, 1, 10) AS day, COUNT(*) AS cnt
                FROM vacancies
                GROUP BY day
                ORDER BY day
                """
            ).fetchall()

    def salary_values(self) -> list[float]:
        """Значения зарплаты (в рублях) там, где HH её указал — сырьё для статистики.
        Если указаны и from, и to — берём середину вилки; если только одно значение —
        его. Не-рублёвые вакансии не считаем, чтобы не мешать валюты в одном числе."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT salary_from, salary_to FROM vacancies "
                "WHERE currency = 'RUR' AND (salary_from IS NOT NULL OR salary_to IS NOT NULL)"
            ).fetchall()
        values: list[float] = []
        for r in rows:
            lo, hi = r["salary_from"], r["salary_to"]
            if lo is not None and hi is not None:
                values.append((lo + hi) / 2)
            else:
                values.append(lo if lo is not None else hi)
        return values

    def salary_values_with_date(self) -> list[tuple[str, float]]:
        """То же самое, что salary_values(), но вместе с fetched_at — сырьё для
        недельной динамики. Группировка по ISO-неделям делается в Python
        (SQLite strftime не умеет ISO-недели, только %Y/%m/%d/%w и т.п.)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT fetched_at, salary_from, salary_to FROM vacancies "
                "WHERE currency = 'RUR' AND (salary_from IS NOT NULL OR salary_to IS NOT NULL)"
            ).fetchall()
        result: list[tuple[str, float]] = []
        for r in rows:
            lo, hi = r["salary_from"], r["salary_to"]
            value = (lo + hi) / 2 if (lo is not None and hi is not None) else (lo if lo is not None else hi)
            result.append((r["fetched_at"], value))
        return result

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def disagreements(self, limit: int = 8) -> list[sqlite3.Row]:
        """Вакансии, где твоё решение (fit/not_fit) разошлось с рекомендацией
        модели — сырьё для few-shot подсказки в промпте скоринга."""
        with self._conn() as conn:
            return conn.execute(
                """
                SELECT * FROM vacancies
                WHERE (decision = 'fit' AND recommend != 'respond')
                   OR (decision = 'not_fit' AND recommend != 'skip')
                ORDER BY scored_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def scored_today(self) -> list[sqlite3.Row]:
        """Вакансии, оценённые сегодня — сырьё для дневного комментария."""
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM vacancies WHERE substr(scored_at, 1, 10) = date('now') ORDER BY score DESC"
            ).fetchall()

    def save_daily_comment(self, day: str, comment: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO daily_comments (day, comment, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(day) DO UPDATE SET comment = excluded.comment, created_at = excluded.created_at",
                (day, comment, now_iso()),
            )

    def get_latest_daily_comment(self) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM daily_comments ORDER BY day DESC LIMIT 1"
            ).fetchone()

    def mark_status(self, vacancy_id: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE vacancies SET status=? WHERE id=?", (status, vacancy_id))

    def set_liked(self, vacancy_id: str, liked: bool) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE vacancies SET liked=? WHERE id=?", (int(liked), vacancy_id))

    def set_archived(self, vacancy_id: str, archived: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE vacancies SET archived=?, archive_checked_at=? WHERE id=?",
                (int(archived), now_iso(), vacancy_id),
            )

    def set_decision(self, vacancy_id: str, decision: str | None, reason: str | None = None) -> None:
        """decision: 'fit' | 'not_fit' | None (сброс решения). reason — необязательное
        объяснение, попадает в подсказку скоринга (см. disagreements())."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE vacancies SET decision=?, decision_reason=? WHERE id=?",
                (decision, reason or None, vacancy_id),
            )

    def mark_digested(self, ids: list[str]) -> None:
        with self._conn() as conn:
            conn.executemany(
                "UPDATE vacancies SET status='digested' WHERE id=? AND status='new'",
                [(i,) for i in ids],
            )

    def get(self, vacancy_id: str) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM vacancies WHERE id=?", (vacancy_id,)).fetchone()
