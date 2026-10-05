"""Read-only development diagnostics. Never probe seller accounts or print secrets."""
import importlib.metadata
import json
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'workbench'))
from media import ffmpeg, draw_text
from PIL import Image, ImageDraw, features


def main():
    if sys.version_info < (3, 11):
        raise SystemExit('Python 3.11+ required')
    versions = {name: importlib.metadata.version(name) for name in
                ('Pillow', 'cryptography', 'imageio-ffmpeg', 'boto3', 'arabic-reshaper', 'python-bidi')}
    engine = subprocess.run([ffmpeg(), '-version'], capture_output=True, text=True, timeout=15)
    engine.check_returncode()
    if not shutil.which('node'):
        raise SystemExit('Node.js required for browser and JavaScript checks')
    canvas = ImageDraw.Draw(Image.new('RGB', (600, 200), 'white'))
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        fallback_layout = bool(arabic_reshaper and get_display)
    except ImportError:
        fallback_layout = False
    raqm_layout = features.check('raqm')
    arabic_layout = raqm_layout or fallback_layout
    labels = ['Cloud template', '商品模板'] + (['قالب المنتج'] if arabic_layout else [])
    for label in labels:
        draw_text(canvas, label, (0, 0, 600, 200), 24)
    print(json.dumps({'python': sys.version.split()[0], 'dependencies': versions,
                      'ffmpeg': engine.stdout.splitlines()[0],
                      'arabic_layout': arabic_layout,
                      'arabic_layout_engine': 'raqm' if raqm_layout else ('python_fallback' if fallback_layout else 'unavailable'),
                      'business_data': 'not inspected', 'real_noon_verified': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
