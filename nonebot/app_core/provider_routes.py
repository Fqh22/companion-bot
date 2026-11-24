"""[infra] Resolve service endpoints, protocol options and credential references."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from app_core.kernel import keys

DEEPSEEK_CHAT = "https://api.deepseek.com/v1/chat/completions"


@dataclass(frozen=True)
class ModelRoute:
    api: str
    protocol: str = "deepseek"
    key_ref: str = "deepseek"

    def request_fields(self, messages: list[dict], *, thinking: bool, stream: bool = False) -> dict:
        if self.protocol == "deepseek":
            fields = {"messages": messages, "thinking": {"type": "enabled" if thinking else "disabled"}}
            if stream:
                fields["stream_options"] = {"include_usage": True}
            return fields
        return {"messages": [{k: v for k, v in message.items() if k != "reasoning_content"}
                             for message in messages]}


def _credential_ref(api: str, default_api: str, value: str, default_key: str) -> str:
    url = urlsplit(api)
    default = urlsplit(default_api)
    if not url.hostname or url.scheme != "https" or url.username is not None or url.fragment:
        raise ValueError("供应商地址须为 HTTPS，且不能含用户信息或片段")
    same_origin = (url.hostname, url.port or 443) == (default.hostname, default.port or 443)
    if not same_origin and (not value or value == default_key):
        raise ValueError("替代服务地址须配置单独的 key_ref")
    return value or default_key


def model_route(options: dict) -> ModelRoute:
    protocol = str(options.get("protocol") or "deepseek").strip()
    if protocol not in {"deepseek", "openai_compatible"}:
        raise ValueError("未知的模型接口协议")
    api = str(options.get("api") or DEEPSEEK_CHAT).strip()
    ref = str(options.get("key_ref") or "").strip()
    if protocol == "openai_compatible" and (not options.get("api") or not ref or ref == "deepseek"):
        raise ValueError("OpenAI 兼容协议须配置 API 地址和独立 key_ref")
    return ModelRoute(api, protocol, _credential_ref(api, DEEPSEEK_CHAT, ref, "deepseek"))


def chat_routes(llm: dict, summary: dict) -> tuple[ModelRoute, ModelRoute | None]:
    # 旧配置里 summary 随 llm.api 走。选择新路由时，未另行配置的总结留在原服务。
    legacy = not (llm.get("protocol") or llm.get("key_ref"))
    chat = ModelRoute(llm.get("api") or DEEPSEEK_CHAT) if legacy else model_route(llm)
    if any(summary.get(key) for key in ("api", "protocol", "key_ref")):
        return chat, model_route(summary)
    return chat, None if legacy else ModelRoute(DEEPSEEK_CHAT)


@dataclass(frozen=True)
class ServiceRoute:
    api: str
    key_ref: str

    def key(self) -> str:
        return keys.get(self.key_ref)


_SERVICE_DEFAULTS = {
    "search": ("https://api.deepseek.com/v1/tools/search", "deepseek"),
    "search_model": ("https://api.deepseek.com/chat/completions", "deepseek"),
    "ocr": ("https://api.deepseek.com/v1/tools/ocr", "deepseek"),
    "asr": ("https://api.deepseek.com/v1/tools/asr", "deepseek"),
    "tts_deepseek": ("https://api.deepseek.com/v1/tools/tts", "deepseek"),
    "tts_zhipu": ("https://open.bigmodel.cn/api/paas/v4/audio/speech", "zhipu"),
    "tts_breeze": ("https://api.breeze.blue/v1/text-to-speech", "breeze"),
}


def service_route(name: str, routes: dict | None = None) -> ServiceRoute:
    if routes is None:
        from app_core import config
        routes = config.load().get("provider_routes") or {}
    default_api, default_key = _SERVICE_DEFAULTS[name]
    options = routes.get(name) or {}
    api = str(options.get("api") or default_api).strip()
    ref = str(options.get("key_ref") or "").strip()
    return ServiceRoute(api, _credential_ref(api, default_api, ref, default_key))


def validate_services(routes: dict) -> None:
    for name in routes:
        try:
            if name == "speech_rewrite":
                model_route(routes[name] or {})
            elif name in _SERVICE_DEFAULTS:
                service_route(name, routes)
            else:
                raise ValueError("未知服务名称")
        except (AttributeError, ValueError) as exc:
            raise ValueError(f"provider_routes.{name}: {exc}") from exc
