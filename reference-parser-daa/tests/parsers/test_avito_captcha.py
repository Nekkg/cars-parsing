from unittest.mock import Mock, patch
from io import BytesIO
import json
import time
from PIL import Image
from django.test import SimpleTestCase
from parsers.avito_captcha import captcha_id, jsonp, selected_geetest, validate_captcha_url, recover_captcha, ResourceSession
from parsers.sources import SourceBlocked
from parsers.vendor.geekedtest.sign import Signer


class AvitoCaptchaTests(SimpleTestCase):
    def test_generic_ip_page_is_not_a_geetest_task(self):
        self.assertIsNone(captcha_id(Mock(status_code=429, text='IP restricted')))
        html="geetest_captcha initGeetest4 gt4.js const captchaId='"+'a'*32+"';"
        self.assertEqual(captcha_id(Mock(status_code=429,text=html)), 'a'*32)

    def test_provider_hosts_ports_and_paths_are_restricted(self):
        for url in ('https://evil.example/verify','https://user:pw@gcaptcha4.geevisit.com/load',
                    'http://gcaptcha4.geevisit.com/load','https://static.geetest.com:8080/captcha_v4/image.png',
                    'https://gcaptcha4.geevisit.com/other'):
            with self.assertRaises(ValueError):validate_captcha_url(url)
        validate_captcha_url('https://static.geetest.com/captcha_v4/test/image.png')
        validate_captcha_url('https://static.geetest.com/pictures/v4_pic/slide/image.png')
        validate_captcha_url('https://gcaptcha4.geevisit.com/load')

    def test_jsonp_does_not_execute_code_or_accept_wrong_callback(self):
        r=Mock(status_code=200,content=b'body',text='wrong({"status":"success","data":{}})')
        with self.assertRaises(SourceBlocked):jsonp(r,'geetest_123')
        r.text='geetest_123({"status":"success","data":{"captcha_type":"slide"}});'
        self.assertEqual(jsonp(r,'geetest_123')['captcha_type'],'slide')
        r.text='geetest_123(__import__("os").system("dummy"))'
        with self.assertRaises(SourceBlocked):jsonp(r,'geetest_123')

    def test_selection_requires_the_selected_geetest_variant(self):
        self.assertTrue(selected_geetest({'captcha':{'geeTest':{'type':'geeTest'}}}))
        self.assertFalse(selected_geetest({'captcha':{'hCaptcha':{'type':'hCaptcha'}}}))

    def test_modified_signer_has_bounded_and_correct_pow(self):
        work=Signer.generate_pow('lot','id','sha256','1',0,'date','')
        self.assertIn('pow_sign',work)
        for bits in (-1,True,21):
            with self.assertRaises(ValueError):Signer.generate_pow('lot','id','sha256','1',bits,'date','')
        # For non-multiple-of-four work, check the hash nibble, not its index.
        with patch.object(Signer,'rand_uid',side_effect=['a','b']), patch('parsers.vendor.geekedtest.sign.hashlib.sha256') as sha:
            sha.return_value.hexdigest.side_effect=['f'+'0'*63,'7'+'0'*63]
            work=Signer.generate_pow('lot','id','sha256','1',1,'date','')
        self.assertTrue(work['pow_msg'].endswith('b'))

    def test_captcha_image_size_is_bounded_before_opencv(self):
        client=Mock();client.request.return_value=Mock(status_code=200,content=b'x'*2_000_001)
        with self.assertRaises(SourceBlocked):ResourceSession(client,time.monotonic()+20).get('https://static.geetest.com/captcha_v4/image.png')

    def test_avatar_or_unsupported_provider_does_not_advance(self):
        client=Mock()
        client.request.return_value.status_code=200
        client.request.return_value.json.return_value={'success':{'result':{'captcha':{'type':'hCaptcha'}}}}
        html="geetest_captcha initGeetest4 gt4.js const captchaId='"+'a'*32+"';"
        with self.assertRaises(SourceBlocked):recover_captcha(client,Mock(status_code=429,text=html),'https://www.avito.ru/',time.monotonic()+30)
        client.request.assert_called_once()

    def flow_client(self, *, verified=True, lot='lot'):
        client=Mock()
        task={'captcha_type':'slide','pt':'1','lot_number':'lot','payload':'opaque','process_token':'opaque',
              'pow_detail':{'bits':0}}
        seccode={'captcha_id':'a'*32,'lot_number':lot,'pass_token':'opaque','gen_time':'1','captcha_output':'opaque'}
        def exchange(method,url,**kwargs):
            r=Mock(status_code=200)
            if url.endswith('/firewallCaptcha/get'):
                r.json.return_value={'success':{'result':{'captcha':{'geeTest':{'type':'geeTest'}}}}}
            elif url.endswith('/firewallCaptcha/verify'):
                r.json.return_value={'success':{'result':{'verified':verified}}}
            else:
                data=task if url.endswith('/load') else {'seccode':seccode}
                r.text=kwargs['params']['callback']+'('+json.dumps({'status':'success','data':data})+')'
                r.content=r.text.encode()
            return r
        client.request.side_effect=exchange
        return client

    def test_full_flow_requires_confirmation_from_avito(self):
        client=self.flow_client()
        html="geetest_captcha initGeetest4 gt4.js const captchaId='"+'a'*32+"';"
        with patch.object(Signer,'generate_w',return_value='opaque'):
            self.assertTrue(recover_captcha(client,Mock(status_code=429,text=html),'https://www.avito.ru/samara/avtomobili?p=2',time.monotonic()+90))
        self.assertEqual(client.request.call_count,4)
        final=client.request.call_args
        self.assertIn('/firewallCaptcha/verify',final.args[1])
        self.assertEqual(final.kwargs['headers']['Referer'],'https://www.avito.ru/samara/avtomobili?p=2')
        self.assertEqual(final.kwargs['json']['lot_number'],'lot')

    def test_truthy_or_wrong_task_confirmation_is_rejected(self):
        html="geetest_captcha initGeetest4 gt4.js const captchaId='"+'a'*32+"';"
        for client in (self.flow_client(verified='true'),self.flow_client(lot='other')):
            with patch.object(Signer,'generate_w',return_value='opaque'),self.assertRaises(SourceBlocked):
                recover_captcha(client,Mock(status_code=429,text=html),'https://www.avito.ru/',time.monotonic()+90)

    def test_slide_matching_works_with_installed_opencv(self):
        import numpy as np
        from parsers.vendor.geekedtest.slide import SlideSolver
        noise=np.random.default_rng(42).integers(0,256,(120,300,3),dtype=np.uint8)
        def png(array):
            out=BytesIO();Image.fromarray(array).save(out,format='PNG');return out.getvalue()
        offset=SlideSolver(png(noise[:,80:162]),png(noise)).find_puzzle_piece_position()
        self.assertLessEqual(abs(offset-80),1)


class RepeatedAvitoChallengeTests(SimpleTestCase):
    def make_client(self, responses):
        from parsers.avito_client import AvitoClient
        client = AvitoClient.__new__(AvitoClient)
        client.page_source = ''
        client.load_cookies = Mock()
        client.recover_pow = Mock(return_value=False)
        client.request = Mock(side_effect=responses)
        return client

    def test_second_challenge_can_finish_within_the_same_bounded_request(self):
        blocked = Mock(status_code=429,text='challenge',headers={})
        ok = Mock(status_code=200,text='<html><h1>Volvo S60</h1></html>',headers={})
        client = self.make_client([blocked,blocked,ok])
        url = 'https://www.avito.ru/all/avtomobili?p=2&q=Volvo+S60'
        with patch('parsers.avito_captcha.recover_captcha',return_value=True) as recover:
            client.get(url)
        self.assertEqual(recover.call_count,2)
        self.assertEqual([call.args[1] for call in client.request.call_args_list],[url]*3)
        self.assertIn('Volvo S60',client.page_source)

    def test_persistent_captcha_is_bounded_and_never_yields_a_fake_listing(self):
        blocked = Mock(status_code=429,text='challenge',headers={})
        client = self.make_client([blocked]*3)
        with patch('parsers.avito_captcha.recover_captcha',return_value=True) as recover:
            with self.assertRaises(SourceBlocked):
                client.get('https://www.avito.ru/all/avtomobili')
        self.assertEqual(recover.call_count,2)
        self.assertEqual(client.request.call_count,3)
        self.assertEqual(client.page_source,'')
