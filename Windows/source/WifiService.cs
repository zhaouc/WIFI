using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security;
using System.Text;

namespace CampusConnect;

// Only fields required for choosing the correct wireless connection.
// Auth is DOT11_AUTH_ALGORITHM, needed to distinguish personal and enterprise Wi-Fi.
internal sealed record WifiNetwork(Guid InterfaceId, string Ssid, uint Signal, bool Secure, bool Connectable, uint Auth);
internal sealed record WifiSnapshot(string? Current, IReadOnlyList<WifiNetwork> Networks);

// Native WLAN avoids parsing localized netsh output and keeps passwords out of command lines.
internal sealed class WifiService : IDisposable
{
    private IntPtr handle;
    public WifiService() => Check(WlanOpenHandle(2, IntPtr.Zero, out _, out handle));
    public void Dispose() { if (handle != IntPtr.Zero) { WlanCloseHandle(handle, IntPtr.Zero); handle = IntPtr.Zero; } }

    // Opening a WLAN handle and even querying the current connection can wait
    // on a driver/service. Keep the entire handle lifetime off the UI thread.
    public static Task<string?> ReadCurrentAsync(string? preferred = null) => Task.Run(() => {
        using var wifi = new WifiService();
        return wifi.CurrentSsid(preferred);
    });
    public static Task<Guid?> ReadInterfaceAsync(string ssid) => Task.Run(() => {
        using var wifi = new WifiService();
        var connection = wifi.CurrentConnection(ssid);
        return connection.Ssid == ssid ? connection.InterfaceId : null;
    });
    public static Task<WifiSnapshot> ReadSnapshotAsync(string? preferred = null, bool scan = true, bool waitForScan = true) => Task.Run(async () => {
        using var wifi = new WifiService();
        return await wifi.ScanAsync(preferred, scan, waitForScan).ConfigureAwait(false);
    });
    public static Task JoinAsync(CampusProfile profile, string wifiPassword, WifiSnapshot snapshot, bool allowSwitch = false) => Task.Run(async () => {
        using var wifi = new WifiService();
        await wifi.ConnectAsync(profile, wifiPassword, snapshot, allowSwitch).ConfigureAwait(false);
    });

    private List<InterfaceInfo> Interfaces()
    {
        Check(WlanEnumInterfaces(handle, IntPtr.Zero, out var pointer));
        try { return ReadList<InterfaceInfo>(pointer); }
        finally { WlanFreeMemory(pointer); }
    }
    private static List<T> ReadList<T>(IntPtr pointer) where T : struct
    {
        var count = Marshal.ReadInt32(pointer);
        if (count < 0 || count > 10000) throw new InvalidDataException("无线接口返回了无效数据。");
        var result = new List<T>();
        for (var i = 0; i < count; i++) result.Add(Marshal.PtrToStructure<T>(IntPtr.Add(pointer, 8 + i * Marshal.SizeOf<T>())));
        return result;
    }
    public string? CurrentSsid(string? preferred = null) => CurrentConnection(preferred).Ssid;
    private (string? Ssid, Guid? InterfaceId) CurrentConnection(string? preferred)
    {
        (string? Ssid, Guid? InterfaceId) first = (null, null);
        foreach (var item in Interfaces()) {
            var id = item.Id;
            var code = WlanQueryInterface(handle, ref id, 7, IntPtr.Zero, out _, out var data, out _);
            if (code == 5023) continue; // ERROR_INVALID_STATE: interface is disconnected.
            Check(code);
            try {
                var connection = Marshal.PtrToStructure<ConnectionAttributes>(data);
                if (connection.State != 1) continue;
                var ssid = connection.Association.Ssid.Text;
                if (ssid == preferred) return (ssid, id);
                if (first.Ssid is null) first = (ssid, id);
            } finally { WlanFreeMemory(data); }
        }
        return first;
    }
    public async Task<WifiSnapshot> ScanAsync(string? preferred = null, bool scan = true, bool waitForScan = true)
    {
        var interfaces = Interfaces();
        if (interfaces.Count == 0) throw new InvalidOperationException("没有找到 Wi-Fi 网卡，请检查无线网卡和 WLAN AutoConfig 服务。");
        if (scan && waitForScan) {
            RequestScan(interfaces);
            await Task.Delay(1800).ConfigureAwait(false);
        }
        var networks = new List<WifiNetwork>();
        foreach (var item in interfaces) {
            var id = item.Id;
            var code = WlanGetAvailableNetworkList(handle, ref id, 0, IntPtr.Zero, out var pointer);
            if (code == 5023 || code == 2150899714) continue; // Radio powered off.
            Check(code);
            try {
                foreach (var n in ReadList<AvailableNetwork>(pointer))
                    if (n.Ssid.Length > 0) networks.Add(new(id, n.Ssid.Text, n.Signal, n.SecurityEnabled != 0, n.Connectable != 0, n.Auth));
            } finally { WlanFreeMemory(pointer); }
        }
        var snapshot = new WifiSnapshot(CurrentSsid(preferred), networks.OrderByDescending(n => n.Signal).ToList());
        // WlanScan may flush its cache: read the previous result BEFORE requesting
        // a background scan. The next scheduled check reads the refreshed result.
        if (scan && !waitForScan) RequestScan(interfaces);
        return snapshot;
    }
    private void RequestScan(IEnumerable<InterfaceInfo> interfaces)
    {
        foreach (var item in interfaces) {
            var id = item.Id;
            var code = WlanScan(handle, ref id, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero);
            if (code == 5) Check(code);
        }
    }
    public async Task ConnectAsync(CampusProfile profile, string wifiPassword, WifiSnapshot snapshot, bool allowSwitch = false)
    {
        var current = CurrentSsid(profile.Ssid);
        if (current == profile.Ssid) return;
        if (!allowSwitch && !string.IsNullOrEmpty(current)) throw new InvalidOperationException("当前连接其他网络，自动连接已暂停。");
        var matches = snapshot.Networks.Where(n => n.Ssid == profile.Ssid).ToList();
        // The password field is the source of truth: blank means a genuinely open network.
        // Never silently reuse an encrypted Windows profile after the user clears the password.
        var secure = !string.IsNullOrEmpty(wifiPassword);
        // Native WLAN authentication values: 7 = WPA2-Personal, 9 = WPA3-SAE.
        // 6/8/11 are enterprise authentication, and 10 is OWE (not a PSK network).
        // Reject unsupported networks instead of creating an incorrect WPA2 profile.
        var network = matches.FirstOrDefault(n => n.Secure == secure && n.Connectable &&
            (!secure || n.Auth is 7 or 9))
            ?? throw new InvalidOperationException(secure
                ? "当前网络不是受支持的 WPA2/WPA3 个人密码网络；企业级 802.1X 请在系统网络设置中配置。"
                : "该 Wi-Fi 可能需要密码；密码留空只允许连接未加密网络。");
        if (secure && (wifiPassword.Length < 8 || wifiPassword.Length > 63))
            throw new InvalidOperationException("WPA/WPA2 个人 Wi-Fi 密码应为 8 至 63 个字符；企业 Wi-Fi 请使用系统网络设置。");
        var id = network.InterfaceId;
        // Use an application-owned profile name, do not overwrite the user's saved
        // Windows Wi-Fi profiles or reuse their credentials.
        var profileName = "WIFI助手-" + Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(Encoding.UTF8.GetBytes(profile.Ssid)))[..16];
        var xml = secure ? PersonalProfileXml(profile.Ssid, profileName, wifiPassword, network.Auth == 9) : OpenProfileXml(profile.Ssid, profileName);
        // Overwrite only the stable application-owned profile so reconnection and
        // password changes work without touching the user's existing Wi-Fi profiles.
        Check(WlanSetProfile(handle, ref id, 2, xml, null, true, IntPtr.Zero, out _));
        var parameters = new ConnectionParameters { Mode = 0, Profile = profileName, BssType = 1 };
        current = CurrentSsid(profile.Ssid);
        if (current == profile.Ssid) return;
        if (!allowSwitch && !string.IsNullOrEmpty(current))
            throw new InvalidOperationException("网络已变化，自动连接已暂停。");
        Check(WlanConnect(handle, ref id, ref parameters, IntPtr.Zero));
        for (var i = 0; i < 25; i++) {
            await Task.Delay(1000).ConfigureAwait(false);
            if (CurrentSsid(profile.Ssid) == profile.Ssid) return;
        }
        throw new TimeoutException("连接超时，请检查 Wi-Fi 信号和系统中保存的网络配置。");
    }
    internal static string OpenProfileXml(string ssid, string? profileName = null)
    {
        var hex = Convert.ToHexString(Encoding.UTF8.GetBytes(ssid));
        return $"""
            <?xml version="1.0"?>
            <WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
              <name>{SecurityElement.Escape(profileName ?? ssid)}</name>
              <SSIDConfig><SSID><hex>{hex}</hex><name>{SecurityElement.Escape(ssid)}</name></SSID></SSIDConfig>
              <connectionType>ESS</connectionType><connectionMode>manual</connectionMode>
              <MSM><security><authEncryption><authentication>open</authentication><encryption>none</encryption><useOneX>false</useOneX></authEncryption></security></MSM>
            </WLANProfile>
            """;
    }
    internal static string PersonalProfileXml(string ssid, string profileName, string password, bool wpa3 = false)
    {
        var hex = Convert.ToHexString(Encoding.UTF8.GetBytes(ssid));
        return $"""
            <?xml version="1.0"?>
            <WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
              <name>{SecurityElement.Escape(profileName)}</name>
              <SSIDConfig><SSID><hex>{hex}</hex><name>{SecurityElement.Escape(ssid)}</name></SSID></SSIDConfig>
              <connectionType>ESS</connectionType><connectionMode>manual</connectionMode>
              <MSM><security><authEncryption><authentication>{(wpa3 ? "WPA3SAE" : "WPA2PSK")}</authentication><encryption>AES</encryption><useOneX>false</useOneX></authEncryption>
                <sharedKey><keyType>passPhrase</keyType><protected>false</protected><keyMaterial>{SecurityElement.Escape(password)}</keyMaterial></sharedKey>
              </security></MSM>
            </WLANProfile>
            """;
    }
    private static void Check(uint code)
    {
        if (code == 5) throw new InvalidOperationException("Windows 拒绝访问 Wi-Fi 信息。请在 设置 → 隐私和安全性 → 位置 中开启定位服务并允许桌面应用访问，然后重试。");
        if (code != 0) throw new Win32Exception((int)code);
    }

    [StructLayout(LayoutKind.Sequential)] internal struct Ssid
    {
        public uint Length;
        [MarshalAs(UnmanagedType.ByValArray, SizeConst = 32)] public byte[] Bytes;
        public readonly string Text => Encoding.UTF8.GetString(Bytes, 0, (int)Math.Min(Length, 32));
    }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] internal struct InterfaceInfo
    {
        public Guid Id;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string Description;
        public uint State;
    }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] internal struct AvailableNetwork
    {
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string ProfileName;
        public Ssid Ssid;
        public uint BssType, BssidCount;
        public int Connectable;
        public uint Reason, PhyCount;
        [MarshalAs(UnmanagedType.ByValArray, SizeConst = 8)] public uint[] Phys;
        public int MorePhys;
        public uint Signal;
        public int SecurityEnabled;
        public uint Auth, Cipher, Flags, Reserved;
    }
    [StructLayout(LayoutKind.Sequential)] internal struct Association
    {
        public Ssid Ssid;
        public uint BssType;
        [MarshalAs(UnmanagedType.ByValArray, SizeConst = 6)] public byte[] Bssid;
        public uint PhyType, PhyIndex, Signal, RxRate, TxRate;
    }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] internal struct ConnectionAttributes
    {
        public uint State, Mode;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string ProfileName;
        public Association Association;
        public int SecurityEnabled, OneXEnabled;
        public uint Auth, Cipher;
    }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] private struct ConnectionParameters
    {
        public uint Mode;
        [MarshalAs(UnmanagedType.LPWStr)] public string Profile;
        public IntPtr Ssid, BssidList;
        public uint BssType, Flags;
    }
    [DllImport("wlanapi.dll")] private static extern uint WlanOpenHandle(uint version, IntPtr reserved, out uint negotiated, out IntPtr handle);
    [DllImport("wlanapi.dll")] private static extern uint WlanCloseHandle(IntPtr handle, IntPtr reserved);
    [DllImport("wlanapi.dll")] private static extern uint WlanEnumInterfaces(IntPtr handle, IntPtr reserved, out IntPtr list);
    [DllImport("wlanapi.dll")] private static extern void WlanFreeMemory(IntPtr memory);
    [DllImport("wlanapi.dll")] private static extern uint WlanQueryInterface(IntPtr handle, ref Guid id, uint opcode, IntPtr reserved, out uint size, out IntPtr data, out uint type);
    [DllImport("wlanapi.dll")] private static extern uint WlanScan(IntPtr handle, ref Guid id, IntPtr ssid, IntPtr ie, IntPtr reserved);
    [DllImport("wlanapi.dll")] private static extern uint WlanGetAvailableNetworkList(IntPtr handle, ref Guid id, uint flags, IntPtr reserved, out IntPtr list);
    [DllImport("wlanapi.dll", CharSet = CharSet.Unicode)] private static extern uint WlanSetProfile(IntPtr handle, ref Guid id, uint flags, string xml, string? security, [MarshalAs(UnmanagedType.Bool)] bool overwrite, IntPtr reserved, out uint reason);
    [DllImport("wlanapi.dll", CharSet = CharSet.Unicode)] private static extern uint WlanConnect(IntPtr handle, ref Guid id, ref ConnectionParameters parameters, IntPtr reserved);
}
