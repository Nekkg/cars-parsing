"""Independent native entry points. Importing the CLI never starts Telegram."""
import argparse
import os
from pathlib import Path
import subprocess
import sys


def stop_collector(process):
    """Stop the entire Windows venv process tree, or the POSIX child."""
    if process.poll() is not None:
        return
    if sys.platform == 'win32':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def prepare():
    from dotenv import load_dotenv
    load_dotenv(os.getenv('CARS_ENV_FILE') or Path(__file__).resolve().parent.parent / '.env', encoding='utf-8')
    os.environ.setdefault('CARS_DATABASE', 'sqlite')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
    import django
    django.setup()
    from django.core.management import call_command
    call_command('migrate', interactive=False, verbosity=0)
    from parsers.models import Parser
    for source in ('avito', 'autoru', 'drom'):
        Parser.objects.get_or_create(name=source, defaults={'is_active': True})


def main(argv=None):
    parser = argparse.ArgumentParser(description='Локальный сайт, бот и сборщики автомобилей')
    parser.add_argument('--env', help='Путь к локальному файлу .env')
    sub = parser.add_subparsers(dest='command', required=True)
    web = sub.add_parser('web', help='Сайт без Telegram и Docker')
    web.add_argument('--port', type=int, default=8000)
    web.add_argument('--no-collect', action='store_true', help='Только сохранённые данные')
    sub.add_parser('bot', help='Telegram long polling (нужен TELEGRAM_BOT_TOKEN)')
    sub.add_parser('collect', help='Только общий сборщик')
    sub.add_parser('init', help='Миграции и регистрация источников')
    sub.add_parser('check', help='Проверка конфигурации без запросов к Telegram')
    args = parser.parse_args(argv)
    if args.env:
        os.environ['CARS_ENV_FILE'] = str(Path(args.env).resolve())
    if args.command == 'web' and not 1 <= args.port <= 65535:
        parser.error('Порт должен быть от 1 до 65535')
    # Token validation precedes setup; never print the token or send a message.
    if args.command == 'bot':
        from core.config import required_env
        try:
            from aiogram.utils.token import validate_token
            validate_token(required_env('TELEGRAM_BOT_TOKEN'))
        except Exception:
            parser.error('Укажите корректный TELEGRAM_BOT_TOKEN в .env; значение скрыто.')
    prepare()
    from django.core.management import call_command
    if args.command == 'check':
        call_command('check')
    elif args.command == 'web':
        collector = None
        try:
            if not args.no_collect:
                options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == 'win32' else {}
                collector = subprocess.Popen([sys.executable, '-m', 'parsers.collector'], **options)
            print(f'Сайт: http://127.0.0.1:{args.port}  Остановка: Ctrl+C', flush=True)
            call_command('runserver', f'127.0.0.1:{args.port}', use_reloader=False, insecure=True)
        finally:
            if collector is not None:
                stop_collector(collector)
    elif args.command == 'bot':
        import asyncio
        from bot.tg_bot import main as run_bot
        asyncio.run(run_bot())
    elif args.command == 'collect':
        from parsers.collector import main as run_collectors
        run_collectors()


if __name__ == '__main__':
    main()
