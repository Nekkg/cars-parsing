"""Fetch source images locally; Telegram receives JPEG bytes, not a remote URL."""
import re
from functools import lru_cache
from io import BytesIO
from http.client import IncompleteRead
from ssl import SSLEOFError
from time import monotonic
from urllib.error import URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request
from PIL import Image, ImageOps
from core.network import source_opener

MAX_DOWNLOAD = 10 * 1024 * 1024
_autoru_cdn_retry_at = 0.0


@lru_cache(maxsize=32)
def download_photo(url, referer=''):
    if urlparse(url).scheme not in ('http','https'):
        raise ValueError('Unsupported image URL')
    headers = {'User-Agent':'Mozilla/5.0', 'Accept':'image/jpeg,image/png,image/webp,image/*;q=0.8'}
    if referer: headers['Referer']=referer
    global _autoru_cdn_retry_at
    parsed = urlparse(url)
    # Both official CDN names serve the same get-autoru asset paths. This only
    # handles connection failures, never access restrictions or certificate errors.
    alternate = urlunparse(parsed._replace(netloc='avatars.mds.yandex.net')) if (
        parsed.scheme == 'https' and parsed.netloc == 'avatars.avto.ru' and
        re.fullmatch(r'/get-autoru(?:-[\w-]+)?/[^/]+/[^/]+/[^/]+',parsed.path)) else None
    first = alternate if alternate and _autoru_cdn_retry_at > monotonic() else url
    second = (url if first == alternate else alternate) if alternate else url
    deadline = monotonic() + 12
    for attempt in range(2):
        # Retry only a broken connection, once, within the same time budget.
        # Access restrictions and TLS certificate errors are never retried here.
        timeout = 6 if not attempt else max(0.1, deadline - monotonic())
        try:
            target = first if not attempt else second
            with source_opener().open(Request(target,headers=headers),timeout=timeout) as response:
                data=response.read(MAX_DOWNLOAD+1)
            if alternate and target == url:
                _autoru_cdn_retry_at = 0.0
            break
        except (URLError, TimeoutError, ConnectionError, IncompleteRead) as error:
            reason = error.reason if isinstance(error, URLError) else error
            if attempt or not isinstance(reason, (TimeoutError, ConnectionError, SSLEOFError, IncompleteRead)) or monotonic() >= deadline:
                raise
            if alternate and target == url:
                # Other gallery photos immediately use the available CDN for five minutes.
                _autoru_cdn_retry_at = monotonic() + 300
    if len(data) > MAX_DOWNLOAD:
        raise ValueError('Image exceeds download limit')
    with Image.open(BytesIO(data)) as original:
        if original.width * original.height > 25_000_000:
            raise ValueError('Image dimensions exceed limit')
        photo=ImageOps.exif_transpose(original).convert('RGB')
        photo.thumbnail((1600,1600))
        if max(photo.size) > min(photo.size)*20:
            raise ValueError('Unsupported photo aspect ratio')
        output=BytesIO()
        photo.save(output,format='JPEG',quality=88,optimize=True)
        return output.getvalue()


@lru_cache(maxsize=1)
def placeholder_photo():
    from PIL import ImageDraw
    photo = Image.new('RGB',(960,600),'#192735')
    draw = ImageDraw.Draw(photo)
    draw.rounded_rectangle((250,200,710,405),radius=30,outline='#536575',width=8)
    draw.polygon([(315,200),(365,125),(595,125),(645,200)],outline='#536575',width=8)
    draw.ellipse((295,360,385,450),fill='#536575')
    draw.ellipse((575,360,665,450),fill='#536575')
    output = BytesIO()
    photo.save(output,format='JPEG',quality=85)
    return output.getvalue()