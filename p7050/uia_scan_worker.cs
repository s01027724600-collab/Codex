using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows;
using System.Windows.Automation;

public static class UiaScanWorker
{
    private const int GwlExStyle = -20;
    private const long WsExToolWindow = 0x00000080L;
    private const long WsExAppWindow = 0x00040000L;
    private const int DwmwaCloaked = 14;
    private const int WmGetTitleBarInfoEx = 0x033F;
    private const int StateSystemUnavailable = 0x00000001;
    private const int StateSystemInvisible = 0x00008000;
    private const int TitleBarChildMinimize = 2;
    private const int TitleBarChildMaximize = 3;
    private const int TitleBarChildClose = 5;
    private const int SmCxSize = 30;
    private const int SmCySize = 31;
    private const int SmXVirtualScreen = 76;
    private const int SmYVirtualScreen = 77;
    private const int SmCxVirtualScreen = 78;
    private const int SmCyVirtualScreen = 79;
    private const int GwHwndNext = 2;
    private const int GwOwner = 4;
    private const uint GaRoot = 2;
    private const int VkLButton = 0x01;
    private const int VkRButton = 0x02;
    private const int VkMButton = 0x04;
    private const int MinVisiblePiece = 10;
    private const uint EventSystemForeground = 0x0003;
    private const uint EventSystemMinimizeStart = 0x0016;
    private const uint EventSystemMinimizeEnd = 0x0017;
    private const uint EventObjectCreate = 0x8000;
    private const uint EventObjectDestroy = 0x8001;
    private const uint EventObjectShow = 0x8002;
    private const uint EventObjectHide = 0x8003;
    private const uint EventObjectReorder = 0x8004;
    private const uint EventObjectFocus = 0x8005;
    private const uint EventObjectSelection = 0x8006;
    private const uint EventObjectSelectionAdd = 0x8007;
    private const uint EventObjectSelectionRemove = 0x8008;
    private const uint EventObjectSelectionWithin = 0x8009;
    private const uint EventObjectStateChange = 0x800A;
    private const uint EventObjectLocationChange = 0x800B;
    private const uint EventObjectNameChange = 0x800C;
    private const uint WineventOutofcontext = 0x0000;
    private const uint WineventSkipownprocess = 0x0002;
    private const int ObjidWindow = 0;
    private static readonly long DesktopShellCacheTtlTicks = TimeSpan.FromMilliseconds(1800).Ticks;
    private static readonly long TaskbarShellCacheTtlTicks = TimeSpan.FromMilliseconds(4000).Ticks;
    private static readonly long SnapshotEventSettleTicks = TimeSpan.FromMilliseconds(120).Ticks;
    private static readonly long SnapshotMinimumRefreshTicks = TimeSpan.FromMilliseconds(250).Ticks;
    private static readonly long SnapshotFallbackTtlTicks = TimeSpan.FromSeconds(10).Ticks;
    private static readonly long DetailTimeoutCooldownTicks = TimeSpan.FromSeconds(20).Ticks;
    private const int DetailScanTimeoutMs = 1600;

    private static readonly Dictionary<long, DetailCache> DetailCaches = new Dictionary<long, DetailCache>();
    private static readonly Dictionary<long, long> DetailTimeoutUntilTicks = new Dictionary<long, long>();
    private static readonly Dictionary<string, DetailCache> ShellCaches = new Dictionary<string, DetailCache>();
    private static readonly object ScanLock = new object();
    private static readonly object SnapshotLock = new object();
    private static Thread SnapshotThread;
    private static bool SnapshotStop;
    private static bool SnapshotDirty = true;
    private static bool SnapshotScanning;
    private static int SnapshotMaxItems = 60;
    private static int SnapshotVisitLimit = 160;
    private static int SnapshotIntervalMs = 1600;
    private static int SnapshotSeq;
    private static int SnapshotWarmupScansRemaining;
    private static int SnapshotDirtyVersion;
    private static long SnapshotDirtySinceTicks;
    private static long SnapshotGeometrySignature = long.MinValue;
    private static string LatestSnapshotJson = "";
    private static string SnapshotLastError = "";
    private static long SnapshotLastCompletedTicks;
    private static Thread WinEventThread;
    private static WinEventProc WinEventCallbackRef;
    private static readonly List<IntPtr> WinEventHooks = new List<IntPtr>();
    private static int detailCursor = 0;
    private static long ForcedDetailHwnd;
    private static int LastMouseButtonMask;
    private static long MouseFollowupDueTicks;
    private static long LastMouseReleaseTicks;
    private static long LastSoftContentEventTicks;
    private static readonly Queue<long> DetailPriorityQueue = new Queue<long>();
    private static readonly HashSet<long> DetailPrioritySet = new HashSet<long>();

    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    private static extern short GetAsyncKeyState(int vKey);

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out int processId);

    [DllImport("user32.dll")]
    private static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern IntPtr GetTopWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern IntPtr GetWindow(IntPtr hWnd, int uCmd);

    [DllImport("user32.dll")]
    private static extern IntPtr GetAncestor(IntPtr hWnd, uint gaFlags);

    [DllImport("user32.dll")]
    private static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool IsIconic(IntPtr hWnd);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool GetWindowRect(IntPtr hWnd, out WinRect lpRect);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetWindowTextLengthW(IntPtr hWnd);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetWindowTextW(IntPtr hWnd, StringBuilder lpString, int nMaxCount);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetClassNameW(IntPtr hWnd, StringBuilder lpClassName, int nMaxCount);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr FindWindowExW(IntPtr hwndParent, IntPtr hwndChildAfter, string lpszClass, string lpszWindow);

    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")]
    private static extern IntPtr GetWindowLongPtr64(IntPtr hWnd, int nIndex);

    [DllImport("user32.dll")]
    private static extern int GetSystemMetrics(int nIndex);

    [DllImport("user32.dll", CharSet = CharSet.Auto)]
    private static extern IntPtr SendMessage(IntPtr hWnd, int msg, IntPtr wParam, ref TitleBarInfoEx lParam);

    [DllImport("dwmapi.dll")]
    private static extern int DwmGetWindowAttribute(IntPtr hwnd, int dwAttribute, out int pvAttribute, int cbAttribute);

    [DllImport("user32.dll")]
    private static extern IntPtr SetWinEventHook(
        uint eventMin,
        uint eventMax,
        IntPtr hmodWinEventProc,
        WinEventProc lpfnWinEventProc,
        uint idProcess,
        uint idThread,
        uint dwFlags);

    [DllImport("user32.dll")]
    private static extern bool GetMessage(out NativeMessage lpMsg, IntPtr hWnd, uint wMsgFilterMin, uint wMsgFilterMax);

    [DllImport("user32.dll")]
    private static extern bool TranslateMessage(ref NativeMessage lpMsg);

    [DllImport("user32.dll")]
    private static extern IntPtr DispatchMessage(ref NativeMessage lpMsg);

    private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    private delegate void WinEventProc(
        IntPtr hWinEventHook,
        uint eventType,
        IntPtr hWnd,
        int idObject,
        int idChild,
        uint idEventThread,
        uint eventTime);

    [StructLayout(LayoutKind.Sequential)]
    private struct WinRect
    {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct NativePoint
    {
        public int X;
        public int Y;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct NativeMessage
    {
        public IntPtr HWnd;
        public uint Message;
        public UIntPtr WParam;
        public IntPtr LParam;
        public uint Time;
        public NativePoint Point;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct TitleBarInfoEx
    {
        public int cbSize;
        public WinRect rcTitleBar;
        [MarshalAs(UnmanagedType.ByValArray, SizeConst = 6)]
        public int[] rgstate;
        [MarshalAs(UnmanagedType.ByValArray, SizeConst = 6)]
        public WinRect[] rgrect;
    }

    private sealed class RectInfo
    {
        public int X;
        public int Y;
        public int Width;
        public int Height;
        public int Right { get { return X + Width; } }
        public int Bottom { get { return Y + Height; } }
        public int CenterX { get { return X + Width / 2; } }
        public int CenterY { get { return Y + Height / 2; } }
    }

    private sealed class Item
    {
        public int Id;
        public string Label = "";
        public string Name = "";
        public string Control = "";
        public int X;
        public int Y;
        public int Width;
        public int Height;
        public int CenterX;
        public int CenterY;
        public List<string> Patterns = new List<string>();
        public string AutomationId = "";
        public string ClassName = "";
    }

    private sealed class ScanCommand
    {
        public int MaxItems = 60;
        public int VisitLimit = 160;
    }

    private sealed class ElementDepth
    {
        public AutomationElement Element;
        public int Depth;

        public ElementDepth(AutomationElement element, int depth)
        {
            Element = element;
            Depth = depth;
        }
    }

    private sealed class TopWindowInfo
    {
        public IntPtr Hwnd;
        public RectInfo Rect;
        public string Title = "";
        public string ClassName = "";
        public bool IsTaskbar;
    }

    private sealed class DetailCache
    {
        public long Hwnd;
        public RectInfo WindowRect;
        public string Title = "";
        public string ClassName = "";
        public List<Item> Items = new List<Item>();
        public long UpdatedAtTicks;
    }

    private static readonly HashSet<string> WantedControls = new HashSet<string>(StringComparer.Ordinal)
    {
        "Button", "Edit", "Hyperlink", "MenuItem", "ListItem", "TabItem",
        "CheckBox", "RadioButton", "ComboBox", "TreeItem", "DataItem",
        "Slider", "Thumb", "ScrollBar", "SplitButton", "Spinner"
    };

    private static readonly HashSet<string> BlankNameAllowed = new HashSet<string>(StringComparer.Ordinal)
    {
        "Edit", "Slider", "ScrollBar", "ComboBox", "Spinner"
    };

    private static readonly HashSet<string> ContainerControls = new HashSet<string>(StringComparer.Ordinal)
    {
        "Pane", "Group", "Document", "ToolBar"
    };

    public static int Main()
    {
        try
        {
            Console.InputEncoding = Encoding.UTF8;
            Console.OutputEncoding = new UTF8Encoding(false);
        }
        catch { }

        string line;
        while ((line = Console.ReadLine()) != null)
        {
            line = line.Trim();
            if (line.Length == 0)
            {
                continue;
            }
            if (line.Equals("quit", StringComparison.OrdinalIgnoreCase))
            {
                break;
            }

            try
            {
                string verb = GetCommandVerb(line);
                ScanCommand command = ParseCommand(line);
                string json;
                if (verb.Equals("snapshot", StringComparison.OrdinalIgnoreCase) ||
                    verb.Equals("status", StringComparison.OrdinalIgnoreCase))
                {
                    int intervalMs = ParseIntervalMs(line, 1600);
                    EnsureSnapshotWorker(command.MaxItems, command.VisitLimit, intervalMs);
                    json = GetSnapshotJson();
                }
                else if (verb.Equals("refresh", StringComparison.OrdinalIgnoreCase))
                {
                    MarkSnapshotDirty();
                    json = GetSnapshotJson();
                }
                else if (verb.Equals("refresh-foreground", StringComparison.OrdinalIgnoreCase))
                {
                    Interlocked.Exchange(ref ForcedDetailHwnd, GetForegroundWindow().ToInt64());
                    MarkSnapshotDirty();
                    json = GetSnapshotJson();
                }
                else if (verb.Equals("scan-foreground", StringComparison.OrdinalIgnoreCase))
                {
                    Interlocked.Exchange(ref ForcedDetailHwnd, GetForegroundWindow().ToInt64());
                    lock (ScanLock)
                    {
                        json = Scan(command.MaxItems, command.VisitLimit);
                    }
                    StoreSnapshot(json);
                    json = GetSnapshotJson();
                }
                else
                {
                    lock (ScanLock)
                    {
                        json = Scan(command.MaxItems, command.VisitLimit);
                    }
                    StoreSnapshot(json);
                    json = GetSnapshotJson();
                }
                Console.WriteLine(json);
                Console.Out.Flush();
            }
            catch (Exception ex)
            {
                Console.WriteLine(ErrorJson(ex.Message));
                Console.Out.Flush();
            }
        }

        return 0;
    }

    private static ScanCommand ParseCommand(string command)
    {
        ScanCommand result = new ScanCommand();
        string[] parts = command.Split(new[] { ' ', '\t' }, StringSplitOptions.RemoveEmptyEntries);
        if (parts.Length >= 2)
        {
            int parsed;
            if (int.TryParse(parts[1], NumberStyles.Integer, CultureInfo.InvariantCulture, out parsed))
            {
                result.MaxItems = Math.Max(1, Math.Min(400, parsed));
            }
        }
        if (parts.Length >= 3)
        {
            int parsed;
            if (int.TryParse(parts[2], NumberStyles.Integer, CultureInfo.InvariantCulture, out parsed))
            {
                result.VisitLimit = Math.Max(40, Math.Min(4000, parsed));
            }
        }
        return result;
    }

    private static string GetCommandVerb(string command)
    {
        string[] parts = command.Split(new[] { ' ', '\t' }, StringSplitOptions.RemoveEmptyEntries);
        return parts.Length == 0 ? "" : parts[0];
    }

    private static int ParseIntervalMs(string command, int fallback)
    {
        string[] parts = command.Split(new[] { ' ', '\t' }, StringSplitOptions.RemoveEmptyEntries);
        if (parts.Length >= 4)
        {
            int parsed;
            if (int.TryParse(parts[3], NumberStyles.Integer, CultureInfo.InvariantCulture, out parsed))
            {
                return Math.Max(250, Math.Min(15000, parsed));
            }
        }
        return fallback;
    }

    private static void EnsureSnapshotWorker(int maxItems, int visitLimit, int intervalMs)
    {
        lock (SnapshotLock)
        {
            SnapshotMaxItems = maxItems;
            SnapshotVisitLimit = visitLimit;
            SnapshotIntervalMs = intervalMs;
            if (SnapshotThread != null && SnapshotThread.IsAlive)
            {
                return;
            }
            SnapshotStop = false;
            SnapshotDirty = true;
            SnapshotDirtyVersion++;
            SnapshotDirtySinceTicks = 0;
            SnapshotWarmupScansRemaining = 1;
            SnapshotThread = new Thread(SnapshotLoop);
            SnapshotThread.IsBackground = true;
            SnapshotThread.Name = "uia-snapshot-loop";
            SnapshotThread.Start();
            StartWinEventThread();
        }
    }

    private static void MarkSnapshotDirty()
    {
        lock (SnapshotLock)
        {
            if (!SnapshotDirty)
            {
                SnapshotDirtySinceTicks = DateTime.UtcNow.Ticks;
            }
            SnapshotDirty = true;
            SnapshotDirtyVersion++;
        }
    }

    private static void QueueDetailRefresh(IntPtr hWnd)
    {
        if (hWnd == IntPtr.Zero)
        {
            return;
        }
        long value = hWnd.ToInt64();
        lock (SnapshotLock)
        {
            if (DetailPrioritySet.Add(value))
            {
                DetailPriorityQueue.Enqueue(value);
            }
        }
    }

    private static long TakeDetailRefresh()
    {
        lock (SnapshotLock)
        {
            while (DetailPriorityQueue.Count > 0)
            {
                long value = DetailPriorityQueue.Dequeue();
                DetailPrioritySet.Remove(value);
                return value;
            }
        }
        return 0;
    }

    private static string GetSnapshotJson()
    {
        lock (SnapshotLock)
        {
            if (!String.IsNullOrWhiteSpace(LatestSnapshotJson))
            {
                return LatestSnapshotJson;
            }
            StringBuilder sb = new StringBuilder();
            sb.Append("{\"ok\":true,\"engine\":\"csharp-uia-worker-async\",\"pending\":true,\"scanning\":");
            sb.Append(SnapshotScanning ? "true" : "false");
            sb.Append(",\"workerScanSeq\":");
            sb.Append(SnapshotSeq.ToString(CultureInfo.InvariantCulture));
            sb.Append(",\"elapsedMs\":0,\"count\":0,\"visited\":0,\"items\":[]}");
            return sb.ToString();
        }
    }

    private static void StoreSnapshot(
        string json,
        int scannedDirtyVersion = -1,
        bool complete = true,
        long triggerTicks = 0,
        bool hasUpdateLatency = false)
    {
        if (String.IsNullOrWhiteSpace(json))
        {
            return;
        }
        lock (SnapshotLock)
        {
            SnapshotSeq++;
            long nowTicks = DateTime.UtcNow.Ticks;
            long updateLatencyMs = hasUpdateLatency && triggerTicks > 0
                ? Math.Max(0, (nowTicks - triggerTicks) / TimeSpan.TicksPerMillisecond)
                : -1;
            LatestSnapshotJson = AddSnapshotMetadata(json, SnapshotSeq, updateLatencyMs, complete);
            SnapshotLastError = "";
            if (!complete)
            {
                return;
            }
            if (SnapshotWarmupScansRemaining > 0)
            {
                SnapshotWarmupScansRemaining--;
                SnapshotDirty = true;
                SnapshotDirtySinceTicks = 0;
            }
            else
            {
                SnapshotDirty = scannedDirtyVersion >= 0 && SnapshotDirtyVersion != scannedDirtyVersion;
                if (!SnapshotDirty)
                {
                    SnapshotDirtySinceTicks = 0;
                }
            }
            SnapshotLastCompletedTicks = nowTicks;
        }
    }

    private static string AddSnapshotMetadata(string json, int scanSeq, long updateLatencyMs, bool complete)
    {
        int index = json.LastIndexOf('}');
        if (index < 0)
        {
            return json;
        }
        return json.Substring(0, index) +
               ",\"asyncCache\":true,\"workerScanSeq\":" +
               scanSeq.ToString(CultureInfo.InvariantCulture) +
               ",\"updateLatencyMs\":" +
               (updateLatencyMs >= 0
                   ? updateLatencyMs.ToString(CultureInfo.InvariantCulture)
                   : "null") +
               ",\"snapshotPhase\":\"" +
               (complete ? "complete" : "geometry") +
               "\"" +
               json.Substring(index);
    }

    private static void SnapshotLoop()
    {
        while (true)
        {
            ObserveMouseButtons();
            long geometrySignature = ComputeGeometrySignature();
            lock (SnapshotLock)
            {
                if (geometrySignature != SnapshotGeometrySignature)
                {
                    SnapshotGeometrySignature = geometrySignature;
                    if (!SnapshotDirty)
                    {
                        SnapshotDirtySinceTicks = DateTime.UtcNow.Ticks;
                    }
                    SnapshotDirty = true;
                    SnapshotDirtyVersion++;
                }
            }

            int maxItems;
            int visitLimit;
            int intervalMs;
            int dirtyVersion;
            bool shouldScan;
            bool hasUpdateLatency;
            long triggerTicks;
            lock (SnapshotLock)
            {
                if (SnapshotStop)
                {
                    return;
                }
                maxItems = SnapshotMaxItems;
                visitLimit = SnapshotVisitLimit;
                intervalMs = SnapshotIntervalMs;
                dirtyVersion = SnapshotDirtyVersion;
                long nowTicks = DateTime.UtcNow.Ticks;
                bool hasSnapshot = !String.IsNullOrWhiteSpace(LatestSnapshotJson);
                bool eventSettled =
                    SnapshotDirtySinceTicks == 0 ||
                    nowTicks - SnapshotDirtySinceTicks >= SnapshotEventSettleTicks;
                bool dirtyReady =
                    SnapshotDirty &&
                    eventSettled &&
                    (!hasSnapshot || nowTicks - SnapshotLastCompletedTicks >= SnapshotMinimumRefreshTicks);
                shouldScan =
                    dirtyReady ||
                    !hasSnapshot ||
                    nowTicks - SnapshotLastCompletedTicks >= SnapshotFallbackTtlTicks;
                hasUpdateLatency = SnapshotDirty && SnapshotDirtySinceTicks > 0;
                triggerTicks = hasUpdateLatency ? SnapshotDirtySinceTicks : nowTicks;
                SnapshotScanning = shouldScan;
            }

            if (shouldScan)
            {
                try
                {
                    string geometryJson;
                    lock (ScanLock)
                    {
                        geometryJson = ScanCachedGeometry();
                    }
                    StoreSnapshot(geometryJson, dirtyVersion, false, triggerTicks, hasUpdateLatency);
                    Thread.Sleep(100);

                    string json;
                    lock (ScanLock)
                    {
                        json = Scan(maxItems, visitLimit);
                    }
                    StoreSnapshot(json, dirtyVersion, true, triggerTicks, hasUpdateLatency);
                }
                catch (Exception ex)
                {
                    lock (SnapshotLock)
                    {
                        SnapshotLastError = ex.Message;
                        SnapshotDirty = false;
                    }
                }
                finally
                {
                    lock (SnapshotLock)
                    {
                        SnapshotScanning = false;
                    }
                }
            }

            Thread.Sleep(Math.Max(80, Math.Min(160, intervalMs)));
        }
    }

    private static void ObserveMouseButtons()
    {
        int mask = 0;
        if ((GetAsyncKeyState(VkLButton) & 0x8000) != 0) mask |= 1;
        if ((GetAsyncKeyState(VkRButton) & 0x8000) != 0) mask |= 2;
        if ((GetAsyncKeyState(VkMButton) & 0x8000) != 0) mask |= 4;

        int previous = Interlocked.Exchange(ref LastMouseButtonMask, mask);
        long nowTicks = DateTime.UtcNow.Ticks;
        if ((previous & ~mask) != 0)
        {
            Interlocked.Exchange(ref LastMouseReleaseTicks, nowTicks);
            Interlocked.Exchange(ref ForcedDetailHwnd, GetForegroundWindow().ToInt64());
            Interlocked.Exchange(ref MouseFollowupDueTicks, nowTicks + TimeSpan.FromMilliseconds(900).Ticks);
            MarkSnapshotDirty();
        }

        long dueTicks = Interlocked.Read(ref MouseFollowupDueTicks);
        if (dueTicks > 0 &&
            nowTicks >= dueTicks &&
            Interlocked.CompareExchange(ref MouseFollowupDueTicks, 0, dueTicks) == dueTicks)
        {
            Interlocked.Exchange(ref ForcedDetailHwnd, GetForegroundWindow().ToInt64());
            MarkSnapshotDirty();
        }
    }

    private static long ComputeGeometrySignature()
    {
        unchecked
        {
            long hash = 1469598103934665603L;
            hash = MixGeometryHash(hash, GetForegroundWindow().ToInt64());
            List<TopWindowInfo> windows = GetTopWindowsInZOrder();
            for (int i = 0; i < windows.Count; i++)
            {
                TopWindowInfo window = windows[i];
                hash = MixGeometryHash(hash, window.Hwnd.ToInt64());
                hash = MixGeometryHash(hash, window.Rect.X);
                hash = MixGeometryHash(hash, window.Rect.Y);
                hash = MixGeometryHash(hash, window.Rect.Width);
                hash = MixGeometryHash(hash, window.Rect.Height);
                hash = MixGeometryHash(hash, window.Title == null ? 0 : window.Title.GetHashCode());
                hash = MixGeometryHash(hash, window.ClassName == null ? 0 : window.ClassName.GetHashCode());
            }
            return MixGeometryHash(hash, windows.Count);
        }
    }

    private static long MixGeometryHash(long hash, long value)
    {
        unchecked
        {
            return (hash ^ value) * 1099511628211L;
        }
    }

    private static void StartWinEventThread()
    {
        if (WinEventThread != null && WinEventThread.IsAlive)
        {
            return;
        }
        WinEventThread = new Thread(WinEventLoop);
        WinEventThread.IsBackground = true;
        WinEventThread.Name = "window-event-loop";
        WinEventThread.Start();
    }

    private static void WinEventLoop()
    {
        WinEventCallbackRef = OnWinEvent;
        AddWinEventHook(EventSystemForeground, EventSystemForeground);
        AddWinEventHook(EventSystemMinimizeStart, EventSystemMinimizeEnd);
        AddWinEventHook(EventObjectCreate, EventObjectCreate);
        AddWinEventHook(EventObjectDestroy, EventObjectDestroy);
        AddWinEventHook(EventObjectShow, EventObjectSelectionWithin);
        AddWinEventHook(EventObjectLocationChange, EventObjectLocationChange);
        AddWinEventHook(EventObjectNameChange, EventObjectNameChange);

        NativeMessage message;
        while (GetMessage(out message, IntPtr.Zero, 0, 0))
        {
            TranslateMessage(ref message);
            DispatchMessage(ref message);
        }
    }

    private static void AddWinEventHook(uint eventMin, uint eventMax)
    {
        IntPtr hook = SetWinEventHook(
            eventMin,
            eventMax,
            IntPtr.Zero,
            WinEventCallbackRef,
            0,
            0,
            WineventOutofcontext | WineventSkipownprocess);
        if (hook != IntPtr.Zero)
        {
            WinEventHooks.Add(hook);
        }
    }

    private static void OnWinEvent(
        IntPtr hWinEventHook,
        uint eventType,
        IntPtr hWnd,
        int idObject,
        int idChild,
        uint idEventThread,
        uint eventTime)
    {
        if (hWnd == IntPtr.Zero)
        {
            return;
        }
        bool structuralContentEvent =
            eventType >= EventObjectCreate &&
            eventType <= EventObjectReorder;
        bool softContentEvent =
            eventType >= EventObjectFocus &&
            eventType <= EventObjectSelectionWithin;
        bool contentEvent = structuralContentEvent || softContentEvent;
        if (softContentEvent)
        {
            long nowTicks = DateTime.UtcNow.Ticks;
            long lastMouseReleaseTicks = Interlocked.Read(ref LastMouseReleaseTicks);
            if (lastMouseReleaseTicks <= 0 ||
                nowTicks - lastMouseReleaseTicks > TimeSpan.FromMilliseconds(2500).Ticks)
            {
                return;
            }
            long previousTicks = Interlocked.Read(ref LastSoftContentEventTicks);
            if (previousTicks > 0 && nowTicks - previousTicks < TimeSpan.FromMilliseconds(700).Ticks)
            {
                return;
            }
            Interlocked.Exchange(ref LastSoftContentEventTicks, nowTicks);
        }
        if (idObject != ObjidWindow &&
            eventType != EventSystemForeground &&
            eventType != EventSystemMinimizeStart &&
            eventType != EventSystemMinimizeEnd &&
            !contentEvent)
        {
            if (!IsTaskbarShellClass(GetWindowClassName(hWnd)))
            {
                return;
            }
        }
        if (contentEvent || eventType == EventObjectLocationChange || eventType == EventObjectNameChange)
        {
            IntPtr root = GetAncestor(hWnd, GaRoot);
            if (root == IntPtr.Zero)
            {
                root = hWnd;
            }
            QueueDetailRefresh(root);
        }
        else if (eventType == EventSystemForeground)
        {
            QueueDetailRefresh(hWnd);
        }
        MarkSnapshotDirty();
    }

    private static CacheRequest BuildCacheRequest()
    {
        CacheRequest cache = new CacheRequest();
        cache.TreeScope = TreeScope.Element | TreeScope.Children;
        cache.AutomationElementMode = AutomationElementMode.Full;
        cache.Add(AutomationElement.NameProperty);
        cache.Add(AutomationElement.ControlTypeProperty);
        cache.Add(AutomationElement.BoundingRectangleProperty);
        cache.Add(AutomationElement.IsOffscreenProperty);
        cache.Add(AutomationElement.IsEnabledProperty);
        cache.Add(AutomationElement.AutomationIdProperty);
        cache.Add(AutomationElement.ClassNameProperty);
        cache.Add(AutomationElement.IsInvokePatternAvailableProperty);
        cache.Add(AutomationElement.IsValuePatternAvailableProperty);
        cache.Add(AutomationElement.IsTogglePatternAvailableProperty);
        cache.Add(AutomationElement.IsSelectionItemPatternAvailableProperty);
        cache.Add(AutomationElement.IsExpandCollapsePatternAvailableProperty);
        cache.Add(AutomationElement.IsRangeValuePatternAvailableProperty);
        cache.Add(AutomationElement.IsScrollItemPatternAvailableProperty);
        return cache;
    }

    private static string Scan(int maxItems, int visitLimit)
    {
        Stopwatch sw = Stopwatch.StartNew();
        IntPtr foregroundHwnd = GetForegroundWindow();
        List<TopWindowInfo> topWindows = GetTopWindowsInZOrder();
        List<Item> screenItems = GetVisibleScreenItems(topWindows);
        long geometryMs = sw.ElapsedMilliseconds;
        int shellVisited = 0;
        long desktopShellMs;
        long taskbarShellMs;
        List<Item> shellItems = GetVisibleShellItems(
            topWindows,
            maxItems,
            visitLimit,
            out shellVisited,
            out desktopShellMs,
            out taskbarShellMs);
        TopWindowInfo target = SelectDetailTarget(topWindows, foregroundHwnd);
        int visited = shellVisited;
        int processId = 0;
        string processName = "";
        List<Item> items = new List<Item>();
        string title = target == null ? "" : target.Title;
        RectInfo rootRect = target == null ? null : target.Rect;

        PruneDetailCaches(topWindows);

        if (target != null)
        {
            int targetProcessId;
            GetWindowThreadProcessId(target.Hwnd, out targetProcessId);
            processId = targetProcessId;
            try { processName = Process.GetProcessById(processId).ProcessName; } catch { }
            DetailCache refreshed;
            int targetVisited;
            if (TryScanWindowDetailsBounded(target, maxItems, visitLimit, out refreshed, out targetVisited))
            {
                visited += targetVisited;
                DetailCaches[target.Hwnd.ToInt64()] = refreshed;
            }
        }
        long detailMs = sw.ElapsedMilliseconds - geometryMs - desktopShellMs - taskbarShellMs;

        items.AddRange(screenItems);
        items.AddRange(shellItems);
        foreach (TopWindowInfo window in topWindows)
        {
            if (window.IsTaskbar)
            {
                continue;
            }
            DetailCache cached;
            if (!DetailCaches.TryGetValue(window.Hwnd.ToInt64(), out cached))
            {
                continue;
            }
            if (!SameRect(cached.WindowRect, window.Rect))
            {
                continue;
            }
            List<RectInfo> occluders = GetOccludersAbove(window.Hwnd);
            List<Item> visibleDetails = ClipItemsByOccluders(cached.Items, occluders);
            for (int i = 0; i < visibleDetails.Count; i++)
            {
                items.Add(visibleDetails[i]);
            }
        }

        for (int i = 0; i < items.Count; i++)
        {
            items[i].Id = i + 1;
        }

        sw.Stop();
        return BuildScanJson(
            "csharp-uia-worker",
            sw.ElapsedMilliseconds,
            target,
            title,
            processId,
            processName,
            rootRect,
            visited,
            geometryMs,
            desktopShellMs,
            taskbarShellMs,
            detailMs,
            items);
    }

    private static string ScanCachedGeometry()
    {
        Stopwatch sw = Stopwatch.StartNew();
        IntPtr foregroundHwnd = GetForegroundWindow();
        List<TopWindowInfo> topWindows = GetTopWindowsInZOrder();
        List<Item> items = GetVisibleScreenItems(topWindows);
        List<RectInfo> topOccluders = new List<RectInfo>();
        for (int i = 0; i < topWindows.Count; i++)
        {
            topOccluders.Add(topWindows[i].Rect);
        }

        foreach (DetailCache cache in ShellCaches.Values)
        {
            if (cache.Title.Equals("DesktopItem", StringComparison.Ordinal))
            {
                items.AddRange(ClipItemsByOccluders(cache.Items, topOccluders));
            }
            else if (cache.Title.Equals("TaskbarRoot", StringComparison.Ordinal) ||
                     cache.Title.Equals("TaskbarItem", StringComparison.Ordinal))
            {
                items.AddRange(ClipTaskbarItemsToVisible(cache.Items, topWindows));
            }
        }

        for (int i = 0; i < topWindows.Count; i++)
        {
            TopWindowInfo window = topWindows[i];
            if (window.IsTaskbar)
            {
                continue;
            }
            DetailCache cached;
            if (!DetailCaches.TryGetValue(window.Hwnd.ToInt64(), out cached) ||
                !SameRect(cached.WindowRect, window.Rect))
            {
                continue;
            }
            items.AddRange(ClipItemsByOccluders(cached.Items, GetOccludersAbove(window.Hwnd)));
        }

        TopWindowInfo target = FindTopWindow(topWindows, foregroundHwnd);
        int processId = 0;
        string processName = "";
        if (target != null)
        {
            GetWindowThreadProcessId(target.Hwnd, out processId);
            try { processName = Process.GetProcessById(processId).ProcessName; } catch { }
        }
        for (int i = 0; i < items.Count; i++)
        {
            items[i].Id = i + 1;
        }
        sw.Stop();
        return BuildScanJson(
            "csharp-uia-worker-geometry",
            sw.ElapsedMilliseconds,
            target,
            target == null ? "" : target.Title,
            processId,
            processName,
            target == null ? null : target.Rect,
            0,
            sw.ElapsedMilliseconds,
            0,
            0,
            0,
            items);
    }

    private static TopWindowInfo FindTopWindow(List<TopWindowInfo> topWindows, IntPtr hWnd)
    {
        for (int i = 0; i < topWindows.Count; i++)
        {
            if (topWindows[i].Hwnd == hWnd)
            {
                return topWindows[i];
            }
        }
        return null;
    }

    private static string BuildScanJson(
        string engine,
        long elapsedMs,
        TopWindowInfo target,
        string title,
        int processId,
        string processName,
        RectInfo rootRect,
        int visited,
        long geometryMs,
        long desktopShellMs,
        long taskbarShellMs,
        long detailMs,
        List<Item> items)
    {
        StringBuilder sb = new StringBuilder(8192);
        sb.Append("{\"ok\":true,\"engine\":");
        AppendJsonString(sb, engine);
        sb.Append(",\"elapsedMs\":");
        sb.Append(elapsedMs.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"hwnd\":\"0x");
        sb.Append((target == null ? IntPtr.Zero : target.Hwnd).ToInt64().ToString("X", CultureInfo.InvariantCulture));
        sb.Append("\",\"title\":");
        AppendJsonString(sb, title);
        sb.Append(",\"processId\":");
        sb.Append(processId.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"processName\":");
        AppendJsonString(sb, processName);
        sb.Append(",\"root\":");
        AppendRect(sb, rootRect);
        sb.Append(",\"count\":");
        sb.Append(items.Count.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"visited\":");
        sb.Append(visited.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"geometryMs\":");
        sb.Append(geometryMs.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"desktopShellMs\":");
        sb.Append(desktopShellMs.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"taskbarShellMs\":");
        sb.Append(taskbarShellMs.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"detailMs\":");
        sb.Append(detailMs.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"items\":[");
        for (int i = 0; i < items.Count; i++)
        {
            if (i > 0) sb.Append(',');
            AppendItem(sb, items[i]);
        }
        sb.Append("]}");
        return sb.ToString();
    }

    private static bool TryScanWindowDetails(TopWindowInfo target, int maxItems, int visitLimit, out DetailCache cacheResult, out int visited)
    {
        cacheResult = null;
        visited = 0;
        CacheRequest cache = BuildCacheRequest();
        AutomationElement root;
        using (cache.Activate())
        {
            root = AutomationElement.FromHandle(target.Hwnd);
        }
        if (root == null)
        {
            return false;
        }

        RectInfo rootRect = GetRect(root) ?? target.Rect;
        double rootArea = rootRect == null ? 0 : (double)rootRect.Width * rootRect.Height;
        List<Item> items = new List<Item>();
        Queue<AutomationElement> queue = new Queue<AutomationElement>();
        HashSet<string> seen = new HashSet<string>(StringComparer.Ordinal);
        queue.Enqueue(root);
        TreeWalker walker = TreeWalker.ControlViewWalker;

        while (queue.Count > 0 && visited < visitLimit && items.Count < maxItems)
        {
            AutomationElement element = queue.Dequeue();
            visited++;

            try
            {
                using (cache.Activate())
                {
                    AutomationElement child = walker.GetFirstChild(element, cache);
                    while (child != null && queue.Count < visitLimit)
                    {
                        queue.Enqueue(child);
                        child = walker.GetNextSibling(child, cache);
                    }
                }
            }
            catch { }

            if (visited == 1)
            {
                continue;
            }
            if (GetBool(element, AutomationElement.IsOffscreenProperty, false))
            {
                continue;
            }
            if (!GetBool(element, AutomationElement.IsEnabledProperty, true))
            {
                continue;
            }

            RectInfo rect = GetRect(element);
            if (rect == null)
            {
                continue;
            }

            string control = GetControlName(element);
            if (String.IsNullOrWhiteSpace(control) || control.Equals("Text", StringComparison.Ordinal))
            {
                continue;
            }

            List<string> patterns = GetPatterns(element);
            int actionPatternCount = 0;
            for (int i = 0; i < patterns.Count; i++)
            {
                if (patterns[i] != "ScrollItem")
                {
                    actionPatternCount++;
                }
            }

            bool usefulControl = WantedControls.Contains(control);
            if (!usefulControl && actionPatternCount == 0)
            {
                continue;
            }

            string name = Normalize(GetString(element, AutomationElement.NameProperty));
            if (String.IsNullOrWhiteSpace(name) && actionPatternCount == 0 && !BlankNameAllowed.Contains(control))
            {
                continue;
            }

            double area = (double)rect.Width * rect.Height;
            if (rootArea > 0 && area > rootArea * 0.72 && actionPatternCount == 0)
            {
                continue;
            }

            string className = GetString(element, AutomationElement.ClassNameProperty);
            if (className.IndexOf("monaco-sash", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                continue;
            }
            if (className.IndexOf("statusbar-item", StringComparison.OrdinalIgnoreCase) >= 0 ||
                className.IndexOf("monaco-icon-label", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                continue;
            }
            if (control.Equals("ToolBar", StringComparison.Ordinal))
            {
                continue;
            }
            if (className.Equals("WinCaptionButtonContainer", StringComparison.OrdinalIgnoreCase) ||
                className.Equals("menubar-menu-button", StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }
            if (ContainerControls.Contains(control))
            {
                if (String.IsNullOrWhiteSpace(name))
                {
                    continue;
                }
                if (rootArea > 0 && area > rootArea * 0.18)
                {
                    continue;
                }
            }

            string key = rect.X + "," + rect.Y + "," + rect.Width + "," + rect.Height + "," + control + "," + name;
            if (!seen.Add(key))
            {
                continue;
            }

            string label = String.IsNullOrWhiteSpace(name) ? control : name;
            if (label.Length > 42)
            {
                label = label.Substring(0, 42) + "...";
            }

            Item item = new Item();
            item.Label = label;
            item.Name = name;
            item.Control = control;
            item.X = rect.X;
            item.Y = rect.Y;
            item.Width = rect.Width;
            item.Height = rect.Height;
            item.CenterX = rect.X + rect.Width / 2;
            item.CenterY = rect.Y + rect.Height / 2;
            item.Patterns = patterns;
            item.AutomationId = GetString(element, AutomationElement.AutomationIdProperty);
            item.ClassName = className;
            items.Add(item);
        }

        items = PruneContainerItems(items);
        cacheResult = new DetailCache();
        cacheResult.Hwnd = target.Hwnd.ToInt64();
        cacheResult.WindowRect = target.Rect;
        cacheResult.Title = target.Title;
        cacheResult.ClassName = target.ClassName;
        cacheResult.Items = items;
        cacheResult.UpdatedAtTicks = DateTime.UtcNow.Ticks;
        return true;
    }

    private static bool TryScanWindowDetailsBounded(
        TopWindowInfo target,
        int maxItems,
        int visitLimit,
        out DetailCache cacheResult,
        out int visited)
    {
        cacheResult = null;
        visited = 0;
        if (target == null)
        {
            return false;
        }

        long key = target.Hwnd.ToInt64();
        long nowTicks = DateTime.UtcNow.Ticks;
        long timeoutUntil;
        if (DetailTimeoutUntilTicks.TryGetValue(key, out timeoutUntil) && nowTicks < timeoutUntil)
        {
            return false;
        }

        DetailCache threadCache = null;
        int threadVisited = 0;
        bool threadResult = false;
        Thread thread = new Thread(delegate()
        {
            try
            {
                threadResult = TryScanWindowDetails(
                    target,
                    maxItems,
                    visitLimit,
                    out threadCache,
                    out threadVisited);
            }
            catch { }
        });
        thread.IsBackground = true;
        thread.Name = "uia-detail-scan";
        try { thread.SetApartmentState(ApartmentState.MTA); } catch { }
        thread.Start();

        if (!thread.Join(DetailScanTimeoutMs))
        {
            DetailTimeoutUntilTicks[key] = nowTicks + DetailTimeoutCooldownTicks;
            return false;
        }

        DetailTimeoutUntilTicks.Remove(key);
        cacheResult = threadCache;
        visited = threadVisited;
        return threadResult;
    }

    private static TopWindowInfo SelectDetailTarget(List<TopWindowInfo> topWindows, IntPtr foregroundHwnd)
    {
        if (topWindows.Count == 0)
        {
            return null;
        }
        long forcedHwnd = Interlocked.Exchange(ref ForcedDetailHwnd, 0);
        if (forcedHwnd != 0)
        {
            for (int i = 0; i < topWindows.Count; i++)
            {
                if (!topWindows[i].IsTaskbar &&
                    !IsUnsafeDetailWindow(topWindows[i]) &&
                    topWindows[i].Hwnd.ToInt64() == forcedHwnd)
                {
                    return topWindows[i];
                }
            }
        }
        long priorityHwnd = TakeDetailRefresh();
        if (priorityHwnd != 0)
        {
            for (int i = 0; i < topWindows.Count; i++)
            {
                if (!topWindows[i].IsTaskbar &&
                    !IsUnsafeDetailWindow(topWindows[i]) &&
                    topWindows[i].Hwnd.ToInt64() == priorityHwnd)
                {
                    return topWindows[i];
                }
            }
        }
        for (int i = 0; i < topWindows.Count; i++)
        {
            if (!topWindows[i].IsTaskbar &&
                !IsUnsafeDetailWindow(topWindows[i]) &&
                topWindows[i].Hwnd == foregroundHwnd)
            {
                DetailCache cached;
                if (!DetailCaches.TryGetValue(topWindows[i].Hwnd.ToInt64(), out cached) ||
                    !SameRect(cached.WindowRect, topWindows[i].Rect))
                {
                    return topWindows[i];
                }
                break;
            }
        }
        int tries = topWindows.Count;
        while (tries > 0)
        {
            int index = detailCursor % topWindows.Count;
            detailCursor++;
            tries--;
            if (!topWindows[index].IsTaskbar && !IsUnsafeDetailWindow(topWindows[index]))
            {
                return topWindows[index];
            }
        }
        return null;
    }

    private static bool IsUnsafeDetailWindow(TopWindowInfo window)
    {
        if (window == null)
        {
            return true;
        }
        return window.ClassName.Equals("Ghost", StringComparison.OrdinalIgnoreCase) ||
               window.ClassName.IndexOf("ApplicationFrameInputSinkWindow", StringComparison.OrdinalIgnoreCase) >= 0;
    }

    private static void PruneDetailCaches(List<TopWindowInfo> topWindows)
    {
        HashSet<long> visible = new HashSet<long>();
        for (int i = 0; i < topWindows.Count; i++)
        {
            if (!topWindows[i].IsTaskbar)
            {
                visible.Add(topWindows[i].Hwnd.ToInt64());
            }
        }
        List<long> remove = new List<long>();
        foreach (long key in DetailCaches.Keys)
        {
            if (!visible.Contains(key))
            {
                remove.Add(key);
            }
        }
        for (int i = 0; i < remove.Count; i++)
        {
            DetailCaches.Remove(remove[i]);
            DetailTimeoutUntilTicks.Remove(remove[i]);
        }
    }

    private static bool SameRect(RectInfo a, RectInfo b)
    {
        if (a == null || b == null)
        {
            return false;
        }
        return a.X == b.X && a.Y == b.Y && a.Width == b.Width && a.Height == b.Height;
    }

    private static List<Item> PruneContainerItems(List<Item> items)
    {
        if (items.Count < 2)
        {
            return items;
        }
        List<Item> kept = new List<Item>(items.Count);
        for (int i = 0; i < items.Count; i++)
        {
            Item candidate = items[i];
            if (!IsContainerLike(candidate))
            {
                kept.Add(candidate);
                continue;
            }

            bool containsSpecificChild = false;
            double candidateArea = Math.Max(1, candidate.Width * candidate.Height);
            for (int j = 0; j < items.Count; j++)
            {
                if (i == j)
                {
                    continue;
                }
                Item other = items[j];
                if (IsContainerLike(other))
                {
                    continue;
                }
                double otherArea = Math.Max(1, other.Width * other.Height);
                if (candidateArea <= otherArea * 1.35)
                {
                    continue;
                }
                if (ContainsCenter(candidate, other))
                {
                    containsSpecificChild = true;
                    break;
                }
            }

            if (!containsSpecificChild)
            {
                kept.Add(candidate);
            }
        }

        for (int i = 0; i < kept.Count; i++)
        {
            kept[i].Id = i + 1;
        }
        return kept;
    }

    private static bool IsContainerLike(Item item)
    {
        return item.Control == "Group" ||
               item.Control == "Pane" ||
               item.Control == "Document" ||
               item.Control == "ToolBar" ||
               item.Control == "List" ||
               item.Control == "Tab";
    }

    private static bool ContainsCenter(Item container, Item child)
    {
        return child.CenterX >= container.X &&
               child.CenterX <= container.X + container.Width &&
               child.CenterY >= container.Y &&
               child.CenterY <= container.Y + container.Height;
    }

    private static string ErrorJson(string message)
    {
        StringBuilder sb = new StringBuilder();
        sb.Append("{\"ok\":false,\"engine\":\"csharp-uia-worker\",\"error\":");
        AppendJsonString(sb, message);
        sb.Append(",\"items\":[]}");
        return sb.ToString();
    }

    private static List<Item> GetVisibleShellItems(
        List<TopWindowInfo> topWindows,
        int maxItems,
        int visitLimit,
        out int visited,
        out long desktopMs,
        out long taskbarMs)
    {
        Stopwatch shellWatch = Stopwatch.StartNew();
        visited = 0;
        desktopMs = 0;
        taskbarMs = 0;
        List<Item> result = new List<Item>();
        HashSet<string> validCacheKeys = new HashSet<string>(StringComparer.Ordinal);
        List<RectInfo> topOccluders = new List<RectInfo>();
        for (int i = 0; i < topWindows.Count; i++)
        {
            topOccluders.Add(topWindows[i].Rect);
        }

        int desktopMaxItems = Math.Max(80, Math.Min(180, maxItems * 3));
        int desktopVisitLimit = Math.Max(500, Math.Min(1600, visitLimit * 8));
        List<IntPtr> desktopHandles = GetDesktopSurfaceHandles();
        for (int i = 0; i < desktopHandles.Count; i++)
        {
            RectInfo surfaceRect;
            if (!TryGetRawWindowRect(desktopHandles[i], out surfaceRect))
            {
                surfaceRect = GetVirtualScreenRect();
            }

            int shellVisited;
            string cacheKey;
            List<Item> raw = GetCachedShellSurfaceItems(
                desktopHandles[i],
                surfaceRect,
                "DesktopItem",
                desktopMaxItems,
                desktopVisitLimit,
                DesktopShellCacheTtlTicks,
                out shellVisited,
                out cacheKey);
            validCacheKeys.Add(cacheKey);
            visited += shellVisited;
            result.AddRange(ClipItemsByOccluders(raw, topOccluders));
        }
        desktopMs = shellWatch.ElapsedMilliseconds;

        if (HasTaskbar(topWindows))
        {
            int shellVisited;
            string cacheKey;
            List<Item> raw = GetCachedRootTaskbarItems(topWindows, maxItems, visitLimit, out shellVisited, out cacheKey);
            validCacheKeys.Add(cacheKey);
            visited += shellVisited;
            result.AddRange(ClipTaskbarItemsToVisible(raw, topWindows));
        }
        taskbarMs = shellWatch.ElapsedMilliseconds - desktopMs;

        PruneShellCaches(validCacheKeys);
        return result;
    }

    private static List<Item> GetCachedShellSurfaceItems(
        IntPtr hWnd,
        RectInfo surfaceRect,
        string surfaceKind,
        int maxItems,
        int visitLimit,
        long ttlTicks,
        out int visited,
        out string cacheKey)
    {
        visited = 0;
        cacheKey = GetShellCacheKey(surfaceKind, hWnd);
        DetailCache cached = null;
        long now = DateTime.UtcNow.Ticks;
        if (ShellCaches.TryGetValue(cacheKey, out cached) &&
            SameRect(cached.WindowRect, surfaceRect) &&
            now - cached.UpdatedAtTicks < ttlTicks)
        {
            return cached.Items;
        }

        DetailCache refreshed;
        int shellVisited;
        if (TryScanShellSurfaceDetails(hWnd, surfaceRect, surfaceKind, maxItems, visitLimit, out refreshed, out shellVisited))
        {
            visited = shellVisited;
            ShellCaches[cacheKey] = refreshed;
            return refreshed.Items;
        }

        if (cached != null)
        {
            return cached.Items;
        }
        return new List<Item>();
    }

    private static List<Item> GetCachedRootTaskbarItems(
        List<TopWindowInfo> topWindows,
        int maxItems,
        int visitLimit,
        out int visited,
        out string cacheKey)
    {
        visited = 0;
        cacheKey = "TaskbarRoot:0";
        RectInfo surfaceRect = GetTaskbarUnionRect(topWindows);
        DetailCache cached = null;
        long now = DateTime.UtcNow.Ticks;
        if (surfaceRect != null &&
            ShellCaches.TryGetValue(cacheKey, out cached) &&
            SameRect(cached.WindowRect, surfaceRect) &&
            now - cached.UpdatedAtTicks < TaskbarShellCacheTtlTicks)
        {
            return cached.Items;
        }

        DetailCache refreshed;
        int taskbarVisited;
        if (surfaceRect != null &&
            TryScanRootTaskbarDetails(topWindows, surfaceRect, maxItems, visitLimit, out refreshed, out taskbarVisited))
        {
            visited = taskbarVisited;
            ShellCaches[cacheKey] = refreshed;
            return refreshed.Items;
        }

        if (cached != null)
        {
            return cached.Items;
        }
        return new List<Item>();
    }

    private static bool TryScanRootTaskbarDetails(
        List<TopWindowInfo> topWindows,
        RectInfo surfaceRect,
        int maxItems,
        int visitLimit,
        out DetailCache cacheResult,
        out int visited)
    {
        cacheResult = null;
        visited = 0;
        if (surfaceRect == null)
        {
            return false;
        }

        CacheRequest cache = BuildCacheRequest();
        AutomationElement root;
        try
        {
            root = AutomationElement.RootElement;
        }
        catch
        {
            return false;
        }
        if (root == null)
        {
            return false;
        }

        int taskbarMaxItems = Math.Max(90, Math.Min(220, maxItems * 4));
        int taskbarVisitLimit = Math.Max(1200, Math.Min(2200, visitLimit * 14));
        List<Item> items = new List<Item>();
        Queue<ElementDepth> queue = new Queue<ElementDepth>();
        HashSet<string> seen = new HashSet<string>(StringComparer.Ordinal);
        TreeWalker walker = TreeWalker.RawViewWalker;
        Stopwatch taskbarWatch = Stopwatch.StartNew();
        queue.Enqueue(new ElementDepth(root, 0));

        while (queue.Count > 0 &&
               visited < taskbarVisitLimit &&
               items.Count < taskbarMaxItems &&
               taskbarWatch.ElapsedMilliseconds < 1800)
        {
            ElementDepth node = queue.Dequeue();
            AutomationElement element = node.Element;
            visited++;

            try
            {
                using (cache.Activate())
                {
                    AutomationElement child = walker.GetFirstChild(element, cache);
                    while (child != null && queue.Count < taskbarVisitLimit)
                    {
                        if (ShouldVisitTaskbarBranch(child, topWindows, node.Depth + 1))
                        {
                            queue.Enqueue(new ElementDepth(child, node.Depth + 1));
                        }
                        child = walker.GetNextSibling(child, cache);
                    }
                }
            }
            catch { }

            if (visited == 1)
            {
                continue;
            }
            if (GetBool(element, AutomationElement.IsOffscreenProperty, false))
            {
                continue;
            }

            RectInfo rect = GetRect(element);
            if (rect == null)
            {
                continue;
            }
            RectInfo clipped = IntersectTaskbarRect(rect, topWindows);
            if (clipped == null)
            {
                continue;
            }

            string control = GetControlName(element);
            if (String.IsNullOrWhiteSpace(control) || control.Equals("Text", StringComparison.Ordinal))
            {
                continue;
            }
            string className = GetString(element, AutomationElement.ClassNameProperty);
            string name = Normalize(GetString(element, AutomationElement.NameProperty));
            List<string> patterns = GetPatterns(element);
            if (!IsRootTaskbarUseful(control, className, name, patterns))
            {
                continue;
            }

            string key = clipped.X + "," + clipped.Y + "," + clipped.Width + "," + clipped.Height + "," + control + "," + className + "," + name;
            if (!seen.Add(key))
            {
                continue;
            }

            string label = String.IsNullOrWhiteSpace(name) ? control : name;
            if (label.Length > 42)
            {
                label = label.Substring(0, 42) + "...";
            }

            Item item = new Item();
            item.Label = label;
            item.Name = name;
            item.Control = "TaskbarItem";
            item.X = clipped.X;
            item.Y = clipped.Y;
            item.Width = clipped.Width;
            item.Height = clipped.Height;
            item.CenterX = clipped.X + clipped.Width / 2;
            item.CenterY = clipped.Y + clipped.Height / 2;
            item.Patterns = patterns;
            item.AutomationId = GetString(element, AutomationElement.AutomationIdProperty);
            item.ClassName = "TaskbarItem " + control + " " + className;
            items.Add(item);
        }

        cacheResult = new DetailCache();
        cacheResult.Hwnd = 0;
        cacheResult.WindowRect = surfaceRect;
        cacheResult.Title = "TaskbarRoot";
        cacheResult.ClassName = "RootElement";
        cacheResult.Items = items;
        cacheResult.UpdatedAtTicks = DateTime.UtcNow.Ticks;
        return true;
    }

    private static bool TryScanShellSurfaceDetails(
        IntPtr hWnd,
        RectInfo surfaceRect,
        string surfaceKind,
        int maxItems,
        int visitLimit,
        out DetailCache cacheResult,
        out int visited)
    {
        cacheResult = null;
        visited = 0;
        if (hWnd == IntPtr.Zero || surfaceRect == null)
        {
            return false;
        }

        CacheRequest cache = BuildCacheRequest();
        AutomationElement root;
        try
        {
            using (cache.Activate())
            {
                root = AutomationElement.FromHandle(hWnd);
            }
        }
        catch
        {
            return false;
        }
        if (root == null)
        {
            return false;
        }

        double surfaceArea = Math.Max(1, (double)surfaceRect.Width * surfaceRect.Height);
        List<Item> items = new List<Item>();
        Queue<AutomationElement> queue = new Queue<AutomationElement>();
        HashSet<string> seen = new HashSet<string>(StringComparer.Ordinal);
        TreeWalker walker = surfaceKind.Equals("TaskbarItem", StringComparison.Ordinal)
            ? TreeWalker.RawViewWalker
            : TreeWalker.ControlViewWalker;
        queue.Enqueue(root);

        while (queue.Count > 0 && visited < visitLimit && items.Count < maxItems)
        {
            AutomationElement element = queue.Dequeue();
            visited++;

            try
            {
                using (cache.Activate())
                {
                    AutomationElement child = walker.GetFirstChild(element, cache);
                    while (child != null && queue.Count < visitLimit)
                    {
                        queue.Enqueue(child);
                        child = walker.GetNextSibling(child, cache);
                    }
                }
            }
            catch { }

            if (visited == 1)
            {
                continue;
            }
            if (GetBool(element, AutomationElement.IsOffscreenProperty, false))
            {
                continue;
            }

            RectInfo rect = GetRect(element);
            if (rect == null)
            {
                continue;
            }
            RectInfo clipped = IntersectRect(rect, surfaceRect);
            if (clipped == null)
            {
                continue;
            }

            string control = GetControlName(element);
            if (String.IsNullOrWhiteSpace(control) || control.Equals("Text", StringComparison.Ordinal))
            {
                continue;
            }
            string className = GetString(element, AutomationElement.ClassNameProperty);
            string name = Normalize(GetString(element, AutomationElement.NameProperty));
            List<string> patterns = GetPatterns(element);
            if (!IsShellSurfaceUseful(surfaceKind, control, className, name, patterns))
            {
                continue;
            }
            if (String.IsNullOrWhiteSpace(name) && patterns.Count == 0)
            {
                continue;
            }

            double area = (double)clipped.Width * clipped.Height;
            if (area > surfaceArea * 0.42)
            {
                continue;
            }

            string key = clipped.X + "," + clipped.Y + "," + clipped.Width + "," + clipped.Height + "," + control + "," + name;
            if (!seen.Add(key))
            {
                continue;
            }

            string label = String.IsNullOrWhiteSpace(name) ? control : name;
            if (label.Length > 42)
            {
                label = label.Substring(0, 42) + "...";
            }

            Item item = new Item();
            item.Label = label;
            item.Name = name;
            item.Control = surfaceKind;
            item.X = clipped.X;
            item.Y = clipped.Y;
            item.Width = clipped.Width;
            item.Height = clipped.Height;
            item.CenterX = clipped.X + clipped.Width / 2;
            item.CenterY = clipped.Y + clipped.Height / 2;
            item.Patterns = patterns;
            item.AutomationId = GetString(element, AutomationElement.AutomationIdProperty);
            item.ClassName = surfaceKind + " " + control + " " + className;
            items.Add(item);
        }

        cacheResult = new DetailCache();
        cacheResult.Hwnd = hWnd.ToInt64();
        cacheResult.WindowRect = surfaceRect;
        cacheResult.Title = surfaceKind;
        cacheResult.ClassName = GetWindowClassName(hWnd);
        cacheResult.Items = items;
        cacheResult.UpdatedAtTicks = DateTime.UtcNow.Ticks;
        return true;
    }

    private static bool IsShellSurfaceUseful(string surfaceKind, string control, string className, string name, List<string> patterns)
    {
        if (ContainerControls.Contains(control) || control.Equals("ToolBar", StringComparison.Ordinal))
        {
            return false;
        }
        if (surfaceKind.Equals("DesktopItem", StringComparison.Ordinal))
        {
            if (control.Equals("ListItem", StringComparison.Ordinal) ||
                control.Equals("Button", StringComparison.Ordinal) ||
                control.Equals("DataItem", StringComparison.Ordinal) ||
                control.Equals("TreeItem", StringComparison.Ordinal))
            {
                return true;
            }
            return !String.IsNullOrWhiteSpace(name) &&
                   (HasPattern(patterns, "Invoke") || HasPattern(patterns, "SelectionItem"));
        }

        if (surfaceKind.Equals("TaskbarItem", StringComparison.Ordinal))
        {
            if (control.Equals("Button", StringComparison.Ordinal) ||
                control.Equals("ListItem", StringComparison.Ordinal) ||
                control.Equals("TabItem", StringComparison.Ordinal) ||
                control.Equals("MenuItem", StringComparison.Ordinal) ||
                control.Equals("Hyperlink", StringComparison.Ordinal) ||
                control.Equals("DataItem", StringComparison.Ordinal) ||
                control.Equals("SplitButton", StringComparison.Ordinal))
            {
                return true;
            }
            return !String.IsNullOrWhiteSpace(name) && HasPattern(patterns, "Invoke");
        }

        return false;
    }

    private static bool IsRootTaskbarUseful(string control, string className, string name, List<string> patterns)
    {
        if (control.Equals("Text", StringComparison.Ordinal) ||
            control.Equals("Image", StringComparison.Ordinal) ||
            control.Equals("Pane", StringComparison.Ordinal) ||
            control.Equals("Group", StringComparison.Ordinal) ||
            control.Equals("ToolBar", StringComparison.Ordinal))
        {
            return false;
        }

        bool named = !String.IsNullOrWhiteSpace(name);
        bool shellClass =
            className.IndexOf("Taskbar.", StringComparison.OrdinalIgnoreCase) >= 0 ||
            className.IndexOf("SystemTray.", StringComparison.OrdinalIgnoreCase) >= 0 ||
            className.IndexOf("ToggleButton", StringComparison.OrdinalIgnoreCase) >= 0 ||
            className.Equals("Button", StringComparison.OrdinalIgnoreCase);

        if (control.Equals("Button", StringComparison.Ordinal) ||
            control.Equals("SplitButton", StringComparison.Ordinal) ||
            control.Equals("MenuItem", StringComparison.Ordinal) ||
            control.Equals("ListItem", StringComparison.Ordinal) ||
            control.Equals("TabItem", StringComparison.Ordinal) ||
            control.Equals("Hyperlink", StringComparison.Ordinal) ||
            control.Equals("DataItem", StringComparison.Ordinal))
        {
            return named || shellClass || HasPattern(patterns, "Invoke");
        }

        return named && HasPattern(patterns, "Invoke") && shellClass;
    }

    private static bool ShouldVisitTaskbarBranch(AutomationElement element, List<TopWindowInfo> topWindows, int depth)
    {
        if (depth <= 1)
        {
            return true;
        }

        string className = GetString(element, AutomationElement.ClassNameProperty);
        if (IsTaskbarShellClass(className))
        {
            return true;
        }

        RectInfo rect = GetRect(element);
        if (rect == null)
        {
            return depth < 4;
        }

        if (IntersectTaskbarRect(rect, topWindows) == null)
        {
            return false;
        }

        string control = GetControlName(element);
        if (control.Equals("Pane", StringComparison.Ordinal) ||
            control.Equals("Group", StringComparison.Ordinal) ||
            control.Equals("Window", StringComparison.Ordinal) ||
            control.Equals("ToolBar", StringComparison.Ordinal) ||
            control.Equals("MenuBar", StringComparison.Ordinal))
        {
            return true;
        }

        if (IsRootTaskbarUseful(control, className, Normalize(GetString(element, AutomationElement.NameProperty)), GetPatterns(element)))
        {
            return true;
        }

        return depth < 5 && ((rect.Width >= 600 && rect.Height >= 40) || rect.Height >= 70);
    }

    private static bool IsTaskbarShellClass(string className)
    {
        if (String.IsNullOrWhiteSpace(className))
        {
            return false;
        }
        return className.IndexOf("Taskbar", StringComparison.OrdinalIgnoreCase) >= 0 ||
               className.IndexOf("SystemTray", StringComparison.OrdinalIgnoreCase) >= 0 ||
               className.IndexOf("Shell_TrayWnd", StringComparison.OrdinalIgnoreCase) >= 0 ||
               className.IndexOf("TrayNotifyWnd", StringComparison.OrdinalIgnoreCase) >= 0 ||
               className.IndexOf("MSTaskSwWClass", StringComparison.OrdinalIgnoreCase) >= 0 ||
               className.IndexOf("DesktopWindowXamlSource", StringComparison.OrdinalIgnoreCase) >= 0 ||
               className.IndexOf("DesktopWindowContentBridge", StringComparison.OrdinalIgnoreCase) >= 0 ||
               className.IndexOf("Windows.UI.Input.InputSite.WindowClass", StringComparison.OrdinalIgnoreCase) >= 0;
    }

    private static bool HasTaskbar(List<TopWindowInfo> topWindows)
    {
        for (int i = 0; i < topWindows.Count; i++)
        {
            if (topWindows[i].IsTaskbar)
            {
                return true;
            }
        }
        return false;
    }

    private static RectInfo GetTaskbarUnionRect(List<TopWindowInfo> topWindows)
    {
        bool found = false;
        int left = 0;
        int top = 0;
        int right = 0;
        int bottom = 0;
        for (int i = 0; i < topWindows.Count; i++)
        {
            TopWindowInfo window = topWindows[i];
            if (!window.IsTaskbar || window.Rect == null)
            {
                continue;
            }
            if (!found)
            {
                left = window.Rect.X;
                top = window.Rect.Y;
                right = window.Rect.Right;
                bottom = window.Rect.Bottom;
                found = true;
            }
            else
            {
                left = Math.Min(left, window.Rect.X);
                top = Math.Min(top, window.Rect.Y);
                right = Math.Max(right, window.Rect.Right);
                bottom = Math.Max(bottom, window.Rect.Bottom);
            }
        }
        if (!found || right - left < MinVisiblePiece || bottom - top < MinVisiblePiece)
        {
            return null;
        }
        return new RectInfo { X = left, Y = top, Width = right - left, Height = bottom - top };
    }

    private static RectInfo IntersectTaskbarRect(RectInfo rect, List<TopWindowInfo> topWindows)
    {
        RectInfo best = null;
        int bestArea = 0;
        for (int i = 0; i < topWindows.Count; i++)
        {
            TopWindowInfo window = topWindows[i];
            if (!window.IsTaskbar)
            {
                continue;
            }
            RectInfo intersect = IntersectRect(rect, window.Rect);
            if (intersect == null)
            {
                continue;
            }
            int area = intersect.Width * intersect.Height;
            if (area > bestArea)
            {
                bestArea = area;
                best = intersect;
            }
        }
        return best;
    }

    private static List<Item> ClipTaskbarItemsToVisible(List<Item> items, List<TopWindowInfo> topWindows)
    {
        if (items.Count == 0)
        {
            return items;
        }
        List<Item> result = new List<Item>(items.Count);
        Dictionary<long, List<RectInfo>> occluderCache = new Dictionary<long, List<RectInfo>>();
        for (int i = 0; i < items.Count; i++)
        {
            Item item = items[i];
            TopWindowInfo taskbar = FindTaskbarForItem(item, topWindows);
            if (taskbar == null)
            {
                result.Add(item);
                continue;
            }
            long key = taskbar.Hwnd.ToInt64();
            List<RectInfo> occluders;
            if (!occluderCache.TryGetValue(key, out occluders))
            {
                occluders = GetOccludersAbove(taskbar.Hwnd);
                occluderCache[key] = occluders;
            }
            List<Item> one = new List<Item>(1);
            one.Add(item);
            result.AddRange(ClipItemsByOccluders(one, occluders));
        }
        return result;
    }

    private static TopWindowInfo FindTaskbarForItem(Item item, List<TopWindowInfo> topWindows)
    {
        RectInfo rect = new RectInfo { X = item.X, Y = item.Y, Width = item.Width, Height = item.Height };
        TopWindowInfo best = null;
        int bestArea = 0;
        for (int i = 0; i < topWindows.Count; i++)
        {
            TopWindowInfo window = topWindows[i];
            if (!window.IsTaskbar)
            {
                continue;
            }
            RectInfo intersect = IntersectRect(rect, window.Rect);
            if (intersect == null)
            {
                continue;
            }
            int area = intersect.Width * intersect.Height;
            if (area > bestArea)
            {
                bestArea = area;
                best = window;
            }
        }
        return best;
    }

    private static bool HasPattern(List<string> patterns, string pattern)
    {
        for (int i = 0; i < patterns.Count; i++)
        {
            if (patterns[i].Equals(pattern, StringComparison.Ordinal))
            {
                return true;
            }
        }
        return false;
    }

    private static List<IntPtr> GetDesktopSurfaceHandles()
    {
        List<IntPtr> handles = new List<IntPtr>();
        HashSet<long> seen = new HashSet<long>();
        for (IntPtr hWnd = GetTopWindow(IntPtr.Zero); hWnd != IntPtr.Zero; hWnd = GetWindow(hWnd, GwHwndNext))
        {
            string className = GetWindowClassName(hWnd);
            if (!className.Equals("Progman", StringComparison.Ordinal) &&
                !className.Equals("WorkerW", StringComparison.Ordinal))
            {
                continue;
            }

            IntPtr defView = FindWindowExW(hWnd, IntPtr.Zero, "SHELLDLL_DefView", null);
            if (defView == IntPtr.Zero)
            {
                continue;
            }
            IntPtr listView = FindWindowExW(defView, IntPtr.Zero, "SysListView32", null);
            AddUniqueHandle(handles, seen, listView == IntPtr.Zero ? defView : listView);
        }
        return handles;
    }

    private static void AddUniqueHandle(List<IntPtr> handles, HashSet<long> seen, IntPtr hWnd)
    {
        if (hWnd == IntPtr.Zero)
        {
            return;
        }
        long key = hWnd.ToInt64();
        if (seen.Add(key))
        {
            handles.Add(hWnd);
        }
    }

    private static bool TryGetRawWindowRect(IntPtr hWnd, out RectInfo rect)
    {
        rect = null;
        if (hWnd == IntPtr.Zero || !IsWindowVisible(hWnd))
        {
            return false;
        }
        WinRect wr;
        if (!GetWindowRect(hWnd, out wr))
        {
            return false;
        }
        int width = wr.Right - wr.Left;
        int height = wr.Bottom - wr.Top;
        if (width < 6 || height < 6)
        {
            return false;
        }
        rect = new RectInfo { X = wr.Left, Y = wr.Top, Width = width, Height = height };
        return true;
    }

    private static string GetShellCacheKey(string surfaceKind, IntPtr hWnd)
    {
        return surfaceKind + ":" + hWnd.ToInt64().ToString(CultureInfo.InvariantCulture);
    }

    private static void PruneShellCaches(HashSet<string> validKeys)
    {
        List<string> remove = new List<string>();
        foreach (string key in ShellCaches.Keys)
        {
            if (!validKeys.Contains(key))
            {
                remove.Add(key);
            }
        }
        for (int i = 0; i < remove.Count; i++)
        {
            ShellCaches.Remove(remove[i]);
        }
    }

    private static List<Item> GetVisibleScreenItems(List<TopWindowInfo> topWindows)
    {
        List<Item> windows = new List<Item>();
        List<RectInfo> occluders = new List<RectInfo>();

        for (int i = 0; i < topWindows.Count; i++)
        {
            TopWindowInfo top = topWindows[i];
            List<RectInfo> pieces = SubtractOcclusions(top.Rect, occluders);

            for (int j = 0; j < pieces.Count; j++)
            {
                RectInfo piece = pieces[j];
                if (!IsMeaningfulStructuralPiece(piece))
                {
                    continue;
                }
                Item window = new Item();
                window.Label = String.IsNullOrWhiteSpace(top.Title) ? top.ClassName : top.Title;
                window.Name = top.Title;
                window.Control = top.IsTaskbar ? "Taskbar" : "Window";
                window.X = piece.X;
                window.Y = piece.Y;
                window.Width = piece.Width;
                window.Height = piece.Height;
                window.CenterX = piece.X + piece.Width / 2;
                window.CenterY = piece.Y + piece.Height / 2;
                window.ClassName = (top.IsTaskbar ? "VisibleTaskbar " : "VisibleWindowPiece ") + top.ClassName;
                if (!top.IsTaskbar)
                {
                    window.ClassName += GetOriginalEdgeClasses(piece, top.Rect);
                }
                windows.Add(window);
            }

            if (!top.IsTaskbar)
            {
                AddTitleBarInfoButtons(windows, top.Hwnd, occluders);
            }
            occluders.Add(top.Rect);
        }

        RectInfo desktopRect = GetVirtualScreenRect();
        List<RectInfo> desktopPieces = SubtractOcclusions(desktopRect, occluders);
        for (int i = 0; i < desktopPieces.Count; i++)
        {
            RectInfo piece = desktopPieces[i];
            if (!IsMeaningfulStructuralPiece(piece))
            {
                continue;
            }
            Item item = new Item();
            item.Label = "桌面";
            item.Name = "桌面";
            item.Control = "Desktop";
            item.X = piece.X;
            item.Y = piece.Y;
            item.Width = piece.Width;
            item.Height = piece.Height;
            item.CenterX = piece.X + piece.Width / 2;
            item.CenterY = piece.Y + piece.Height / 2;
            item.ClassName = "VisibleDesktop";
            windows.Add(item);
        }

        return windows;
    }

    private static string GetOriginalEdgeClasses(RectInfo piece, RectInfo original)
    {
        if (piece == null || original == null)
        {
            return "";
        }
        StringBuilder edges = new StringBuilder();
        if (piece.Y == original.Y)
        {
            edges.Append(" EdgeTop");
        }
        if (piece.Right == original.Right)
        {
            edges.Append(" EdgeRight");
        }
        if (piece.Bottom == original.Bottom)
        {
            edges.Append(" EdgeBottom");
        }
        if (piece.X == original.X)
        {
            edges.Append(" EdgeLeft");
        }
        return edges.ToString();
    }

    private static bool IsMeaningfulStructuralPiece(RectInfo piece)
    {
        return piece != null &&
               piece.Width >= 24 &&
               piece.Height >= 24 &&
               (long)piece.Width * piece.Height >= 1200;
    }

    private static RectInfo GetVirtualScreenRect()
    {
        int x = GetSystemMetrics(SmXVirtualScreen);
        int y = GetSystemMetrics(SmYVirtualScreen);
        int width = GetSystemMetrics(SmCxVirtualScreen);
        int height = GetSystemMetrics(SmCyVirtualScreen);
        if (width <= 0 || height <= 0)
        {
            x = 0;
            y = 0;
            width = GetSystemMetrics(0);
            height = GetSystemMetrics(1);
        }
        return new RectInfo { X = x, Y = y, Width = width, Height = height };
    }

    private static List<TopWindowInfo> GetTopWindowsInZOrder()
    {
        List<TopWindowInfo> windows = new List<TopWindowInfo>();
        for (IntPtr hWnd = GetTopWindow(IntPtr.Zero); hWnd != IntPtr.Zero; hWnd = GetWindow(hWnd, GwHwndNext))
        {
            RectInfo rect;
            string title;
            string className;
            bool isTaskbar;
            if (!TryGetVisibleWindow(hWnd, out rect, out title, out className, out isTaskbar))
            {
                continue;
            }

            TopWindowInfo info = new TopWindowInfo();
            info.Hwnd = hWnd;
            info.Rect = rect;
            info.Title = title;
            info.ClassName = className;
            info.IsTaskbar = isTaskbar;
            windows.Add(info);
        }
        return windows;
    }

    private static List<RectInfo> GetVisiblePiecesForWindow(IntPtr targetHwnd)
    {
        List<RectInfo> occluders = new List<RectInfo>();
        for (IntPtr hWnd = GetTopWindow(IntPtr.Zero); hWnd != IntPtr.Zero; hWnd = GetWindow(hWnd, GwHwndNext))
        {
            RectInfo rect;
            string title;
            string className;
            bool isTaskbar;
            if (!TryGetVisibleWindow(hWnd, out rect, out title, out className, out isTaskbar))
            {
                continue;
            }
            if (hWnd == targetHwnd)
            {
                return SubtractOcclusions(rect, occluders);
            }
            occluders.Add(rect);
        }
        return new List<RectInfo>();
    }

    private static List<RectInfo> GetOccludersAbove(IntPtr targetHwnd)
    {
        List<RectInfo> occluders = new List<RectInfo>();
        for (IntPtr hWnd = GetTopWindow(IntPtr.Zero); hWnd != IntPtr.Zero; hWnd = GetWindow(hWnd, GwHwndNext))
        {
            RectInfo rect;
            string title;
            string className;
            bool isTaskbar;
            if (!TryGetVisibleWindow(hWnd, out rect, out title, out className, out isTaskbar))
            {
                continue;
            }
            if (hWnd == targetHwnd)
            {
                return occluders;
            }
            occluders.Add(rect);
        }
        return occluders;
    }

    private static List<Item> ClipItemsByOccluders(List<Item> items, List<RectInfo> occluders)
    {
        if (items.Count == 0 || occluders.Count == 0)
        {
            return items;
        }
        List<Item> clipped = new List<Item>(items.Count);
        for (int i = 0; i < items.Count; i++)
        {
            Item item = items[i];
            RectInfo itemRect = new RectInfo { X = item.X, Y = item.Y, Width = item.Width, Height = item.Height };
            List<RectInfo> pieces = SubtractOcclusions(itemRect, occluders);
            for (int j = 0; j < pieces.Count; j++)
            {
                if (!IsMeaningfulClippedPiece(item, itemRect, pieces[j]))
                {
                    continue;
                }
                clipped.Add(CloneItemWithRect(item, pieces[j]));
            }
        }
        return clipped;
    }

    private static bool IsMeaningfulClippedPiece(Item item, RectInfo original, RectInfo piece)
    {
        if (piece == null || piece.Width < 14 || piece.Height < 14)
        {
            return false;
        }
        long originalArea = Math.Max(1L, (long)original.Width * original.Height);
        long pieceArea = (long)piece.Width * piece.Height;
        if (pieceArea == originalArea)
        {
            return true;
        }
        double ratio = (double)pieceArea / originalArea;
        if (item.Control == "DesktopItem" || item.Control == "TaskbarItem")
        {
            return ratio >= 0.18;
        }
        if (item.Control == "CaptionButton")
        {
            return ratio >= 0.25;
        }
        return ratio >= 0.12;
    }

    private static List<Item> ClipItemsToVisiblePieces(List<Item> items, List<RectInfo> visiblePieces)
    {
        if (items.Count == 0 || visiblePieces.Count == 0)
        {
            return items;
        }
        List<Item> clipped = new List<Item>(items.Count);
        for (int i = 0; i < items.Count; i++)
        {
            Item item = items[i];
            RectInfo itemRect = new RectInfo { X = item.X, Y = item.Y, Width = item.Width, Height = item.Height };
            for (int j = 0; j < visiblePieces.Count; j++)
            {
                RectInfo intersect = IntersectRect(itemRect, visiblePieces[j]);
                if (intersect == null)
                {
                    continue;
                }
                clipped.Add(CloneItemWithRect(item, intersect));
            }
        }
        return clipped;
    }

    private static RectInfo IntersectRect(RectInfo a, RectInfo b)
    {
        int left = Math.Max(a.X, b.X);
        int top = Math.Max(a.Y, b.Y);
        int right = Math.Min(a.Right, b.Right);
        int bottom = Math.Min(a.Bottom, b.Bottom);
        if (right - left < MinVisiblePiece || bottom - top < MinVisiblePiece)
        {
            return null;
        }
        return new RectInfo { X = left, Y = top, Width = right - left, Height = bottom - top };
    }

    private static Item CloneItemWithRect(Item source, RectInfo rect)
    {
        Item item = new Item();
        item.Id = source.Id;
        item.Label = source.Label;
        item.Name = source.Name;
        item.Control = source.Control;
        item.X = rect.X;
        item.Y = rect.Y;
        item.Width = rect.Width;
        item.Height = rect.Height;
        item.CenterX = rect.X + rect.Width / 2;
        item.CenterY = rect.Y + rect.Height / 2;
        item.Patterns = source.Patterns;
        item.AutomationId = source.AutomationId;
        item.ClassName = source.ClassName;
        return item;
    }

    private static List<RectInfo> SubtractOcclusions(RectInfo source, List<RectInfo> occluders)
    {
        List<RectInfo> pieces = new List<RectInfo>();
        pieces.Add(source);

        for (int i = 0; i < occluders.Count; i++)
        {
            RectInfo blocker = occluders[i];
            List<RectInfo> next = new List<RectInfo>();
            for (int j = 0; j < pieces.Count; j++)
            {
                AddSubtractPiece(next, pieces[j], blocker);
            }
            pieces = next;
            if (pieces.Count == 0)
            {
                break;
            }
        }

        return pieces;
    }

    private static void AddSubtractPiece(List<RectInfo> output, RectInfo source, RectInfo blocker)
    {
        int left = Math.Max(source.X, blocker.X);
        int top = Math.Max(source.Y, blocker.Y);
        int right = Math.Min(source.Right, blocker.Right);
        int bottom = Math.Min(source.Bottom, blocker.Bottom);

        if (right <= left || bottom <= top)
        {
            AddIfLarge(output, source.X, source.Y, source.Width, source.Height);
            return;
        }

        AddIfLarge(output, source.X, source.Y, source.Width, top - source.Y);
        AddIfLarge(output, source.X, bottom, source.Width, source.Bottom - bottom);
        AddIfLarge(output, source.X, top, left - source.X, bottom - top);
        AddIfLarge(output, right, top, source.Right - right, bottom - top);
    }

    private static void AddIfLarge(List<RectInfo> output, int x, int y, int width, int height)
    {
        if (width < MinVisiblePiece || height < MinVisiblePiece)
        {
            return;
        }
        output.Add(new RectInfo { X = x, Y = y, Width = width, Height = height });
    }

    private static bool TryGetVisibleWindow(IntPtr hWnd, out RectInfo rect, out string title, out string className)
    {
        bool isTaskbar;
        return TryGetVisibleWindow(hWnd, out rect, out title, out className, out isTaskbar);
    }

    private static bool TryGetVisibleWindow(IntPtr hWnd, out RectInfo rect, out string title, out string className, out bool isTaskbar)
    {
        rect = null;
        title = "";
        className = GetWindowClassName(hWnd);
        isTaskbar = className == "Shell_TrayWnd" || className == "Shell_SecondaryTrayWnd";

        if (!IsWindowVisible(hWnd) || IsIconic(hWnd) || IsCloaked(hWnd))
        {
            return false;
        }

        if (className == "Progman" ||
            className == "WorkerW" ||
            className == "Button")
        {
            return false;
        }

        WinRect wr;
        if (!GetWindowRect(hWnd, out wr))
        {
            return false;
        }
        int width = wr.Right - wr.Left;
        int height = wr.Bottom - wr.Top;
        if (!isTaskbar && (width < 120 || height < 80))
        {
            return false;
        }

        long exStyle = GetWindowLongPtr64(hWnd, GwlExStyle).ToInt64();
        IntPtr owner = GetWindow(hWnd, GwOwner);
        bool meaningfulOwnedPopup =
            !isTaskbar &&
            owner != IntPtr.Zero &&
            width >= 160 &&
            height >= 100;
        if (!isTaskbar &&
            (exStyle & WsExToolWindow) != 0 &&
            (exStyle & WsExAppWindow) == 0 &&
            !meaningfulOwnedPopup)
        {
            return false;
        }

        title = GetWindowTitle(hWnd);
        if (isTaskbar && String.IsNullOrWhiteSpace(title))
        {
            title = "任务栏";
        }
        if (!isTaskbar &&
            String.IsNullOrWhiteSpace(title) &&
            (exStyle & WsExAppWindow) == 0 &&
            !meaningfulOwnedPopup)
        {
            return false;
        }
        if (meaningfulOwnedPopup && String.IsNullOrWhiteSpace(title))
        {
            title = String.IsNullOrWhiteSpace(className) ? "Popup" : className;
        }

        rect = new RectInfo { X = wr.Left, Y = wr.Top, Width = width, Height = height };
        return true;
    }

    private static bool IsCloaked(IntPtr hWnd)
    {
        try
        {
            int cloaked;
            int hr = DwmGetWindowAttribute(hWnd, DwmwaCloaked, out cloaked, Marshal.SizeOf(typeof(int)));
            return hr == 0 && cloaked != 0;
        }
        catch
        {
            return false;
        }
    }

    private static string GetWindowTitle(IntPtr hWnd)
    {
        int length = Math.Max(0, GetWindowTextLengthW(hWnd));
        if (length == 0)
        {
            return "";
        }
        StringBuilder sb = new StringBuilder(length + 1);
        GetWindowTextW(hWnd, sb, sb.Capacity);
        return Normalize(sb.ToString());
    }

    private static string GetWindowClassName(IntPtr hWnd)
    {
        StringBuilder sb = new StringBuilder(256);
        GetClassNameW(hWnd, sb, sb.Capacity);
        return sb.ToString();
    }

    private static int AddTitleBarInfoButtons(List<Item> items, IntPtr hWnd, List<RectInfo> occluders)
    {
        TitleBarInfoEx info = new TitleBarInfoEx();
        info.cbSize = Marshal.SizeOf(typeof(TitleBarInfoEx));
        info.rgstate = new int[6];
        info.rgrect = new WinRect[6];
        try
        {
            SendMessage(hWnd, WmGetTitleBarInfoEx, IntPtr.Zero, ref info);
        }
        catch
        {
            return 0;
        }

        int added = 0;
        added += AddTitleBarButton(items, "最小化", info, TitleBarChildMinimize, occluders);
        added += AddTitleBarButton(items, "最大化", info, TitleBarChildMaximize, occluders);
        added += AddTitleBarButton(items, "关闭", info, TitleBarChildClose, occluders);
        return added;
    }

    private static int AddTitleBarButton(List<Item> items, string label, TitleBarInfoEx info, int index, List<RectInfo> occluders)
    {
        if (info.rgstate == null || info.rgrect == null || index < 0 || index >= info.rgrect.Length)
        {
            return 0;
        }
        int state = info.rgstate[index];
        if ((state & StateSystemInvisible) != 0 || (state & StateSystemUnavailable) != 0)
        {
            return 0;
        }
        WinRect wr = info.rgrect[index];
        int width = wr.Right - wr.Left;
        int height = wr.Bottom - wr.Top;
        if (width < 8 || height < 8)
        {
            return 0;
        }

        RectInfo rect = new RectInfo { X = wr.Left, Y = wr.Top, Width = width, Height = height };
        List<RectInfo> pieces = SubtractOcclusions(rect, occluders);
        int added = 0;
        for (int i = 0; i < pieces.Count; i++)
        {
            RectInfo piece = pieces[i];
            Item item = new Item();
            item.Label = label;
            item.Name = label;
            item.Control = "CaptionButton";
            item.X = piece.X;
            item.Y = piece.Y;
            item.Width = piece.Width;
            item.Height = piece.Height;
            item.CenterX = piece.X + piece.Width / 2;
            item.CenterY = piece.Y + piece.Height / 2;
            item.ClassName = "TitleBarInfoEx";
            items.Add(item);
            added++;
        }
        return added;
    }

    private static void AddExactCaptionButtonsForWindow(List<Item> items, IntPtr hWnd, RectInfo windowRect)
    {
        if (windowRect == null)
        {
            return;
        }

        CacheRequest cache = BuildCacheRequest();
        AutomationElement root;
        try
        {
            using (cache.Activate())
            {
                root = AutomationElement.FromHandle(hWnd);
            }
        }
        catch
        {
            return;
        }
        if (root == null)
        {
            return;
        }

        Queue<ElementDepth> queue = new Queue<ElementDepth>();
        HashSet<string> seen = new HashSet<string>(StringComparer.Ordinal);
        TreeWalker walker = TreeWalker.ControlViewWalker;
        queue.Enqueue(new ElementDepth(root, 0));
        int visited = 0;
        int visitLimit = 90;
        int topLimit = windowRect.Y + Math.Max(96, Math.Min(140, windowRect.Height / 5));

        while (queue.Count > 0 && visited < visitLimit)
        {
            ElementDepth node = queue.Dequeue();
            AutomationElement element = node.Element;
            visited++;
            if (node.Depth < 2)
            {
                try
                {
                    using (cache.Activate())
                    {
                        AutomationElement child = walker.GetFirstChild(element, cache);
                        while (child != null && queue.Count < visitLimit)
                        {
                            RectInfo childRect = GetRect(child);
                            if (node.Depth == 0 || childRect == null || childRect.Y <= topLimit || childRect.CenterY <= topLimit)
                            {
                                queue.Enqueue(new ElementDepth(child, node.Depth + 1));
                            }
                            child = walker.GetNextSibling(child, cache);
                        }
                    }
                }
                catch { }
            }

            if (visited == 1)
            {
                continue;
            }

            RectInfo rect = GetRect(element);
            if (rect == null)
            {
                continue;
            }
            string className = GetString(element, AutomationElement.ClassNameProperty);
            string control = GetControlName(element);
            string name = Normalize(GetString(element, AutomationElement.NameProperty));
            bool exactClass = className.Equals("WinCaptionButton", StringComparison.OrdinalIgnoreCase);
            if (rect.CenterY > topLimit)
            {
                continue;
            }
            if (!exactClass)
            {
                continue;
            }

            string key = rect.X + "," + rect.Y + "," + rect.Width + "," + rect.Height + "," + name;
            if (!seen.Add(key))
            {
                continue;
            }

            Item item = new Item();
            item.Label = String.IsNullOrWhiteSpace(name) ? "Caption button" : name;
            item.Name = name;
            item.Control = "CaptionButton";
            item.X = rect.X;
            item.Y = rect.Y;
            item.Width = rect.Width;
            item.Height = rect.Height;
            item.CenterX = rect.CenterX;
            item.CenterY = rect.CenterY;
            item.ClassName = "ExactCaptionButton " + className;
            items.Add(item);
        }
    }

    private static bool IsCaptionButtonName(string name)
    {
        if (String.IsNullOrWhiteSpace(name))
        {
            return false;
        }
        return name.IndexOf("最小", StringComparison.OrdinalIgnoreCase) >= 0 ||
               name.IndexOf("最大", StringComparison.OrdinalIgnoreCase) >= 0 ||
               name.IndexOf("关闭", StringComparison.OrdinalIgnoreCase) >= 0 ||
               name.IndexOf("還原", StringComparison.OrdinalIgnoreCase) >= 0 ||
               name.IndexOf("还原", StringComparison.OrdinalIgnoreCase) >= 0 ||
               name.IndexOf("Minimize", StringComparison.OrdinalIgnoreCase) >= 0 ||
               name.IndexOf("Maximize", StringComparison.OrdinalIgnoreCase) >= 0 ||
               name.IndexOf("Close", StringComparison.OrdinalIgnoreCase) >= 0 ||
               name.IndexOf("Restore", StringComparison.OrdinalIgnoreCase) >= 0;
    }

    private static void AddSyntheticCaptionButtons(List<Item> items, IntPtr hWnd, RectInfo rect, string title, string className, bool isForeground)
    {
        if (rect.Width < 180 || rect.Height < 110)
        {
            return;
        }
        int buttonWidth = Math.Max(42, GetSystemMetrics(SmCxSize));
        int buttonHeight = Math.Max(28, GetSystemMetrics(SmCySize));
        buttonHeight = Math.Min(buttonHeight, Math.Max(28, rect.Height / 8));
        buttonWidth = Math.Min(buttonWidth, Math.Max(38, rect.Width / 6));
        int y = rect.Y;
        int closeX = rect.X + rect.Width - buttonWidth;
        int maxX = closeX - buttonWidth;
        int minX = maxX - buttonWidth;
        if (minX < rect.X)
        {
            return;
        }

        AddCaptionButton(items, "最小化", minX, y, buttonWidth, buttonHeight, isForeground);
        AddCaptionButton(items, "最大化", maxX, y, buttonWidth, buttonHeight, isForeground);
        AddCaptionButton(items, "关闭", closeX, y, buttonWidth, buttonHeight, isForeground);
    }

    private static void AddCaptionButton(List<Item> items, string label, int x, int y, int width, int height, bool isForeground)
    {
        Item item = new Item();
        item.Label = label;
        item.Name = label;
        item.Control = "CaptionButton";
        item.X = x;
        item.Y = y;
        item.Width = width;
        item.Height = height;
        item.CenterX = x + width / 2;
        item.CenterY = y + height / 2;
        item.ClassName = isForeground ? "SyntheticCaptionButton foreground" : "SyntheticCaptionButton";
        items.Add(item);
    }

    private static object GetCached(AutomationElement element, AutomationProperty property)
    {
        try
        {
            object value = element.GetCachedPropertyValue(property, true);
            return value == AutomationElement.NotSupported ? null : value;
        }
        catch
        {
            return null;
        }
    }

    private static string GetString(AutomationElement element, AutomationProperty property)
    {
        object value = GetCached(element, property);
        return value == null ? "" : value.ToString();
    }

    private static object GetCurrent(AutomationElement element, AutomationProperty property)
    {
        try
        {
            object value = element.GetCurrentPropertyValue(property, true);
            return value == AutomationElement.NotSupported ? null : value;
        }
        catch
        {
            return null;
        }
    }

    private static string GetCurrentString(AutomationElement element, AutomationProperty property)
    {
        object value = GetCurrent(element, property);
        return value == null ? "" : value.ToString();
    }

    private static bool GetCurrentBool(AutomationElement element, AutomationProperty property, bool fallback)
    {
        object value = GetCurrent(element, property);
        if (value is bool) return (bool)value;
        return fallback;
    }

    private static bool GetBool(AutomationElement element, AutomationProperty property, bool fallback)
    {
        object value = GetCached(element, property);
        if (value is bool) return (bool)value;
        return fallback;
    }

    private static string GetControlName(AutomationElement element)
    {
        object value = GetCached(element, AutomationElement.ControlTypeProperty);
        ControlType control = value as ControlType;
        if (control == null) return "";
        return control.ProgrammaticName.Replace("ControlType.", "");
    }

    private static string GetCurrentControlName(AutomationElement element)
    {
        object value = GetCurrent(element, AutomationElement.ControlTypeProperty);
        ControlType control = value as ControlType;
        if (control == null) return "";
        return control.ProgrammaticName.Replace("ControlType.", "");
    }

    private static RectInfo GetRect(AutomationElement element)
    {
        object value = GetCached(element, AutomationElement.BoundingRectangleProperty);
        if (!(value is Rect))
        {
            return null;
        }
        Rect rect = (Rect)value;
        if (rect.IsEmpty)
        {
            return null;
        }
        int x = (int)Math.Round(rect.Left);
        int y = (int)Math.Round(rect.Top);
        int width = (int)Math.Round(rect.Width);
        int height = (int)Math.Round(rect.Height);
        if (width < 6 || height < 6)
        {
            return null;
        }
        return new RectInfo { X = x, Y = y, Width = width, Height = height };
    }

    private static RectInfo GetCurrentRect(AutomationElement element)
    {
        object value = GetCurrent(element, AutomationElement.BoundingRectangleProperty);
        if (!(value is Rect))
        {
            return null;
        }
        Rect rect = (Rect)value;
        if (rect.IsEmpty)
        {
            return null;
        }
        int x = (int)Math.Round(rect.Left);
        int y = (int)Math.Round(rect.Top);
        int width = (int)Math.Round(rect.Width);
        int height = (int)Math.Round(rect.Height);
        if (width < 6 || height < 6)
        {
            return null;
        }
        return new RectInfo { X = x, Y = y, Width = width, Height = height };
    }

    private static List<string> GetPatterns(AutomationElement element)
    {
        List<string> patterns = new List<string>(7);
        if (GetBool(element, AutomationElement.IsInvokePatternAvailableProperty, false)) patterns.Add("Invoke");
        if (GetBool(element, AutomationElement.IsValuePatternAvailableProperty, false)) patterns.Add("Value");
        if (GetBool(element, AutomationElement.IsTogglePatternAvailableProperty, false)) patterns.Add("Toggle");
        if (GetBool(element, AutomationElement.IsSelectionItemPatternAvailableProperty, false)) patterns.Add("SelectionItem");
        if (GetBool(element, AutomationElement.IsExpandCollapsePatternAvailableProperty, false)) patterns.Add("ExpandCollapse");
        if (GetBool(element, AutomationElement.IsRangeValuePatternAvailableProperty, false)) patterns.Add("RangeValue");
        if (GetBool(element, AutomationElement.IsScrollItemPatternAvailableProperty, false)) patterns.Add("ScrollItem");
        return patterns;
    }

    private static List<string> GetCurrentPatterns(AutomationElement element)
    {
        List<string> patterns = new List<string>(7);
        if (GetCurrentBool(element, AutomationElement.IsInvokePatternAvailableProperty, false)) patterns.Add("Invoke");
        if (GetCurrentBool(element, AutomationElement.IsValuePatternAvailableProperty, false)) patterns.Add("Value");
        if (GetCurrentBool(element, AutomationElement.IsTogglePatternAvailableProperty, false)) patterns.Add("Toggle");
        if (GetCurrentBool(element, AutomationElement.IsSelectionItemPatternAvailableProperty, false)) patterns.Add("SelectionItem");
        if (GetCurrentBool(element, AutomationElement.IsExpandCollapsePatternAvailableProperty, false)) patterns.Add("ExpandCollapse");
        if (GetCurrentBool(element, AutomationElement.IsRangeValuePatternAvailableProperty, false)) patterns.Add("RangeValue");
        if (GetCurrentBool(element, AutomationElement.IsScrollItemPatternAvailableProperty, false)) patterns.Add("ScrollItem");
        return patterns;
    }

    private static string Normalize(string text)
    {
        if (String.IsNullOrWhiteSpace(text))
        {
            return "";
        }
        StringBuilder sb = new StringBuilder(text.Length);
        bool wasSpace = false;
        for (int i = 0; i < text.Length; i++)
        {
            char c = text[i];
            if (Char.IsWhiteSpace(c))
            {
                if (!wasSpace)
                {
                    sb.Append(' ');
                    wasSpace = true;
                }
            }
            else
            {
                sb.Append(c);
                wasSpace = false;
            }
        }
        return sb.ToString().Trim();
    }

    private static void AppendRect(StringBuilder sb, RectInfo rect)
    {
        if (rect == null)
        {
            sb.Append("null");
            return;
        }
        sb.Append("{\"x\":");
        sb.Append(rect.X.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"y\":");
        sb.Append(rect.Y.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"width\":");
        sb.Append(rect.Width.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"height\":");
        sb.Append(rect.Height.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"right\":");
        sb.Append(rect.Right.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"bottom\":");
        sb.Append(rect.Bottom.ToString(CultureInfo.InvariantCulture));
        sb.Append('}');
    }

    private static void AppendItem(StringBuilder sb, Item item)
    {
        sb.Append("{\"id\":");
        sb.Append(item.Id.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"label\":");
        AppendJsonString(sb, item.Label);
        sb.Append(",\"name\":");
        AppendJsonString(sb, item.Name);
        sb.Append(",\"control\":");
        AppendJsonString(sb, item.Control);
        sb.Append(",\"x\":");
        sb.Append(item.X.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"y\":");
        sb.Append(item.Y.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"width\":");
        sb.Append(item.Width.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"height\":");
        sb.Append(item.Height.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"centerX\":");
        sb.Append(item.CenterX.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"centerY\":");
        sb.Append(item.CenterY.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"patterns\":[");
        for (int i = 0; i < item.Patterns.Count; i++)
        {
            if (i > 0) sb.Append(',');
            AppendJsonString(sb, item.Patterns[i]);
        }
        sb.Append("],\"automationId\":");
        AppendJsonString(sb, item.AutomationId);
        sb.Append(",\"className\":");
        AppendJsonString(sb, item.ClassName);
        sb.Append('}');
    }

    private static void AppendJsonString(StringBuilder sb, string value)
    {
        if (value == null)
        {
            sb.Append("null");
            return;
        }
        sb.Append('"');
        for (int i = 0; i < value.Length; i++)
        {
            char c = value[i];
            switch (c)
            {
                case '"': sb.Append("\\\""); break;
                case '\\': sb.Append("\\\\"); break;
                case '\b': sb.Append("\\b"); break;
                case '\f': sb.Append("\\f"); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                default:
                    if (c < 32)
                    {
                        sb.Append("\\u");
                        sb.Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                    }
                    else
                    {
                        sb.Append(c);
                    }
                    break;
            }
        }
        sb.Append('"');
    }
}
