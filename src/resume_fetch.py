"""
Получение текста резюме с hh.ru по прямой ссылке — БЕЗ OAuth.

Официальный HH API не даёт публичного/токен-приложения эндпоинта на чтение
чужого (в т.ч. своего) резюме — это требует персональной OAuth-авторизации
(authorization_code grant), которой в проекте сознательно нет (см. hh_client.py:
только client_credentials, токен приложения, без входа в личный аккаунт).

Вместо API — обычный HTTP-запрос публичной страницы резюме, ровно то же самое,
что делает браузер, когда резюме отмечено «видно всем компаниям» на hh.ru.
Приватные резюме так прочитать нельзя (и не должно быть можно).

Экстрактор текста — общий (весь видимый текст страницы, без спец-логики под
конкретные data-qa/классы hh.ru): вёрстка страницы результатов поиска резюме
не документирована как публичный контракт (в отличие от HH API) и может
измениться в любой момент, поэтому не привязываемся к хрупким селекторам.
Один плоский текстовый блок — этого достаточно для промпта LLM.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

import requests

_HH_RESUME_URL_RE = re.compile(r"hh\.ru/resume/([0-9a-f]+)")

# Ниже этой длины извлечённый текст считаем мусором — вёрстка не совпала с
# ожидаемой (JS-заглушка, редирект на логин без явного статуса и т.п.), а не
# честным коротким резюме.
_MIN_TEXT_LENGTH = 200

_SKIP_TAGS = {"script", "style", "noscript", "svg", "header", "footer", "nav"}


class ResumeFetchError(RuntimeError):
    """Не удалось получить или прочитать резюме — сообщение уже человекочитаемое."""


class _VisibleTextExtractor(HTMLParser):
    """Собирает видимый текст страницы, пропуская script/style/nav/header/footer.
    Если в разметке есть <main> — берём текст только из него (обычно это и есть
    содержательная часть страницы, без шапки/подвала/меню сайта); иначе — весь
    <body>. Общий, не завязанный на конкретную вёрстку hh.ru подход."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._main_depth: int | None = None
        self._body_depth = 0
        self._depth = 0
        self._main_chunks: list[str] = []
        self._body_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._depth += 1
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "main" and self._main_depth is None:
            self._main_depth = self._depth
        elif tag == "body":
            self._body_depth = self._depth

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # самозакрывающиеся теги (<br/>, <img/> и т.п.) не меняют глубину/стек
        pass

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag == "main" and self._main_depth is not None and self._depth == self._main_depth:
            self._main_depth = None
        self._depth = max(self._depth - 1, 0)

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        text = data.strip()
        if not text:
            return
        if self._main_depth is not None:
            self._main_chunks.append(text)
        elif self._body_depth:
            self._body_chunks.append(text)

    def get_text(self) -> str:
        chunks = self._main_chunks or self._body_chunks
        return "\n".join(chunks)


def parse_resume_url(url: str) -> str | None:
    """Возвращает hash резюме из ссылки вида hh.ru/resume/<hash> (в т.ч. с
    региональным поддоменом типа spb.hh.ru — .search(), как у _HH_URL_RE в
    sources.py), либо None, если ссылка не распознана."""
    m = _HH_RESUME_URL_RE.search(url)
    return m.group(1) if m else None


def fetch_resume_text(url: str, user_agent: str) -> str:
    """Скачивает публичную страницу резюме и возвращает извлечённый видимый
    текст. `user_agent` — обычный HTTP-заголовок User-Agent (НЕ HH-User-Agent,
    который hh_client.py шлёт в официальный API — разные вещи для разных
    запросов), берём то же значение, что и у API-клиента (cfg["hh"]["user_agent"]).

    Бросает ResumeFetchError с готовым для показа пользователю текстом на
    любую проблему: сеть, код ответа, пустой/подозрительно короткий результат."""
    try:
        resp = requests.get(url, headers={"User-Agent": user_agent}, timeout=20, allow_redirects=True)
    except requests.RequestException as e:
        raise ResumeFetchError(f"сетевая ошибка — {e}") from e

    if resp.status_code == 404:
        raise ResumeFetchError("резюме не найдено — проверь ссылку.")
    if resp.status_code in (401, 403):
        raise ResumeFetchError(
            "резюме недоступно — скорее всего оно не отмечено как «видно всем "
            "компаниям» в настройках видимости на hh.ru."
        )
    if resp.status_code != 200:
        raise ResumeFetchError(f"сайт ответил кодом {resp.status_code}.")

    parser = _VisibleTextExtractor()
    try:
        parser.feed(resp.text)
    except Exception as e:  # noqa: BLE001 — html.parser редко, но может споткнуться на битой разметке
        raise ResumeFetchError(f"не получилось разобрать страницу — {e}") from e
    text = parser.get_text()

    if len(text) < _MIN_TEXT_LENGTH:
        raise ResumeFetchError(
            "не получилось прочитать текст резюме со страницы — возможно, "
            "hh.ru изменил вёрстку, либо резюме не отмечено «видно всем "
            "компаниям» (страница выглядит как заглушка/логин, а не резюме)."
        )
    return text
