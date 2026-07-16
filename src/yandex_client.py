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
from dataclasses import dataclass

import requests

log = logging.getLogger("yandex_client")

COMPLETION_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"


@dataclass
class YandexConfig:
    api_key: str
    folder_id: str
    model: str  # напр. "yandexgpt-lite/latest" или "yandexgpt/latest"


def complete(cfg: YandexConfig, system_prompt: str, user_content: str,
             max_tokens: int = 1000, temperature: float = 0.3) -> str:
    """Один запрос completion, возвращает текст ответа модели."""
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
        return data["result"]["alternatives"][0]["message"]["text"].strip()
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Неожиданный формат ответа YandexGPT: {e}\n{data}") from e
