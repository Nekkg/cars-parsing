"""Independent explicit routes; never inherit a workstation's system proxy."""
import os
from urllib.parse import urlparse
from urllib.request import build_opener, ProxyHandler


def proxy_setting(name):
    value = os.getenv(name, '').strip()
    if value and urlparse(value).scheme not in ('http','socks4','socks5'):
        raise ValueError(f'{name}: expected http://, socks4:// or socks5:// URL')
    return value or None


def telegram_session():
    from aiogram.client.session.aiohttp import AiohttpSession
    return AiohttpSession(proxy=proxy_setting('TELEGRAM_PROXY_URL'))


def source_opener():
    proxy = proxy_setting('SOURCES_PROXY_URL')
    if proxy and not proxy.startswith('http://'):
        raise ValueError('SOURCES_PROXY_URL must be an HTTP proxy for the catalogue client')
    return build_opener(ProxyHandler({'http':proxy,'https':proxy} if proxy else {}))
