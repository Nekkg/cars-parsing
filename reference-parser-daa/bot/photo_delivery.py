"""Fetch source images locally; Telegram receives JPEG bytes, not a remote URL."""
from functools import lru_cache
from io import BytesIO
from urllib.parse import urlparse
from urllib.request import Request
from PIL import Image, ImageOps
from core.network import source_opener

MAX_DOWNLOAD = 10 * 1024 * 1024


@lru_cache(maxsize=32)
def download_photo(url, referer=''):
    if urlparse(url).scheme not in ('http','https'):
        raise ValueError('Unsupported image URL')
    headers = {'User-Agent':'Mozilla/5.0', 'Accept':'image/jpeg,image/png,image/webp,image/*;q=0.8'}
    if referer: headers['Referer']=referer
    with source_opener().open(Request(url,headers=headers),timeout=12) as response:
        data=response.read(MAX_DOWNLOAD+1)
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
