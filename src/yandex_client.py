"""
Общий клиент для YandexGPT (Yandex Cloud, Foundation Models API).

Используется и скорингом, и tailor'ом — единая точка похода в модель,
чтобы формат запроса/ответа не дублировался в двух местах.

Документация: https://yandex.cloud/ru/docs/foundation-models/concepts/yandexgpt/
Аутентификация — статический API-ключ ("Authorization: Api-Key <key>").
folder_id зашивается прямо в modelUri (gpt://<folder_id>/<model>).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

from .llm_provider import LLMProvider

log = logging.getLogger("yandex_client")

COMPLETION_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
ASYNC_COMPLETION_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completionAsync"
OPERATION_URL = "https://llm.api.cloud.yandex.net/operations/{operation_id}"
MODELS_URL = "https://ai.api.cloud.yandex.net/v1/models"


@dataclass
class YandexConfig:
    api_key: str
    folder_id: str
    model: str  # напр. "yandexgpt-lite/latest" или "yandexgpt/latest"


def _extract_usage(usage: dict) -> dict:
    """Yandex отдаёт токены строками (inputTextTokens/completionTokens/totalTokens,
    см. живой ответ API) — приводим к int и к именам полей, общим с GigaChat
    (prompt_tokens/completion_tokens/total_tokens), для единого token_usage в БД."""
    return {
        "prompt_tokens": int(usage.get("inputTextTokens", 0)),
        "completion_tokens": int(usage.get("completionTokens", 0)),
        "total_tokens": int(usage.get("totalTokens", 0)),
    }


def complete(cfg: YandexConfig, system_prompt: str, user_content: str,
             max_tokens: int = 1000, temperature: float = 0.3) -> tuple[str, dict]:
    """Один запрос completion — текст ответа модели + использованные токены."""
    model_uri = f"gpt://{cfg.folder_id}/{cfg.model}"
    resp = requests.post(
        COMPLETION_URL,
        headers={
            "Authorization": f"Api-Key {cfg.api_key}",
            "content-type": "application/json",
        },
        json={
            "modelUri": model_uri,
            "completionOptions": {
                "stream": False,
                "temperature": temperature,
                "maxTokens": str(max_tokens),
            },
            "messages": [
                {"role": "system", "text": system_prompt},
                {"role": "user", "text": user_content},
            ],
        },
        timeout=90,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"YandexGPT API {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    try:
        text = data["result"]["alternatives"][0]["message"]["text"].strip()
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Неожиданный формат ответа YandexGPT: {e}\n{data}") from e
    return text, _extract_usage(data["result"].get("usage", {}))


def complete_async(
    cfg: YandexConfig,
    system_prompt: str,
    user_content: str,
    max_tokens: int = 1000,
    temperature: float = 0.3,
    poll_interval: float = 1.0,
    timeout: float = 120.0,
) -> tuple[str, dict]:
    """То же самое, что complete(), но через completionAsync + polling операции —
    для cron-пайплайна (fetch → score → digest) реальное время ожидания не
    видно пользователю, так что polling не мешает. Формат запроса и итоговый
    текст ответа — те же поля, что и у синхронного complete()."""
    model_uri = f"gpt://{cfg.folder_id}/{cfg.model}"
    resp = requests.post(
        ASYNC_COMPLETION_URL,
        headers={
            "Authorization": f"Api-Key {cfg.api_key}",
            "content-type": "application/json",
        },
        json={
            "modelUri": model_uri,
            "completionOptions": {
                "stream": False,
                "temperature": temperature,
                "maxTokens": str(max_tokens),
            },
            "messages": [
                {"role": "system", "text": system_prompt},
                {"role": "user", "text": user_content},
            ],
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"YandexGPT API (async submit) {resp.status_code}: {resp.text[:300]}")
    operation_id = resp.json()["id"]

    deadline = time.monotonic() + timeout
    while True:
        op_resp = requests.get(
            OPERATION_URL.format(operation_id=operation_id),
            headers={"Authorization": f"Api-Key {cfg.api_key}"},
            timeout=30,
        )
        if op_resp.status_code != 200:
            raise RuntimeError(f"YandexGPT API (operation poll) {op_resp.status_code}: {op_resp.text[:300]}")
        data = op_resp.json()
        if data.get("done"):
            if data.get("error"):
                raise RuntimeError(f"YandexGPT async-операция завершилась ошибкой: {data['error']}")
            try:
                text = data["response"]["alternatives"][0]["message"]["text"].strip()
            except (KeyError, IndexError) as e:
                raise RuntimeError(f"Неожиданный формат ответа YandexGPT (async): {e}\n{data}") from e
            return text, _extract_usage(data["response"].get("usage", {}))
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"YandexGPT async-операция не завершилась за {timeout:.0f} сек (operation_id={operation_id})"
            )
        time.sleep(poll_interval)


def list_models(cfg: YandexConfig) -> list[str]:
    """Реальный каталог моделей аккаунта через OpenAI-совместимый эндпоинт
    Foundation Models — вместо угадывания modelUri по докам (см. models.yaml).
    Возвращает "model/version" в том же формате, что modelUri
    (gpt://<folder_id>/<model>/<version>)."""
    resp = requests.get(
        MODELS_URL,
        headers={"Authorization": f"Api-Key {cfg.api_key}", "x-folder-id": cfg.folder_id},
        timeout=15,
    )
    resp.raise_for_status()
    prefix = f"gpt://{cfg.folder_id}/"
    return sorted(
        m["id"][len(prefix):] for m in resp.json().get("data", []) if m["id"].startswith(prefix)
    )


def ping(cfg: YandexConfig) -> tuple[bool, str]:
    """Минимальный синхронный запрос — для кнопки проверки связи в /settings.
    Не использует complete_async, чтобы результат был виден сразу же."""
    try:
        text, _usage = complete(cfg, "Ответь одним словом.", "Скажи 'ок'.", max_tokens=16, temperature=0)
        return True, text or "(пустой ответ)"
    except Exception as e:
        return False, str(e)


class YandexProvider(LLMProvider):
    """Обёртка над complete()/complete_async() под интерфейс LLMProvider — так
    scorer.py/tailor.py/digest.py зовут provider.complete(...) одинаково,
    не зная ни про какого провайдера, ни про sync/async режим."""

    name = "yandex"

    def __init__(self, cfg: YandexConfig, mode: str = "sync"):
        self.cfg = cfg
        self.mode = mode
        self.last_usage: dict | None = None

    def complete(
        self, system_prompt: str, user_content: str, max_tokens: int = 1000, temperature: float = 0.3
    ) -> str:
        fn = complete_async if self.mode == "async" else complete
        text, usage = fn(self.cfg, system_prompt, user_content, max_tokens, temperature)
        self.last_usage = usage
        return text
