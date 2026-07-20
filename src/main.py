"""
Точка входа. Команды:

  python -m src.main fetch        — тянет вакансии с hh.ru по фильтрам из config.yaml
  python -m src.main score        — оценивает новые вакансии через YandexGPT
  python -m src.main digest       — печатает/сохраняет markdown-дайджест топ-вакансий
  python -m src.main mark <id> <status>   — interested / skip / applied
  python -m src.main tailor <id>          — резюме (md+docx) + сопроводительное письмо
  python -m src.main dictionaries         — сохраняет areas/professional_roles в out/,
                                             чтобы свериться с id для config.yaml
  python -m src.main serve                — веб-интерфейс: список вакансий + tailor по клику
  python -m src.main check-liked          — проверяет актуальность вакансий, отмеченных «по душе»
  python -m src.main backup               — резервная копия БД в ./backups/, хранит последние 7

Типичный сценарий (например, из cron раз в день):
  python -m src.main fetch && python -m src.main score && python -m src.main digest
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .filters import (
    EMPLOYMENT_OPTIONS,
    EXPERIENCE_OPTIONS,
    SCHEDULE_OPTIONS,
    hh_values,
    sj_experience_or_employment,
    sj_schedule_params,
)
from .hh_client import HHClient, HHConfig
from .llm_provider import get_provider
from .storage import Storage
from .superjob_client import SuperJobClient, SuperJobConfig
from .sources import get_full_vacancy, get_vacancy_status
from .yandex_client import YandexConfig
from .scorer import build_corrections_note, get_metro, score_vacancy, vacancy_to_text
from .tailor import tailor_for_vacancy
from .digest import build_daily_comment, build_digest
from .docx_export import build_resume_docx, extract_candidate_name

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")


def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_models_config(path: str = "models.yaml") -> dict:
    """Список моделей для выбора на /settings (по задачам: scoring/tailor) —
    не секрет, обычный справочник, можно смело коммитить и редактировать."""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_career_base(path: str) -> str:
    p = Path(path)
    if not p.exists():
        raise SystemExit(
            f"Не найден файл карьерной базы: {path}\n"
            "Скопируй career_base.example.md в career_base.md и заполни своими данными, "
            "или поправь paths.career_base_md в config.yaml."
        )
    return p.read_text(encoding="utf-8")


def get_yandex_config(cfg: dict, model: str) -> YandexConfig:
    y = cfg["yandex"]
    api_key = os.environ.get(y["api_key_env"])
    if not api_key:
        raise SystemExit(
            f"Не задан {y['api_key_env']}. Положи ключ в .env (см. .env.example) "
            "или экспортируй переменную окружения."
        )
    folder_id = y.get("folder_id")
    if not folder_id:
        raise SystemExit("Не задан yandex.folder_id в config.yaml.")
    return YandexConfig(api_key=api_key, folder_id=folder_id, model=model)


def get_hh_config(cfg: dict) -> HHConfig:
    h = cfg["hh"]
    client_id = os.environ.get(h["client_id_env"])
    client_secret = os.environ.get(h["client_secret_env"])
    if not client_id or not client_secret:
        raise SystemExit(
            f"Не заданы {h['client_id_env']}/{h['client_secret_env']} в .env — "
            "нужны для доступа к HH API (см. .env.example, зарегистрировать приложение: "
            "https://dev.hh.ru/admin)."
        )
    return HHConfig(client_id=client_id, client_secret=client_secret, user_agent=h["user_agent"])


def get_gigachat_config(cfg: dict):
    """GigaChat — опциональный второй LLM-провайдер, читается только если реально
    выбран в /settings или в config.yaml (llm.provider/score_provider/tailor_provider),
    см. get_provider() в llm_provider.py. Секция gigachat в config.yaml обязательна
    в этот момент (в отличие от superjob — там источник можно не подключать вовсе)."""
    from .gigachat_client import GigaChatConfig

    g = cfg.get("gigachat")
    if not g:
        raise SystemExit(
            "Выбран провайдер gigachat, но в config.yaml нет секции gigachat "
            "(credentials_env, опционально scope) — см. config.example.yaml."
        )
    credentials = os.environ.get(g["credentials_env"])
    if not credentials:
        raise SystemExit(
            f"Не задан {g['credentials_env']} в .env — нужен Authorization key из личного "
            "кабинета Sber Studio (https://developers.sber.ru/studio/)."
        )
    return GigaChatConfig(credentials=credentials, scope=g.get("scope", "GIGACHAT_API_PERS"))


def get_priority_metro_lines(storage: Storage) -> list[str]:
    """Линии метро, дающие небольшой плюс к score — настраиваются на странице
    «Настройки», не в config.yaml (см. settings_search в webapp.py)."""
    return json.loads(storage.get_setting("priority_metro_lines", "[]"))


def get_filter_selection(storage: Storage, category: str) -> list[str]:
    """category: "experience" | "employment" | "schedule" — список выбранных
    ключей опций (см. src/filters.py), настраивается на странице «Настройки»."""
    return json.loads(storage.get_setting(f"filter_{category}", "[]"))


def get_superjob_config(cfg: dict) -> SuperJobConfig | None:
    """None, если секции superjob нет в конфиге вовсе — источник полностью опционален."""
    s = cfg.get("superjob")
    if not s:
        return None
    secret_key = os.environ.get(s["secret_key_env"])
    if not secret_key:
        raise SystemExit(
            f"Не задан {s['secret_key_env']} в .env — нужен для SuperJob API "
            "(см. .env.example, получить ключ: https://api.superjob.ru/register/)."
        )
    return SuperJobConfig(secret_key=secret_key, town=s.get("town"))


def cmd_fetch(cfg: dict) -> None:
    storage = Storage(cfg["paths"]["db"])
    if storage.get_setting("collection_paused") == "1":
        log.info("Сбор новых вакансий на паузе (см. /settings в веб-интерфейсе) — fetch пропущен.")
        return
    s = cfg["search"]
    total_new = 0

    # город, зарплатный порог и фильтры опыта/занятости/графика — настраиваются
    # на странице «Настройки» (веб-интерфейс), не в config.yaml, чтобы человек
    # без техфона мог выбрать всё из списков, а не искать значения в доке API.
    search_area = storage.get_setting("search_area", "1")            # 1 = Москва
    superjob_town = storage.get_setting("superjob_town", "4") or None  # 4 = Москва
    salary_from = int(storage.get_setting("search_salary_from", "0") or 0)
    filter_experience = get_filter_selection(storage, "experience")
    filter_employment = get_filter_selection(storage, "employment")
    filter_schedule = get_filter_selection(storage, "schedule")

    if storage.get_setting("source_hh_enabled", "1") == "1":
        hh = HHClient(get_hh_config(cfg))
        for query in s["queries"]:
            log.info("[hh.ru] Поиск: %r", query)
            params = {
                "text": query,
                "search_field": s.get("search_field") or None,
                "area": search_area,
                "employment": hh_values(EMPLOYMENT_OPTIONS, filter_employment) or None,
                "schedule": hh_values(SCHEDULE_OPTIONS, filter_schedule) or None,
                "experience": hh_values(EXPERIENCE_OPTIONS, filter_experience) or None,
                "salary": salary_from or None,
                "currency": s.get("currency"),
                "only_with_salary": "true" if s.get("only_with_salary") else None,
                "period": s.get("period"),
                "per_page": s.get("per_page", 50),
                "max_pages": s.get("max_pages", 4),
            }
            params = {k: v for k, v in params.items() if v not in (None, "")}
            items = hh.search_vacancies(**params)
            new_count = 0
            for v in items:
                if storage.upsert_vacancy(v, source="hh"):
                    new_count += 1
            log.info("  всего найдено: %s, новых: %s", len(items), new_count)
            total_new += new_count
    else:
        log.info("Источник hh.ru выключен в настройках — пропускаю.")

    if storage.get_setting("source_superjob_enabled", "1") == "1" and cfg.get("superjob"):
        sj = SuperJobClient(get_superjob_config(cfg))
        # SuperJob не поддерживает мультивыбор в этих категориях (см. src/filters.py) —
        # берём первый применимый вариант на категорию; при конфликте на параметре
        # type_of_work (его используют и employment, и часть schedule-вариантов —
        # "сменный график"/"вахта") employment побеждает как более общая категория.
        sj_extra_params: dict = sj_schedule_params(filter_schedule)
        sj_employment = sj_experience_or_employment(EMPLOYMENT_OPTIONS, filter_employment)
        if sj_employment is not None:
            sj_extra_params["type_of_work"] = sj_employment
        sj_extra_params["experience"] = sj_experience_or_employment(EXPERIENCE_OPTIONS, filter_experience)
        for query in s["queries"]:
            log.info("[SuperJob] Поиск: %r", query)
            params = {
                "keyword": query,
                "town": superjob_town,
                "payment_from": salary_from or None,
                # period у SuperJob — не число дней, а перечисление (0=всё время,
                # 1=сутки, 3=трое суток, 7=неделя, 30=месяц); совпадение с числом
                # дней из search.period случайно, но пока значение 7 подходит и там,
                # и там — если сменишь search.period на что-то другое, сверься
                # с перечислением SuperJob (см. api.superjob.ru).
                "period": s.get("period"),
                "count": min(s.get("per_page", 50), 100),
                "max_pages": s.get("max_pages", 4),
                **sj_extra_params,
            }
            params = {k: v for k, v in params.items() if v not in (None, "")}
            items = sj.search_vacancies(**params)
            new_count = 0
            for v in items:
                if storage.upsert_vacancy(v, source="superjob"):
                    new_count += 1
            log.info("  всего найдено: %s, новых: %s", len(items), new_count)
            total_new += new_count
    else:
        log.info("Источник SuperJob выключен в настройках (или не задан в config.yaml) — пропускаю.")

    log.info("Готово. Новых вакансий за запуск: %s", total_new)


def cmd_score(cfg: dict) -> None:
    hh = HHClient(get_hh_config(cfg))
    sj_cfg = get_superjob_config(cfg)
    sj = SuperJobClient(sj_cfg) if sj_cfg else None
    storage = Storage(cfg["paths"]["db"])
    career_base = load_career_base(cfg["paths"]["career_base_md"])
    provider = get_provider(cfg, "score", storage)

    priority_lines = get_priority_metro_lines(storage)
    # реальные расхождения решение/рекомендация — один раз на весь прогон,
    # не на каждую вакансию заново (список меняется не так часто)
    corrections_note = build_corrections_note(storage.disagreements())
    # настраивается на /settings в веб-интерфейсе, не в config.yaml — см. storage.get_setting
    auto_reject_max = int(storage.get_setting("auto_reject_max_score", "40"))

    rows = storage.unscored()
    log.info("К оценке: %s вакансий", len(rows))
    for i, row in enumerate(rows, 1):
        try:
            # добираем полное описание — в поиске приходит только сниппет
            full = get_full_vacancy(hh, sj, row["id"], row["source"])
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось получить полную карточку %s: %s. Оцениваю по сниппету.", row["id"], e)
            full = json.loads(row["raw_json"])
        text = vacancy_to_text(full, priority_lines)
        result = score_vacancy(provider, career_base, text, corrections_note)
        station, line = get_metro(full)
        metro = {"station": station, "line": line, "priority": bool(line and line in priority_lines)}
        storage.save_score(row["id"], result, metro)
        fit_score = result.get("fit_score")
        if auto_reject_max is not None and fit_score is not None and fit_score <= auto_reject_max:
            storage.set_decision(row["id"], "not_fit", f"автоматически: fit_score {fit_score} ≤ {auto_reject_max}")
            storage.set_liked(row["id"], False)
            storage.mark_status(row["id"], "skip")
        log.info(
            "[%s/%s] %s — score=%s recommend=%s",
            i, len(rows), row["name"], result.get("fit_score"), result.get("recommend"),
        )


def cmd_digest(cfg: dict) -> None:
    from datetime import date

    storage = Storage(cfg["paths"]["db"])
    d = cfg["digest"]
    text = build_digest(storage, d["min_score_to_show"], d["top_n"])

    today = date.today().isoformat()
    try:
        comment = build_daily_comment(get_provider(cfg, "score", storage), storage.scored_today())
        storage.save_daily_comment(today, comment)
        text = f"## Комментарий дня\n{comment}\n\n{text}"
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось собрать дневной комментарий: %s", e)

    out_dir = Path(cfg["paths"]["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"digest_{today}.md"
    out_path.write_text(text, encoding="utf-8")
    print(text)
    log.info("Дайджест сохранён: %s", out_path)


def cmd_mark(cfg: dict, vacancy_id: str, status: str) -> None:
    storage = Storage(cfg["paths"]["db"])
    storage.mark_status(vacancy_id, status)
    log.info("Вакансия %s помечена как '%s'", vacancy_id, status)


def cmd_tailor(cfg: dict, vacancy_id: str) -> None:
    hh = HHClient(get_hh_config(cfg))
    sj_cfg = get_superjob_config(cfg)
    sj = SuperJobClient(sj_cfg) if sj_cfg else None
    storage = Storage(cfg["paths"]["db"])
    career_base = load_career_base(cfg["paths"]["career_base_md"])
    provider = get_provider(cfg, "tailor", storage)

    row = storage.get(vacancy_id)
    if row is None:
        raise SystemExit(f"Вакансия {vacancy_id} не найдена в базе. Сначала fetch/score.")

    priority_lines = get_priority_metro_lines(storage)
    full = get_full_vacancy(hh, sj, vacancy_id, row["source"])
    text = vacancy_to_text(full, priority_lines)
    notes, resume_full, letter = tailor_for_vacancy(provider, career_base, text)

    out_dir = Path(cfg["paths"]["out_dir"]) / vacancy_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "resume_notes.md").write_text(notes, encoding="utf-8")
    (out_dir / "resume_full.md").write_text(resume_full, encoding="utf-8")
    (out_dir / "cover_letter.txt").write_text(letter, encoding="utf-8")
    (out_dir / "vacancy.txt").write_text(text, encoding="utf-8")

    candidate_name = extract_candidate_name(career_base)
    build_resume_docx(resume_full, candidate_name).save(out_dir / "resume.docx")

    log.info("Готово: %s", out_dir)
    print(f"\n--- resume_notes.md ---\n{notes}\n")
    print(f"--- resume_full.md ---\n{resume_full}\n")
    print(f"--- cover_letter.txt ---\n{letter}\n")


def refresh_vacancy_status(
    hh: HHClient, sj: SuperJobClient | None, storage: Storage, vacancy_id: str, source: str = "hh"
) -> dict:
    """Дёргает источник (hh.ru/SuperJob) за актуальным статусом вакансии и сохраняет
    в БД. Общий код для ручной кнопки в веб-интерфейсе и для cmd_check_liked."""
    status = get_vacancy_status(hh, sj, vacancy_id, source)
    storage.set_archived(vacancy_id, status["archived"])
    return status


def cmd_check_liked(cfg: dict) -> None:
    hh = HHClient(get_hh_config(cfg))
    sj_cfg = get_superjob_config(cfg)
    sj = SuperJobClient(sj_cfg) if sj_cfg else None
    storage = Storage(cfg["paths"]["db"])
    rows = storage.list_scored(decision="fit")
    log.info("Проверяю актуальность %s вакансий из «по душе»", len(rows))
    for row in rows:
        try:
            status = refresh_vacancy_status(hh, sj, storage, row["id"], row["source"])
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось проверить %s: %s", row["id"], e)
            continue
        state = "в архиве/снята" if status["archived"] else "актуальна"
        log.info("  %s — %s (%s)", row["id"], row["name"], state)


def cmd_backup(cfg: dict, keep: int | None = None) -> None:
    """Резервная копия БД через штатный sqlite3 .backup() API (безопасно даже
    если БД в этот момент кем-то читается/пишется — обычный cp так не умеет).
    Хранит только последние `keep` копий, старые удаляет. Если `keep` не
    передан явно — берёт значение из настроек веб-интерфейса (/settings),
    по умолчанию 7."""
    import sqlite3
    from datetime import date

    storage = Storage(cfg["paths"]["db"])
    if keep is None:
        keep = int(storage.get_setting("backup_keep_count", "7"))

    db_path = Path(cfg["paths"]["db"])
    backups_dir = db_path.parent / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    dst_path = backups_dir / f"{db_path.stem}_{date.today().isoformat()}.db"

    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dst_path)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()

    backups = sorted(backups_dir.glob(f"{db_path.stem}_*.db"))
    for old in backups[:-keep]:
        old.unlink()
        log.info("Удалена старая резервная копия: %s", old)
    log.info("Бэкап БД сохранён: %s (хранится копий: %s)", dst_path, min(len(backups), keep))


def cmd_serve(cfg: dict) -> None:
    from .webapp import create_app  # локальный импорт — избегаем цикла main<->webapp

    w = cfg["webapp"]
    app = create_app(cfg)
    app.run(host=w["host"], port=w["port"])


def cmd_dictionaries(cfg: dict) -> None:
    hh = HHClient(get_hh_config(cfg))
    out_dir = Path(cfg["paths"]["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "areas.json").write_text(
        json.dumps(hh.get_areas(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "professional_roles.json").write_text(
        json.dumps(hh.get_professional_roles(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "dictionaries.json").write_text(
        json.dumps(hh.get_dictionaries(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("Справочники сохранены в %s", out_dir)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="hh-helper: поиск и оценка вакансий")
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("fetch")
    sub.add_parser("score")
    sub.add_parser("digest")
    p_mark = sub.add_parser("mark")
    p_mark.add_argument("vacancy_id")
    p_mark.add_argument("status", choices=["interested", "skip", "applied", "new"])
    p_tailor = sub.add_parser("tailor")
    p_tailor.add_argument("vacancy_id")
    sub.add_parser("dictionaries")
    sub.add_parser("serve")
    sub.add_parser("check-liked")
    sub.add_parser("backup")

    args = parser.parse_args()
    cfg = load_config(args.config)

    if args.command == "fetch":
        cmd_fetch(cfg)
    elif args.command == "score":
        cmd_score(cfg)
    elif args.command == "digest":
        cmd_digest(cfg)
    elif args.command == "mark":
        cmd_mark(cfg, args.vacancy_id, args.status)
    elif args.command == "tailor":
        cmd_tailor(cfg, args.vacancy_id)
    elif args.command == "dictionaries":
        cmd_dictionaries(cfg)
    elif args.command == "serve":
        cmd_serve(cfg)
    elif args.command == "check-liked":
        cmd_check_liked(cfg)
    elif args.command == "backup":
        cmd_backup(cfg)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
