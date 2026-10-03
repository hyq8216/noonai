"""Read-only development diagnostics. Never probe seller accounts or print secrets."""
import importlib.metadata
import json
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'workbench'))
from media import ffmpeg


def main():
    if sys.version_info < (3, 11):
        raise SystemExit('Python 3.11+ required')
    versions = {name: importlib.metadata.version(name) for name in
                ('Pillow', 'cryptography', 'imageio-ffmpeg', 'boto3')}
    engine = subprocess.run([ffmpeg(), '-version'], capture_output=True, text=True, timeout=15)
    engine.check_returncode()
    if not shutil.which('node'):
        raise SystemExit('Node.js required for browser and JavaScript checks')
    print(json.dumps({'python': sys.version.split()[0], 'dependencies': versions,
                      'ffmpeg': engine.stdout.splitlines()[0],
                      'business_data': 'not inspected', 'real_noon_verified': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
