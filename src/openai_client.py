"""
Клиент для любого OpenAI-совместимого эндпоинта (`/chat/completions`,
`/models`) — третий LLM-провайдер поверх LLMProvider (src/llm_provider.py),
опциональный аналог yandex_client.py/gigachat_client.py.

Смысл: не быть завязанным на конкретного вендора (Yandex/GigaChat) — тем же
кодом можно сходить в OpenAI, OpenRouter или локальную модель (Ollama, LM
Studio, vLLM — все они говорят по этому же протоколу), просто поменяв
base_url в config.yaml. Ключ (api_key) опционален: у локальных серверов его
обычно нет вообще, тогда заголовок Authorization не отправляется.

Проверено вживую перед написанием файла на реальном сервере: у Yandex
Foundation Models тоже есть OpenAI-совместимый эндпоинт
(https://ai.api.cloud.yandex.net/v1/chat/completions, Authorization: Bearer
<YANDEX_API_KEY>, model="gpt://<folder_id>/<model>/<version>") — им и
проверяли этот провайдер на настоящей модели без новых кредов.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from .llm_provider import LLMProvider

log = logging.getLogger("openai_client")

# Показывается в /settings вместо сырого текста ошибки провайдера (может быть
# длинным JSON — например OpenAI на 403 из региона, где сервис не поддерживается,
# отдаёт целый объект error) — подробности всегда есть в логе, а как реально
# подключить провайдера (base_url/ключ) — см. help.html → «Как подключить провайдера».
_NOT_CONNECTED_MSG = 'Не подключён — проверь base_url/ключ в config.yaml (см. «Справка» → «Как подключить провайдера»).'


@dataclass
class OpenAIConfig:
    base_url: str  # без хвостового /, например "https://api.openai.com/v1"
    api_key: str | None = None


def _headers(cfg: OpenAIConfig) -> dict:
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    return headers


def complete(
    cfg: OpenAIConfig,
    system_prompt: str,
    user_content: str,
    model: str,
    max_tokens: int = 1000,
    temperature: float = 0.3,
) -> tuple[str, dict]:
    """Один запрос completion — текст ответа модели + использованные токены
    (пустой dict, если сервер usage не вернул — бывает у локальных моделей;
    storage.record_token_usage() пустой usage просто пропускает)."""
    resp = requests.post(
        f"{cfg.base_url}/chat/completions",
        headers=_headers(cfg),
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        timeout=90,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI-совместимый API {resp.status_code} ({cfg.base_url}): {resp.text[:300]}")
    data = resp.json()
    try:
        text = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Неожиданный формат ответа ({cfg.base_url}): {e}\n{data}") from e
    raw_usage = data.get("usage") or {}
    usage = {
        "prompt_tokens": raw_usage.get("prompt_tokens", 0),
        "completion_tokens": raw_usage.get("completion_tokens", 0),
        "total_tokens": raw_usage.get("total_tokens", 0),
    }
    return text, usage


def list_models(cfg: OpenAIConfig) -> list[str]:
    """Реальный каталог моделей эндпоинта (GET /models, стандартный для
    OpenAI-совместимых серверов) — вместо угадывания имени модели."""
    try:
        resp = requests.get(f"{cfg.base_url}/models", headers=_headers(cfg), timeout=15)
        resp.raise_for_status()
        return sorted(m["id"] for m in resp.json().get("data", []) if m.get("id"))
    except Exception as e:
        log.warning("Каталог моделей (%s) недоступен: %s", cfg.base_url, e)
        raise RuntimeError(_NOT_CONNECTED_MSG) from e


def ping(cfg: OpenAIConfig, model: str) -> tuple[bool, str]:
    """Минимальный синхронный запрос — для кнопки проверки связи в /settings."""
    try:
        text, _usage = complete(cfg, "Ответь одним словом.", "Скажи 'ок'.", model, max_tokens=16, temperature=0)
        return True, text or "(пустой ответ)"
    except Exception as e:
        log.warning("OpenAI-совместимый провайдер (%s) не отвечает: %s", cfg.base_url, e)
        return False, _NOT_CONNECTED_MSG


class OpenAIProvider(LLMProvider):
    """Обёртка под интерфейс LLMProvider (см. src/llm_provider.py) —
    scorer.py/tailor.py/digest.py зовут provider.complete(...) одинаково
    для Yandex, GigaChat и любого OpenAI-совместимого эндпоинта."""

    name = "openai"

    def __init__(self, cfg: OpenAIConfig, model: str):
        self.cfg = cfg
        self.model = model
        self.last_usage: dict | None = None

    def complete(
        self, system_prompt: str, user_content: str, max_tokens: int = 1000, temperature: float = 0.3
    ) -> str:
        text, usage = complete(self.cfg, system_prompt, user_content, self.model, max_tokens, temperature)
        self.last_usage = usage
        return text
