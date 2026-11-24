"""Provider registry for the PyScribe AI agent.

Every provider is described declaratively so the UI (settings page, model
picker) and the HTTP client share one source of truth.

Two wire protocols are supported:

``openai``
    ``POST {base}/chat/completions`` with SSE ``data: {...}`` chunks. Covers
    DeepSeek, OpenAI, OpenRouter, Groq, Mistral, Gemini's
    compatibility endpoint, Ollama, LM Studio and any custom server.
``anthropic``
    ``POST {base}/messages`` with the native Claude Messages API
    (``x-api-key`` + ``anthropic-version`` headers, ``content_block_delta`` SSE
    events), used for Claude.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

__all__ = [
    "ProviderSpec",
    "PROVIDERS",
    "KIND_OPENAI",
    "KIND_ANTHROPIC",
    "get_provider",
    "provider_choices",
    "models_for",
    "family_of",
]

KIND_OPENAI = "openai"
KIND_ANTHROPIC = "anthropic"


@dataclass(frozen=True)
class ModelSpec:
    """One selectable model."""

    id: str
    label: str
    free: bool = False
    note: str = ""
    version: str = ""   # human-facing model version, e.g. "DeepSeek-V4.1-Flash-1017"


@dataclass(frozen=True)
class ProviderSpec:
    key: str
    label: str
    kind: str
    base_url: str
    models: List[ModelSpec] = field(default_factory=list)
    key_url: str = ""
    docs_url: str = ""
    local: bool = False
    hint: str = ""
    # Anthropic-format endpoint for providers that proxy Claude models
    # (DeepSeek serves one at https://api.deepseek.com/anthropic).
    anthropic_base_url: str = ""
    # Several entries (one per wire protocol / network route) that share one
    # account and one key point at the same credential group, so the user types
    # the key once and it works for every route.
    credential_group: str = ""
    # Preferred model when the stored setting has none; defaults to models[0].
    default_model_id: str = ""

    @property
    def default_model(self) -> str:
        if self.default_model_id:
            return self.default_model_id
        return self.models[0].id if self.models else ""

    @property
    def credential_key(self) -> str:
        """Key under which this provider's API key is stored."""
        return self.credential_group or self.key


def _m(model_id: str, label: str = "", free: bool = False, note: str = "",
       version: str = "") -> ModelSpec:
    return ModelSpec(model_id, label or model_id, free, note, version)


# ----------------------------------------------------------------------
# Model families (used to keep big aggregator catalogues navigable)
# ----------------------------------------------------------------------
_FAMILY_PREFIXES = (
    ("claude", "Claude"),
    ("gpt", "GPT"),
    ("deepseek", "DeepSeek"),
    ("gemini", "Gemini"),
    ("glm", "GLM"),
    ("kimi", "Kimi"),
    ("grok", "Grok"),
    ("composer", "Composer"),
    ("qwen", "Qwen"),
    ("minimax", "MiniMax"),
    ("mimo", "MiMo"),
    ("solar", "Solar"),
    ("hy", "HY"),
    ("nemotron", "NVIDIA"),
    ("step", "Step"),
    ("ling", "Ling"),
    ("lfm", "LFM"),
    ("muse", "Muse"),
    ("nex", "Nex"),
    ("ternary", "Ternary"),
    ("inkling", "Inkling"),
    ("llama", "Llama"),
    ("o3", "o-series"),
    ("o4", "o-series"),
    ("mistral", "Mistral"),
)


def family_of(model_id: str) -> str:
    """Human-facing family of a model (``Claude``, ``GPT``, ``Gemini``, …).

    Used by the model picker to fold an 80-model catalogue into a handful of
    submenus. ``free-`` models are shown in their real family with a "(free)"
    suffix instead of a separate "free" bucket.
    """
    mid = str(model_id or "").strip().lower()
    if mid.startswith("free-"):
        mid = mid[len("free-"):]
    for prefix, name in _FAMILY_PREFIXES:
        if mid.startswith(prefix):
            return name
    return "Другие"


# ----------------------------------------------------------------------
# Tokenator — partner aggregator: one key, every model, Russia without VPN.
#
# Catalogue order is the partner's own listing; labels are display names only,
# the ``id`` is exactly what the API expects. ``free-`` models are free of
# charge but the partner only opens them for a key that has been topped up at
# least once in the last 7 days.
# ----------------------------------------------------------------------
_FREE_NOTE = "Бесплатная: ключ должен быть пополнен"

_TOKENATOR_MODELS: List[ModelSpec] = [
    _m("claude-haiku-4-5", "Claude Haiku 4.5", note="Быстрый и дешёвый"),
    _m("claude-opus-4-6", "Claude Opus 4.6"),
    _m("claude-opus-4-7", "Claude Opus 4.7"),
    _m("claude-sonnet-4-6", "Claude Sonnet 4.6"),
    _m("gpt-5.5", "GPT-5.5"),
    _m("deepseek-v4-pro", "DeepSeek V4 Pro", note="Максимальное качество DeepSeek"),
    _m("gemini-3.1-flash-lite", "Gemini 3.1 Flash-Lite", note="Дёшево и быстро"),
    _m("claude-opus-4-8", "Claude Opus 4.8"),
    _m("claude-fable-5", "Claude Fable 5"),
    _m("glm-5.2", "GLM 5.2"),
    _m("claude-sonnet-5", "Claude Sonnet 5"),
    _m("kimi-k2-7-code", "Kimi K2.7 Code", note="Заточена под код"),
    _m("grok-4.5", "Grok 4.5"),
    _m("composer-2.5", "Composer 2.5"),
    _m("gpt-5.6-luna", "GPT-5.6 Luna"),
    _m("gpt-5.6-terra", "GPT-5.6 Terra"),
    _m("gpt-5.6-sol", "GPT-5.6 Sol"),
    _m("kimi-k3", "Kimi K3"),
    _m("qwen-3-8-max", "Qwen 3.8 Max"),
    _m("claude-opus-5", "Claude Opus 5"),
    _m("gemini-3.6-flash", "Gemini 3.6 Flash"),
    _m("hy3", "HY3"),
    _m("gpt-4o-mini", "GPT-4o mini", note="Старая, но совсем дешёвая"),
    _m("minimax-m3", "MiniMax M3"),
    _m("qwen-3.7-plus", "Qwen 3.7 Plus"),
    _m("qwen-3.7-max", "Qwen 3.7 Max"),
    _m("gemini-3.1-pro-preview", "Gemini 3.1 Pro Preview"),
    _m("grok-4.6", "Grok 4.6"),
    _m("glm-5.3", "GLM 5.3"),
    _m("solar-pro-4", "Solar Pro 4"),
    _m("hy-mt2-1.8b", "HY-MT2 1.8B", note="Машинный перевод"),
    _m("hy-mt2-30b-a3b", "HY-MT2 30B A3B", note="Машинный перевод"),
    _m("hy-mt2-7b", "HY-MT2 7B", note="Машинный перевод"),
    _m("grok-build-0.1", "Grok Build 0.1"),
    _m("glm-5.3-flash", "GLM 5.3 Flash", note="Быстрая и недорогая"),
    _m("qwen-3.8-flash", "Qwen 3.8 Flash"),
    _m("free-glm-5.3-flash", "GLM 5.3 Flash (free)", free=True, note=_FREE_NOTE),
    _m("claude-fable-5.1", "Claude Fable 5.1"),
    _m("gemini-3.8-flash", "Gemini 3.8 Flash"),
    _m("gpt-6-astra", "GPT-6 Astra"),
    _m("deepseek-v4.1-flash", "DeepSeek V4.1 Flash",
       version="DeepSeek-V4.1-Flash"),
    _m("glm-5.3-flashx", "GLM 5.3 FlashX"),
    _m("ternary-bonsai-2-27b", "Ternary Bonsai 2 27B", note="Лёгкая, 27B параметров"),
    _m("grok-4.7", "Grok 4.7"),
    _m("mimo-v2.6-flash", "MiMo V2.6 Flash"),
    _m("mimo-v2.6-pro", "MiMo V2.6 Pro"),
    _m("claude-opus-5.5", "Claude Opus 5.5", note="Флагман Claude"),
    _m("gpt-6-luna", "GPT-6 Luna"),
    _m("gpt-6-sol", "GPT-6 Sol"),
    _m("solar-mini-4", "Solar Mini 4"),
    _m("free-claude-opus-5.5", "Claude Opus 5.5 (free)", free=True, note=_FREE_NOTE),
    _m("minimax-m3.1-flash", "MiniMax M3.1 Flash"),
    _m("free-minimax-m3.1-flash", "MiniMax M3.1 Flash (free)", free=True,
       note=_FREE_NOTE),
    _m("claude-sonnet-5.5", "Claude Sonnet 5.5"),
    _m("gpt-6.1-sol", "GPT-6.1 Sol"),
    _m("kimi-k2.8-preview", "Kimi K2.8 Preview"),
    _m("ling-3.1-flash", "Ling 3.1 Flash"),
    _m("claude-haiku-5.5", "Claude Haiku 5.5", note="Быстрый и дешёвый"),
    _m("gemini-4-argon", "Gemini 4 Argon"),
    _m("nemotron-3.5-super-vl", "Nemotron 3.5 Super VL", note="Мультимодальная"),
    _m("deepseek-v4.1-flash-1017", "DeepSeek V4.1 Flash 1017",
       version="DeepSeek-V4.1-Flash-1017"),
    _m("step-5-preview", "Step 5 Preview"),
    _m("kimi-k3.1-pro", "Kimi K3.1 Pro"),
    _m("gpt-6.1-terra", "GPT-6.1 Terra"),
    _m("qwen3.8-27b", "Qwen3.8 27B"),
    _m("hy4", "HY4"),
    _m("lfm-3-1.7b", "LFM 3 1.7B", note="Крошечная, для черновиков"),
    _m("muse-ember", "Muse Ember"),
    _m("claude-fable-5.2", "Claude Fable 5.2"),
    _m("nex-n2.6", "Nex N2.6"),
    _m("gemini-4-carbon", "Gemini 4 Carbon"),
    _m("qwen-4-max-preview", "Qwen 4 Max Preview"),
    _m("gemini-4-hydrogen", "Gemini 4 Hydrogen"),
    _m("gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite"),
    _m("qwen-4-max", "Qwen 4 Max"),
    _m("hy4-flash", "HY4 Flash"),
    _m("gpt-6.2-astra", "GPT-6.2 Astra"),
    _m("gpt-6.2-sol", "GPT-6.2 Sol"),
    _m("free-gpt-6.2-sol", "GPT-6.2 Sol (free)", free=True, note=_FREE_NOTE),
    _m("gemini-4-neon", "Gemini 4 Neon"),
    _m("free-gemini-4-neon", "Gemini 4 Neon (free)", free=True, note=_FREE_NOTE),
    _m("inkling-2-mini", "Inkling 2 Mini"),
]


def _tokenator_models() -> List[ModelSpec]:
    """A fresh copy so no caller can mutate the shared catalogue."""
    return list(_TOKENATOR_MODELS)


PROVIDERS: Dict[str, ProviderSpec] = {
    # ------------------------------------------------------------------
    "deepseek": ProviderSpec(
        key="deepseek",
        label="DeepSeek",
        kind=KIND_OPENAI,
        base_url="https://api.deepseek.com",
        anthropic_base_url="https://api.deepseek.com/anthropic",
        key_url="https://platform.deepseek.com/api_keys",
        docs_url="https://api-docs.deepseek.com/quick_start/pricing",
        models=[
            _m("deepseek-flash", "DeepSeek Flash", note="1M контекст, thinking по умолчанию",
               version="DeepSeek-V4.1-Flash-1017"),
            _m("deepseek-v4-pro", "DeepSeek V4 Pro", note="Максимальное качество",
               version="DeepSeek-V4-Pro-0813"),
        ],
        hint="Ключ начинается с «sk-». Актуальные модели: deepseek-flash (V4.1-Flash-1017) "
             "и deepseek-v4-pro. Старое имя deepseek-chat тоже принимается, но это тот же Flash.",
    ),
    "openai": ProviderSpec(
        key="openai",
        label="OpenAI / ChatGPT",
        kind=KIND_OPENAI,
        base_url="https://api.openai.com/v1",
        key_url="https://platform.openai.com/api-keys",
        models=[
            _m("gpt-5.1", "GPT-5.1"),
            _m("gpt-5", "GPT-5"),
            _m("gpt-5-mini", "GPT-5 mini", note="Быстро и дёшево"),
            _m("gpt-4.1", "GPT-4.1"),
            _m("o3", "o3", note="Рассуждения"),
            _m("gpt-4o-mini", "GPT-4o mini", note="Устаревает"),
        ],
        hint="Обычный API-ключ OpenAI (не подписка ChatGPT).",
    ),
    "anthropic": ProviderSpec(
        key="anthropic",
        label="Claude (Anthropic)",
        kind=KIND_ANTHROPIC,
        base_url="https://api.anthropic.com/v1",
        key_url="https://console.anthropic.com/settings/keys",
        models=[
            _m("claude-opus-4-6", "Claude Opus 4.6", version="claude-opus-4-6-20260205"),
            _m("claude-sonnet-4-5", "Claude Sonnet 4.5"),
            _m("claude-haiku-4-5", "Claude Haiku 4.5", note="Быстрый и дешёвый"),
            _m("claude-opus-4-1", "Claude Opus 4.1"),
        ],
        hint="Ключ вида «sk-ant-…». Подписка Claude Pro/Max не подходит — нужен API-ключ.",
    ),
    "gemini": ProviderSpec(
        key="gemini",
        label="Google Gemini",
        kind=KIND_OPENAI,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_url="https://aistudio.google.com/apikey",
        models=[
            _m("gemini-4-argon", "Gemini 4 Argon", note="Самая новая, 1M контекст"),
            _m("gemini-3.6-flash", "Gemini 3.6 Flash", free=True, note="Бесплатный тариф"),
            _m("gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite", free=True, note="Бесплатный тариф"),
            _m("gemini-3.1-flash-lite", "Gemini 3.1 Flash-Lite", free=True),
            _m("gemini-2.5-pro", "Gemini 2.5 Pro"),
        ],
        hint="Ключ бесплатно в Google AI Studio (aistudio.google.com/apikey).",
    ),
    "opencode": ProviderSpec(
        key="opencode",
        label="opencode zen",
        kind=KIND_OPENAI,
        base_url="https://opencode.ai/zen/v1",
        key_url="https://opencode.ai/auth",
        models=[
            _m("gpt-5.1", "GPT-5.1"),
            _m("claude-sonnet-4-5", "Claude Sonnet 4.5"),
            _m("qwen3-coder", "Qwen3 Coder"),
            _m("kimi-k2", "Kimi K2"),
        ],
        hint="Ключ из opencode (/connect → opencode zen).",
    ),
    # ------------------------------------------------------------------
    # Tokenator — partner aggregator. Four entries, one account: the same
    # ``sk-tkn-…`` key opens every model, and each entry only picks the wire
    # protocol (OpenAI vs native Claude) and the route (Russia without a VPN
    # via api.tokenator.top, or through a VPN via api.tokenator.cloud).
    # ------------------------------------------------------------------
    "tokenator": ProviderSpec(
        key="tokenator",
        label="Tokenator · РФ (OpenAI API)",
        kind=KIND_OPENAI,
        base_url="https://api.tokenator.top/v1",
        anthropic_base_url="https://api.tokenator.top/anthropic/v1",
        credential_group="tokenator",
        default_model_id="claude-opus-5.5",
        models=_tokenator_models(),
        hint="Один ключ «sk-tkn-…» на ~80 моделей; из России без VPN. "
             "Эндпоинт: https://api.tokenator.top/v1.",
    ),
    "tokenator-claude": ProviderSpec(
        key="tokenator-claude",
        label="Tokenator · РФ (Claude API)",
        kind=KIND_ANTHROPIC,
        base_url="https://api.tokenator.top/anthropic/v1",
        credential_group="tokenator",
        default_model_id="claude-opus-5.5",
        models=_tokenator_models(),
        hint="Те же модели по тому же ключу через нативный Claude API: "
             "https://api.tokenator.top/anthropic/v1.",
    ),
    "tokenator-vpn": ProviderSpec(
        key="tokenator-vpn",
        label="Tokenator · VPN (OpenAI API)",
        kind=KIND_OPENAI,
        base_url="https://api.tokenator.cloud/v1",
        anthropic_base_url="https://api.tokenator.cloud/anthropic/v1",
        credential_group="tokenator",
        default_model_id="claude-opus-5.5",
        models=_tokenator_models(),
        hint="Тот же каталог через https://api.tokenator.cloud/v1 — маршрут для VPN.",
    ),
    "tokenator-claude-vpn": ProviderSpec(
        key="tokenator-claude-vpn",
        label="Tokenator · VPN (Claude API)",
        kind=KIND_ANTHROPIC,
        base_url="https://api.tokenator.cloud/anthropic/v1",
        credential_group="tokenator",
        default_model_id="claude-opus-5.5",
        models=_tokenator_models(),
        hint="Нативный Claude API через VPN: https://api.tokenator.cloud/anthropic/v1.",
    ),
    # ------------------------------------------------------------------
    # Free / no-key-required tier
    # ------------------------------------------------------------------
    "openrouter": ProviderSpec(
        key="openrouter",
        label="OpenRouter",
        kind=KIND_OPENAI,
        base_url="https://openrouter.ai/api/v1",
        key_url="https://openrouter.ai/keys",
        models=[
            _m("deepseek/deepseek-v4.1-flash-1017", "DeepSeek V4.1 Flash 1017", free=True),
            _m("deepseek/deepseek-v4.1-flash", "DeepSeek V4.1 Flash", free=True),
            _m("qwen/qwen3-coder:free", "Qwen3 Coder (free)", free=True),
            _m("google/gemini-3.6-flash:free", "Gemini 3.6 Flash (free)", free=True),
            _m("meta-llama/llama-3.3-70b-instruct:free", "Llama 3.3 70B (free)", free=True),
        ],
        hint="Бесплатные модели помечены «:free» и работают с бесплатным ключом OpenRouter.",
    ),
    "groq": ProviderSpec(
        key="groq",
        label="Groq",
        kind=KIND_OPENAI,
        base_url="https://api.groq.com/openai/v1",
        key_url="https://console.groq.com/keys",
        models=[
            _m("llama-3.3-70b-versatile", "Llama 3.3 70B", free=True, note="Бесплатный тариф"),
            _m("qwen/qwen3-32b", "Qwen3 32B", free=True),
            _m("openai/gpt-oss-120b", "GPT-OSS 120B", free=True),
        ],
        hint="Groq даёт бесплатный тариф для разработки.",
    ),
    "ollama": ProviderSpec(
        key="ollama",
        label="Ollama (локально, бесплатно)",
        kind=KIND_OPENAI,
        base_url="http://localhost:11434/v1",
        key_url="https://ollama.com/download",
        local=True,
        models=[
            _m("qwen3-coder:30b", "Qwen3 Coder 30B", free=True),
            _m("qwen2.5-coder:7b", "Qwen2.5 Coder 7B", free=True),
            _m("llama3.2:3b", "Llama 3.2 3B", free=True),
        ],
        hint="Локальная модель: бесплатно, без ключа, работает без интернета.",
    ),
    "lmstudio": ProviderSpec(
        key="lmstudio",
        label="LM Studio (локально, бесплатно)",
        kind=KIND_OPENAI,
        base_url="http://localhost:1234/v1",
        key_url="https://lmstudio.ai",
        local=True,
        models=[_m("local-model", "Любая загруженная модель", free=True)],
        hint="В LM Studio включите локальный сервер (Developer → Start Server).",
    ),
    "custom": ProviderSpec(
        key="custom",
        label="Свой сервер (OpenAI-совместимый)",
        kind=KIND_OPENAI,
        base_url="",
        models=[],
        hint="Укажите базовый URL вида https://host/v1 и имя модели.",
    ),
}


def get_provider(key: str) -> ProviderSpec:
    return PROVIDERS.get(str(key or "").strip().lower(), PROVIDERS["deepseek"])


def provider_choices() -> List[ProviderSpec]:
    return list(PROVIDERS.values())


def models_for(key: str) -> List[ModelSpec]:
    return list(get_provider(key).models)
