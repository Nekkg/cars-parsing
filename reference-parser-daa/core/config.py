"""Validated local settings. Secret values never appear in errors."""
import os
from pathlib import Path
from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent.parent


def load_configuration():
    """Use one env file, and pass its path to child collectors."""
    explicit = os.getenv('CARS_ENV_FILE')
    project_env = APP_DIR.parent / '.env'
    selected = Path(explicit).expanduser().resolve() if explicit else (
        project_env if project_env.is_file() else APP_DIR / '.env'
    )
    load_dotenv(selected, encoding='utf-8', override=False)
    os.environ['CARS_ENV_FILE'] = str(selected)
    return selected


load_configuration()


def required_env(name):
    value = os.getenv(name)
    if not value or not value.strip():
        raise ValueError(f'{name} must be configured in .env')
    return value


def env_port(name, default):
    value = os.getenv(name, str(default))
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ValueError(f'{name} must be an integer port') from None
    if not 1 <= port <= 65535:
        raise ValueError(f'{name} must be between 1 and 65535')
    return port
