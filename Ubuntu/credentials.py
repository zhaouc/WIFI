"""Store campus login secrets in Secret Service, never in JSON or command lines."""
from __future__ import annotations

import os
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile


def secret_service_available() -> bool:
    return shutil.which('secret-tool') is not None


def get_secret(kind: str) -> str:
    if not secret_service_available():
        return ''
    try:
        p = subprocess.run(['secret-tool', 'lookup', 'application', 'wifi-assistant', 'kind', kind],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           text=True, timeout=8, check=False)
        return p.stdout.rstrip('\n') if p.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''


def put_secret(kind: str, password: str) -> bool:
    """Send the password on stdin; never pass it in argv or save it in config."""
    if not secret_service_available():
        return False
    try:
        if password:
            cmd = ['secret-tool', 'store', '--label=WIFI助手 校园网凭据',
                   'application', 'wifi-assistant', 'kind', kind]
            p = subprocess.run(cmd, input=password, text=True, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=12, check=False)
        else:
            p = subprocess.run(['secret-tool', 'clear', 'application', 'wifi-assistant', 'kind', kind],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8, check=False)
        return p.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def split_nmcli_record(line: str) -> list[str]:
    """Decode NetworkManager's escaped colons and backslashes in terse output."""
    result, buf, escaped = [], [], False
    for character in line:
        if escaped:
            buf.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == ":":
            result.append("".join(buf))
            buf = []
        else:
            buf.append(character)
    if escaped:
        buf.append("\\")
    result.append("".join(buf))
    return result


def find_wifi_profile(ssid: str, runner) -> tuple[str, str] | None:
    """Return (id-or-uuid, value) for an actual SSID, not a profile display name.

    NetworkManager's profile name is arbitrary and often differs from SSID.
    Prefer UUID to avoid ambiguity if two saved profiles have the same name.
    """
    entries = runner('-t', '-f', 'NAME,UUID,TYPE', 'connection', 'show')
    candidates = []
    for line in entries.splitlines():
        parts = split_nmcli_record(line)
        if len(parts) < 3 or parts[2] not in ('wifi', '802-11-wireless'):
            continue
        candidates.append((parts[0], parts[1]))
    # Reuse app-owned connection profiles first; leave unrelated system profiles alone.
    candidates.sort(key=lambda item: not item[0].startswith('WIFI助手-'))
    # Examine the actual configured SSID, even when the profile name differs.
    for name, uuid in candidates:
        if uuid:
            configured_ssid = runner('-g', '802-11-wireless.ssid', 'connection', 'show', 'uuid', uuid).strip()
            if configured_ssid == ssid:
                return 'uuid', uuid
        elif name == ssid:
            return 'id', name
    return None


def connect_wifi(ssid: str, password: str, runner) -> None:
    """Activate the right saved Wi-Fi profile without putting passwords in argv."""
    if not ssid:
        raise ValueError('尚未设置 Wi-Fi 名称')
    if '\n' in password or '\r' in password:
        raise ValueError('Wi-Fi 密码不能包含换行符')
    existing = find_wifi_profile(ssid, runner)
    if not password:
        # A cleared password must never activate an old protected profile.
        # NetworkManager otherwise transparently reuses its stored WPA secret.
        if existing:
            key_mgmt = runner('-g', '802-11-wireless-security.key-mgmt',
                              'connection', 'show', *existing).strip().lower()
            if key_mgmt not in ('', '--', 'none'):
                raise ValueError('该 Wi-Fi 已保存加密配置；密码留空表示无密码，请填写正确密码')
        # Also check a visible protected SSID when there is no saved profile.
        networks = runner('-t', '-f', 'SSID,SECURITY', 'device', 'wifi', 'list', '--rescan', 'no')
        for line in networks.splitlines():
            # nmcli uses escaping for ':' in SSIDs; match escaped form as output.
            fields = line.rsplit(':', 1)
            if len(fields) >= 2 and fields[0].replace('\\:', ':').replace('\\\\', '\\') == ssid and fields[1].strip().lower() not in ('', '--', 'none'):
                raise ValueError('该 Wi-Fi 需要密码；留空只适用于无密码网络')
        if existing:
            runner('connection', 'up', *existing)
        else:
            runner('device', 'wifi', 'connect', ssid)
        return
    # Use a dedicated application-owned profile whenever a password was
    # explicitly entered. Activating an unrelated system profile might reuse
    # an old saved secret and ignore the supplied passwd-file. Never mutate it.
    managed_name = 'WIFI助手-' + hashlib.sha256(ssid.encode('utf-8')).hexdigest()[:16]
    if existing is not None:
        existing_name = (runner('-g', 'connection.id', 'connection', 'show', *existing).strip()
                         if existing[0] == 'uuid' else existing[1])
        if existing_name != managed_name:
            existing = None
        else:
            security = runner('-g', '802-11-wireless-security.key-mgmt', 'connection', 'show', *existing).strip().lower()
            if security in ('', '--', 'none'):
                existing = None
    # Distinguish WPA3-only from WPA2/WPA3 transition networks. Enterprise
    # authentication needs an 802.1X identity/certificate, not a personal PSK.
    available = runner('-t', '-f', 'SSID,SECURITY', 'device', 'wifi', 'list', '--rescan', 'no')
    security = ''
    for line in available.splitlines():
        fields = split_nmcli_record(line)
        if len(fields) >= 2 and fields[0] == ssid:
            security = fields[1].upper()
            break
    if '802.1X' in security or 'EAP' in security:
        raise ValueError('该 Wi-Fi 使用企业级身份认证，请在 Ubuntu 系统网络设置中配置 802.1X')
    key_mgmt = 'sae' if 'WPA3' in security and 'WPA2' not in security else 'wpa-psk'
    if existing is None:
        runner('connection', 'add', 'type', 'wifi', 'ifname', '*', 'con-name', managed_name, 'ssid', ssid)
        existing = ('id', managed_name)
    # Only update our own profile. Do not store the Wi-Fi PSK in NetworkManager;
    # the keyring supplies it via the private passwd-file on every connection.
    runner('connection', 'modify', *existing, 'wifi-sec.key-mgmt', key_mgmt,
           'wifi-sec.psk-flags', '2')
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR') or '/nonexistent')
    private_dir = runtime if runtime.is_dir() and runtime.stat().st_uid == os.getuid() else None
    fd, path = tempfile.mkstemp(prefix='wifi-nm-', dir=str(private_dir) if private_dir else None)
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write('802-11-wireless-security.psk:' + password + '\n')
        runner('connection', 'up', *existing, 'passwd-file', path)
    finally:
        Path(path).unlink(missing_ok=True)
