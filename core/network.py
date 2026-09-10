"""Plugin-local proxy settings; never modify process-wide proxy variables."""
from contextlib import asynccontextmanager
import re
from urllib.parse import unquote, urlsplit, urlunsplit

import aiohttp


def normalize_proxy_url(value):
    """Validate administrator configuration without echoing credentials."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise ValueError("插件代理地址必须是字符串")
    value = value.strip()
    if not value:
        return ""
    try:
        if len(value) > 2048 or any(ch.isspace() or ord(ch) < 32 for ch in value):
            raise ValueError
        url = urlsplit(value)
        if url.scheme.lower() not in {"http", "https", "socks5", "socks5h"}:
            raise ValueError
        if not url.hostname or url.path not in {"", "/"} or url.query or url.fragment:
            raise ValueError
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError
        # Both SOCKS clients below use proxy-side DNS; accept the familiar h alias.
        scheme = "socks5" if url.scheme.lower() == "socks5h" else url.scheme.lower()
        netloc = url.netloc
        if scheme == "socks5" and url.port is None:
            netloc += ":1080"
        return urlunsplit((scheme, netloc, "", "", ""))
    except (TypeError, ValueError):
        raise ValueError("插件代理地址无效：请填写 http://、https:// 或 socks5://主机:端口，不要填写路径或订阅链接") from None


def configured_proxy(config):
    return normalize_proxy_url(config.get("PROXY_URL", ""))


def redact_proxy_error(text, config):
    """Remove proxy URLs and decoded passwords before errors enter logs."""
    text = str(text or "")
    raw = str(config.get("PROXY_URL", "") or "").strip()
    if raw:
        text = text.replace(raw, "[插件代理]")
        try:
            text = text.replace(normalize_proxy_url(raw), "[插件代理]")
            password = urlsplit(raw).password
            if password:
                for secret in {password, unquote(password)}:
                    text = text.replace(secret, "[代理密码已隐藏]")
        except ValueError:
            pass
    return re.sub(r"(?i)((?:https?|socks5h?)://)[^/\s@]+@", r"\1[凭据已隐藏]@", text)


class _ProxySession:
    """Facade for aiohttp 3.9+, which lacks a session-level proxy option."""

    def __init__(self, session, proxy):
        self._session = session
        self._proxy = proxy

    def get(self, *args, **kwargs):
        return self._session.get(*args, **{**kwargs, "proxy": self._proxy})

    def post(self, *args, **kwargs):
        return self._session.post(*args, **{**kwargs, "proxy": self._proxy})


@asynccontextmanager
async def plugin_http_session(config):
    proxy = configured_proxy(config)
    options = {}
    if proxy.startswith("socks5://"):
        try:
            from aiohttp_socks import ProxyConnector
        except ImportError:
            raise ValueError("SOCKS5 代理缺少依赖，请重装插件依赖 aiohttp-socks") from None
        options["connector"] = ProxyConnector.from_url(proxy, rdns=True)
    try:
        async with aiohttp.ClientSession(**options) as session:
            yield _ProxySession(session, proxy) if proxy.startswith(("http://", "https://")) else session
    except (aiohttp.ClientError, OSError) as exc:
        if not proxy:
            raise
        # Never silently fall back to direct access when a configured proxy fails.
        raise aiohttp.ClientError(
            f"插件代理请求失败（{type(exc).__name__}）: {redact_proxy_error(exc, config)}"
        ) from None


def openai_proxy_kwargs(config):
    proxy = configured_proxy(config)
    if not proxy:
        return {}  # Preserve the SDK's original environment/default behavior.
    from openai import DefaultAsyncHttpxClient
    return {"http_client": DefaultAsyncHttpxClient(proxy=proxy, trust_env=False)}


def download_proxy_args(config):
    proxy = configured_proxy(config)
    return ["--proxy", proxy] if proxy else []


async def close_plugin_clients(plugin):
    """Close only SDK clients owned by this plugin, never AstrBot providers."""
    for name in ("_embed_client", "_video_vision_client", "_image_vision_client", "_web_search_client"):
        client = getattr(plugin, name, None)
        setattr(plugin, name, None)
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass
