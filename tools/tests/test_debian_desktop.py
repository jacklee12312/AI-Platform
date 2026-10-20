"""Exercise package-lock gating and unprivileged desktop dispatch."""
import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'installer-bundle/installer'))
import bootstrap


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'tools' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


desktop, debian, entry = load('debian_desktop'), load('debian'), load('debian_entry')


class DebianDesktopTests(unittest.TestCase):
    def test_gnome_user_path_finds_real_admin_commands_and_keeps_desktop_identity(self):
        original = {'PATH': '/usr/local/bin:/usr/bin:/bin:/usr/games', 'DISPLAY': ':0',
                    'WAYLAND_DISPLAY': 'wayland-0', 'XAUTHORITY': '/run/user/1000/auth',
                    'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/1000/bus'}
        env = bootstrap.gui_environment(Path('/private/qt'), original)
        for command in ('usermod', 'runuser'):
            self.assertIsNone(shutil.which(command, path=original['PATH']))
            self.assertIsNotNone(shutil.which(command, path=env['PATH']))
        for key in ('DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS'):
            self.assertEqual(env[key], original[key])
        self.assertEqual(env['QT_QPA_PLATFORM'], 'xcb')

    def test_debian_entry_supplies_commands_even_with_minimal_desktop_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / 'bundle').mkdir()
            with patch.object(entry, '__file__', str(root / 'debian_entry.py')), \
                    patch.object(entry, 'verify_bundle'), patch.object(entry, 'prepare_sources', return_value=root / 'sources.json'), \
                    patch.dict(entry.os.environ, {'PATH': '/usr/bin:/bin'}, clear=True), \
                    patch.object(entry.sys, 'argv', ['debian_entry.py']), patch.object(entry.subprocess, 'call', return_value=0) as call:
                self.assertEqual(entry.main(), 0)
            env = call.call_args.kwargs['env']
            self.assertIsNotNone(shutil.which('usermod', path=env['PATH']))
            self.assertIsNotNone(shutil.which('runuser', path=env['PATH']))

    def test_real_dpkg_style_record_lock_delays_launch_without_removing_locks(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder) / name for name in ('frontend', 'database')]
            for path in paths:
                path.write_text('preserved')
            code = ('import fcntl,sys; f=open(sys.argv[1],"r+"); '
                    'fcntl.lockf(f,fcntl.LOCK_EX); print("ready",flush=True); sys.stdin.read()')
            child = subprocess.Popen([sys.executable, '-c', code, str(paths[0])],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), 'ready')
                with patch.object(desktop, 'run', return_value=SimpleNamespace(returncode=0, stdout='install ok installed\n1.3.0-9')) as query:
                    self.assertFalse(desktop.package_ready('1.3.0-9', paths))
                    query.assert_not_called()
                    child.communicate('', timeout=5)
                    self.assertTrue(desktop.package_ready('1.3.0-9', paths))
                    self.assertFalse(desktop.package_ready('1.3.0-8', paths))
                self.assertTrue(all(path.read_text() == 'preserved' for path in paths))
            finally:
                if child.poll() is None:
                    child.kill(); child.communicate()

    def test_only_active_graphical_user_sessions_are_selected(self):
        rows = [SimpleNamespace(returncode=0, stdout='1 1000 cix seat0 tty2\nc4 1000 cix\n2 0 root\n3 1000 cix\n'),
                SimpleNamespace(returncode=0, stdout='Id=1\nUser=1000\nClass=user\nType=wayland\nActive=yes\nSeat=seat0\n'),
                SimpleNamespace(returncode=0, stdout='Id=c4\nUser=1000\nClass=user\nType=x11\nActive=no\n'),
                SimpleNamespace(returncode=0, stdout='Id=2\nUser=0\nClass=user\nType=x11\nActive=yes\n'),
                SimpleNamespace(returncode=0, stdout='Id=3\nUser=1000\nClass=user\nType=tty\nActive=yes\n')]
        with patch.object(desktop, 'run', side_effect=rows):
            self.assertEqual([row['Id'] for row in desktop.graphical_sessions()], ['1'])

    def test_session_environment_does_not_inherit_root_or_qt_overrides(self):
        with tempfile.TemporaryDirectory() as folder:
            source = {'XDG_SESSION_ID': '1', 'DISPLAY': ':0', 'WAYLAND_DISPLAY': 'wayland-0',
                      'XAUTHORITY': folder + '/auth', 'LD_PRELOAD': '/bad.so', 'PYTHONPATH': '/bad',
                      'HOME': '/root', 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + folder + '/bus'}
            account = SimpleNamespace(pw_name='onsite', pw_dir='/home/onsite')
            with patch.object(desktop.pwd, 'getpwuid', return_value=account):
                session = {'User': str(os.getuid()), 'Id': '1', 'Type': 'wayland'}
                env = desktop.session_environment(session, [(['/usr/bin/gnome-shell'], source)], Path(folder))
                self.assertEqual(env['HOME'], '/home/onsite')
                self.assertEqual(env['XAUTHORITY'], source['XAUTHORITY'])
                self.assertNotIn('LD_PRELOAD', env); self.assertNotIn('PYTHONPATH', env)
                source['XDG_SESSION_ID'] = 'other'
                self.assertIsNone(desktop.session_environment(session, [(['/usr/bin/gnome-shell'], source)], Path(folder)))

    def test_dispatch_drops_root_before_running_systemd_user_or_wizard(self):
        env = {'DISPLAY': ':0', 'WAYLAND_DISPLAY': 'wayland-0', 'PATH': desktop.COMMAND_PATH}
        session = {'User': '1000', 'Id': '1', 'Type': 'wayland'}
        account = SimpleNamespace(pw_name='onsite')
        with patch.object(desktop, 'process_environments', return_value=[]), \
                patch.object(desktop, 'session_environment', return_value=env), \
                patch.object(desktop.pwd, 'getpwuid', return_value=account), \
                patch.object(desktop, 'run', return_value=SimpleNamespace(returncode=0)) as call:
            result = desktop.launch_wizard(Path('/opt/installer'), [session])
            self.assertTrue(result['opened'])
            args = call.call_args.args[0]
            self.assertEqual(args[:6], ['/usr/sbin/runuser', '-u', 'onsite', '--', '/usr/bin/env', '-i'])
            self.assertEqual(args[-1], '/usr/bin/ai-platform-installer')
            self.assertNotIn('install', args)
            self.assertIn('--user', args)

    def test_gnome_manager_environment_is_used_only_for_the_active_physical_session(self):
        with tempfile.TemporaryDirectory() as folder, socket.socket(socket.AF_UNIX) as server:
            runtime = Path(folder); server.bind(str(runtime / 'wayland-0'))
            session = {'User': str(os.getuid()), 'Id': '1', 'Type': 'wayland', 'Seat': 'seat0'}
            manager = {'DISPLAY': ':0', 'WAYLAND_DISPLAY': 'wayland-0', 'XDG_SESSION_TYPE': 'wayland',
                       'XAUTHORITY': folder + '/mutter-auth'}
            processes = [(['/usr/bin/gnome-shell'], {'XDG_SESSION_TYPE': 'wayland'})]
            env = desktop.session_environment(session, processes, runtime, manager)
            self.assertEqual(env['DISPLAY'], ':0'); self.assertEqual(env['XAUTHORITY'], manager['XAUTHORITY'])
            session['Seat'] = ''
            self.assertIsNone(desktop.session_environment(session, processes, runtime, manager))
            session['Seat'] = 'seat0'; manager['XDG_SESSION_ID'] = 'old-xrdp'
            self.assertIsNone(desktop.session_environment(session, processes, runtime, manager))

    def test_existing_wizard_is_reused_and_headless_install_has_no_gui(self):
        env = {'DISPLAY': ':0', 'WAYLAND_DISPLAY': 'wayland-0'}
        processes = [(['/usr/bin/python3', '/opt/app/bundle/installer/wizard.py'], env)]
        with patch.object(desktop, 'process_environments', return_value=processes), \
                patch.object(desktop, 'session_environment', return_value=env), patch.object(desktop, 'run') as call:
            result = desktop.launch_wizard(Path('/opt/app'), [{'User': '1000', 'Id': '1'}])
            self.assertEqual(result['reason'], 'wizard_already_running')
            call.assert_not_called()
            self.assertEqual(desktop.launch_wizard(Path('/opt/app'), [])['reason'], 'no_active_graphical_session')

    def test_postinst_dispatch_is_async_and_failure_does_not_break_package_install(self):
        controls = debian.maintainer_scripts('opt/ai-platform-installer/1.3.0', '1.3.0-9')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); script = root / 'postinst'
            script.write_bytes(controls['postinst'][0])
            subprocess.run(['/bin/sh', '-n', str(script)], check=True)
            for command in ('systemd-run', 'update-desktop-database'):
                path = root / command; path.write_text('#!/bin/sh\nexit 1\n' if command == 'systemd-run' else '#!/bin/sh\nexit 0\n'); path.chmod(0o755)
            result = subprocess.run(['/bin/sh', str(script), 'configure'], env={'PATH': str(root)}, capture_output=True)
            self.assertEqual(result.returncode, 0)
        self.assertIn(b'--collect --no-block', controls['postinst'][0])
        self.assertNotIn(b'pkexec', controls['postinst'][0])
        self.assertEqual(controls['postinst'][1], 0o755)
