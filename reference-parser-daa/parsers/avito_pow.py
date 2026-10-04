"""Bounded local solver for Avito's published firewallPow protocol.

Protocol reference: mickberrad659-sketch/Avito-Parser (master, reviewed 2026-10-04).
Independently implemented; no external solver, captured tokens or reference code.
A decoded JWT is untrusted work input, never authentication evidence.
"""
import base64
import hashlib
import json
import time
from urllib.parse import unquote

from parsers.sources import SourceBlocked

MAX_TOKEN_LENGTH = 16384
MAX_COMPLEXITY = 5
MAX_NONCES = 2_000_000
SOLVE_SECONDS = 8


def response_payload(response):
    try:
        value = response.json()
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def pow_challenge(response, cookies):
    """HTML uses a fresh cookie; XHR uses JSON. Never reuse it on an IP block."""
    if response.status_code not in (429, 439):
        return None
    challenge = response_payload(response).get('pow_challenge')
    if not isinstance(challenge, str):
        if response.status_code != 439 or '/web/3/firewallPow/get' not in response.text:
            return None
        # Resolve cookies for this HTTPS host/path; do not pick an unrelated domain.
        from urllib.request import Request
        request = Request(response.url)
        cookies.jar.add_cookie_header(request)
        from http.cookies import SimpleCookie
        parsed = SimpleCookie()
        parsed.load(request.get_header('Cookie', ''))
        challenge = parsed['pow_challenge'].value if 'pow_challenge' in parsed else None
        challenge = unquote(challenge) if challenge else None
    return challenge if isinstance(challenge, str) and 0 < len(challenge) <= MAX_TOKEN_LENGTH else None


def solution(token, *, seconds=SOLVE_SECONDS):
    """SHA256(id:nonce) must start with compl zero hex digits; work is capped."""
    try:
        if not isinstance(token, str) or not 0 < len(token) <= MAX_TOKEN_LENGTH:
            raise ValueError
        parts = token.split('.')
        if len(parts) != 3:
            raise ValueError
        encoded = parts[1]
        payload = json.loads(base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True))
        identifier, complexity = payload['id'], payload['compl']
        if not isinstance(identifier, str) or not 0 < len(identifier) <= 256:
            raise ValueError
        if type(complexity) is not int or not 0 <= complexity <= MAX_COMPLEXITY:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise SourceBlocked('avito: invalid or excessive PoW challenge') from None
    deadline = time.monotonic() + min(SOLVE_SECONDS, max(0, seconds))
    prefix, target = (identifier + ':').encode(), '0' * complexity
    for nonce in range(MAX_NONCES):
        if nonce % 2048 == 0 and time.monotonic() >= deadline:
            break
        if hashlib.sha256(prefix + str(nonce).encode('ascii')).hexdigest().startswith(target):
            return {'challenge': token, 'nonce': nonce}
    raise SourceBlocked('avito: PoW work budget exceeded')


def result(response, *, stage='PoW'):
    """Only successful HTTP and a strict typed envelope may advance the flow."""
    if response.status_code != 200:
        raise SourceBlocked(f'avito: {stage} endpoint HTTP {response.status_code}')
    payload = response_payload(response)
    success = payload.get('success')
    value = success.get('result') if isinstance(success, dict) else None
    if not isinstance(value, dict):
        raise SourceBlocked(f'avito: invalid {stage} endpoint response')
    return value