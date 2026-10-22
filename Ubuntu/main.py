#!/usr/bin/env python3
"""WIFI助手 1.0.0 — Ubuntu desktop frontend powered by NetworkManager."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import traceback
import tkinter as tk
from tkinter import ttk
from urllib.parse import urlparse
from credentials import connect_wifi, get_secret, put_secret, secret_service_available
from instance import SingleInstance

APP_NAME = "WIFI助手"
AUTO_CHECK_INTERVAL_MS = 5 * 60 * 1000
BLUE = "#2563eb"
DARK_BLUE = "#2563eb"
PALE = "#e5efff"
BACKGROUND = "#f5f8fd"
SIDEBAR = "#f6f9ff"
CARD = "#ffffff"
INPUT = "#ffffff"
TEXT = "#142d50"
MUTED = "#617894"
CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "wifi-assistant" / "settings.json"
AUTOSTART = Path.home() / ".config" / "autostart" / "wifi-assistant.desktop"


def run_nmcli(*args: str) -> str:
    """Never invoke a shell: SSIDs are passed as literal arguments."""
    if not shutil.which("nmcli"):
        raise RuntimeError("需要安装 NetworkManager（nmcli）")
    result = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=90 if "up" in args or "connect" in args else 20, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "NetworkManager 操作失败").strip())
    return result.stdout


def split_nmcli(line: str) -> list[str]:
    """nmcli -t uses backslash to escape colons/backslashes in SSIDs."""
    pieces, value, escaping = [], [], False
    for character in line:
        if escaping:
            value.append(character)
            escaping = False
        elif character == "\\":
            escaping = True
        elif character == ":":
            pieces.append("".join(value))
            value = []
        else:
            value.append(character)
    if escaping:
        value.append("\\")
    pieces.append("".join(value))
    return pieces


def parse_networks(text: str) -> list[dict]:
    seen = {}
    for line in text.splitlines():
        parts = split_nmcli(line)
        if len(parts) < 4:
            continue
        connected, ssid, signal, security = parts[:4]
        if not ssid.strip():
            continue
        try:
            strength = max(0, min(100, int(signal)))
        except ValueError:
            strength = 0
        network = {"ssid": ssid, "signal": strength, "secured": bool(security.strip() and security.strip() != "--"), "connected": connected.strip() == "*"}
        if ssid not in seen or strength > seen[ssid]["signal"]:
            seen[ssid] = network
    return sorted(seen.values(), key=lambda n: (not n["connected"], -n["signal"], n["ssid"].casefold()))


def probe_online() -> bool:
    """Only used when NetworkManager cannot determine captive portal state.

    Checks Ubuntu's standard connectivity URL, never sends credentials.
    """
    from urllib.request import Request, urlopen
    try:
        with urlopen(Request('http://connectivity-check.ubuntu.com/',
                             headers={'User-Agent': 'WIFI助手/1.0'}), timeout=5) as response:
            return 'NetworkManager is online' in response.read(256).decode('utf-8', 'replace')
    except (OSError, ValueError):
        return False


def needs_reconnect(mode: str, ssid: str, active_connections: str) -> bool:
    """Only reconnect if NetworkManager has no active connection of the chosen type.

    Do not displace a different active Wi-Fi network automatically; the Windows
    edition follows the same non-disruptive policy.
    """
    active_types = set()
    for line in active_connections.splitlines():
        fields = split_nmcli(line)
        if len(fields) >= 2:
            active_types.add(fields[0].strip().lower())
    if mode == "wifi":
        return bool(ssid) and not bool(active_types & {"wifi", "802-11-wireless"})
    return not bool(active_types & {"ethernet", "802-3-ethernet"})



def active_wifi_ssid(network_list: str) -> str:
    """Read the active SSID from escaped nmcli IN-USE:SSID records."""
    for line in network_list.splitlines():
        fields = split_nmcli(line)
        if len(fields) >= 2 and fields[0].strip() == '*':
            return fields[1]
    return ''


def portal_check_needed(state: str, *, online_probe=None) -> bool:
    """NetworkManager sometimes reports `none` for captive networks.

    Never auto-submit on a connection reported as fully online. On an
    indeterminate connection, use the no-credential connectivity probe.
    """
    state = state.strip().lower()
    if state in ('portal', 'limited'):
        return True
    if state in ('none', 'unknown'):
        return not online_probe() if online_probe else True
    if state == 'full':
        # A stale/misconfigured NetworkManager connectivity result can report
        # full even when the captive portal intercepts unrelated HTTP. Check
        # with a credential-free independent probe before skipping authentication.
        return not online_probe() if online_probe else False
    return False


def load_settings(path: Path = CONFIG_PATH) -> dict:
    defaults = {"ssid": "", "portal_url": "", "username": "", "auto_connect": False, "auto_auth": False, "startup": False, "connection": "wifi"}
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            return {**defaults, **{k: existing[k] for k in defaults if k in existing}}
    except (FileNotFoundError, ValueError, OSError):
        pass
    return defaults


def save_settings(data: dict, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Never persist Wi-Fi credentials or portal passwords in plaintext.
    allowed = ("ssid", "portal_url", "username", "auto_connect", "auto_auth", "startup", "connection")
    tmp = path.with_name(path.name + ".tmp")
    try:
        with os.fdopen(os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600), "w", encoding="utf-8") as f:
            json.dump({k: data[k] for k in allowed if k in data}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        tmp.unlink(missing_ok=True)


def write_autostart(enable: bool) -> None:
    if not enable:
        AUTOSTART.unlink(missing_ok=True)
        return
    import shlex
    command = (shlex.quote(sys.executable) if getattr(sys, "frozen", False) else
               "python3 " + shlex.quote(str(Path(__file__).resolve())))
    AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART.write_text("[Desktop Entry]\nType=Application\nName=WIFI助手\nExec=" + command +
                         "\nTerminal=false\nX-GNOME-Autostart-enabled=true\n", encoding="utf-8")


def valid_portal(url: str) -> bool:
    if not url:
        return True
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1080x770")
        self.minsize(790, 600)
        self.configure(bg=BACKGROUND)
        self.data = load_settings()
        self.page = "dashboard"
        self.save_job = None
        self.scan_busy = False
        self.auto_busy = False
        self.auto_job = None
        self.portal_process = None
        self._secret_changes = {}
        self._ignore_secret_trace = False
        self._secret_targets = {}
        self._secret_revisions = {'wifi': 0, 'portal': 0}
        self._secret_user_edited = set()
        self.instance = None
        self.display_status = tk.StringVar(value="就绪")
        self.ssid = tk.StringVar(value=self.data["ssid"])
        self.portal_url = tk.StringVar(value=self.data["portal_url"])
        self.username = tk.StringVar(value=self.data["username"])
        self.wifi_password = tk.StringVar(value="")
        self.portal_password = tk.StringVar(value="")
        self.auto = tk.BooleanVar(value=self.data["auto_connect"])
        self.auto_auth = tk.BooleanVar(value=self.data["auto_auth"])
        self.startup = tk.BooleanVar(value=self.data["startup"])
        self.mode = tk.StringVar(value=self.data["connection"])
        self._secret_targets = {'wifi': self._target_key('wifi'), 'portal': self._target_key('portal')}
        self._configure_style()
        self._draw_shell()
        for value in (self.ssid, self.portal_url, self.username, self.auto, self.auto_auth, self.startup, self.mode):
            value.trace_add("write", self._schedule_save)
        self.auto.trace_add("write", self._auto_changed)
        self.auto_auth.trace_add("write", self._auto_changed)
        self.wifi_password.trace_add("write", lambda *_: self._password_changed("wifi", self.wifi_password.get()))
        self.portal_password.trace_add("write", lambda *_: self._password_changed("portal", self.portal_password.get()))
        self.ssid.trace_add("write", lambda *_: self._credential_target_changed('wifi'))
        self.portal_url.trace_add("write", lambda *_: self._credential_target_changed('portal'))
        self.username.trace_add("write", lambda *_: self._credential_target_changed('portal'))
        self.show_page("dashboard")
        self.after(250, self.refresh_networks)
        if '--smoke-test' not in sys.argv:
            threading.Thread(target=self._load_secrets, daemon=True).start()
        # Initial check shortly after startup, then every five minutes.
        self._schedule_auto_check(15000)

    def _configure_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Vertical.TScrollbar", background="#aac8ef", troughcolor="#edf4fc", arrowsize=11)

    def _label(self, parent, text, size=12, color=TEXT, bold=False, **kwargs):
        return tk.Label(parent, text=text, font=("DejaVu Sans", size, "bold" if bold else "normal"), bg=parent.cget("bg"), fg=color, anchor="w", **kwargs)

    def _button(self, parent, text, command, primary=False):
        return tk.Button(parent, text=text, command=command, relief="flat", padx=17, pady=10,
                         bg=DARK_BLUE if primary else PALE, fg="white" if primary else "#2453a1",
                         activebackground="#1d4ed8" if primary else "#d4e7ff", activeforeground="white" if primary else "#2453a1",
                         font=("DejaVu Sans", 11, "bold"), cursor="hand2", highlightthickness=0, borderwidth=0)

    def _card(self, parent):
        card = tk.Frame(parent, bg=CARD, highlightbackground="#dce6f3", highlightthickness=1, padx=20, pady=18)
        card.pack(fill="x", pady=(0, 16))
        return card

    def _draw_shell(self):
        sidebar = tk.Frame(self, bg=SIDEBAR, width=228)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="◉", bg=SIDEBAR, fg=BLUE, font=("DejaVu Sans", 31, "bold")).pack(anchor="w", padx=24, pady=(35, 0))
        self._label(sidebar, "WIFI助手", 19, TEXT, True).pack(anchor="w", padx=24)
        self._label(sidebar, "WI-FI ASSISTANT", 8, MUTED).pack(anchor="w", padx=24, pady=(5, 35))
        self.nav_buttons = {}
        for key, title in (("dashboard", "▣  网络概览"), ("settings", "⚙  校园网设置")):
            b = self._button(sidebar, title, lambda k=key: self.show_page(k))
            b.pack(fill="x", padx=16, pady=5)
            self.nav_buttons[key] = b
        self.main = tk.Frame(self, bg=BACKGROUND)
        self.main.pack(side="right", fill="both", expand=True)
        top = tk.Frame(self.main, bg=CARD, height=101)
        top.pack(fill="x")
        top.pack_propagate(False)
        title_side = tk.Frame(top, bg=CARD)
        title_side.pack(side="left", padx=28, pady=20)
        self.title_label = self._label(title_side, "网络概览", 23, TEXT, True)
        self.title_label.pack(anchor="w")
        self.subtitle_label = self._label(title_side, "到达校园，连接自动完成。", 10, MUTED)
        self.subtitle_label.pack(anchor="w", pady=(6, 0))
        self._button(top, "↻ 刷新状态", self.refresh_networks).pack(side="right", padx=25)
        content = tk.Frame(self.main, bg=BACKGROUND)
        content.pack(fill="both", expand=True, padx=27, pady=25)
        self.pages = {}
        for key in ("dashboard", "settings"):
            page = tk.Frame(content, bg=BACKGROUND)
            self.pages[key] = page
        self._draw_dashboard(self.pages["dashboard"])
        # Settings contain password inputs and must remain accessible on
        # smaller laptop screens; scroll only inside this page, never switch
        # pages on scroll or hover.
        settings_canvas = tk.Canvas(self.pages["settings"], bg=BACKGROUND, highlightthickness=0, bd=0)
        settings_scroll = ttk.Scrollbar(self.pages["settings"], orient="vertical", command=settings_canvas.yview)
        settings_canvas.configure(yscrollcommand=settings_scroll.set)
        settings_scroll.pack(side="right", fill="y")
        settings_canvas.pack(side="left", fill="both", expand=True)
        settings_body = tk.Frame(settings_canvas, bg=BACKGROUND)
        settings_window = settings_canvas.create_window((0, 0), window=settings_body, anchor="nw")
        settings_body.bind('<Configure>', lambda _e: settings_canvas.configure(scrollregion=settings_canvas.bbox('all')))
        settings_canvas.bind('<Configure>', lambda e: settings_canvas.itemconfigure(settings_window, width=e.width))
        settings_canvas.bind('<Button-4>', lambda _e: settings_canvas.yview_scroll(-2, 'units'))
        settings_canvas.bind('<Button-5>', lambda _e: settings_canvas.yview_scroll(2, 'units'))
        settings_canvas.bind('<MouseWheel>', lambda e: settings_canvas.yview_scroll(-int(e.delta/120), 'units'))
        self._draw_settings(settings_body)
        bottom = tk.Frame(self.main, bg=CARD, height=45)
        bottom.pack(fill="x", side="bottom")
        self._label(bottom, "●", 11, BLUE).pack(side="left", padx=(26, 7))
        tk.Label(bottom, textvariable=self.display_status, bg=CARD, fg=MUTED, anchor="w").pack(side="left", fill="x", expand=True)

    def _draw_dashboard(self, parent):
        card = self._card(parent)
        self._label(card, "当前连接", 11, MUTED).pack(anchor="w")
        self.connection_label = self._label(card, "正在读取网络状态", 19, TEXT, True)
        self.connection_label.pack(anchor="w", pady=11)
        self.target_label = self._label(card, "目标校园网络：尚未设置", 10, MUTED)
        self.target_label.pack(anchor="w", pady=(0, 14))
        buttons = tk.Frame(card, bg=CARD)
        buttons.pack(anchor="w")
        self._button(buttons, "立即连接", self.connect_network, True).pack(side="left", padx=(0, 10))
        self._button(buttons, "修改配置", lambda: self.show_page("settings")).pack(side="left")
        card = self._card(parent)
        self._label(card, "自动连接与认证", 14, TEXT, True).pack(anchor="w")
        self._label(card, "每 5 分钟检查网络；断线重连，必要时尝试校园网自动认证。", 10, MUTED).pack(anchor="w", pady=(9, 12))
        tk.Checkbutton(card, text="启用自动连接（每 5 分钟）", variable=self.auto, bg=CARD, fg=TEXT,
                       activebackground=CARD, selectcolor=INPUT, cursor="hand2").pack(anchor="w")
        tk.Checkbutton(card, text="启用自动认证（需 HTTPS 登录地址和已保存凭据）", variable=self.auto_auth, bg=CARD, fg=TEXT,
                       activebackground=CARD, selectcolor=INPUT, cursor="hand2").pack(anchor="w", pady=(8,0))

    def _entry(self, parent, title, var, secret=False):
        wrapper = tk.Frame(parent, bg=CARD)
        wrapper.pack(fill="x", pady=(0, 15))
        self._label(wrapper, title, 10, MUTED).pack(anchor="w", pady=(0, 6))
        entry = tk.Entry(wrapper, textvariable=var, show="•" if secret else "", font=("DejaVu Sans", 12),
                         bg=INPUT, fg=TEXT, insertbackground=BLUE, relief="flat", highlightbackground="#cdddec", highlightcolor=BLUE, highlightthickness=1)
        entry.pack(fill="x", ipady=9)
        return entry

    def _draw_settings(self, parent):
        card = self._card(parent)
        self._label(card, "偏好设置", 15, TEXT, True).pack(anchor="w", pady=(0, 13))
        tk.Checkbutton(card, text="登录 Ubuntu 后自动启动", variable=self.startup, bg=CARD, fg=TEXT,
                       activebackground=CARD, selectcolor=INPUT, cursor="hand2").pack(anchor="w")
        self._label(card, "修改后自动保存，无需点击保存按钮。", 10, MUTED).pack(anchor="w", pady=(12, 0))
        card = self._card(parent)
        self._label(card, "校园网络", 15, TEXT, True).pack(anchor="w", pady=(0, 14))
        modes = tk.Frame(card, bg=CARD)
        modes.pack(anchor="w", pady=(0, 15))
        for text, value in (("Wi-Fi 无线网络", "wifi"), ("有线网络", "wired")):
            tk.Radiobutton(modes, text=text, value=value, variable=self.mode, bg=CARD, fg=TEXT,
                           activebackground=CARD, selectcolor=INPUT, cursor="hand2").pack(side="left", padx=(0, 20))
        self.wifi_entry = self._entry(card, "Wi-Fi 名称", self.ssid)
        self._entry(card, "Wi-Fi 密码（留空表示无密码）", self.wifi_password, secret=True)
        self._entry(card, "学校认证网址（建议 HTTPS）", self.portal_url)
        self._entry(card, "学号 / 上网账号", self.username)
        self._entry(card, "校园网认证密码", self.portal_password, secret=True)
        self._label(card, "密码仅存于系统密钥环（Secret Service），不写入设置 JSON；自动登录仅在 HTTPS 上启用。", 9, MUTED).pack(anchor="w")
        self._button(card, "打开内置认证页面", self.open_portal, True).pack(anchor="w", pady=(12, 0))
        self.save_indicator = self._label(card, "✓ 设置已自动保存", 10, BLUE)
        self.save_indicator.pack(anchor="w", pady=(16, 0))

    def show_page(self, page):
        self.page = page
        title, subtitle = {
            "dashboard": ("网络概览", "到达校园，连接自动完成。"),
            "settings": ("校园网设置", "网络与开机自启设置将自动保存。"),
        }[page]
        for frame in self.pages.values():
            frame.pack_forget()
        self.pages[page].pack(fill="both", expand=True)
        self.title_label.configure(text=title)
        self.subtitle_label.configure(text=subtitle)
        for key, button in self.nav_buttons.items():
            button.configure(bg=PALE if key == page else SIDEBAR, fg="#1559c9" if key == page else MUTED)
        # Deliberately no gesture, mouse-hover, animation or slide navigation.

    def _schedule_save(self, *_):
        if self.save_job:
            self.after_cancel(self.save_job)
        self.save_indicator.configure(text="修改中 · 待自动保存")
        self.save_job = self.after(750, self._save)

    def _save(self):
        self.save_job = None
        url = self.portal_url.get().strip()
        if not valid_portal(url):
            self.save_indicator.configure(text="请输入有效的 http:// 或 https:// 网址")
            return
        self.data = {"ssid": self.ssid.get().strip(), "portal_url": url, "username": self.username.get().strip(),
                     "auto_connect": self.auto.get(), "auto_auth": self.auto_auth.get(),
                     "startup": self.startup.get(), "connection": self.mode.get()}
        try:
            save_settings(self.data)
            write_autostart(self.data["startup"])
            self.save_indicator.configure(text="✓ 已自动保存")
            self.target_label.configure(text="目标校园网络：" + (self.data["ssid"] or "尚未设置"))
        except OSError as e:
            self.save_indicator.configure(text="保存失败：" + str(e))

    def refresh_networks(self):
        if self.scan_busy:
            return
        self.scan_busy = True
        self.display_status.set("正在更新 Wi-Fi 状态…")
        def run():
            try:
                networks = parse_networks(run_nmcli("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "auto"))
                self.after(0, lambda: self._update_networks(networks))
            except Exception as e:
                error = str(e)
                self.after(0, lambda: self._scan_failed(error))
        threading.Thread(target=run, daemon=True).start()

    def _scan_failed(self, error):
        self.scan_busy = False
        self.display_status.set("Wi-Fi 扫描失败：" + error)

    def _update_networks(self, networks):
        self.scan_busy = False
        connected = next((n for n in networks if n["connected"]), None)
        self.connection_label.configure(text=("已连接：" + connected["ssid"]) if connected else "当前未连接 Wi-Fi")
        self.target_label.configure(text="目标校园网络：" + (self.ssid.get() or "尚未设置"))
        self.display_status.set("网络状态已更新")

    def connect_network(self, silent=False):
        if self.auto_busy:
            return
        if self.save_job:
            self.after_cancel(self.save_job)
            self.save_job = None
            self._save()
        mode, ssid = self.mode.get(), self.ssid.get().strip()
        wifi_password = self.wifi_password.get()
        if mode == "wifi" and not ssid:
            if not silent:
                self.show_page("settings")
                self.display_status.set("请先填写目标 Wi-Fi 名称")
            return
        self.auto_busy = True
        self.display_status.set("正在连接网络…")
        def run():
            try:
                if mode == "wifi":
                    connect_wifi(ssid, wifi_password, run_nmcli)
                else:
                    output = run_nmcli("-t", "-f", "TYPE,NAME", "connection", "show")
                    name = next((split_nmcli(line)[1] for line in output.splitlines()
                                 if len(split_nmcli(line)) >= 2 and split_nmcli(line)[0] == "802-3-ethernet"), None)
                    if not name:
                        raise RuntimeError("没有找到已配置的以太网连接")
                    run_nmcli("connection", "up", "id", name)
                self.after(0, lambda: self._connected(silent))
            except Exception as e:
                error = str(e)
                self.after(0, lambda: self._connect_failed(error))
        threading.Thread(target=run, daemon=True).start()

    def _connected(self, silent):
        self.auto_busy = False
        self.display_status.set("网络连接成功 · 可前往上网认证")
        self.refresh_networks()
        if self.auto.get() and self.auto_auth.get() and self.portal_url.get().strip():
            # Recheck whether captive login is needed; a successful Wi-Fi
            # connection alone does NOT mean portal credentials should be sent.
            self._schedule_auto_check(2000)
        elif not silent and self.portal_url.get().strip():
            self.open_portal()

    def _connect_failed(self, error):
        self.auto_busy = False
        self.display_status.set("连接失败：" + error)

    def _target_key(self, kind):
        if kind == 'wifi':
            return 'wifi:' + self.ssid.get().strip()
        # Account-specific, so changing student ID never reuses credentials.
        return 'portal:' + self.portal_url.get().strip() + '|' + self.username.get().strip()

    def _credential_target_changed(self, kind):
        target = self._target_key(kind)
        if target == self._secret_targets[kind]:
            return
        self._secret_targets[kind] = target
        self._secret_changes.pop(kind, None)
        self._secret_user_edited.discard(kind)
        self._ignore_secret_trace = True
        try:
            (self.wifi_password if kind == 'wifi' else self.portal_password).set('')
        finally:
            self._ignore_secret_trace = False
        threading.Thread(target=lambda: self._read_target_secret(kind, target), daemon=True).start()

    def _read_target_secret(self, kind, target):
        revision = self._secret_revisions[kind]
        value = get_secret(target) if not target.endswith(':') and not target.endswith('|') else ''
        def apply():
            if (self._secret_targets[kind] != target or self._secret_revisions[kind] != revision
                    or kind in self._secret_user_edited):
                return
            field = self.wifi_password if kind == 'wifi' else self.portal_password
            if value and not field.get():
                self._ignore_secret_trace = True
                try:
                    field.set(value)
                finally:
                    self._ignore_secret_trace = False
        self.after(0, apply)

    def _password_changed(self, kind, password):
        if self._ignore_secret_trace:
            return
        self._secret_revisions[kind] += 1
        self._secret_user_edited.add(kind)
        self._secret_changes[kind] = (self._target_key(kind), password)
        if getattr(self, '_secret_job', None):
            self.after_cancel(self._secret_job)
        self._secret_job = self.after(800, self._persist_secrets)

    def _load_secrets(self):
        self._read_target_secret('wifi', self._secret_targets['wifi'])
        self._read_target_secret('portal', self._secret_targets['portal'])

    def _persist_secrets(self):
        self._secret_job = None
        changes = dict(self._secret_changes)
        self._secret_changes.clear()
        def store():
            failed = []
            for kind, (target, password) in changes.items():
                if target.endswith(':') or target.endswith('|') or not put_secret(target, password):
                    failed.append(kind)
            if failed:
                self.after(0, lambda: self.save_indicator.configure(
                    text='密码无法保存到密钥环；请检查登录密钥环和 libsecret-tools'))
        if changes:
            threading.Thread(target=store, daemon=True).start()

    def _portal_helper(self):
        bundled = Path('/usr/share/wifi-assistant/portal_window.py')
        return bundled if bundled.is_file() else Path(__file__).resolve().parent / 'portal_window.py'

    def open_portal(self, manual=True):
        url = self.portal_url.get().strip()
        if not url or not valid_portal(url):
            self.show_page('settings')
            self.display_status.set('请先填写有效的学校认证地址')
            return
        if self.portal_process is not None and self.portal_process.poll() is None:
            # Ask the existing WebKit window to restore instead of opening duplicates.
            from instance import signal_existing
            if manual:
                signal_existing('wifi-assistant-portal')
            return
        helper = self._portal_helper()
        if not helper.exists():
            self.display_status.set('缺少内置认证组件，请重新安装完整 DEB')
            return
        config = {'url': url, 'username': self.username.get().strip(),
                  'password': self.portal_password.get(), 'automatic': not manual and self.auto_auth.get()}
        try:
            cache = Path.home() / '.cache' / 'wifi-assistant'
            cache.mkdir(mode=0o700, parents=True, exist_ok=True)
            with (cache / 'portal.log').open('a', encoding='utf-8') as log:
                self.portal_process = subprocess.Popen(['/usr/bin/python3', str(helper)], stdin=subprocess.PIPE,
                                                       stdout=subprocess.DEVNULL, stderr=log)
                assert self.portal_process.stdin is not None
                self.portal_process.stdin.write(json.dumps(config, ensure_ascii=False).encode('utf-8') + b'\n')
                self.portal_process.stdin.flush()
                self.portal_process.stdin.close()
                self.after(1800, lambda p=self.portal_process: self._check_portal_start(p))
        except (OSError, ValueError) as exc:
            self.display_status.set('无法启动内置认证窗口：' + str(exc))
            return
        self.display_status.set('正在使用内置页面认证…；如窗口未出现请检查 ~/.cache/wifi-assistant/portal.log')

    def _check_portal_start(self, process):
        if process is self.portal_process and process.poll() not in (None, 0):
            self.display_status.set(
                '内置认证窗口启动失败，请运行 wifi-assistant --diagnose 或查看 portal.log')

    def _maybe_authenticate(self, manual=False):
        if manual:
            self.open_portal(manual=True)
            return
        if not self.auto_auth.get() or not self.auto.get():
            return
        url = self.portal_url.get().strip()
        if not url.lower().startswith('https://') or not self.username.get().strip():
            self.display_status.set('自动认证仅支持 HTTPS：请填写学校最终登录页地址与账号')
            return
        if not self.portal_password.get():
            self.display_status.set('自动认证需要在校园网设置中输入认证密码')
            return
        self.open_portal(manual=False)

    def _schedule_auto_check(self, delay_ms=AUTO_CHECK_INTERVAL_MS):
        # Keep exactly one pending background check at any time.
        if self.auto_job is not None:
            self.after_cancel(self.auto_job)
        self.auto_job = self.after(delay_ms, self.auto_tick)

    def _auto_changed(self, *_):
        # The user can enable automation without waiting for the next 5-minute slot.
        self._schedule_auto_check(1000 if self.auto.get() else AUTO_CHECK_INTERVAL_MS)

    def auto_tick(self):
        self.auto_job = None
        if self.auto.get() and (self.auto_busy or self.scan_busy):
            # Retry a deferred check promptly rather than silently skipping it
            # for a full five minutes while a manual operation is in progress.
            self._schedule_auto_check(5000)
            return
        if self.auto.get():
            self.auto_busy = True
            mode, ssid = self.mode.get(), self.ssid.get().strip()
            should_auth = self.auto_auth.get() and bool(self.portal_url.get().strip())
            def check():
                try:
                    if mode == "wifi" and not ssid:
                        self.after(0, self._finish_auto_check)
                        return
                    output = run_nmcli("-t", "-f", "TYPE,NAME", "connection", "show", "--active")
                    if not needs_reconnect(mode, ssid, output):
                        if should_auth:
                            # Do not send campus credentials when the computer is
                            # connected to a different Wi-Fi network.
                            if mode == 'wifi':
                                current_wifi = active_wifi_ssid(run_nmcli('-t', '-f', 'IN-USE,SSID',
                                    'device', 'wifi', 'list', '--rescan', 'no'))
                                if current_wifi != ssid:
                                    self.after(0, self._finish_auto_check)
                                    return
                            connectivity = run_nmcli('networking', 'connectivity', 'check')
                            if portal_check_needed(connectivity, online_probe=probe_online):
                                self.after(0, self._finish_auto_check_and_auth)
                                return
                        self.after(0, self._finish_auto_check)
                        return
                    self.after(0, self._finish_auto_check_and_reconnect)
                    return
                except Exception as e:
                    error = str(e)
                    self.after(0, lambda: self.display_status.set("自动检查失败：" + error))
                self.after(0, self._finish_auto_check)
            threading.Thread(target=check, daemon=True).start()
        self._schedule_auto_check(AUTO_CHECK_INTERVAL_MS)

    def _finish_auto_check_and_auth(self):
        self.auto_busy = False
        self._maybe_authenticate(manual=False)

    def _finish_auto_check(self):
        self.auto_busy = False

    def _finish_auto_check_and_reconnect(self):
        self.auto_busy = False
        # Re-check the option before attempting a connection. connect_network()
        # persists pending configuration and runs the attempt in a worker.
        if self.auto.get():
            self.connect_network(silent=True)

    def restore_window(self):
        self.deiconify()
        self.state('normal')
        self.lift()
        self.after_idle(self.focus_force)

    def poll_instance(self):
        if self.instance:
            self.instance.poll()
        self.after(200, self.poll_instance)

    def close_window(self):
        if self.instance:
            self.instance.close()
        self.destroy()


def print_diagnostics() -> None:
    """Simple support report. Never include passwords, usernames or SSIDs."""
    print('WIFI助手 Ubuntu 诊断（不包含账号密码）')
    print('DISPLAY:', 'available' if os.environ.get('DISPLAY') else 'missing')
    print('WAYLAND_DISPLAY:', 'available' if os.environ.get('WAYLAND_DISPLAY') else 'missing')
    print('DBUS_SESSION_BUS_ADDRESS:', 'available' if os.environ.get('DBUS_SESSION_BUS_ADDRESS') else 'missing')
    print('nmcli:', shutil.which('nmcli') or 'missing')
    print('secret-tool:', shutil.which('secret-tool') or 'missing')
    checks = [
        (['nmcli', '-g', 'STATE', 'general'], 'NetworkManager state'),
        (['nmcli', 'networking', 'connectivity', 'check'], 'NetworkManager connectivity'),
        (['/usr/bin/python3', '-c',
          "import gi;gi.require_version('Gtk','3.0');gi.require_version('WebKit2','4.1');from gi.repository import Gtk,WebKit2;print('available')"],
         'WebKitGTK Python bindings'),
    ]
    for command, title in checks:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=8, check=False)
            detail = (result.stdout or result.stderr or '').strip().splitlines()
            print(title + ':', ('; '.join(detail[:2]))[:200] if detail else 'exit ' + str(result.returncode))
        except (OSError, subprocess.TimeoutExpired) as e:
            print(title + ':', type(e).__name__)
    for name in ('startup.log', 'portal.log'):
        path = Path.home() / '.cache' / 'wifi-assistant' / name
        print(name + ':', 'exists' if path.exists() else 'not found')


def launch() -> None:
    if '--diagnose' in sys.argv[1:]:
        print_diagnostics()
        return
    if "--smoke-test" in sys.argv[1:]:
        # Both source and PyInstaller desktop builds must be able to create
        # their real widgets; Python syntax checks alone cannot catch bad ttk
        # style names or missing Tcl/Tk resources.
        app = App()
        try:
            app.update_idletasks()
            if not app.winfo_exists() or app.title() != APP_NAME:
                raise RuntimeError("图形界面初始化失败")
        finally:
            app.destroy()
        return
    lock = SingleInstance('wifi-assistant-main', lambda: None)
    if not lock.primary:
        return
    try:
        app = App()
        app.instance = lock
        lock.on_wake = app.restore_window
        app.protocol('WM_DELETE_WINDOW', app.close_window)
        app.after(200, app.poll_instance)
        app.mainloop()
    finally:
        lock.close()


if __name__ == "__main__":
    try:
        launch()
    except Exception:
        # Desktop launchers usually hide stderr. Keep an actionable log
        # without using desktop notifications or installing a daemon.
        try:
            cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
            error_log = cache_home / "wifi-assistant" / "startup.log"
            error_log.parent.mkdir(parents=True, exist_ok=True)
            with error_log.open("a", encoding="utf-8") as log:
                traceback.print_exc(file=log)
        except OSError:
            pass
        raise
