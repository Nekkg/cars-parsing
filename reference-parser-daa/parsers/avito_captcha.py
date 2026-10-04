"""Local GeeTest recovery for the variants supported by our MIT signer.

Only the captcha provider already selected by Avito is contacted. No outsourced
solver, tokens in logs, browser, dynamic JS execution or automatic model download.
"""
import json
import logging
import re
import secrets
import time
import uuid
from io import BytesIO
from urllib.parse import urlparse

from PIL import Image
from parsers.avito_pow import result as endpoint_result
from parsers.sources import SourceBlocked

logger = logging.getLogger(__name__)
PROVIDER = 'https://gcaptcha4.geevisit.com'


def result(response):
    return endpoint_result(response, stage='captcha')


def validate_captcha_url(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('Unsafe captcha resource URL')
    if parsed.hostname == 'gcaptcha4.geevisit.com' and parsed.path in ('/load', '/verify'):
        return
    if parsed.hostname == 'static.geetest.com' and parsed.path.startswith(('/captcha_v4/', '/pictures/v4_pic/')):
        return
    raise ValueError('Unexpected captcha resource')


def captcha_id(response):
    if response.status_code != 429 or not isinstance(response.text, str):
        return None
    if not all(marker in response.text for marker in ('geetest_captcha', 'initGeetest4', 'gt4.js')):
        return None
    match = re.search(r"const\s+captchaId\s*=\s*['\"]([a-f0-9]{32})['\"]", response.text)
    return match[1] if match else None


def jsonp(response, callback):
    if response.status_code != 200 or len(response.content) > 200_000:
        raise SourceBlocked('avito: invalid captcha provider response')
    match = re.fullmatch(r'\s*' + re.escape(callback) + r'\((.*)\)\s*;?\s*', response.text, re.S)
    try:
        payload = json.loads(match[1]) if match else None
        if not isinstance(payload, dict) or payload.get('status') != 'success' or not isinstance(payload.get('data'), dict):
            raise ValueError
        return payload['data']
    except (ValueError, TypeError):
        raise SourceBlocked('avito: malformed captcha provider response') from None


def selected_geetest(body):
    captcha = body.get('captcha', {})
    if not isinstance(captcha, dict):
        return False
    candidates = [captcha, captcha.get('geeTest')]
    return any(isinstance(value, dict) and value.get('type') == 'geeTest' for value in candidates)


class ResourceSession:
    """Signer image I/O stays on the same route with limits and no redirects."""
    def __init__(self, client, deadline):
        self.client, self.deadline = client, deadline

    def get(self, url, **kwargs):
        validate_captcha_url(url)
        response = self.client.request('get', url, deadline=self.deadline, _challenge=True,
                                       headers={'Referer':'https://www.avito.ru/', 'Accept':'image/*',
                                                'Sec-Fetch-Dest':'image', 'Sec-Fetch-Mode':'no-cors',
                                                'Sec-Fetch-Site':'cross-site'})
        if response.status_code != 200 or len(response.content) > 2_000_000:
            raise SourceBlocked('avito: captcha image unavailable')
        with Image.open(BytesIO(response.content)) as image:
            if not 0 < image.width * image.height <= 1_000_000:
                raise SourceBlocked('avito: captcha image dimensions exceeded')
            # OpenCV receives a canonical image with bounded dimensions.
            output = BytesIO()
            image.convert('RGB').save(output, format='PNG')
            response.content = output.getvalue()
        return response


def recover_captcha(client, response, url, deadline):
    identifier = captcha_id(response)
    if not identifier:
        return False
    headers = {'Accept':'application/json', 'Origin':'https://www.avito.ru', 'Referer':url,
               'Sec-Fetch-Dest':'empty', 'Sec-Fetch-Mode':'cors', 'Sec-Fetch-Site':'same-origin'}
    selected = result(client.request('post', 'https://www.avito.ru/web/5/firewallCaptcha/get',
                                    deadline=deadline, headers=headers,
                                    json={'refreshInternalCaptcha':False}))
    if not selected_geetest(selected):
        raise SourceBlocked('avito: unsupported captcha provider selected')
    callback = 'geetest_' + str(int(time.time()*1000) + secrets.randbelow(10000))
    provider_headers = {'Referer':'https://www.avito.ru/', 'Accept':'*/*',
                        'Sec-Fetch-Dest':'script', 'Sec-Fetch-Mode':'no-cors',
                        'Sec-Fetch-Site':'cross-site'}
    load = client.request('get', PROVIDER + '/load', deadline=deadline, _challenge=True,
                          headers=provider_headers, params={'callback':callback, 'captcha_id':identifier,
                          'challenge':str(uuid.uuid4()), 'client_type':'web', 'lang':'rus'})
    task = jsonp(load, callback)
    if task.get('captcha_type') not in ('slide', 'ai', 'invisible') or task.get('pt') != '1':
        raise SourceBlocked('avito: unsupported captcha task')
    required = ('lot_number', 'payload', 'process_token')
    if not all(isinstance(task.get(key), str) and 0 < len(task[key]) <= 16384 for key in required):
        raise SourceBlocked('avito: incomplete captcha task')
    pow_detail = task.get('pow_detail')
    if not isinstance(pow_detail, dict) or type(pow_detail.get('bits')) is not int or not 0 <= pow_detail['bits'] <= 20:
        raise SourceBlocked('avito: excessive captcha work')
    import cv2
    cv2.setNumThreads(1)
    from parsers.vendor.geekedtest.sign import Signer
    encrypted = Signer.generate_w(task, identifier, task['captcha_type'],
                                  session=ResourceSession(client, deadline))
    if time.monotonic() >= deadline:
        raise SourceBlocked('avito: captcha time budget exceeded')
    callback = 'geetest_' + str(int(time.time()*1000) + secrets.randbelow(10000))
    verify = client.request('get', PROVIDER + '/verify', deadline=deadline, _challenge=True,
                            headers=provider_headers, params={'callback':callback, 'captcha_id':identifier,
                            'client_type':'web', 'lot_number':task['lot_number'], 'payload':task['payload'],
                            'process_token':task['process_token'], 'payload_protocol':'1', 'pt':'1', 'w':encrypted})
    seccode = jsonp(verify, callback).get('seccode')
    keys = ('captcha_id', 'lot_number', 'pass_token', 'gen_time', 'captcha_output')
    if not isinstance(seccode, dict) or not all(isinstance(seccode.get(key), str) and 0 < len(seccode[key]) <= 16384 for key in keys):
        raise SourceBlocked('avito: local captcha solution rejected')
    if seccode['captcha_id'] != identifier or seccode['lot_number'] != task['lot_number']:
        raise SourceBlocked('avito: captcha task identity mismatch')
    confirmed = result(client.request('post','https://www.avito.ru/web/3/firewallCaptcha/verify',
                                      deadline=deadline, headers={**headers, 'X-Cube':str(10+secrets.randbelow(90))},
                                      json={'captcha':'', 'hCaptchaResponse':'', **{key:seccode[key] for key in keys}}))
    if confirmed.get('verified') is not True:
        raise SourceBlocked('avito: captcha verification rejected by Avito')
    logger.info('Avito local GeeTest verification accepted')
    return True