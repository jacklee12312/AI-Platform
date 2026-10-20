"""Open the installed wizard in a real user's desktop after dpkg has finished."""
import argparse
import fcntl
import json
import os
import pwd
import re
import subprocess
import time
from pathlib import Path

COMMAND_PATH = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
DESKTOP_PROCESSES = {'gnome-shell', 'gnome-session-binary', 'xfce4-session',
                     'mate-session', 'plasmashell', 'cinnamon-session', 'lxqt-session'}


def run(argv, **kwargs):
    return subprocess.run(argv, capture_output=True, text=True, timeout=15, **kwargs)


def package_ready(version, lock_paths=None):
    """dpkg uses POSIX record locks, not flock; never delete its lock files."""
    lock_paths = lock_paths or ['/var/lib/dpkg/lock-frontend', '/var/lib/dpkg/lock']
    locks = []
    try:
        for path in lock_paths:
            try:
                fd = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
            except FileNotFoundError:
                continue
            locks.append(fd)
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run(['/usr/bin/dpkg-query', '-W', '-f=${Status}\n${Version}',
                      'ai-platform-installer'])
        return result.returncode == 0 and result.stdout == 'install ok installed\n' + version
    except (BlockingIOError, PermissionError):
        return False
    finally:
        for fd in locks:
            os.close(fd)


def graphical_sessions():
    result = run(['/usr/bin/loginctl', 'list-sessions', '--no-legend', '--no-pager'])
    if result.returncode:
        return []
    sessions = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if not fields or not re.fullmatch(r'[A-Za-z0-9_-]+', fields[0]):
            continue
        details = run(['/usr/bin/loginctl', 'show-session', fields[0], '--no-pager',
                       '-p', 'Id', '-p', 'User', '-p', 'Type', '-p', 'Class',
                       '-p', 'Active', '-p', 'State', '-p', 'Seat'])
        values = dict(row.split('=', 1) for row in details.stdout.splitlines() if '=' in row)
        if (not details.returncode and values.get('Type') in ('wayland', 'x11')
                and values.get('Class') == 'user' and values.get('Active') == 'yes'
                and values.get('User', '').isdigit() and int(values['User']) >= 1000):
            sessions.append(values)
    # Prefer the physical desktop; desktop sharing uses that same session.
    return sorted(sessions, key=lambda row: (not bool(row.get('Seat')), row['Id']))


def process_environments(uid, proc=Path('/proc')):
    for path in proc.iterdir():
        if not path.name.isdigit():
            continue
        try:
            if path.stat().st_uid != uid:
                continue
            argv = (path / 'cmdline').read_bytes().decode().split('\0')
            env = dict(item.split('=', 1) for item in (path / 'environ').read_bytes().decode().split('\0') if '=' in item)
            yield argv, env
        except (OSError, UnicodeError):
            continue


def manager_environment(uid):
    account = pwd.getpwuid(uid)
    result = run(['/usr/sbin/runuser', '-u', account.pw_name, '--', '/usr/bin/env', '-i',
                  'HOME=' + account.pw_dir, 'PATH=' + COMMAND_PATH,
                  'XDG_RUNTIME_DIR=/run/user/' + str(uid),
                  'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/' + str(uid) + '/bus',
                  '/usr/bin/systemctl', '--user', 'show-environment'])
    return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line) if not result.returncode else {}


def session_environment(session, processes, runtime, fallback=None):
    uid = int(session['User'])
    account = pwd.getpwuid(uid)
    if not runtime.is_dir() or runtime.is_symlink() or runtime.stat().st_uid != uid:
        return None
    sources = [source for argv, source in processes if argv and Path(argv[0]).name in DESKTOP_PROCESSES
               and source.get('XDG_SESSION_ID') == session['Id']]
    # GNOME starts its shell before DISPLAY exists, then exports the final
    # display/authentication environment to the systemd user manager.
    if (fallback and session.get('Seat') and fallback.get('XDG_SESSION_TYPE') == session['Type']
            and fallback.get('XDG_SESSION_ID') in (None, session['Id'])):
        wayland = fallback.get('WAYLAND_DISPLAY', '')
        if session['Type'] == 'x11' or (re.fullmatch(r'wayland-[A-Za-z0-9_-]+', wayland)
                                       and (runtime / wayland).is_socket()):
            sources.append(fallback)
    for source in sources:
        env = {'HOME': account.pw_dir, 'USER': account.pw_name, 'LOGNAME': account.pw_name,
               'PATH': COMMAND_PATH, 'XDG_RUNTIME_DIR': str(runtime),
               'XDG_SESSION_ID': session['Id'], 'XDG_SESSION_TYPE': session['Type']}
        display, wayland = source.get('DISPLAY', ''), source.get('WAYLAND_DISPLAY', '')
        if re.fullmatch(r':\d+(?:\.\d+)?', display):
            env['DISPLAY'] = display
        if re.fullmatch(r'wayland-[A-Za-z0-9_-]+', wayland):
            env['WAYLAND_DISPLAY'] = wayland
        if not any(key in env for key in ('DISPLAY', 'WAYLAND_DISPLAY')):
            continue
        for key in ('XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'XDG_CURRENT_DESKTOP', 'LANG', 'LANGUAGE'):
            if source.get(key):
                env[key] = source[key]
        env.setdefault('DBUS_SESSION_BUS_ADDRESS', 'unix:path=' + str(runtime / 'bus'))
        return env
    return None


def wizard_running(processes, bundle, env):
    target = str(bundle / 'installer/wizard.py')
    return any(target in argv and source.get('DISPLAY') == env.get('DISPLAY')
               and source.get('WAYLAND_DISPLAY') == env.get('WAYLAND_DISPLAY')
               for argv, source in processes)


def launch_wizard(root, sessions=None):
    for session in graphical_sessions() if sessions is None else sessions:
        uid = int(session['User'])
        processes = list(process_environments(uid))
        env = session_environment(session, processes, Path('/run/user') / str(uid))
        if env is None:
            env = session_environment(session, processes, Path('/run/user') / str(uid), manager_environment(uid))
        if env is None:
            continue
        if wizard_running(processes, root / 'bundle', env):
            return {'opened': False, 'reason': 'wizard_already_running', 'session': session['Id']}
        user = pwd.getpwuid(uid).pw_name
        # Both env processes run after runuser has dropped root privileges.
        user_env = ['/usr/bin/env', '-i', *[key + '=' + value for key, value in sorted(env.items())]]
        command = ['/usr/sbin/runuser', '-u', user, '--', *user_env,
                   '/usr/bin/systemd-run', '--user', '--collect', '--quiet',
                   '--unit=ai-platform-installer-wizard', '--property=Type=exec',
                   '--', *user_env, '/usr/bin/ai-platform-installer']
        result = run(command)
        if result.returncode:
            raise RuntimeError('Could not open the wizard in user session ' + session['Id'] + ': ' + result.stderr[-1200:])
        return {'opened': True, 'session': session['Id'], 'uid': uid}
    return {'opened': False, 'reason': 'no_active_graphical_session'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package-version', required=True)
    parser.add_argument('--timeout', type=float, default=240)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('Post-install desktop dispatch must run as root')
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if package_ready(args.package_version):
            print(json.dumps(launch_wizard(Path(__file__).resolve().parent)), flush=True)
            return
        time.sleep(1)
    raise RuntimeError('Timed out waiting for completed package installation; open AI Platform Installer from the applications menu')


if __name__ == '__main__':
    main()
