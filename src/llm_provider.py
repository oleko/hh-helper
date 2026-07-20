"""
Единый интерфейс поверх LLM-провайдеров (Yandex, GigaChat, ...). scorer.py,
tailor.py и digest.py зовут provider.complete(...) и не знают, чья это
модель — так выбор провайдера/модели переключается конфигом (llm.* в
config.yaml, позже — из /settings), а не if-ами внутри промпт-логики.

Провайдер и модель для скоринга и для tailor'а настраиваются отдельно
(llm.score_provider/llm.tailor_provider) — дешёвая lite-модель на массовый
скоринг, более сильная модель на резюме/письма, у каждой свой провайдер,
если понадобится.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class LLMProvider(ABC):
    @abstractmethod
    def complete(
        self, system_prompt: str, user_content: str, max_tokens: int = 1000, temperature: float = 0.3
    ) -> str:
        """Один запрос completion, возвращает текст ответа модели."""


def get_provider(cfg: dict, task: str, storage=None) -> LLMProvider:
    """task: "score" | "tailor". Провайдер+модель выбираются в таком порядке:
    1) сохранённый на /settings выбор (storage["llm_<task>_choice"] =
       "provider:model", см. models.yaml) — если storage передан и там
       что-то сохранено;
    2) иначе llm.<task>_provider / llm.provider из config.yaml, и модель
       из секции конкретного провайдера (yandex.scorer_model/tailor_model и т.п.)
       — поведение по умолчанию для тех, кто ещё не выбирал модель в UI."""
    llm_cfg = cfg.get("llm") or {}
    provider_name = llm_cfg.get(f"{task}_provider") or llm_cfg.get("provider", "yandex")
    model_key = "scorer_model" if task == "score" else f"{task}_model"
    model_override = None

    if storage is not None:
        choice = storage.get_setting(f"llm_{task}_choice")
        if choice and ":" in choice:
            provider_name, model_override = choice.split(":", 1)

    if provider_name == "yandex":
        # локальный импорт — иначе цикл yandex_client -> llm_provider -> yandex_client
        from .main import get_yandex_config
        from .yandex_client import YandexProvider

        ycfg = get_yandex_config(cfg, model_override or cfg["yandex"][model_key])
        mode = llm_cfg.get("mode", "sync")
        return YandexProvider(ycfg, mode=mode)

    if provider_name == "gigachat":
        from .gigachat_client import GigaChatProvider
        from .main import get_gigachat_config

        model = model_override or (cfg.get("gigachat") or {}).get(model_key, "GigaChat-2-Pro")
        return GigaChatProvider(get_gigachat_config(cfg), model)

    raise ValueError(f"Неизвестный LLM-провайдер: {provider_name!r} (задача={task!r})")
