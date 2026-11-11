// SPDX-License-Identifier: MIT
// Windows 10+ only. No shell, breakaway flag, or process-name termination.
// The JOB_LIST attribute assigns the job during CreateProcess, before any child
// instruction can run; closing our non-inherited job handle kills its tree.
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using Microsoft.Win32.SafeHandles;

namespace Neuro.Windows
{
    public sealed class ProcessResult
    {
        public int ExitCode { get; internal set; }
        public bool TimedOut { get; internal set; }
        public bool OutputComplete { get; internal set; }
        public bool StdoutTruncated { get; internal set; }
        public bool StderrTruncated { get; internal set; }
        public long DurationMs { get; internal set; }
        public long StdoutBytes { get; internal set; }
        public long StderrBytes { get; internal set; }
        public string Stdout { get; internal set; }
        public string Stderr { get; internal set; }
        public string Error { get; internal set; }
        public uint ProcessId { get; internal set; }
        public ProcessResult()
        {
            ExitCode = -1;
            Stdout = Stderr = Error = "";
        }
    }

    public static class BoundedProcess
    {
        const uint KillOnJobClose = 0x2000;
        const uint CreateSuspended = 0x4;
        const uint CreateNoWindow = 0x08000000;
        const uint ExtendedStartupInfo = 0x00080000;
        const uint WaitTimeout = 258;
        const uint WaitFailed = 0xffffffff;

        [StructLayout(LayoutKind.Sequential)]
        struct SecurityAttributes
        {
            public int Length;
            public IntPtr Descriptor;
            public int Inherit;
        }
        [StructLayout(LayoutKind.Sequential)]
        struct BasicLimits
        {
            public long ProcessTime, JobTime;
            public uint Flags;
            public UIntPtr MinWorkingSet, MaxWorkingSet;
            public uint ActiveProcesses;
            public UIntPtr Affinity;
            public uint Priority, Scheduling;
        }
        [StructLayout(LayoutKind.Sequential)]
        struct IoCounters
        {
            public ulong ReadOps, WriteOps, OtherOps, ReadBytes, WriteBytes, OtherBytes;
        }
        [StructLayout(LayoutKind.Sequential)]
        struct ExtendedLimits
        {
            public BasicLimits Basic;
            public IoCounters Io;
            public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
        }
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        struct StartupInfo
        {
            public int Size;
            public string Reserved, Desktop, Title;
            public uint X, Y, XSize, YSize, XChars, YChars, Fill, Flags;
            public ushort Show, ReservedSize;
            public IntPtr ReservedBytes, Stdin, Stdout, Stderr;
        }
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        struct StartupInfoEx
        {
            public StartupInfo Startup;
            public IntPtr Attributes;
        }
        [StructLayout(LayoutKind.Sequential)]
        struct ProcessInfo
        {
            public IntPtr Process, Thread;
            public uint ProcessId, ThreadId;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern IntPtr CreateJobObjectW(IntPtr attributes, string name);
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool SetInformationJobObject(IntPtr job, int kind,
            ref ExtendedLimits information, uint length);
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool TerminateJobObject(IntPtr job, uint exitCode);
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool CreatePipe(out IntPtr read, out IntPtr write,
            ref SecurityAttributes attributes, uint size);
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool SetHandleInformation(IntPtr handle, uint mask, uint flags);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern IntPtr CreateFileW(string file, uint access, uint share,
            ref SecurityAttributes attributes, uint disposition, uint flags, IntPtr template);
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool InitializeProcThreadAttributeList(IntPtr list, int count,
            uint flags, ref IntPtr bytes);
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool UpdateProcThreadAttribute(IntPtr list, uint flags,
            IntPtr attribute, IntPtr value, IntPtr bytes, IntPtr previous, IntPtr returned);
        [DllImport("kernel32.dll")]
        static extern void DeleteProcThreadAttributeList(IntPtr list);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool CreateProcessW(string application, StringBuilder command,
            IntPtr processSecurity, IntPtr threadSecurity,
            [MarshalAs(UnmanagedType.Bool)] bool inherit, uint flags,
            IntPtr environment, string directory, ref StartupInfoEx startup,
            out ProcessInfo process);
        [DllImport("kernel32.dll", SetLastError = true)]
        static extern uint ResumeThread(IntPtr thread);
        [DllImport("kernel32.dll", SetLastError = true)]
        static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool GetExitCodeProcess(IntPtr process, out uint code);
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool CloseHandle(IntPtr handle);

        static void Require(bool ok, string operation)
        {
            if (!ok) throw new Win32Exception(Marshal.GetLastWin32Error(), operation);
        }
        static void Close(ref IntPtr handle)
        {
            if (handle != IntPtr.Zero && handle != new IntPtr(-1)) CloseHandle(handle);
            handle = IntPtr.Zero;
        }

        // Microsoft CRT argv rules, including quotes and trailing backslashes.
        public static string QuoteArgument(string value)
        {
            if (value == null || value.IndexOf('\0') >= 0)
                throw new ArgumentException("Argument is null or contains NUL");
            StringBuilder quoted = new StringBuilder("\"");
            int slashes = 0;
            foreach (char c in value)
            {
                if (c == '\\') { slashes++; continue; }
                if (c == '"') quoted.Append('\\', slashes * 2 + 1);
                else quoted.Append('\\', slashes);
                quoted.Append(c);
                slashes = 0;
            }
            quoted.Append('\\', slashes * 2);
            return quoted.Append('"').ToString();
        }

        sealed class Capture
        {
            readonly MemoryStream kept = new MemoryStream();
            readonly int cap;
            readonly SafeFileHandle handle;
            public readonly Thread Reader;
            public long Total;
            public string Error = "";
            public Capture(IntPtr input, int limit)
            {
                handle = new SafeFileHandle(input, true);
                cap = limit;
                Reader = new Thread(Drain);
                Reader.IsBackground = true;
            }
            void Drain()
            {
                try
                {
                    using (FileStream stream = new FileStream(handle, FileAccess.Read, 4096, false))
                    {
                        byte[] block = new byte[8192];
                        int count;
                        while ((count = stream.Read(block, 0, block.Length)) != 0)
                        {
                            Total += count;
                            int retain = Math.Min(count, cap - (int)kept.Length);
                            if (retain > 0) kept.Write(block, 0, retain);
                        }
                    }
                }
                catch (Exception error) { Error = error.GetType().Name; }
            }
            public string Text { get { return Encoding.UTF8.GetString(kept.ToArray()); } }
        }

        public static ProcessResult Run(string executable, string[] arguments,
            string cwd, int timeoutMs, int maxOutputBytes)
        {
            if (Environment.OSVersion.Platform != PlatformID.Win32NT)
                throw new PlatformNotSupportedException("Windows 10 or newer is required");
            if (timeoutMs < 1 || timeoutMs > 3600000)
                throw new ArgumentOutOfRangeException("timeoutMs");
            if (maxOutputBytes < 1 || maxOutputBytes > 4194304)
                throw new ArgumentOutOfRangeException("maxOutputBytes");
            if (String.IsNullOrEmpty(executable) || !Path.IsPathRooted(executable)
                || String.IsNullOrEmpty(cwd) || !Path.IsPathRooted(cwd) || arguments == null)
                throw new ArgumentException("Explicit executable and working directory are required");
            StringBuilder command = new StringBuilder(QuoteArgument(executable));
            foreach (string argument in arguments) command.Append(' ').Append(QuoteArgument(argument));
            if (command.Length >= 32767) throw new ArgumentException("Command line exceeds Windows limit");

            ProcessResult result = new ProcessResult();
            Stopwatch clock = Stopwatch.StartNew();
            IntPtr job = IntPtr.Zero, outputRead = IntPtr.Zero, outputWrite = IntPtr.Zero;
            IntPtr errorRead = IntPtr.Zero, errorWrite = IntPtr.Zero, input = IntPtr.Zero;
            IntPtr attributes = IntPtr.Zero, inherited = IntPtr.Zero, jobs = IntPtr.Zero;
            bool initializedAttributes = false;
            ProcessInfo child = new ProcessInfo();
            Capture stdout = null, stderr = null;
            try
            {
                job = CreateJobObjectW(IntPtr.Zero, null);
                Require(job != IntPtr.Zero, "CreateJobObject");
                ExtendedLimits limits = new ExtendedLimits();
                limits.Basic.Flags = KillOnJobClose;
                Require(SetInformationJobObject(job, 9, ref limits,
                    (uint)Marshal.SizeOf(typeof(ExtendedLimits))), "SetInformationJobObject");
                SecurityAttributes security = new SecurityAttributes();
                security.Length = Marshal.SizeOf(typeof(SecurityAttributes));
                security.Inherit = 1;
                Require(CreatePipe(out outputRead, out outputWrite, ref security, 0), "CreatePipe(stdout)");
                Require(CreatePipe(out errorRead, out errorWrite, ref security, 0), "CreatePipe(stderr)");
                Require(SetHandleInformation(outputRead, 1, 0), "SetHandleInformation(stdout)");
                Require(SetHandleInformation(errorRead, 1, 0), "SetHandleInformation(stderr)");
                input = CreateFileW("NUL", 0x80000000, 3, ref security, 3, 0, IntPtr.Zero);
                Require(input != new IntPtr(-1), "CreateFile(NUL)");

                IntPtr attributeBytes = IntPtr.Zero;
                InitializeProcThreadAttributeList(IntPtr.Zero, 2, 0, ref attributeBytes);
                Require(attributeBytes != IntPtr.Zero, "Attribute list size");
                attributes = Marshal.AllocHGlobal(attributeBytes);
                Require(InitializeProcThreadAttributeList(attributes, 2, 0, ref attributeBytes),
                    "InitializeProcThreadAttributeList");
                initializedAttributes = true;
                inherited = Marshal.AllocHGlobal(3 * IntPtr.Size);
                Marshal.WriteIntPtr(inherited, 0, input);
                Marshal.WriteIntPtr(inherited, IntPtr.Size, outputWrite);
                Marshal.WriteIntPtr(inherited, 2 * IntPtr.Size, errorWrite);
                Require(UpdateProcThreadAttribute(attributes, 0, new IntPtr(0x20002),
                    inherited, new IntPtr(3 * IntPtr.Size), IntPtr.Zero, IntPtr.Zero),
                    "UpdateProcThreadAttribute(HANDLE_LIST)");
                jobs = Marshal.AllocHGlobal(IntPtr.Size);
                Marshal.WriteIntPtr(jobs, job);
                Require(UpdateProcThreadAttribute(attributes, 0, new IntPtr(0x2000d),
                    jobs, new IntPtr(IntPtr.Size), IntPtr.Zero, IntPtr.Zero),
                    "UpdateProcThreadAttribute(JOB_LIST; Windows 10+ required)");
                StartupInfoEx startup = new StartupInfoEx();
                startup.Startup.Size = Marshal.SizeOf(typeof(StartupInfoEx));
                startup.Startup.Flags = 0x100; // STARTF_USESTDHANDLES
                startup.Startup.Stdin = input;
                startup.Startup.Stdout = outputWrite;
                startup.Startup.Stderr = errorWrite;
                startup.Attributes = attributes;
                Require(CreateProcessW(executable, command, IntPtr.Zero, IntPtr.Zero, true,
                    CreateSuspended | CreateNoWindow | ExtendedStartupInfo,
                    IntPtr.Zero, cwd, ref startup, out child), "CreateProcess(in job)");
                result.ProcessId = child.ProcessId;
                Close(ref outputWrite); Close(ref errorWrite); Close(ref input);
                stdout = new Capture(outputRead, maxOutputBytes); outputRead = IntPtr.Zero;
                stderr = new Capture(errorRead, maxOutputBytes); errorRead = IntPtr.Zero;
                stdout.Reader.Start(); stderr.Reader.Start();
                Require(ResumeThread(child.Thread) != 0xffffffff, "ResumeThread");
                Close(ref child.Thread);
                uint remaining = (uint)Math.Max(0, timeoutMs - clock.ElapsedMilliseconds);
                uint waited = WaitForSingleObject(child.Process, remaining);
                Require(waited != WaitFailed, "WaitForSingleObject");
                result.TimedOut = waited == WaitTimeout;
                if (result.TimedOut)
                {
                    Require(TerminateJobObject(job, 124), "TerminateJobObject(timeout)");
                    Require(WaitForSingleObject(child.Process, 5000) == 0,
                        "WaitForSingleObject(after termination)");
                }
                uint exitCode;
                Require(GetExitCodeProcess(child.Process, out exitCode), "GetExitCodeProcess");
                result.ExitCode = unchecked((int)exitCode);
            }
            catch (Exception error)
            {
                result.Error = error.GetType().Name + ": " + error.Message;
            }
            finally
            {
                // Kill leftover descendants even when their original parent exited normally.
                if (job != IntPtr.Zero)
                {
                    TerminateJobObject(job, 125);
                    Close(ref job);
                }
                Close(ref child.Thread); Close(ref child.Process);
                Close(ref outputWrite); Close(ref errorWrite); Close(ref input);
                Close(ref outputRead); Close(ref errorRead);
                if (initializedAttributes) DeleteProcThreadAttributeList(attributes);
                if (attributes != IntPtr.Zero) Marshal.FreeHGlobal(attributes);
                if (inherited != IntPtr.Zero) Marshal.FreeHGlobal(inherited);
                if (jobs != IntPtr.Zero) Marshal.FreeHGlobal(jobs);
            }
            bool outputDone = stdout != null && stdout.Reader.Join(2000);
            bool errorDone = stderr != null && stderr.Reader.Join(2000);
            result.OutputComplete = outputDone && errorDone
                && stdout.Error.Length == 0 && stderr.Error.Length == 0;
            if (outputDone)
            {
                result.Stdout = stdout.Text; result.StdoutBytes = stdout.Total;
                result.StdoutTruncated = stdout.Total > maxOutputBytes;
            }
            if (errorDone)
            {
                result.Stderr = stderr.Text; result.StderrBytes = stderr.Total;
                result.StderrTruncated = stderr.Total > maxOutputBytes;
            }
            result.DurationMs = clock.ElapsedMilliseconds;
            return result;
        }
    }
}
