"""Read build-generated version metadata in both the UI and copied worker."""
import json
from pathlib import Path


def current_version():
    here = Path(__file__).resolve()
    for path in (here.with_name('release.json'), here.parents[1] / 'release.json'):
        if path.is_file():
            return json.loads(path.read_text(encoding='utf-8'))['installer_version']
    raise RuntimeError('缺少安装器版本信息，请使用完整交付包')


VERSION = current_version()
