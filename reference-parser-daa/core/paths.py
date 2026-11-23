"""Local runtime artifacts live outside source packages."""
from pathlib import Path
import os

APP_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = Path(os.getenv('CARS_DATA_DIR', str(APP_DIR / 'runtime'))).expanduser().resolve()


def runtime_directory(name):
    directory = RUNTIME_DIR / name
    directory.mkdir(parents=True, exist_ok=True)
    return directory


DIAGNOSTICS_DIR = runtime_directory('diagnostics')
SESSIONS_DIR = runtime_directory('sessions')
SCREENSHOTS_DIR = runtime_directory('screenshots')
BOT_LOG_FILE = runtime_directory('logs') / 'bot.log'
