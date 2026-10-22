#!/usr/bin/env python3
"""Package the PyInstaller application as a native Ubuntu 26.04 AMD64 DEB."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent
EXE = ROOT / "dist" / "WiFiAssistant"
VERSION = "1.0.0"
DESKTOP = """[Desktop Entry]
Type=Application
Name=WIFI助手
Comment=校园 Wi-Fi 网络与认证助手
Exec=/usr/bin/wifi-assistant
Icon=wifi-assistant
Terminal=false
Categories=Network;Utility;
StartupNotify=true
"""
SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128"><rect width="128" height="128" rx="28" fill="#2563eb"/><path d="M25 53q39-35 78 0M41 70q23-21 46 0M55 88q9-9 18 0" fill="none" stroke="white" stroke-width="10" stroke-linecap="round"/><circle cx="64" cy="103" r="6" fill="white"/></svg>"""


def main():
    if not EXE.is_file():
        raise SystemExit("Missing Ubuntu/dist/WiFiAssistant; build it with PyInstaller first.")
    output = ROOT / "dist" / f"wifi-assistant_{VERSION}_amd64.deb"
    with tempfile.TemporaryDirectory(prefix="wifi-deb-") as folder:
        stage = Path(folder) / "package"
        for directory in ("DEBIAN", "usr/bin", "usr/share/applications", "usr/share/icons/hicolor/scalable/apps", "usr/share/wifi-assistant"):
            (stage / directory).mkdir(parents=True)
        dest = stage / "usr/bin/wifi-assistant"
        shutil.copy2(EXE, dest)
        os.chmod(dest, 0o755)
        # WebKitGTK is supplied by Ubuntu. Ship the portal helper as source,
        # executed by the system Python with Ubuntu's native GI bindings.
        for helper in ('portal_window.py', 'instance.py'):
            shutil.copy2(ROOT / helper, stage / 'usr/share/wifi-assistant' / helper)
        (stage / "usr/share/applications/wifi-assistant.desktop").write_text(DESKTOP, encoding="utf-8")
        (stage / "usr/share/icons/hicolor/scalable/apps/wifi-assistant.svg").write_text(SVG, encoding="utf-8")
        (stage / "DEBIAN/control").write_text(
            "Package: wifi-assistant\n"
            "Version: 1.0.0\n"
            "Section: net\n"
            "Priority: optional\n"
            "Architecture: amd64\n"
            "Depends: network-manager, python3, python3-gi, gir1.2-gtk-3.0, gir1.2-webkit2-4.1, libsecret-tools, libx11-6, libxft2, libfontconfig1, libxrender1\n"
            "Maintainer: WIFI助手 project <noreply@example.invalid>\n"
            "Description: WIFI助手 Ubuntu 网络与认证桌面应用\n",
            encoding="utf-8",
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["dpkg-deb", "--build", "--root-owner-group", str(stage), str(output)], check=True)
    print(f"Built {output}")


if __name__ == "__main__":
    main()
