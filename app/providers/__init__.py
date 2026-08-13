"""Provider 注册与工厂。"""
from __future__ import annotations

from ..config import get_provider
from .ark import ArkProvider
from .base import BaseProvider, ProviderError
from .dashscope import DashScopeProvider
from .gemini import GeminiProvider
from .modelscope import ModelScopeProvider
from .openai_compat import OpenAICompatProvider
from .zhipu import ZhipuProvider

REGISTRY: dict[str, type[BaseProvider]] = {
    "openai": OpenAICompatProvider,
    "gemini": GeminiProvider,
    "dashscope": DashScopeProvider,
    "ark": ArkProvider,
    "zhipu": ZhipuProvider,
    "modelscope": ModelScopeProvider,
}


def build(provider_id: str) -> BaseProvider:
    conf = get_provider(provider_id)
    kind = conf.get("kind", "openai")
    cls = REGISTRY.get(kind)
    if cls is None:
        raise ProviderError(f"未知的 provider kind: {kind}")
    return cls(conf)


def resolve(ref: str) -> tuple[BaseProvider, str]:
    """'provider/model' -> (provider 实例, model_id)"""
    if "/" not in ref:
        raise ProviderError(f"模型标识需为 provider/model 格式: {ref}")
    pid, model = ref.split("/", 1)
    return build(pid), model


__all__ = ["build", "resolve", "BaseProvider", "ProviderError", "REGISTRY"]
