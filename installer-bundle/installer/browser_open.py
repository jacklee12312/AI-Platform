"""Open HTTP links with the desktop environment, outside the private Qt runtime."""
import configparser
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
from urllib.parse import urlsplit

ENV_MARKER = 'AI_PLATFORM_DESKTOP_ENV'
RUNTIME_KEYS = ('PYTHONPATH', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM', 'LD_LIBRARY_PATH')
BROWSERS = {'chromium', 'chromium-browser', 'google-chrome', 'google-chrome-stable',
            'firefox', 'firefox-esr', 'brave-browser', 'microsoft-edge'}


def capture_desktop_environment(environ):
    return json.dumps({key: environ.get(key) for key in RUNTIME_KEYS})


def desktop_environment(environ):
    result = dict(environ)
    try:
        original = json.loads(result.pop(ENV_MARKER, '{}'))
    except (TypeError, ValueError):
        original = {}
    if not isinstance(original, dict):original = {}
    for key in RUNTIME_KEYS:
        value = original.get(key)
        if isinstance(value, str):result[key] = value
        else:result.pop(key, None)
    return result


def default_browser_command(url, environ):
    """Work around vendor desktop entries whose Exec omits the URL field code."""
    try:
        settings = subprocess.run(['xdg-settings', 'get', 'default-web-browser'],
                                  env=environ, capture_output=True, text=True, timeout=3)
        name = settings.stdout.strip()
        if settings.returncode or not re.fullmatch(r'[\w.+-]+\.desktop', name):return None
        home = Path(environ.get('HOME', str(Path.home())))
        roots = [Path(environ.get('XDG_DATA_HOME') or home / '.local/share')]
        roots += [Path(p) for p in environ.get('XDG_DATA_DIRS', '/usr/local/share:/usr/share').split(':') if p]
        entry = next((p / 'applications' / name for p in roots
                      if p.is_absolute() and (p / 'applications' / name).is_file()), None)
        if entry is None:return None
        config = configparser.ConfigParser(interpolation=None)
        config.read(entry, encoding='utf-8')
        command = config.get('Desktop Entry', 'Exec', fallback='')
        if re.search(r'(?<!%)%[uU]', command):return None
        arguments = shlex.split(command)
        if not arguments or Path(arguments[0]).name not in BROWSERS:return None
        executable = shutil.which(arguments[0], path=environ.get('PATH'))
        if executable:return [executable, url]
    except (OSError, ValueError, configparser.Error, subprocess.SubprocessError):
        pass
    return None


def browser_request(url, environ=None):
    if (not isinstance(url, str) or any(ord(c) < 32 or ord(c) == 127 for c in url)):
        raise ValueError('打开地址无效')
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('打开地址必须是有效的 HTTP/HTTPS 网址')
    env = desktop_environment(os.environ if environ is None else environ)
    return default_browser_command(url, env) or ['xdg-open', url], env
