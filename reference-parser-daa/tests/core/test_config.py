import os
from unittest.mock import patch
from django.test import SimpleTestCase
from core.config import required_env, env_port


class ConfigurationTests(SimpleTestCase):
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
