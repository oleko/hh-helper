"""
Веб-интерфейс: просмотр уже посчитанного дайджеста и генерация резюме-заметок
+ сопроводительного письма по клику для выбранной вакансии.

Fetch/score сюда сознательно не входят — это остаётся на cron (см. README),
чтобы не блокировать веб-запрос на минуту-другую похода в HH API. Веб-часть
только читает то, что уже лежит в SQLite, и совершает действия (tailor, смена
статуса).

Сессионный логин (страница /login, куки на подписанной сессии Flask) защищает
доступ, т.к. приложение слушает напрямую по IP VPS — см. предупреждение в
README про отсутствие TLS.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import statistics
from datetime import datetime, timedelta
from pathlib import Path

import bleach
from flask import Flask, abort, redirect, render_template, request, send_file, session, url_for

log = logging.getLogger("webapp")

from .docx_export import build_resume_docx, extract_candidate_name
from .geo import CITIES, flatten_hh_areas, flatten_superjob_towns
from .hh_client import HHApiError, HHClient
from .main import (
    get_hh_config,
    get_priority_metro_lines,
    get_superjob_config,
    get_yandex_config,
    load_career_base,
    refresh_vacancy_status,
)
from .scorer import build_corrections_note, get_metro, score_vacancy, vacancy_to_text
from .sources import get_full_vacancy, get_vacancy_status, parse_vacancy_url
from .storage import Storage
from .superjob_client import SuperJobApiError, SuperJobClient
from .superjob_client import prefixed_id as sj_prefixed_id
from .tailor import tailor_for_vacancy

_SOURCE_ERRORS = (HHApiError, SuperJobApiError)

TEMPLATE_DIR = Path(__file__).parent / "templates"

# description от HH — HTML, написанный работодателем. Рендерим его как разметку
# (иначе теги видны буквами), но пропускаем через санитайзер: это чужой ввод,
# доверять ему как "safe" в Jinja напрямую — открытая дверь для XSS.
_ALLOWED_DESCRIPTION_TAGS = [
    "p", "br", "ul", "ol", "li", "strong", "b", "em", "i", "span", "div", "a",
    "h1", "h2", "h3", "h4",
]


def _format_rur(value: float) -> str:
    return f"{round(value):,}".replace(",", " ")


_RECOMMEND_LABELS = {"respond": "Подходит", "consider": "Подумай", "skip": "Пропусти"}
_SOURCE_LABELS = {"hh": "hh.ru", "superjob": "SuperJob"}
PAGE_SIZE = 40


def _render_description(raw_html: str | None) -> str:
    if not raw_html:
        return "<p><em>Описание отсутствует.</em></p>"
    return bleach.clean(
        raw_html, tags=_ALLOWED_DESCRIPTION_TAGS, attributes={"a": ["href"]}, strip=True
    )


def _format_salary_range(row) -> str | None:
    lo, hi, currency = row["salary_from"], row["salary_to"], row["currency"]
    if not lo and not hi:
        return None
    label = "₽" if currency == "RUR" else (currency or "")
    if lo and hi:
        return f"{_format_rur(lo)}–{_format_rur(hi)} {label}".strip()
    if lo:
        return f"от {_format_rur(lo)} {label}".strip()
    return f"до {_format_rur(hi)} {label}".strip()


def _row_to_view(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "employer": row["employer"],
        "area": row["area"],
        "score": row["score"],
        "track": row["track"],
        "salary_fit": row["salary_fit"],
        "salary_display": _format_salary_range(row),
        "published_at": row["published_at"],
        "red_flags": json.loads(row["red_flags"] or "[]"),
        "rationale": row["rationale"],
        "recommend": row["recommend"] or "consider",
        "recommend_label": _RECOMMEND_LABELS.get(row["recommend"] or "consider", row["recommend"]),
        "status": row["status"],
        "url": row["alternate_url"] or row["url"],
        "metro_station": row["metro_station"],
        "metro_line": row["metro_line"],
        "metro_priority": bool(row["metro_priority"]),
        "liked": bool(row["liked"]),
        "archived": bool(row["archived"]),
        "archive_checked_at": row["archive_checked_at"],
        "decision": row["decision"],
        "decision_reason": row["decision_reason"],
        "source": row["source"] or "hh",
        "source_label": _SOURCE_LABELS.get(row["source"] or "hh", row["source"]),
        "ats_keywords": json.loads(row["ats_keywords"] or "[]"),
    }


def create_app(cfg: dict) -> Flask:
    app = Flask(__name__, template_folder=str(TEMPLATE_DIR))

    storage = Storage(cfg["paths"]["db"])
    hh = HHClient(get_hh_config(cfg))
    sj_cfg = get_superjob_config(cfg)
    sj = SuperJobClient(sj_cfg) if sj_cfg else None
    career_base_path = Path(cfg["paths"]["career_base_md"])
    # словарь, а не голая переменная — чтобы правку через /settings/career-base
    # было видно сразу во всех роутах без перезапуска процесса (нужен mutable
    # контейнер, простое переприсваивание в closure тут не сработает)
    career_state = {"text": load_career_base(str(career_base_path))}
    career_state["candidate_name"] = extract_candidate_name(career_state["text"])
    # плоские справочники городов для /tool/areas — тянутся из API один раз и
    # держатся в памяти процесса (справочники регионов меняются очень редко,
    # перечитывать их на каждый запрос смысла нет; сбрасывается перезапуском serve)
    geo_cache: dict[str, list[dict] | None] = {"hh": None, "superjob": None}
    # .resolve() — важно для send_file (см. vacancy_resume_docx): Flask резолвит
    # относительные пути в send_file от root_path пакета (src/), а не от рабочей
    # директории процесса, так что "./out" тихо ломался на скачивании .docx.
    out_dir = Path(cfg["paths"]["out_dir"]).resolve()
    # получаем конфиг модели один раз при старте — если ключ/folder_id не заданы,
    # процесс упадёт сразу при запуске serve, а не посреди случайного запроса
    tailor_ycfg = get_yandex_config(cfg, cfg["yandex"]["tailor_model"])
    # линии метро — настройка, которая может поменяться через /settings, пока
    # серверный процесс уже запущен, поэтому читаем её каждый раз заново из
    # storage, а не кэшируем в closure (в отличие от tailor_ycfg/career_state)

    w = cfg["webapp"]
    login_user = os.environ.get(w["login_user_env"])
    login_password = os.environ.get(w["login_password_env"])
    if not login_user or not login_password:
        raise SystemExit(
            f"Не заданы {w['login_user_env']}/{w['login_password_env']} в .env — "
            "нужны для входа в веб-интерфейс."
        )
    secret_key = os.environ.get("WEBAPP_SECRET_KEY")
    if not secret_key:
        raise SystemExit(
            "Не задан WEBAPP_SECRET_KEY в .env — нужен для подписи сессии логина "
            "(сгенерировать: python -c \"import secrets; print(secrets.token_hex(32))\")."
        )
    app.secret_key = secret_key
    app.permanent_session_lifetime = timedelta(days=30)

    @app.before_request
    def require_auth():
        if request.endpoint == "login":
            return
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            ok = secrets.compare_digest(username, login_user) and secrets.compare_digest(
                password, login_password
            )
            if ok:
                session.permanent = True
                session["logged_in"] = True
                # next — только относительный путь на этот же сайт (защита от open redirect)
                next_url = request.args.get("next") or ""
                if not next_url.startswith("/") or next_url.startswith("//"):
                    next_url = url_for("index")
                return redirect(next_url)
            return render_template("login.html", page="login", error="Неверный логин или пароль."), 401
        return render_template("login.html", page="login", error=None)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/help")
    def help_page():
        return render_template("help.html", metro_lines=get_priority_metro_lines(storage))

    @app.get("/stats")
    def stats_page():
        rows = storage.count_by_day()
        days = [{"day": r["day"], "count": r["cnt"]} for r in rows]
        salary_values = storage.salary_values()
        salary_stats = None
        salary_buckets = []
        if salary_values:
            salary_stats = {
                "avg": _format_rur(statistics.mean(salary_values)),
                "median": _format_rur(statistics.median(salary_values)),
                "count": len(salary_values),
            }
            bucket_size = 50_000
            counts: dict[int, int] = {}
            for v in salary_values:
                b = int(v // bucket_size)
                counts[b] = counts.get(b, 0) + 1
            salary_buckets = [
                {"label": f"{b * bucket_size // 1000}–{(b + 1) * bucket_size // 1000}к", "count": counts[b]}
                for b in sorted(counts)
            ]

        # динамика по неделям — сколько в среднем стоили вакансии, найденные в
        # ту или иную неделю (по дню первого обнаружения, ISO-неделя)
        week_buckets: dict[str, list[float]] = {}
        for fetched_at, value in storage.salary_values_with_date():
            try:
                d = datetime.fromisoformat(fetched_at)
            except ValueError:
                continue
            iso_year, iso_week, _ = d.isocalendar()
            week_buckets.setdefault(f"{iso_year}-W{iso_week:02d}", []).append(value)
        salary_weekly = [
            {
                "week": w,
                "avg": round(statistics.mean(vals)),
                "avg_display": _format_rur(statistics.mean(vals)),
                "count": len(vals),
            }
            for w, vals in sorted(week_buckets.items())
        ]

        return render_template(
            "stats.html",
            days=days,
            salary_stats=salary_stats,
            salary_buckets=salary_buckets,
            salary_weekly=salary_weekly,
        )

    @app.context_processor
    def inject_nav_counts():
        # до входа (страница /login) даже не ходим в базу — незачем
        if not session.get("logged_in"):
            return {"nav_counts": None}
        return {"nav_counts": storage.count_by_decision()}

    def _render_list(page: str, decision: str | None):
        status = request.args.get("status") or None
        min_score = request.args.get("min_score", type=int)
        sort = request.args.get("sort") or "score"
        total = storage.count_scored(status=status, min_score=min_score, decision=decision)
        total_pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
        page_num = min(max(request.args.get("page", 1, type=int), 1), total_pages)
        rows = storage.list_scored(
            status=status, min_score=min_score, decision=decision, sort=sort,
            limit=PAGE_SIZE, offset=(page_num - 1) * PAGE_SIZE,
        )
        vacancies = [_row_to_view(r) for r in rows]
        daily_comment_row = storage.get_latest_daily_comment() if page == "unsorted" else None
        return render_template(
            "list.html",
            page=page,
            vacancies=vacancies,
            status=status or "",
            min_score=min_score or "",
            sort=sort,
            page_num=page_num,
            total_pages=total_pages,
            total=total,
            daily_comment=daily_comment_row["comment"] if daily_comment_row else None,
            daily_comment_day=daily_comment_row["day"] if daily_comment_row else None,
        )

    @app.get("/")
    def index():
        # главная — рабочая очередь: цель разобрать её до нуля (см. /liked, /archive)
        return _render_list("unsorted", "unsorted")

    @app.get("/liked")
    def liked_page():
        return _render_list("liked", "fit")

    @app.get("/archive")
    def archive_page():
        return _render_list("archive", "not_fit")

    @app.get("/settings")
    def settings_page():
        backup_keep_count = int(storage.get_setting("backup_keep_count", "7"))
        collection_paused = storage.get_setting("collection_paused") == "1"
        source_hh_enabled = storage.get_setting("source_hh_enabled", "1") == "1"
        source_superjob_enabled = storage.get_setting("source_superjob_enabled", "1") == "1"
        auto_reject_max_score = int(storage.get_setting("auto_reject_max_score", "40"))
        search_area = storage.get_setting("search_area", "1")
        search_superjob_town = storage.get_setting("superjob_town", "4")
        search_salary_from = int(storage.get_setting("search_salary_from", "0") or 0)
        metro_lines_text = "\n".join(get_priority_metro_lines(storage))
        return render_template(
            "settings.html",
            page="settings",
            backup_keep_count=backup_keep_count,
            collection_paused=collection_paused,
            source_hh_enabled=source_hh_enabled,
            source_superjob_enabled=source_superjob_enabled,
            auto_reject_max_score=auto_reject_max_score,
            cities=CITIES,
            search_area=search_area,
            search_superjob_town=search_superjob_town,
            search_salary_from=search_salary_from,
            metro_lines_text=metro_lines_text,
            career_base=career_state["text"],
        )

    @app.post("/settings/backup-keep")
    def settings_backup_keep():
        try:
            keep = int(request.form.get("backup_keep_count", ""))
        except ValueError:
            abort(400, "Число копий должно быть целым числом.")
        if keep < 1:
            abort(400, "Число копий должно быть не меньше 1.")
        storage.set_setting("backup_keep_count", str(keep))
        return redirect(url_for("settings_page"))

    @app.post("/settings/auto-reject")
    def settings_auto_reject():
        try:
            threshold = int(request.form.get("auto_reject_max_score", ""))
        except ValueError:
            abort(400, "Порог должен быть целым числом.")
        if not (0 <= threshold <= 100):
            abort(400, "Порог должен быть от 0 до 100.")
        storage.set_setting("auto_reject_max_score", str(threshold))
        return redirect(url_for("settings_page"))

    @app.post("/settings/collection")
    def settings_collection():
        action = request.form.get("action")
        if action not in ("pause", "resume"):
            abort(400)
        storage.set_setting("collection_paused", "1" if action == "pause" else "0")
        return redirect(url_for("settings_page"))

    @app.post("/settings/search")
    def settings_search():
        custom_area = (request.form.get("custom_area") or "").strip()
        if custom_area:
            area, town = custom_area, (request.form.get("custom_town") or "").strip()
        else:
            area, _, town = (request.form.get("city") or "").partition("|")
        if not area.isdigit():
            abort(400, "Регион (HH area id) должен быть числом.")
        storage.set_setting("search_area", area)
        storage.set_setting("superjob_town", town if town.isdigit() else "")

        try:
            salary = int(request.form.get("search_salary_from", "0") or 0)
        except ValueError:
            abort(400, "Зарплатный порог должен быть целым числом.")
        if salary < 0:
            abort(400, "Зарплатный порог не может быть отрицательным.")
        storage.set_setting("search_salary_from", str(salary))

        lines = [ln.strip() for ln in (request.form.get("priority_metro_lines") or "").splitlines() if ln.strip()]
        storage.set_setting("priority_metro_lines", json.dumps(lines, ensure_ascii=False))
        return redirect(url_for("settings_page"))

    @app.post("/settings/sources")
    def settings_sources():
        source = request.form.get("source")
        action = request.form.get("action")
        if source not in ("hh", "superjob") or action not in ("enable", "disable"):
            abort(400)
        storage.set_setting(f"source_{source}_enabled", "1" if action == "enable" else "0")
        return redirect(url_for("settings_page"))

    @app.post("/settings/career-base")
    def settings_career_base():
        text = request.form.get("career_base", "")
        if not text.strip():
            abort(400, "Карьерная база не может быть пустой.")
        career_base_path.write_text(text, encoding="utf-8")
        career_state["text"] = text
        career_state["candidate_name"] = extract_candidate_name(text)
        return redirect(url_for("settings_page"))

    @app.get("/tool/areas")
    def areas_lookup():
        q = (request.args.get("q") or "").strip()
        hh_results: list[dict] = []
        sj_results: list[dict] = []
        if q:
            if geo_cache["hh"] is None:
                geo_cache["hh"] = flatten_hh_areas(hh.get_areas())
            if sj is not None and geo_cache["superjob"] is None:
                geo_cache["superjob"] = flatten_superjob_towns(sj.get_towns())
            ql = q.lower()
            hh_results = [a for a in geo_cache["hh"] if ql in a["name"].lower()][:40]
            if geo_cache["superjob"]:
                sj_results = [t for t in geo_cache["superjob"] if ql in t["name"].lower()][:40]
        return render_template("areas.html", page="tool", q=q, hh_results=hh_results, sj_results=sj_results)

    def _generate_tailor_files(vacancy_id: str, text: str) -> None:
        """Общий код генерации 4 файлов (notes/resume_full/letter/docx) —
        используется и обычной кнопкой на карточке, и инструментом "вакансия
        по ссылке" (см. score_url_submit)."""
        notes, resume_full, letter = tailor_for_vacancy(tailor_ycfg, career_state["text"], text)
        v_out_dir = out_dir / vacancy_id
        v_out_dir.mkdir(parents=True, exist_ok=True)
        (v_out_dir / "resume_notes.md").write_text(notes, encoding="utf-8")
        (v_out_dir / "resume_full.md").write_text(resume_full, encoding="utf-8")
        (v_out_dir / "cover_letter.txt").write_text(letter, encoding="utf-8")
        (v_out_dir / "vacancy.txt").write_text(text, encoding="utf-8")
        build_resume_docx(resume_full, career_state["candidate_name"]).save(v_out_dir / "resume.docx")

    @app.get("/vacancy/<vacancy_id>")
    def vacancy_detail(vacancy_id: str):
        row = storage.get(vacancy_id)
        if row is None:
            abort(404)
        unavailable_notice = None
        try:
            full = get_full_vacancy(hh, sj, vacancy_id, row["source"])
            description_html = _render_description(full.get("description"))
        except _SOURCE_ERRORS as e:
            # вакансия могла быть удалена с сайта-источника целиком (не просто
            # в архиве) — тогда карточку по API уже не получить никогда.
            # Показываем то, что уже сохранено, вместо падения с 500.
            log.warning("Не удалось получить %s (%s): %s", vacancy_id, row["source"], e)
            saved_text_path = out_dir / vacancy_id / "vacancy.txt"
            if saved_text_path.exists():
                saved_text = saved_text_path.read_text(encoding="utf-8")
                description_html = f"<pre style='white-space:pre-wrap'>{bleach.clean(saved_text)}</pre>"
            else:
                raw = json.loads(row["raw_json"] or "{}")
                snippet = raw.get("snippet") or {}
                snippet_text = " ".join(
                    filter(None, [snippet.get("requirement"), snippet.get("responsibility")])
                )
                description_html = (
                    f"<p>{bleach.clean(snippet_text)}</p>" if snippet_text else "<p><em>Нет сохранённого текста.</em></p>"
                )
            unavailable_notice = "Не удалось получить полное описание с источника — вакансия, вероятно, удалена или в архиве. Показан сохранённый вариант."
        notes_path = out_dir / vacancy_id / "resume_notes.md"
        resume_full_path = out_dir / vacancy_id / "resume_full.md"
        letter_path = out_dir / vacancy_id / "cover_letter.txt"
        docx_path = out_dir / vacancy_id / "resume.docx"
        notes = notes_path.read_text(encoding="utf-8") if notes_path.exists() else None
        resume_full = resume_full_path.read_text(encoding="utf-8") if resume_full_path.exists() else None
        letter = letter_path.read_text(encoding="utf-8") if letter_path.exists() else None
        return render_template(
            "detail.html",
            v=_row_to_view(row),
            description_html=description_html,
            unavailable_notice=unavailable_notice,
            notes=notes,
            resume_full=resume_full,
            letter=letter,
            has_docx=docx_path.exists(),
        )

    @app.post("/vacancy/<vacancy_id>/tailor")
    def vacancy_tailor(vacancy_id: str):
        row = storage.get(vacancy_id)
        if row is None:
            abort(404)
        try:
            full = get_full_vacancy(hh, sj, vacancy_id, row["source"])
            text = vacancy_to_text(full, get_priority_metro_lines(storage))
        except _SOURCE_ERRORS as e:
            log.warning("Не удалось получить %s (%s): %s", vacancy_id, row["source"], e)
            saved_text_path = out_dir / vacancy_id / "vacancy.txt"
            if not saved_text_path.exists():
                abort(
                    400,
                    "Вакансия недоступна на источнике (удалена/архив), а сохранённого текста "
                    "нет — резюме и письмо сгенерировать не из чего.",
                )
            text = saved_text_path.read_text(encoding="utf-8")
        _generate_tailor_files(vacancy_id, text)
        return redirect(url_for("vacancy_detail", vacancy_id=vacancy_id))

    @app.get("/vacancy/<vacancy_id>/resume.docx")
    def vacancy_resume_docx(vacancy_id: str):
        row = storage.get(vacancy_id)
        if row is None:
            abort(404)
        docx_path = out_dir / vacancy_id / "resume.docx"
        if not docx_path.exists():
            abort(404, "Резюме ещё не сгенерировано — сначала «Подготовить резюме и письмо».")
        safe_name = "".join(c for c in (row["name"] or "vacancy") if c.isalnum() or c in " -_").strip()
        download_name = f"Резюме_{safe_name}_{vacancy_id}.docx"[:120]
        return send_file(docx_path, as_attachment=True, download_name=download_name)

    @app.post("/vacancy/<vacancy_id>/status")
    def vacancy_status(vacancy_id: str):
        status = request.form.get("status")
        if status not in ("interested", "skip", "applied", "new"):
            abort(400)
        storage.mark_status(vacancy_id, status)
        if status == "applied":
            # откликнулся — значит точно "подходит", нельзя оставлять в очереди
            # разбора или числить в архиве "не подходит"
            storage.set_decision(vacancy_id, "fit")
            storage.set_liked(vacancy_id, True)
        return redirect(url_for("vacancy_detail", vacancy_id=vacancy_id))

    @app.post("/vacancy/<vacancy_id>/decide")
    def vacancy_decide(vacancy_id: str):
        decision = request.form.get("decision")
        if decision not in ("fit", "not_fit", "clear"):
            abort(400)
        reason = (request.form.get("reason") or "").strip() or None
        storage.set_decision(vacancy_id, None if decision == "clear" else decision, reason)
        # "подходит"/"по душе" — одно и то же, не два разных понятия; liked
        # заодно решает, какие вакансии check-liked проверяет на актуальность.
        # skip == "не подходит" по той же логике.
        if decision == "fit":
            storage.set_liked(vacancy_id, True)
        elif decision == "not_fit":
            storage.set_liked(vacancy_id, False)
            storage.mark_status(vacancy_id, "skip")
        # со списка — назад в список (с теми же фильтрами), с карточки — на карточку
        return redirect(request.referrer or url_for("vacancy_detail", vacancy_id=vacancy_id))

    @app.post("/vacancy/<vacancy_id>/check")
    def vacancy_check(vacancy_id: str):
        row = storage.get(vacancy_id)
        if row is None:
            abort(404)
        refresh_vacancy_status(hh, sj, storage, vacancy_id, row["source"])
        return redirect(url_for("vacancy_detail", vacancy_id=vacancy_id))

    @app.get("/tool/score-url")
    def score_url_form():
        return render_template("score_url.html", page="tool")

    @app.post("/tool/score-url")
    def score_url_submit():
        url = (request.form.get("url") or "").strip()
        parsed = parse_vacancy_url(url) if url else None
        if parsed is None:
            return render_template(
                "score_url.html",
                page="tool",
                url=url,
                error="Не распознал ссылку — нужна прямая ссылка на вакансию hh.ru "
                "(hh.ru/vacancy/<id>) или superjob.ru (.../vakansii/...-<id>.html).",
            )
        source, native = parsed
        vacancy_id = native if source == "hh" else sj_prefixed_id(native)
        try:
            full = get_full_vacancy(hh, sj, vacancy_id, source)
        except _SOURCE_ERRORS as e:
            return render_template(
                "score_url.html", page="tool", url=url,
                error=f"Не удалось получить вакансию с {_SOURCE_LABELS.get(source, source)}: {e}",
            )
        row = storage.get(vacancy_id)
        if row is None:
            storage.upsert_vacancy(full, source=source, origin="manual_url")
            row = storage.get(vacancy_id)
        priority_lines = get_priority_metro_lines(storage)
        text = vacancy_to_text(full, priority_lines)
        if row["score"] is None:
            scorer_ycfg = get_yandex_config(cfg, cfg["yandex"]["scorer_model"])
            corrections_note = build_corrections_note(storage.disagreements())
            result = score_vacancy(scorer_ycfg, career_state["text"], text, corrections_note)
            station, line = get_metro(full)
            metro = {"station": station, "line": line, "priority": bool(line and line in priority_lines)}
            storage.save_score(vacancy_id, result, metro)
        _generate_tailor_files(vacancy_id, text)
        return redirect(url_for("vacancy_detail", vacancy_id=vacancy_id))

    return app
