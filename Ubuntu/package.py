#!/usr/bin/env python3
"""Build an Ubuntu 26.04 desktop DEB using Ubuntu's system Python and GTK.

No PyInstaller is needed. The DEB contains source scripts and launches them
with /usr/bin/python3, keeping the native WebKitGTK login helper available.
"""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent
VERSION = '1.0.0'
PKG = 'wifi-assistant'
OUTFILE = ROOT / 'dist' / f'{PKG}_{VERSION}_amd64.deb'
PYTHON_FILES = ('main.py', 'credentials.py', 'instance.py', 'portal_window.py')
DESKTOP = """[Desktop Entry]
Type=Application
Name=WIFI助手
Comment=校园 Wi-Fi 网络与认证助手
Exec=/usr/bin/wifi-assistant
TryExec=/usr/bin/wifi-assistant
Icon=wifi-assistant
Terminal=false
Categories=Network;Utility;
StartupNotify=true
"""
ICON = """<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128"><rect width="128" height="128" rx="28" fill="#2563eb"/><path d="M25 53q39-35 78 0M41 70q23-21 46 0M55 88q9-9 18 0" fill="none" stroke="white" stroke-width="10" stroke-linecap="round"/><circle cx="64" cy="103" r="6" fill="white"/></svg>"""


def main() -> None:
    for name in PYTHON_FILES:
        if not (ROOT / name).is_file():
            raise SystemExit(f'缺少必需文件: Ubuntu/{name}')

    with tempfile.TemporaryDirectory(prefix='wifi-deb-') as temp:
        stage = Path(temp) / 'package'
        for folder in ('DEBIAN', 'usr/bin', 'usr/share/applications',
                       'usr/share/icons/hicolor/scalable/apps', 'usr/share/wifi-assistant'):
            (stage / folder).mkdir(parents=True)

        for name in PYTHON_FILES:
            shutil.copy2(ROOT / name, stage / 'usr/share/wifi-assistant' / name)

        entry = stage / 'usr/bin/wifi-assistant'
        entry.write_text('#!/bin/sh\nexec /usr/bin/python3 /usr/share/wifi-assistant/main.py "$@"\n', encoding='utf-8')
        os.chmod(entry, 0o755)
        (stage / 'usr/share/applications/wifi-assistant.desktop').write_text(DESKTOP, encoding='utf-8')
        (stage / 'usr/share/icons/hicolor/scalable/apps/wifi-assistant.svg').write_text(ICON, encoding='utf-8')
        (stage / 'DEBIAN/control').write_text(
            'Package: wifi-assistant\n'
            f'Version: {VERSION}\n'
            'Section: net\n'
            'Priority: optional\n'
            'Architecture: amd64\n'
            'Depends: python3, python3-tk, network-manager, python3-gi, gir1.2-gtk-3.0, gir1.2-webkit2-4.1, libsecret-tools\n'
            'Maintainer: WIFI助手项目\n'
            'Description: WIFI助手 Ubuntu 校园 Wi-Fi 网络与认证桌面应用\n',
            encoding='utf-8',
        )
        OUTFILE.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(('dpkg-deb', '--build', '--root-owner-group', str(stage), str(OUTFILE)), check=True)
    print(f'Built {OUTFILE}')


if __name__ == '__main__':
    main()
