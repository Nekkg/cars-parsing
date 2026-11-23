"""Validated local settings. Secret values never appear in errors."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(os.getenv('CARS_ENV_FILE') or Path(__file__).resolve().parent.parent / '.env', encoding='utf-8')


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
