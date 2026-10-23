import json
import subprocess
import sys
import os
from pathlib import Path
import tempfile
import unittest
from main import AUTO_CHECK_INTERVAL_MS, load_settings, needs_reconnect, parse_networks, save_settings, split_nmcli, valid_portal, current_network

class UbuntuTests(unittest.TestCase):
    def test_colon_escaped_ssid(self):
        self.assertEqual(split_nmcli(r'*:Campus\:WiFi:90:WPA2'), ['*','Campus:WiFi','90','WPA2'])

    def test_network_sort_and_merge(self):
        items = parse_networks(' :WiFi-A:20:WPA2\n*:WiFi-A:70:WPA2\n :Guest:66:--\n')
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]['ssid'], 'WiFi-A')
        self.assertTrue(items[0]['connected'])

    def test_safe_config(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'settings.json'
            save_settings({'ssid':'Campus','portal_url':'https://portal.example.edu','username':'u','password':'must-not-be-written','auto_connect':False}, p)
            self.assertNotIn('password', p.read_text())
            self.assertEqual(load_settings(p)['ssid'], 'Campus')
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)

    def test_auto_check_every_five_minutes(self):
        self.assertEqual(AUTO_CHECK_INTERVAL_MS, 300000)

    def test_avoid_reconnecting_active_wifi(self):
        self.assertFalse(needs_reconnect('wifi', 'Campus', 'wifi:Campus\n'))
        self.assertFalse(needs_reconnect('wifi', 'Campus', '802-11-wireless:OtherWiFi\n'))
        self.assertTrue(needs_reconnect('wifi', 'Campus', 'ethernet:Wired\n'))
        self.assertFalse(needs_reconnect('wifi', '', ''))

    def test_avoid_reconnecting_active_ethernet(self):
        self.assertFalse(needs_reconnect('wired', '', '802-3-ethernet:LAN\n'))
        self.assertFalse(needs_reconnect('wired', '', 'ethernet:LAN\n'))
        self.assertTrue(needs_reconnect('wired', '', 'wifi:Campus\n'))

    def test_tkinter_style_correctly_uses_vertical_layout(self):
        from main import App
        from tkinter import ttk
        # `Vertical.TScrollbar` exists in Tk's standard theme, while the old
        # custom `Wifi.TScrollbar` resolved to a missing vertical layout.
        self.assertEqual(App._configure_style.__name__, "_configure_style")
        from pathlib import Path
        source = (Path(__file__).resolve().parent / "main.py").read_text(encoding="utf-8")
        self.assertIn('orient="vertical", command=settings_canvas.yview', source)
        self.assertNotIn('style="Wifi.TScrollbar"', source)
        self.assertNotIn('附近网络"', source)

    def test_connected_wifi_is_detected_without_scan_in_use_marker(self):
        """The radio is connected even when a nearby-AP scan has no '*' entry."""
        commands = []
        def nmcli(*args):
            commands.append(args)
            if args == ('-t', '-f', 'DEVICE,TYPE,STATE,CONNECTION', 'device', 'status'):
                return 'wlp0s20f3:wifi:connected:Campus profile\nlo:loopback:connected (externally):lo\n'
            if args == ('-t', '-f', 'UUID,TYPE,DEVICE', 'connection', 'show', '--active'):
                return 'edaa-123:wifi:wlp0s20f3\n'
            if args == ('-g', '802-11-wireless.ssid', 'connection', 'show', 'uuid', 'edaa-123'):
                return 'Campus\n'
            raise AssertionError(args)
        actual = current_network(nmcli)
        self.assertEqual(actual['ssid'], 'Campus')
        self.assertEqual(actual['mode'], 'wifi')
        self.assertFalse(any('list' in command for command in commands))

    def test_connection_name_is_not_assumed_to_equal_ssid(self):
        def nmcli(*args):
            if 'device' in args and 'status' in args:
                return 'wlp3s0:wifi:connected:Student\\: Wi-Fi profile\n'
            if args[:3] == ('-t', '-f', 'UUID,TYPE,DEVICE'):
                return 'school-uuid:802-11-wireless:wlp3s0\n'
            if args[:2] == ('-g', '802-11-wireless.ssid'):
                return 'ACTUAL_CAMPUS_SSID\n'
            raise AssertionError(args)
        self.assertEqual(current_network(nmcli)['ssid'], 'ACTUAL_CAMPUS_SSID')

    def test_failed_ssid_lookup_still_reports_connected(self):
        def nmcli(*args):
            if args[:3] == ('-t', '-f', 'DEVICE,TYPE,STATE,CONNECTION'):
                return 'wlan0:wifi:connected (externally):MyProfile\n'
            raise RuntimeError('profile is not accessible')
        connected = current_network(nmcli)
        self.assertEqual(connected['mode'], 'wifi')
        self.assertEqual(connected['profile'], 'MyProfile')
        self.assertEqual(connected['ssid'], '')

    def test_wifi_not_connected_and_wired_connected(self):
        def wired_nmcli(*args):
            return 'wlp2s0:wifi:disconnected:--\nenp0s25:ethernet:connected:Wired connection 1\n'
        self.assertEqual(current_network(wired_nmcli)['mode'], 'wired')
        def offline_nmcli(*args):
            return 'wlan0:wifi:disconnected:--\n'
        self.assertIsNone(current_network(offline_nmcli))

    def test_dashboard_uses_real_device_connection_state(self):
        from main import App
        class FakeWidget:
            def __init__(self): self.content = ''
            def configure(self, **args): self.content = args.get('text', self.content)
        class FakeVar:
            def __init__(self, value): self.value = value
            def get(self): return self.value
            def set(self, value): self.value = value
        class Fake:
            scan_busy = True
            connection_label = FakeWidget()
            target_label = FakeWidget()
            ssid = FakeVar('Campus')
            display_status = FakeVar('')
        fake = Fake()
        App._update_networks(fake, {'mode': 'wifi', 'ssid': 'Campus', 'profile': 'Other', 'device': 'wlan0'})
        self.assertIn('已连接', fake.connection_label.content)
        self.assertIn('Campus', fake.connection_label.content)
        self.assertFalse(fake.scan_busy)
        App._update_networks(fake, {'mode': 'wifi', 'ssid': '', 'profile': 'Other', 'device': 'wlan0'})
        self.assertIn('已连接', fake.connection_label.content)
        self.assertNotIn('未连接', fake.connection_label.content)

    def test_url_validation(self):
        self.assertTrue(valid_portal('https://example.edu/login'))
        self.assertFalse(valid_portal('file:///etc/passwd'))
        self.assertFalse(valid_portal('https://user:password@example.edu/login'))

if __name__ == '__main__':
    unittest.main()
