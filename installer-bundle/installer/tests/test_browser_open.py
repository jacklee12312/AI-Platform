import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import browser_open


class BrowserTests(unittest.TestCase):
    def test_desktop_environment_restores_vendor_libraries_and_session(self):
        original={'LD_LIBRARY_PATH':'/usr/share/cix/lib','DISPLAY':':10',
                  'DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/1000/bus'}
        env=dict(original,PYTHONPATH='/private/qt',QT_PLUGIN_PATH='/private/plugins',
                 QT_QPA_PLATFORM='xcb',LD_LIBRARY_PATH='/private/libs',
                 AI_PLATFORM_DESKTOP_ENV=browser_open.capture_desktop_environment(original))
        self.assertEqual(browser_open.desktop_environment(env),original)
        self.assertEqual(env['PYTHONPATH'],'/private/qt')

    def test_missing_or_invalid_marker_drops_private_runtime(self):
        for marker in ['not json','[]','{"LD_LIBRARY_PATH": []}']:
            self.assertEqual(browser_open.desktop_environment({'DISPLAY':':0',
                'LD_LIBRARY_PATH':'/private/libs','AI_PLATFORM_DESKTOP_ENV':marker}),{'DISPLAY':':0'})

    def test_http_only_and_no_empty_or_credential_urls(self):
        for url in ['', 'file:///etc/passwd','javascript:alert(1)','http:///bad',
                    'http://user:secret@localhost/','http://localhost/\n--bad']:
            with self.subTest(url=url),self.assertRaises(ValueError):browser_open.browser_request(url,{})

    def entry_request(self,command,url='http://127.0.0.1:17780/'):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);applications=root/'applications';applications.mkdir()
            (applications/'chromium.desktop').write_text('[Desktop Entry]\nExec='+command+'\n')
            env={'HOME':str(root),'XDG_DATA_HOME':str(root),'XDG_DATA_DIRS':str(root),'DISPLAY':':10'}
            result=mock.Mock(returncode=0,stdout='chromium.desktop\n')
            with mock.patch('browser_open.subprocess.run',return_value=result),mock.patch('browser_open.shutil.which',return_value='/usr/bin/chromium'):
                return browser_open.browser_request(url,env)

    def test_vendor_entry_missing_url_passes_url_explicitly(self):
        command,env=self.entry_request('chromium --ozone-platform=wayland --gtk-version=4')
        self.assertEqual(command,['/usr/bin/chromium','http://127.0.0.1:17780/'])
        self.assertEqual(env['DISPLAY'],':10')

    def test_correct_default_entry_keeps_system_opener(self):
        command,_=self.entry_request('chromium --gtk-version=4 %U')
        self.assertEqual(command,['xdg-open','http://127.0.0.1:17780/'])

    def test_urls_with_query_are_one_argument_without_shell(self):
        url='http://192.168.1.47:8081/?a=1&b=$(touch /tmp/not-a-command)'
        command,_=self.entry_request('chromium',url)
        self.assertEqual(command,['/usr/bin/chromium',url])

    def test_unknown_custom_browser_entry_uses_system_opener(self):
        command,_=self.entry_request('custom-browser --special')
        self.assertEqual(command,['xdg-open','http://127.0.0.1:17780/'])

    def test_default_lookup_failure_keeps_system_opener(self):
        with mock.patch('browser_open.subprocess.run',side_effect=OSError):
            self.assertEqual(browser_open.browser_request('http://localhost/',{})[0],['xdg-open','http://localhost/'])
