"""Private, route-bound state for the single Avito HTTP worker."""
import hashlib
import json
import logging
import os
import tempfile
import time
from http.cookiejar import Cookie
from pathlib import Path

logger = logging.getLogger(__name__)
MAX_AGE = 86400
MAX_BYTES = 256_000
COOKIE_FIELDS = ('version', 'name', 'value', 'port', 'port_specified', 'domain',
                 'domain_specified', 'domain_initial_dot', 'path', 'path_specified',
                 'secure', 'expires', 'discard', 'comment', 'comment_url', 'rfc2109')


def avito_domain(domain):
    return isinstance(domain, str) and (domain.lstrip('.') == 'avito.ru' or
                                        domain.endswith('.avito.ru'))


class AvitoCookieStore:
    def __init__(self, profile, proxy, *, path=None, tunnel_config=None):
        from core.paths import RUNTIME_DIR
        self.path = Path(path) if path is not None else RUNTIME_DIR / 'sessions' / 'avito-http-cookies.json'
        tunnel = Path(tunnel_config) if tunnel_config is not None else RUNTIME_DIR / 'sessions' / 'avito-proxy.json'
        # The endpoint can stay unchanged while the tunnel's exit node changes.
        fingerprint = hashlib.sha256(f'{profile}|{proxy or "direct"}'.encode())
        if proxy and tunnel.is_file():
            fingerprint.update(tunnel.read_bytes())
        self.route = fingerprint.hexdigest()

    def load(self, cookies):
        try:
            if not self.path.is_file() or self.path.stat().st_size > MAX_BYTES:
                return 0
            payload = json.loads(self.path.read_text(encoding='utf-8'))
            now = time.time()
            if (not isinstance(payload, dict) or payload.get('version') != 1 or
                payload.get('route') != self.route or
                not isinstance(payload.get('saved_at'), (int, float)) or
                not 0 <= now - payload['saved_at'] <= MAX_AGE or
                not isinstance(payload.get('cookies'), list) or len(payload['cookies']) > 100):
                return 0
            accepted = []
            for row in payload['cookies']:
                if not isinstance(row, dict) or not avito_domain(row.get('domain')):
                    continue
                if not all(isinstance(row.get(k), str) and 0 < len(row[k]) <= 16384 for k in ('name', 'value')):
                    continue
                expires = row.get('expires')
                if expires is not None and (type(expires) not in (int, float) or expires <= now):
                    continue
                if not isinstance(row.get('path'), str) or not row['path'].startswith('/'):
                    continue
                values = {field: row[field] for field in COOKIE_FIELDS}
                accepted.append(Cookie(**values, rest={'HttpOnly': None} if row.get('http_only') else {}))
            for cookie in accepted:
                cookies.jar.set_cookie(cookie)
            return len(accepted)
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning('Avito session state: invalid local cache ignored')
            return 0

    def save(self, cookies):
        rows = []
        for cookie in cookies.jar:
            if not avito_domain(cookie.domain) or cookie.is_expired():
                continue
            rows.append({**{field: getattr(cookie, field) for field in COOKIE_FIELDS},
                         'http_only': cookie.has_nonstandard_attr('HttpOnly')})
        if not rows or len(rows) > 100:
            return False
        encoded = json.dumps({'version': 1, 'route': self.route, 'saved_at': time.time(),
                              'cookies': rows}, ensure_ascii=True).encode('utf-8')
        if len(encoded) > MAX_BYTES:
            return False
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix='.avito-session-', dir=self.path.parent)
            with os.fdopen(descriptor, 'wb') as output:
                output.write(encoded)
            # mkstemp is private on Linux; Windows inherits the runtime folder ACL.
            os.replace(temporary, self.path)
            return True
        except OSError:
            logger.warning('Avito session state: could not save local cache')
            return False
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
