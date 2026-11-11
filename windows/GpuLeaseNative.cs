// SPDX-License-Identifier: MIT
// Exact process identity, owner-session discovery and visible console launch.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Text;

namespace Neuro.Windows
{
    public sealed class GpuProcessIdentity
    {
        public int Pid { get; set; }
        public long CreatedFileTime { get; set; }
        public string ExecutablePath { get; set; }
        public string OwnerSid { get; set; }
        public int SessionId { get; set; }
    }
    public static class GpuLeaseNative
    {
        const uint Query = 0x1000, Synchronize = 0x100000, Terminate = 1;
        [StructLayout(LayoutKind.Sequential)] struct FileTime { public uint Low, High; }
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        struct StartupInfo
        {
            public int Size; public string Reserved, Desktop, Title;
            public uint X, Y, XSize, YSize, XChars, YChars, Fill, Flags;
            public ushort Show, ReservedSize; public IntPtr ReservedBytes, Stdin, Stdout, Stderr;
        }
        [StructLayout(LayoutKind.Sequential)]
        struct ProcessInfo { public IntPtr Process, Thread; public uint ProcessId, ThreadId; }
        [StructLayout(LayoutKind.Sequential)]
        struct SessionInfo { public int SessionId; public IntPtr Station; public int State; }
        [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr OpenProcess(uint rights, bool inherit, int pid);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool CloseHandle(IntPtr handle);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool GetProcessTimes(IntPtr handle, out FileTime created, out FileTime exited, out FileTime kernel, out FileTime user);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool QueryFullProcessImageNameW(IntPtr handle, uint flags, StringBuilder path, ref int size);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool ProcessIdToSessionId(uint pid, out uint session);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool TerminateProcess(IntPtr handle, uint code);
        [DllImport("kernel32.dll", SetLastError = true)] static extern uint WaitForSingleObject(IntPtr handle, uint timeout);
        [DllImport("advapi32.dll", SetLastError = true)] static extern bool OpenProcessToken(IntPtr process, uint rights, out IntPtr token);
        [DllImport("advapi32.dll", SetLastError = true)] static extern bool GetTokenInformation(IntPtr token, int kind, IntPtr value, int size, out int needed);
        [DllImport("wtsapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool WTSEnumerateSessionsW(IntPtr server, int reserved, int version, out IntPtr values, out int count);
        [DllImport("wtsapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool WTSQuerySessionInformationW(IntPtr server, int session, int kind, out IntPtr value, out int bytes);
        [DllImport("wtsapi32.dll")] static extern void WTSFreeMemory(IntPtr pointer);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern bool CreateProcessW(string app, StringBuilder command, IntPtr processSecurity, IntPtr threadSecurity, bool inherit, uint flags, IntPtr environment, string cwd, ref StartupInfo startup, out ProcessInfo result);
        delegate bool WindowCallback(IntPtr window, IntPtr value);
        [DllImport("user32.dll")] static extern bool EnumWindows(WindowCallback callback, IntPtr value);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetClassNameW(IntPtr window, StringBuilder name, int count);
        [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr window);
        [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);

        static void Check(bool ok, string name)
        { if (!ok) throw new Win32Exception(Marshal.GetLastWin32Error(), name); }
        static GpuProcessIdentity Read(IntPtr handle, int pid)
        {
            FileTime created, exited, kernel, user;
            Check(GetProcessTimes(handle, out created, out exited, out kernel, out user), "GetProcessTimes");
            StringBuilder path = new StringBuilder(32768); int size = path.Capacity;
            Check(QueryFullProcessImageNameW(handle, 0, path, ref size), "QueryFullProcessImageName");
            uint session; Check(ProcessIdToSessionId((uint)pid, out session), "ProcessIdToSessionId");
            IntPtr token = IntPtr.Zero, buffer = IntPtr.Zero;
            string sid;
            try
            {
                Check(OpenProcessToken(handle, 8, out token), "OpenProcessToken");
                int needed; GetTokenInformation(token, 1, IntPtr.Zero, 0, out needed);
                if (needed < IntPtr.Size || needed > 65536) throw new InvalidOperationException("Invalid token record");
                buffer = Marshal.AllocHGlobal(needed);
                Check(GetTokenInformation(token, 1, buffer, needed, out needed), "GetTokenInformation");
                sid = new SecurityIdentifier(Marshal.ReadIntPtr(buffer)).Value;
            }
            finally { if (buffer != IntPtr.Zero) Marshal.FreeHGlobal(buffer); if (token != IntPtr.Zero) CloseHandle(token); }
            return new GpuProcessIdentity { Pid = pid, CreatedFileTime = ((long)created.High << 32) | created.Low,
                ExecutablePath = path.ToString(), OwnerSid = sid, SessionId = (int)session };
        }
        public static GpuProcessIdentity Inspect(int pid)
        {
            if (pid <= 0) throw new ArgumentOutOfRangeException("pid");
            IntPtr handle = OpenProcess(Query | Synchronize, false, pid);
            if (handle == IntPtr.Zero)
            {
                int error = Marshal.GetLastWin32Error();
                if (error == 87) return null; // No such PID. Access denied is not disappearance.
                throw new Win32Exception(error, "OpenProcess(identity)");
            }
            try { if (WaitForSingleObject(handle, 0) == 0) return null; return Read(handle, pid); }
            finally { CloseHandle(handle); }
        }
        public static bool Matches(GpuProcessIdentity value, int pid, long created, string path, string sid)
        {
            return value != null && value.Pid == pid && value.CreatedFileTime == created &&
                String.Equals(value.ExecutablePath, path, StringComparison.OrdinalIgnoreCase) &&
                String.Equals(value.OwnerSid, sid, StringComparison.Ordinal);
        }
        public static bool IsSameAlive(int pid, long created, string path, string sid)
        { return Matches(Inspect(pid), pid, created, path, sid); }
        public static bool StopExact(int pid, long created, string path, string sid)
        {
            IntPtr handle = OpenProcess(Query | Synchronize | Terminate, false, pid);
            if (handle == IntPtr.Zero)
            {
                int error = Marshal.GetLastWin32Error();
                if (error == 87) return false;
                throw new Win32Exception(error, "OpenProcess(stop)");
            }
            try
            {
                if (WaitForSingleObject(handle, 0) == 0) return false;
                if (!Matches(Read(handle, pid), pid, created, path, sid))
                    throw new InvalidOperationException("Process identity changed; refusing termination");
                Check(TerminateProcess(handle, 0), "TerminateProcess(owned miner)");
                if (WaitForSingleObject(handle, 10000) != 0) throw new InvalidOperationException("Owned process did not terminate");
                return true;
            }
            finally { CloseHandle(handle); }
        }
        static string SessionText(int session, int kind)
        {
            IntPtr value; int bytes;
            Check(WTSQuerySessionInformationW(IntPtr.Zero, session, kind, out value, out bytes), "WTSQuerySessionInformation");
            try { return bytes > 2 ? Marshal.PtrToStringUni(value) : ""; }
            finally { WTSFreeMemory(value); }
        }
        public static int ActiveOwnerSession(string sid)
        {
            IntPtr rows; int count; List<int> matches = new List<int>();
            Check(WTSEnumerateSessionsW(IntPtr.Zero, 0, 1, out rows, out count), "WTSEnumerateSessions");
            try
            {
                int size = Marshal.SizeOf(typeof(SessionInfo));
                for (int i = 0; i < count; i++)
                {
                    SessionInfo row = (SessionInfo)Marshal.PtrToStructure(IntPtr.Add(rows, i * size), typeof(SessionInfo));
                    if (row.State != 0 || row.SessionId <= 0) continue;
                    string name = SessionText(row.SessionId, 5), domain = SessionText(row.SessionId, 7);
                    if (name.Length == 0) continue;
                    string owner = ((SecurityIdentifier)new NTAccount(domain, name).Translate(typeof(SecurityIdentifier))).Value;
                    if (owner == sid) matches.Add(row.SessionId);
                }
            }
            finally { WTSFreeMemory(rows); }
            if (matches.Count != 1) throw new InvalidOperationException("Exactly one active interactive owner session is required");
            return matches[0];
        }
        public static string BatchCommand(string batch)
        {
            if (String.IsNullOrWhiteSpace(batch) || batch.Length < 3 || !Char.IsLetter(batch[0]) || batch[1] != ':' ||
                (batch[2] != '\\' && batch[2] != '/') || !Path.IsPathRooted(batch) ||
                batch.IndexOfAny(new char[] { '"', '\r', '\n', '\0', '%', '!', '^', '&', '|', '<', '>' }) >= 0 ||
                !String.Equals(Path.GetFileName(batch), "guided-setup.bat", StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("Only an explicit safe guided-setup.bat path is supported");
            return "/d /s /c \"\"" + batch + "\"\"";
        }
        static GpuProcessIdentity LaunchInboxConsole(string arguments, string ownerSid, int session, string cwd)
        {
            GpuProcessIdentity self = Inspect(Process.GetCurrentProcess().Id);
            if (self.OwnerSid != ownerSid || self.SessionId != session || session <= 0 || ActiveOwnerSession(ownerSid) != session)
                throw new InvalidOperationException("Visible restart must run in the active owner session");
            string system = Environment.GetFolderPath(Environment.SpecialFolder.System);
            string host = Path.Combine(system, "conhost.exe"), cmd = Path.Combine(system, "cmd.exe");
            StartupInfo startup = new StartupInfo(); startup.Size = Marshal.SizeOf(typeof(StartupInfo));
            startup.Desktop = "winsta0\\default"; startup.Flags = 1; startup.Show = 5;
            ProcessInfo process;
            // Explicit inbox host is Microsoft's per-launch opt-out from default
            // Terminal delegation; it does not modify system/user settings.
            string command = "\"" + host + "\" \"" + cmd + "\" " + arguments;
            Check(CreateProcessW(host, new StringBuilder(command), IntPtr.Zero, IntPtr.Zero,
                false, 0x10, IntPtr.Zero, cwd, ref startup, out process), "CreateProcess(inbox console)");
            try { return Read(process.Process, (int)process.ProcessId); }
            finally { CloseHandle(process.Thread); CloseHandle(process.Process); }
        }
        public static GpuProcessIdentity LaunchVisibleProbe(string ownerSid, int session)
        { return LaunchInboxConsole("/d /q /k title Neuro GPU handoff preflight", ownerSid, session, Environment.GetFolderPath(Environment.SpecialFolder.System)); }
        public static long VisibleConsoleWindow(int hostPid, long created, string sid, int session)
        {
            GpuProcessIdentity identity = Inspect(hostPid);
            string hostPath = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "conhost.exe");
            if (!Matches(identity, hostPid, created, hostPath, sid) || identity.SessionId != session || session <= 0) return 0;
            long found = 0;
            WindowCallback callback = delegate(IntPtr window, IntPtr unused)
            {
                uint pid; GetWindowThreadProcessId(window, out pid);
                if (pid != hostPid || !IsWindowVisible(window)) return true;
                StringBuilder name = new StringBuilder(256);
                if (GetClassNameW(window, name, name.Capacity) > 0 && name.ToString() == "ConsoleWindowClass")
                { found = window.ToInt64(); return false; }
                return true;
            };
            EnumWindows(callback, IntPtr.Zero);
            GC.KeepAlive(callback);
            return found;
        }
    }
}
