import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from curl_cffi.requests import Cookies
from django.test import SimpleTestCase
from parsers.avito_session import AvitoCookieStore


class AvitoSessionTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'private.json'
        self.tunnel = Path(self.temp.name) / 'tunnel.json'
        self.store = self.make_store()

    def make_store(self, profile='firefox147', proxy='http://avito:3068'):
        return AvitoCookieStore(profile, proxy, path=self.path, tunnel_config=self.tunnel)

    def test_restart_preserves_only_source_cookies_and_attributes(self):
        original = Cookies()
        original.set('session', 'synthetic', domain='.avito.ru', path='/web', secure=True)
        original.set('provider', 'not-stored', domain='gcaptcha4.geevisit.com')
        original.set('foreign', 'not-stored', domain='example.com')
        cookie = next(iter(original.jar))
        cookie.expires = int(time.time()) + 500
        cookie.discard = False
        self.assertTrue(self.store.save(original))
        restored = Cookies()
        self.assertEqual(self.make_store().load(restored), 1)
        copy = next(iter(restored.jar))
        self.assertEqual((copy.name, copy.value, copy.domain, copy.path, copy.secure,
                          copy.expires, copy.discard),
                         (cookie.name, cookie.value, cookie.domain, cookie.path, cookie.secure,
                          cookie.expires, cookie.discard))
        self.assertNotIn('not-stored', self.path.read_text())

    def test_route_profile_and_tunnel_changes_discard_cached_session(self):
        cookies = Cookies({'session': 'synthetic'})
        cookies.clear()
        cookies.set('session', 'synthetic', domain='www.avito.ru')
        self.store.save(cookies)
        for store in (self.make_store(proxy='http://other:3068'), self.make_store(profile='chrome')):
            self.assertEqual(store.load(Cookies()), 0)
        self.tunnel.write_text('{"node":"changed"}')
        self.assertEqual(self.make_store().load(Cookies()), 0)

    def test_expired_cache_and_expired_cookie_are_not_loaded(self):
        cookies = Cookies()
        cookies.set('session', 'synthetic', domain='www.avito.ru')
        self.store.save(cookies)
        payload = json.loads(self.path.read_text())
        payload['saved_at'] = time.time() - 86401
        self.path.write_text(json.dumps(payload))
        self.assertEqual(self.store.load(Cookies()), 0)
        payload['saved_at'] = time.time()
        payload['cookies'][0]['expires'] = time.time() - 1
        self.path.write_text(json.dumps(payload))
        self.assertEqual(self.store.load(Cookies()), 0)

    def test_corrupt_or_oversized_cache_is_nonfatal(self):
        for content in ('{', 'x' * 256001):
            self.path.write_text(content)
            self.assertEqual(self.store.load(Cookies()), 0)

    def test_host_only_cookie_is_preserved_without_domain_widening(self):
        cookies = Cookies()
        cookies.set('host', 'synthetic', domain='www.avito.ru')
        cookie = next(iter(cookies.jar))
        cookie.domain_specified = False
        self.store.save(cookies)
        restored = Cookies()
        self.store.load(restored)
        self.assertFalse(next(iter(restored.jar)).domain_specified)

    def test_failed_atomic_replace_keeps_last_successful_state(self):
        cookies = Cookies()
        cookies.set('session', 'first', domain='www.avito.ru')
        self.store.save(cookies)
        cookies.set('session', 'second', domain='www.avito.ru')
        with patch('parsers.avito_session.os.replace', side_effect=OSError):
            self.assertFalse(self.store.save(cookies))
        restored = Cookies()
        self.store.load(restored)
        self.assertEqual(restored.get('session'), 'first')
        self.assertEqual(list(self.path.parent.glob('.avito-session-*')), [])
