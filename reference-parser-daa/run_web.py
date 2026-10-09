"""Optional native development entry point; Docker is documented in README."""
import subprocess
import sys
from pathlib import Path
if __name__ == '__main__':
    subprocess.run([sys.executable, 'manage.py', 'runserver', '--insecure', '127.0.0.1:8000'],
                   cwd=Path(__file__).resolve().parent, check=True)
