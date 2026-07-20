"""
Клиент для GigaChat (Sber) — второй LLM-провайдер поверх LLMProvider
(src/llm_provider.py), опциональный аналог yandex_client.py.

Использует официальную библиотеку `pip install gigachat`. Аутентификация —
Authorization key (base64-строка client_id:client_secret из личного кабинета
Sber Studio) в GIGACHAT_CREDENTIALS; библиотека сама обменивает его на
access token через OAuth и обновляет за минуту до истечения — ключ передаётся
один раз при создании клиента, ничего вручную обновлять не нужно.

verify_ssl_certs=False по умолчанию: у GigaChat собственный корневой
сертификат (Минцифры), которого обычно нет в системном хранилище — без
этого флага запросы падают с ошибкой проверки TLS-сертификата.
"""
from __future__ import annotations

from dataclasses import dataclass

from gigachat import GigaChat
from gigachat.models import Chat, Messages, MessagesRole

from .llm_provider import LLMProvider


@dataclass
class GigaChatConfig:
    credentials: str
    scope: str = "GIGACHAT_API_PERS"
    verify_ssl_certs: bool = False


def complete(
    cfg: GigaChatConfig,
    system_prompt: str,
    user_content: str,
    model: str,
    max_tokens: int = 1000,
    temperature: float = 0.3,
) -> tuple[str, dict]:
    """Один запрос completion — текст ответа модели + использованные токены."""
    with GigaChat(
        credentials=cfg.credentials,
        scope=cfg.scope,
        verify_ssl_certs=cfg.verify_ssl_certs,
        model=model,
    ) as client:
        response = client.chat(
            Chat(
                messages=[
                    Messages(role=MessagesRole.SYSTEM, content=system_prompt),
                    Messages(role=MessagesRole.USER, content=user_content),
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
        )
    if not response.choices:
        raise RuntimeError(f"Пустой ответ GigaChat (нет choices): {response}")
    usage = {
        "prompt_tokens": response.usage.prompt_tokens,
        "completion_tokens": response.usage.completion_tokens,
        "total_tokens": response.usage.total_tokens,
    }
    return response.choices[0].message.content.strip(), usage


def list_models(cfg: GigaChatConfig) -> list[str]:
    """Реальный каталог моделей аккаунта через client.get_models() — вместо
    угадывания названий (см. models.yaml). Эмбеддинги отфильтрованы — не для чата."""
    with GigaChat(credentials=cfg.credentials, scope=cfg.scope, verify_ssl_certs=cfg.verify_ssl_certs) as client:
        return sorted(m.id_ for m in client.get_models().data if "embed" not in m.id_.lower())


def ping(cfg: GigaChatConfig, model: str) -> tuple[bool, str]:
    """Минимальный синхронный запрос — для кнопки проверки связи в /settings."""
    try:
        text, _usage = complete(cfg, "Ответь одним словом.", "Скажи 'ок'.", model, max_tokens=16, temperature=0)
        return True, text or "(пустой ответ)"
    except Exception as e:
        return False, str(e)


class GigaChatProvider(LLMProvider):
    """Обёртка под интерфейс LLMProvider — scorer.py/tailor.py/digest.py зовут
    provider.complete(...) одинаково для Yandex и для GigaChat."""

    name = "gigachat"

    def __init__(self, cfg: GigaChatConfig, model: str):
        self.cfg = cfg
        self.model = model
        self.last_usage: dict | None = None

    def complete(
        self, system_prompt: str, user_content: str, max_tokens: int = 1000, temperature: float = 0.3
    ) -> str:
        text, usage = complete(self.cfg, system_prompt, user_content, self.model, max_tokens, temperature)
        self.last_usage = usage
        return text
