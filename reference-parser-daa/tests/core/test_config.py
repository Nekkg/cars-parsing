import os
import tempfile
from pathlib import Path
from unittest.mock import patch
from django.test import SimpleTestCase
from core.config import required_env, env_port
from core.config import load_configuration


class ConfigurationTests(SimpleTestCase):
    def test_root_env_prevents_old_proxy_from_leaking_into_web(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / 'application'
            app.mkdir()
            (root / '.env').write_text('SOURCES_PROXY_URL=\n', encoding='utf-8')
            (app / '.env').write_text('SOURCES_PROXY_URL=http://legacy.invalid:8080\nLEGACY_ONLY=private\n', encoding='utf-8')
            with patch('core.config.APP_DIR', app), patch.dict(os.environ, {}, clear=True):
                self.assertEqual(load_configuration(), root / '.env')
                self.assertEqual(os.environ['SOURCES_PROXY_URL'], '')
                self.assertNotIn('LEGACY_ONLY', os.environ)
                self.assertEqual(os.environ['CARS_ENV_FILE'], str(root / '.env'))

    def test_explicit_env_and_process_variables_take_priority(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / 'application'
            app.mkdir()
            (root / '.env').write_text('TEST_SETTING=root\n', encoding='utf-8')
            chosen = root / 'chosen.env'
            chosen.write_text('TEST_SETTING=explicit\nEXPORTED_SETTING=file\n', encoding='utf-8')
            with patch('core.config.APP_DIR', app), patch.dict(os.environ, {
                'CARS_ENV_FILE': str(chosen), 'EXPORTED_SETTING': 'terminal'
            }, clear=True):
                self.assertEqual(load_configuration(), chosen)
                self.assertEqual(os.environ['TEST_SETTING'], 'explicit')
                self.assertEqual(os.environ['EXPORTED_SETTING'], 'terminal')

    def test_legacy_env_is_used_when_root_env_is_absent(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Path(temp) / 'application'
            app.mkdir()
            (app / '.env').write_text('TEST_SETTING=legacy\n', encoding='utf-8')
            with patch('core.config.APP_DIR', app), patch.dict(os.environ, {}, clear=True):
                self.assertEqual(load_configuration(), app / '.env')
                self.assertEqual(os.environ['TEST_SETTING'], 'legacy')

    def test_missing_secret_stops_startup(self):
        for value in ('', '   '):
            with patch.dict(os.environ, {'TEST_SECRET': value}):
                with self.assertRaisesRegex(ValueError, 'TEST_SECRET'):
                    required_env('TEST_SECRET')
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                required_env('TEST_SECRET')

    def test_configured_secret_is_preserved(self):
        with patch.dict(os.environ, {'TEST_SECRET': 'test-only-value'}):
            self.assertEqual(required_env('TEST_SECRET'), 'test-only-value')

    def test_ports_are_integers_and_validated(self):
        for value in ('wrong', '0', '-1', '65536'):
            with patch.dict(os.environ, {'TEST_PORT': value}):
                with self.assertRaises(ValueError):
                    env_port('TEST_PORT', 6379)
        with patch.dict(os.environ, {'TEST_PORT': '6380'}):
            self.assertEqual(env_port('TEST_PORT', 6379), 6380)
