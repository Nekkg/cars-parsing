import base64
import hashlib
import json
from unittest.mock import Mock, patch

from curl_cffi.requests import Cookies
from django.test import SimpleTestCase

from parsers.avito_client import AvitoClient
from parsers.avito_pow import MAX_COMPLEXITY, pow_challenge, solution
from parsers.sources import SourceBlocked


def token(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    return 'header.' + encoded + '.signature'


def response(status, text='', payload=None, url='https://www.avito.ru/samara/avtomobili?p=2'):
    value = Mock(status_code=status, text=text, url=url, headers={})
    value.iter_content.side_effect = lambda: iter([text.encode()])
    value.json.return_value = payload
    return value


class AvitoPowTests(SimpleTestCase):
    def make_client(self):
        client = AvitoClient.__new__(AvitoClient)
        client.session = Mock()
        client.session.cookies = Cookies()
        client.last_request = 0
        client.page_source = 'old'
        client.load_cookies = Mock()
        return client

    def test_nonce_satisfies_protocol(self):
        jwt = token({'id': 'synthetic', 'compl': 2})
        work = solution(jwt)
        self.assertEqual(work['challenge'], jwt)
        digest = hashlib.sha256(f"synthetic:{work['nonce']}".encode()).hexdigest()
        self.assertTrue(digest.startswith('00'))

    def test_excessive_and_malformed_work_is_rejected(self):
        for payload in ({'id':'test', 'compl':True}, {'id':'test', 'compl':MAX_COMPLEXITY+1},
                        {'id':'test', 'compl':-1}, {'id':'x'*257, 'compl':1}, []):
            with self.assertRaises(SourceBlocked):
                solution(token(payload))
        with self.assertRaises(SourceBlocked):
            solution('malformed')
        with self.assertRaises(SourceBlocked):
            solution(token({'id':'test', 'compl':2}), seconds=0)

    def test_html_challenge_uses_only_applicable_cookie(self):
        cookies = Cookies()
        cookies.set('pow_challenge', 'wrong', domain='example.com')
        cookies.set('pow_challenge', 'fresh%20value', domain='.avito.ru')
        html = response(439, "fetch('/web/3/firewallPow/get')")
        self.assertEqual(pow_challenge(html, cookies), 'fresh value')
        self.assertIsNone(pow_challenge(response(429, 'IP restricted'), cookies))

    def test_json_challenge_is_supported_without_cookie(self):
        self.assertEqual(pow_challenge(response(439, payload={'pow_challenge':'xhr'}), Cookies()), 'xhr')

    def test_verified_flow_retries_exact_city_query_and_page(self):
        client = self.make_client()
        client.session.get.side_effect = [response(439, payload={'pow_challenge':'fresh'}), response(200, '<h1>Cars</h1>')]
        client.session.post.side_effect = [response(200, payload={'success':{'result':{'challenge_jwt':token({'id':'x', 'compl':0})}}}),
                                         response(200, payload={'success':{'result':{'verified':True}}})]
        url = 'https://www.avito.ru/samara/avtomobili?q=BMW+X1&p=2'
        with patch('parsers.avito_client.time.sleep'):
            client.get(url)
        self.assertEqual([call.args[0] for call in client.session.get.call_args_list], [url, url])
        self.assertEqual(client.page_source, '<h1>Cars</h1>')
        for call in client.session.post.call_args_list:
            self.assertFalse(call.kwargs['allow_redirects'])
            self.assertEqual(call.kwargs['headers']['Referer'], url)

    def test_truthy_verification_does_not_advance(self):
        client = self.make_client()
        client.session.get.return_value = response(439, payload={'pow_challenge':'fresh'})
        client.session.post.side_effect = [response(200, payload={'success':{'result':{'challenge_jwt':token({'id':'x','compl':0})}}}),
                                         response(200, payload={'success':{'result':{'verified':'true'}}})]
        with patch('parsers.avito_client.time.sleep'), self.assertRaises(SourceBlocked):
            client.get('https://www.avito.ru/samara/avtomobili')
        self.assertEqual(client.page_source, '')
        client.session.get.assert_called_once()

    def test_persistent_challenges_stop_after_two_handshakes(self):
        client = self.make_client()
        client.session.get.return_value = response(439, payload={'pow_challenge':'fresh'})
        task = response(200, payload={'success':{'result':{'challenge_jwt':token({'id':'x','compl':0})}}})
        verified = response(200, payload={'success':{'result':{'verified':True}}})
        client.session.post.side_effect = [task, verified, task, verified]
        with patch('parsers.avito_client.time.sleep'), self.assertRaises(SourceBlocked):
            client.get('https://www.avito.ru/samara/avtomobili')
        self.assertEqual(client.session.get.call_count, 3)
        self.assertEqual(client.session.post.call_count, 4)

    def test_expired_deadline_makes_no_request(self):
        client = self.make_client()
        with self.assertRaises(SourceBlocked):
            client.request('get', 'https://www.avito.ru/', deadline=0)
        client.session.get.assert_not_called()


class AvitoStreamTests(SimpleTestCase):
    make_client = AvitoPowTests.make_client
    def test_transient_connection_error_retries_once(self):
        from curl_cffi.requests.exceptions import ConnectionError
        client = self.make_client()
        client.session.get.side_effect = [ConnectionError('synthetic disconnect'), response(200, '<html></html>')]
        with patch('parsers.avito_client.time.sleep'):
            client.get('https://www.avito.ru/samara/avtomobili')
        self.assertEqual(client.session.get.call_count, 2)
        self.assertEqual(client.page_source, '<html></html>')

    def test_complete_html_survives_a_trailer_error(self):
        from curl_cffi.requests.exceptions import ConnectionError
        client = self.make_client()
        document = response(200, '<html><h1>Cars</h1></html>')
        def interrupted():
            yield b'<html><h1>Cars</h1></html>'
            raise ConnectionError('synthetic trailer error')
        document.iter_content.side_effect = interrupted
        client.session.get.return_value = document
        with patch('parsers.avito_client.time.sleep'):
            client.get('https://www.avito.ru/samara/avtomobili')
        self.assertEqual(document.content, b'<html><h1>Cars</h1></html>')
        client.session.get.assert_called_once()

    def test_partial_html_is_never_an_accepted_listing(self):
        from curl_cffi.requests.exceptions import ConnectionError
        client = self.make_client()
        document = response(200, '<html>partial')
        def interrupted():
            yield b'<html>partial'
            raise ConnectionError('synthetic disconnect')
        document.iter_content.side_effect = interrupted
        client.session.get.return_value = document
        with patch('parsers.avito_client.time.sleep'), self.assertRaises(ConnectionError):
            client.get('https://www.avito.ru/samara/avtomobili')
        self.assertEqual(client.page_source, '')
        self.assertEqual(client.session.get.call_count, 2)

    def test_response_size_is_bounded(self):
        client = self.make_client()
        document = response(200, '<html></html>')
        document.iter_content.side_effect = lambda: iter([b'x'*10_000_001])
        client.session.get.return_value = document
        with self.assertRaises(SourceBlocked):
            client.get('https://www.avito.ru/samara/avtomobili')
        self.assertEqual(client.page_source, '')
        client.session.get.assert_called_once()
