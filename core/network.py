"""Plugin-local proxy settings; never modify process-wide proxy variables."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
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


_retired_closes = set()
_RETIRE_DELAY = 120  # 正在用旧连接的请求有时间跑完再关


async def _close_after(client, delay):
    try:
        await asyncio.sleep(delay)
        await client.close()
    except Exception:
        pass


def _retire(client):
    try:
        task = asyncio.get_running_loop().create_task(_close_after(client, _RETIRE_DELAY))
    except RuntimeError:
        return
    _retired_closes.add(task)
    task.add_done_callback(_retired_closes.discard)


def client_fingerprint(*parts):
    """Key、地址、代理这些拼成指纹；只存摘要，不在内存里多留一份密钥。"""
    return hashlib.sha256(repr(parts).encode()).hexdigest()


def cached_client(owner, name, fingerprint, build):
    """取缓存在 ``owner.<name>`` 上的 SDK 客户端，配置变了就换新的。

    插件自己的配置页保存时不重载插件，所以不能只在为空时建一次：
    Key / 地址 / 代理一改，指纹不同，旧连接稍后关掉，按新配置重建。
    ``fingerprint`` 为 None 表示没配齐，返回 None。
    """
    prints = getattr(owner, "_client_fingerprints", None)
    if prints is None:
        prints = {}
        setattr(owner, "_client_fingerprints", prints)
    client = getattr(owner, name, None)
    if client is not None:
        if name not in prints:
            # 外面直接放进来的（测试、旧代码），按当前配置认下。
            prints[name] = fingerprint
        if prints.get(name) == fingerprint:
            return client
        _retire(client)
        setattr(owner, name, None)
    prints.pop(name, None)
    if fingerprint is None:
        return None
    client = build()
    if client is not None:
        setattr(owner, name, client)
        prints[name] = fingerprint
    return client


async def close_plugin_clients(plugin):
    """Close only SDK clients owned by this plugin, never AstrBot providers."""
    prints = getattr(plugin, "_client_fingerprints", None)
    if isinstance(prints, dict):
        prints.clear()
    for name in ("_embed_client", "_video_vision_client", "_image_vision_client", "_web_search_client"):
        client = getattr(plugin, name, None)
        setattr(plugin, name, None)
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass
