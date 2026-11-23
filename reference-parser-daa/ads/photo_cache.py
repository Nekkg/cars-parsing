"""Bounded source-photo downloads and cached JPEG variants for the website.

The HTTP endpoint accepts advertisement IDs, never arbitrary remote URLs.
Only validated source hosts and redirects are allowed.
"""
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import re
import threading
from time import monotonic, time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request

from django.core.cache import cache
from PIL import Image, ImageOps
from core.network import source_opener
from core.paths import runtime_directory
from parsers.photos import valid_photo

MAX_BYTES = 8 * 1024 * 1024
SIZES = {'card': (640, 480), 'full': (1280, 960)}
_locks = [threading.Lock() for _ in range(32)]
_downloads = threading.BoundedSemaphore(8)
_trim_lock = threading.Lock()


def photo_key(url, source, size):
    return sha256(f'v1|{source}|{size}|{url}'.encode()).hexdigest()


def candidates(url, source, size):
    if not valid_photo(url, source):
        raise ValueError('Invalid source photograph')
    parsed = urlparse(url)
    if source == 'autoru' and re.fullmatch(r'/get-autoru(?:-[\w-]+)?/[^/]+/[^/]+/[^/]+', parsed.path):
        path = parsed.path.rsplit('/', 1)[0] + ('/584x438' if size == 'card' else '/1200x900')
        current = urlunparse(parsed._replace(path=path))
        alternate = urlunparse(parsed._replace(netloc='avatars.mds.yandex.net' if parsed.hostname == 'avatars.avto.ru' else 'avatars.avto.ru', path=path))
        return [current, alternate]
    return [url]


class PhotoRedirects(HTTPRedirectHandler):
    def __init__(self, source):
        self.source = source
        self.hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.hops += 1
        if self.hops > 3 or not valid_photo(newurl, self.source):
            raise ValueError('Unsafe photo redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_photo(url, source, referer, size):
    deadline = monotonic() + 6
    last_error = None
    for target in candidates(url, source, size):
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        opener = source_opener()
        opener.add_handler(PhotoRedirects(source))
        headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'image/webp,image/jpeg,image/png', 'Referer': referer}
        try:
            with opener.open(Request(target, headers=headers), timeout=min(2.5, remaining)) as response:
                if not response.headers.get_content_type().startswith('image/'):
                    raise ValueError('Source did not return an image')
                if int(response.headers.get('Content-Length', 0)) > MAX_BYTES:
                    raise ValueError('Photo is too large')
                chunks = []
                total = 0
                while True:
                    if monotonic() > deadline:
                        raise TimeoutError('Photo exceeded the download budget')
                    chunk = response.read1(min(65536, MAX_BYTES + 1 - total))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise ValueError('Photo is too large')
                    chunks.append(chunk)
                data = b''.join(chunks)
            return data
        except HTTPError:
            raise  # Access restrictions are not bypassed using another host.
        except (URLError, TimeoutError, ConnectionError) as error:
            import ssl
            reason = error.reason if isinstance(error, URLError) else error
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise
            last_error = error
    raise TimeoutError('Photograph is temporarily unavailable') from last_error


def cached_photo(url, source, referer, size='card'):
    if size not in SIZES:
        raise ValueError('Unknown photograph size')
    key = photo_key(url, source, size)
    directory = runtime_directory('web-photos')
    path = directory / (key + '.jpg')
    with _locks[int(key[:2], 16) % len(_locks)]:
        if path.exists() and time() - path.stat().st_mtime < 86400:
            return path
        if cache.get('photo-failed:' + key):
            raise TimeoutError('Photo retry is paused')
        acquired = _downloads.acquire(timeout=2)
        if not acquired:
            raise TimeoutError('Photo download queue is busy')
        try:
            data = fetch_photo(url, source, referer, size)
            with Image.open(BytesIO(data)) as original:
                if original.width * original.height > 25_000_000:
                    raise ValueError('Photo dimensions exceed limit')
                image = ImageOps.exif_transpose(original).convert('RGB')
                image.thumbnail(SIZES[size])
                output = BytesIO()
                image.save(output, format='JPEG', quality=82, optimize=True, progressive=True)
            temporary = path.with_suffix('.tmp')
            temporary.write_bytes(output.getvalue())
            os.replace(temporary, path)
            # A local catalogue must not keep an unlimited number of photos.
            if _trim_lock.acquire(blocking=False):
                try:
                    files = list(directory.glob('*.jpg'))
                    if len(files) > 512:
                        for old in sorted(files, key=lambda p: p.stat().st_mtime)[:len(files)-512]:
                            if old != path:
                                old.unlink(missing_ok=True)
                except OSError:
                    pass  # Concurrent cleanup must not turn a valid image into an error.
                finally:
                    _trim_lock.release()
            return path
        except Exception:
            cache.set('photo-failed:' + key, True, 60)
            raise
        finally:
            _downloads.release()
