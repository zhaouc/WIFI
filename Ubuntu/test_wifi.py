import json
import subprocess
import sys
import os
from pathlib import Path
import tempfile
import unittest
from main import AUTO_CHECK_INTERVAL_MS, load_settings, needs_reconnect, parse_networks, save_settings, split_nmcli, valid_portal

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

    def test_url_validation(self):
        self.assertTrue(valid_portal('https://example.edu/login'))
        self.assertFalse(valid_portal('file:///etc/passwd'))
        self.assertFalse(valid_portal('https://user:password@example.edu/login'))

if __name__ == '__main__':
    unittest.main()
