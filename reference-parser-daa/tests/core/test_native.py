import os
import subprocess
import sys
import tempfile
from pathlib import Path
from io import StringIO
from unittest.mock import patch, Mock
from django.test import TestCase, SimpleTestCase
from django.db import connection
from django.utils import timezone
from datetime import timedelta


class NativeDatabaseTests(TestCase):
    def test_filter_lists_round_trip(self):
        from users.models import UserFilters
        row = UserFilters.objects.create(user_id=881, platform=['avito', 'drom'], city=['Самара'])
        row.refresh_from_db()
        self.assertEqual(row.platform, ['avito', 'drom'])
        self.assertEqual(row.city, ['Самара'])
        row.platform = []
        row.save()
        self.assertEqual(UserFilters.objects.get(pk=row.pk).platform, [])

    def test_source_backoff_excludes_transient_audit(self):
        from parsers.collector import saved_backoff_seconds
        from parsers.models import VehicleAudit
        from ads.models import CatalogNode
        model = CatalogNode.objects.create(kind='model', key='test/car', name='Test')
        VehicleAudit.objects.create(source='avito', model=model,
            pause_until=timezone.now()+timedelta(minutes=5), evidence={'error_type':'SourceUnavailable'})
        self.assertEqual(saved_backoff_seconds('avito'), 0)


class NativeEntryPointTests(SimpleTestCase):
    def test_windows_shutdown_closes_the_venv_process_tree(self):
        from core.cli import stop_collector
        process = Mock(pid=12345)
        process.poll.return_value = None
        with patch('core.cli.sys.platform', 'win32'), patch('core.cli.subprocess.run') as run:
            stop_collector(process)
        self.assertEqual(run.call_args.args[0], ['taskkill','/PID','12345','/T','/F'])
        process.wait.assert_called_once_with(timeout=10)

    def test_bot_imports_without_starting_polling(self):
        code = '''
from unittest.mock import patch
with patch('aiogram.Bot') as create_bot, patch('aiogram.Dispatcher.start_polling') as polling:
    import bot.tg_bot
    assert callable(bot.tg_bot.main)
    create_bot.assert_not_called()
    polling.assert_not_called()
'''
        with tempfile.TemporaryDirectory() as temp:
            env = os.environ.copy()
            env.pop('TELEGRAM_BOT_TOKEN', None)
            env['CARS_ENV_FILE'] = str(Path(temp) / 'missing.env')
            env['CARS_DATA_DIR'] = str(Path(temp) / 'data')
            result = subprocess.run([sys.executable, '-c', code], env=env,
                                    capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_invalid_token_gives_clear_error_before_database_setup(self):
        from core.cli import main
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN':''}), patch('core.cli.prepare') as prepare, patch('sys.stderr', new_callable=StringIO) as stderr:
            with self.assertRaises(SystemExit):
                main(['bot'])
            prepare.assert_not_called()
            self.assertIn('TELEGRAM_BOT_TOKEN', stderr.getvalue())

    def test_local_chrome_does_not_require_docker_endpoint(self):
        from parsers.collector import browser
        with patch.dict(os.environ, {'SELENIUM_URL':'', 'SOURCES_PROXY_URL':''}), patch('parsers.collector.webdriver.Chrome') as chrome:
            driver = browser('autoru')
            self.assertEqual(driver, chrome.return_value)
            chrome.assert_called_once()
