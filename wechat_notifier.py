"""
微信消息通知器 v0.2.4 - CMD控制台版
v0.2.4: 精简双模式为单模式 + 深度重搜
作者: 不理笨笨
"""
import os, sys, re, json, time, traceback, ctypes, subprocess, threading, sqlite3, unicodedata
from pathlib import Path
from datetime import datetime

# ── 路径配置 ──
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

RECORDS_FILE  = DATA_DIR / "chat-records.json"
LOG_FILE      = DATA_DIR / "debug.log"
SETTINGS_FILE = DATA_DIR / "settings.json"

# ── 常量 ──
BLACK_LIST   = {"服务号", "公众号", "文件传输助手", "折叠的聊天", "服务通知", "微信团队"}
TIME_WORDS   = {"昨天", "今天", "上午", "下午", "刚刚"}
SCAN_INTERVAL = 0.1

SHORTCUT_PRESETS = {
    "Alt+X":       {"mods": [0x12],           "key": 0x58},
    "Ctrl+Alt+W":  {"mods": [0x11, 0x12],     "key": 0x57},
    "Alt+W":       {"mods": [0x12],           "key": 0x57},
}
DEFAULT_SHORTCUT = "Ctrl+Alt+W"

MUTEX_NAME = "Global\\WeChatNotifier_BG_v4"
PID_FILE = DATA_DIR / "bg_pid.txt"


# ── Toast 弹窗配置 ──
FONT_FAMILY = "Microsoft YaHei"
NOTIFY_W   = 380
NOTIFY_H   = 110
MARGIN     = 20
DURATION   = 5000
SPACING    = 12
CORNER_R   = 16


# ══════════════════════════════════════════════════════════════════
#                          日志
# ══════════════════════════════════════════════════════════════════
_LOG_MAX_SIZE = 1 * 1024 * 1024  # 1MB 轮转阈值
_last_log_msg   = ""
_last_log_count = 0
_last_log_ts    = ""

def _write_log(line):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except:
        pass

def _flush_dup():
    global _last_log_msg, _last_log_count, _last_log_ts
    if _last_log_count > 1:
        _write_log(f"[{_last_log_ts}] {_last_log_msg} (已重复{_last_log_count}次)")
    _last_log_msg = ""
    _last_log_count = 0

def log(msg):
    global _last_log_msg, _last_log_count, _last_log_ts
    ts = datetime.now().strftime("%H:%M:%S")

    # ── 轮转: 超过 1MB 时按起止日期归档 ──
    if LOG_FILE.exists():
        try:
            if LOG_FILE.stat().st_size > _LOG_MAX_SIZE:
                st = LOG_FILE.stat()
                start_dt = datetime.fromtimestamp(st.st_ctime)
                end_dt   = datetime.fromtimestamp(st.st_mtime)
                if start_dt.strftime("%m%d") == end_dt.strftime("%m%d"):
                    date_tag = start_dt.strftime("_%m%d")
                else:
                    date_tag = f"_{start_dt.strftime('%m%d')}-{end_dt.strftime('%m%d')}"
                backup = LOG_FILE.parent / f"debug{date_tag}.log"
                if backup.exists():
                    backup.unlink()
                LOG_FILE.rename(backup)
        except:
            pass

    # ── 重复抑制 (COM/UIA 消息不走去重，确保恢复过程全程可见) ──
    if msg != _last_log_msg or "COM" in msg or "read=None" in msg or "UIA" in msg:
        _flush_dup()
        _last_log_msg = msg
        _last_log_count = 1
        _last_log_ts = ts
        _write_log(f"[{ts}] {msg}")
        return

    _last_log_count += 1
    if _last_log_count % 500 == 0:
        _write_log(f"[{ts}] {msg} (已重复{_last_log_count}次,继续中...)")
    return


# ══════════════════════════════════════════════════════════════════
#                        设置管理
# ══════════════════════════════════════════════════════════════════
def load_settings():
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except:
            pass
    return {"shortcut": DEFAULT_SHORTCUT, "auto_start": False}

def save_settings(s):
    SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")

def get_shortcut_combo():
    return load_settings().get("shortcut", DEFAULT_SHORTCUT)

def set_shortcut_combo(name):
    if name != "关闭" and name not in SHORTCUT_PRESETS:
        return False
    s = load_settings()
    s["shortcut"] = name
    save_settings(s)
    log(f"快捷键已切换为: {name}")
    return True


# ══════════════════════════════════════════════════════════════════
#                      开机自启管理 (VBS + 计划任务)
# ══════════════════════════════════════════════════════════════════
TASK_NAME = "WeChatNotifier"

def _get_vbs_path():
    return DATA_DIR / "start_hidden.vbs"

def _write_vbs():
    exe_path = sys.executable if getattr(sys, "frozen", False) else Path(__file__).resolve()
    vbs_content = f'''Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """{exe_path}"" --background", 0, False'''
    _get_vbs_path().write_text(vbs_content, encoding="ascii")
    return _get_vbs_path()

def _clean_registry_auto_start():
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE)
        try:
            winreg.DeleteValue(key, "WeChatNotifier")
        except FileNotFoundError:
            pass
        winreg.CloseKey(key)
    except:
        pass

def get_auto_start():
    try:
        result = subprocess.run(
            ["schtasks", "/query", "/tn", TASK_NAME],
            capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except:
        return False

def set_auto_start(enable):
    _clean_registry_auto_start()
    try:
        if enable:
            vbs_path = _write_vbs()
            result = subprocess.run(
                ["schtasks", "/create", "/tn", TASK_NAME,
                 "/tr", f'wscript.exe "{vbs_path}"',
                 "/sc", "ONLOGON", "/f"],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0:
                subprocess.run(
                    ["schtasks", "/change", "/tn", TASK_NAME, "/settings", "HIDDEN"],
                    capture_output=True, text=True, timeout=10
                )
                log("开机自启: 已启用（VBS计划任务，登录时无CMD闪现）")
                return True
            else:
                log(f"计划任务创建失败: {result.stderr[:200]}")
                return False
        else:
            result = subprocess.run(
                ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
                capture_output=True, text=True, timeout=10
            )
            log("开机自启: 已禁用（计划任务已删除）")
            return True
    except Exception as e:
        log(f"开机自启设置失败: {e}")
        return False


# ══════════════════════════════════════════════════════════════════
#                    快捷键激活微信
# ══════════════════════════════════════════════════════════════════
def activate_wechat_by_shortcut():
    shortcut = get_shortcut_combo()
    if shortcut == "关闭":
        return
    preset = SHORTCUT_PRESETS.get(shortcut, SHORTCUT_PRESETS[DEFAULT_SHORTCUT])
    mods = preset["mods"]
    key  = preset["key"]
    KEYEVENTF_KEYDOWN = 0
    KEYEVENTF_KEYUP   = 2
    for mod in mods:
        ctypes.windll.user32.keybd_event(mod, 0, KEYEVENTF_KEYDOWN, 0)
    ctypes.windll.user32.keybd_event(key, 0, KEYEVENTF_KEYDOWN, 0)
    ctypes.windll.user32.keybd_event(key, 0, KEYEVENTF_KEYUP, 0)
    for mod in reversed(mods):
        ctypes.windll.user32.keybd_event(mod, 0, KEYEVENTF_KEYUP, 0)


# ══════════════════════════════════════════════════════════════════
#                       Toast 弹窗系统
# ══════════════════════════════════════════════════════════════════
def _get_scale(root):
    """直接取 Windows 系统 DPI 缩放比，不再从屏幕尺寸推算"""
    try:
        import ctypes
        hdc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, hdc)
        return max(0.5, min(2.0, dpi / 96.0))
    except:
        # 兜底：仍用屏幕尺寸推算
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        w_ratio = sw / 2560.0
        h_ratio = sh / 1440.0
        return max(0.5, min(2.0, min(w_ratio, h_ratio)))

def _show_toast(root, title, body):
    import tkinter as tk
    import ctypes

    s = _get_scale(root)
    nw = int(380 * s)
    nh = int(110 * s)
    margin = int(20 * s)
    bottom_off = int(60 * s)
    gap = int(8 * s)
    corner = int(16 * s)
    pad_x = int(16 * s)
    pad_x2 = int(12 * s)
    pad_y1 = int(10 * s)
    pad_y2 = int(6 * s)
    pad_y3 = int(14 * s)
    font_header = int(9 * s)
    font_title = int(12 * s)
    font_body = int(10 * s)

    # 用 GetSystemMetrics 取物理分辨率，避免 Tkinter DPI 感知不一致
    SM_CXSCREEN = 0
    SM_CYSCREEN = 1
    screen_w = ctypes.windll.user32.GetSystemMetrics(SM_CXSCREEN)
    screen_h = ctypes.windll.user32.GetSystemMetrics(SM_CYSCREEN)

    if not hasattr(root, "active_toasts"):
        root.active_toasts = []

    root.active_toasts = [w for w in root.active_toasts if w.winfo_exists()]

    x = screen_w - nw - margin
    base_y = screen_h - nh - bottom_off

    offset = nh + gap
    for i, existing in enumerate(root.active_toasts):
        new_y = base_y - (len(root.active_toasts) - i) * offset
        try:
            existing.geometry(f"{nw}x{nh}+{x}+{new_y}")
        except:
            pass

    y = base_y
    win = tk.Toplevel(root)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.configure(bg="#010101")
    win.attributes("-transparentcolor", "#010101")
    win.attributes("-alpha", 0.93)
    win.geometry(f"{nw}x{nh}+{x}+{y}")

    r = corner
    bg_canvas = tk.Canvas(win, width=nw, height=nh,
                          bg="#010101", highlightthickness=0, bd=0)
    bg_canvas.place(x=0, y=0)
    bg_canvas.create_arc(0, 0, 2*r, 2*r, start=90, extent=90, fill="#1C1C1E", outline="")
    bg_canvas.create_arc(nw-2*r, 0, nw, 2*r, start=0, extent=90, fill="#1C1C1E", outline="")
    bg_canvas.create_arc(0, nh-2*r, 2*r, nh, start=180, extent=90, fill="#1C1C1E", outline="")
    bg_canvas.create_arc(nw-2*r, nh-2*r, nw, nh, start=270, extent=90, fill="#1C1C1E", outline="")
    bg_canvas.create_rectangle(r, 0, nw-r, nh, fill="#1C1C1E", outline="")
    bg_canvas.create_rectangle(0, r, nw, nh-r, fill="#1C1C1E", outline="")

    title_frame = tk.Frame(win, bg="#1C1C1E")
    title_frame.pack(fill="x", padx=(pad_x, pad_x - int(6 * s)), pady=(pad_y1, 0))

    tk.Label(title_frame, text="微信", fg="#8E8E93", bg="#1C1C1E",
             font=(FONT_FAMILY, font_header)).pack(side="left")

    def close():
        win.destroy()
        if hasattr(root, "active_toasts"):
            root.active_toasts = [w for w in root.active_toasts if w.winfo_exists()]
            for i, existing in enumerate(root.active_toasts):
                new_y = base_y - (len(root.active_toasts) - 1 - i) * offset
                try:
                    existing.geometry(f"{nw}x{nh}+{x}+{new_y}")
                except:
                    pass

    content = tk.Frame(win, bg="#1C1C1E")
    content.pack(fill="both", expand=True, padx=(pad_x, pad_x2), pady=(pad_y2, pad_y3))
    text_frame = tk.Frame(content, bg="#1C1C1E")
    text_frame.pack(side="left", fill="both", expand=True)

    tk.Label(text_frame, text=title[:30], fg="#FFFFFF", bg="#1C1C1E",
             font=(FONT_FAMILY, font_title, "bold"), anchor="w").pack(
             side="top", fill="x", pady=(int(4 * s), 1))
    body_text = body[:60] + ("..." if len(body) > 60 else "")
    tk.Label(text_frame, text=body_text, fg="#AEAEB2", bg="#1C1C1E",
             font=(FONT_FAMILY, font_body), anchor="w").pack(
             side="top", fill="x", pady=(1, 0))

    def on_click(e=None):
        activate_wechat_by_shortcut()
        close()

    for widget in [win, bg_canvas, title_frame, content, text_frame]:
        try: widget.bind("<Button-1>", on_click)
        except: pass

    root.active_toasts.append(win)
    win.after(DURATION, close)


# ══════════════════════════════════════════════════════════════════
#                     UIA 消息解析
# ══════════════════════════════════════════════════════════════════



def _wechat_window_handle():
    """FindWindowW 快速嗅探微信主窗口 HWND（~0ms，无需 COM），返回 0 表示未找到"""
    try:
        return ctypes.windll.user32.FindWindowW(None, "微信")
    except:
        return 0


def parse_session(cell_name):
    """解析 mmui::ChatSessionCell 的 Name 属性
    格式: "会话名\\n已置顶\\n[N条] 消息内容\\n时间\\n[标签...]"
    返回: {"user":..., "msg":..., "unread":..., "raw":...} 或 None
    """
    if not cell_name or not cell_name.strip():
        return None
    raw = cell_name.replace("已置顶", "").strip()
    parts = raw.split("\n")
    if len(parts) < 2:
        return None

    user = parts[0]
    start = 1
    unread = ""

    for i in range(start, min(start + 2, len(parts))):
        p = parts[i]
        if "条未读" in p:
            unread = p
            start = i + 1
            break
        elif p.startswith("[") and "条]" in p:
            unread = p

    end = len(parts)
    for i in range(len(parts) - 1, start - 1, -1):
        p = parts[i]
        if ":" in p or "/" in p or "星期" in p or any(tw in p for tw in TIME_WORDS):
            end = i
            if i > start and any(tw in parts[i - 1] for tw in TIME_WORDS):
                end = i - 1
            break

    msg = " ".join(parts[start:end])
    msg = re.sub(r"\[\d+条\]", "", msg).strip()

    return {"user": user, "unread": unread, "msg": msg, "raw": raw}


def read_sessions(hwnd=0):
    import uiautomation as auto
    auto.SetGlobalSearchTimeout(1)
    import pythoncom
    try:
        # ── COM 健康检查: Exists(0) ~0ms 试探连通性，断连直接 None ──
        try:
            root = auto.GetRootControl()
            if not root.Exists(0):
                log("COM断开: GetRootControl 无响应")
                return None
        except Exception:
            log("COM断开: GetRootControl 异常")
            return None

        mainwin = None
        window_class = ""
        try:
            root_children = list(root.GetChildren())
            for c in root_children:
                cn = c.ClassName or ""
                # 兼容两种微信窗口: mmui::MainWindow (标准版) / Qt*QWindowIcon (Qt 渲染版)
                if cn == "mmui::MainWindow" or "QWindowIcon" in cn:
                    mainwin = c
                    window_class = cn
                    break
        except Exception as e:
            log(f"COM断开: GetChildren 遍历异常: {str(e)[:100]}")
            return None

        # ── 回退: GetChildren 没找到 → 用 HWND 通过 ControlFromHandle 直连 ──
        if not mainwin and hwnd:
            try:
                mainwin = auto.ControlFromHandle(hwnd)
                log(f"  read: GetChildren未命中, ControlFromHandle(hwnd={hwnd}) 回退成功")
            except Exception:
                log(f"  read: ControlFromHandle(hwnd={hwnd}) 回退也失败")

        if not mainwin:
            log("read: WeChat MainWindow未找到")
            return None

        if not mainwin.Exists(0):
            log("COM断开: MainWindow Exists 返回 False")
            return None

        # ── Qt 渲染版本兼容: mainwin 只包含 MMUIRenderSubWindowHW 时钻入它 ──
        try:
            mw_children = list(mainwin.GetChildren())
            if len(mw_children) == 1 and "MMUIRenderSubWindowHW" in (mw_children[0].ClassName or ""):
                log("  read: 识别为 Qt 渲染版本，钻入 MMUIRenderSubWindowHW")
                mainwin = mw_children[0]
                # 钻入后再次诊断: 打印子控件树
                deep_children = list(mainwin.GetChildren())
                log(f"  [诊断] MMUIRenderSubWindowHW.GetChildren() 共 {len(deep_children)} 个:")
                for c in deep_children[:30]:
                    try:
                        sc = list(c.GetChildren()) if hasattr(c, 'GetChildren') else []
                        log(f"    ClassName={c.ClassName or '(空)'} | Type={c.ControlTypeName or '(空)'} | Name={(c.Name or '')[:60]} | 子:{len(sc)}个")
                    except:
                        log(f"    (控件读取异常)")
        except Exception:
            pass

        # ═══ 模式A: 直接从MainWindow深搜 XTableView(Name='会话') → ChatSessionCell ═══
        try:
            table = mainwin.Control(searchDepth=20, Name="会话", ClassName="mmui::XTableView")
        except Exception:
            log("COM断开: XTableView 搜索异常")
            return None
        if not table.Exists(0):
            try:
                table = mainwin.Control(searchDepth=20, Name="会话")
            except Exception:
                log("COM断开: ListControl 搜索异常")
                return None
            if not table.Exists(0):
                log("  read: 未找到会话列表控件，触发深度重搜")

        if table.Exists(0):
            try:
                children = list(table.GetChildren())
            except Exception:
                log("COM断开: GetChildren 异常")
                return None
            sessions = []
            for c in children:
                if c.ControlTypeName == "ListItemControl" and c.ClassName == "mmui::ChatSessionCell":
                    p = parse_session(c.Name)
                    if p:
                        sessions.append(p)
            if sessions:
                log(f"  read: {window_class} → {len(sessions)}条 ChatSessionCell")
                return sessions
            else:
                log(f"  read: 会话列表存在但0条ChatSessionCell (children={len(children)})，触发深度重搜")

        # ═══ 重试: 深度重新搜索 ═══
        log("  read: 初次搜索未找到 ChatSessionCell，尝试深度重搜...")
        try:
            table2 = mainwin.Control(searchDepth=30, Name="会话", ClassName="mmui::XTableView")
        except Exception:
            log("COM断开: 深度重搜异常")
            return None
        if not table2.Exists(0):
            try:
                table2 = mainwin.Control(searchDepth=30, Name="会话")
            except Exception:
                log("COM断开: 深度重搜异常")
                return None
            if not table2.Exists(0):
                log("read: 深度重搜仍未找到会话列表控件")
        if table2.Exists(0):
            try:
                children2 = list(table2.GetChildren())
            except Exception:
                log("COM断开: 深度重搜 GetChildren 异常")
                return None
            sessions2 = []
            for c in children2:
                if c.ControlTypeName == "ListItemControl" and c.ClassName == "mmui::ChatSessionCell":
                    p = parse_session(c.Name)
                    if p:
                        sessions2.append(p)
            if sessions2:
                log(f"  深度重搜成功: ChatSessionCell ({len(sessions2)}条)")
                return sessions2
            else:
                log(f"  read: 深度重搜仍为0条ChatSessionCell (children={len(children2)})")
                return []
        log("read: 搜索方案均未获取到会话")

        # ── 诊断: 打印 mainwin 下 searchDepth=15 的所有子控件 ──
        try:
            mw_children = list(mainwin.GetChildren())
            log(f"  [诊断] mainwin.GetChildren() 共 {len(mw_children)} 个:")
            for c in mw_children:
                try:
                    log(f"    ClassName={c.ClassName or '(空)'} | Type={c.ControlTypeName or '(空)'} | Name={(c.Name or '')[:60]}")
                except:
                    log(f"    (控件读取异常)")
            if len(mw_children) == 0:
                log(f"  [诊断] mainwin 无直接子控件。尝试 searchDepth=15 搜所有子控件:")
                all_descendants = list(mainwin.GetChildren())
                log(f"  [诊断] searchDepth=15 共 {len(all_descendants)} 个子控件:")
                for c in all_descendants[:30]:
                    try:
                        log(f"    ClassName={c.ClassName or '(空)'} | Type={c.ControlTypeName or '(空)'} | Name={(c.Name or '')[:60]}")
                    except:
                        log(f"    (控件读取异常)")
        except Exception as de:
            log(f"  [诊断] mainwin 子控件遍历异常: {str(de)[:100]}")

        return []

    except Exception as e:
        es = str(e)
        if "无法调用任何订户" in es or "-2147220991" in es or "-2147024891" in es or "RPC" in es:
            log(f"COM断开: {es[:80]}")
        else:
            log(f"read_sessions异常: {es[:120]}")
        return None


def reset_uia():
    import pythoncom
    try: pythoncom.CoUninitialize()
    except: pass
    time.sleep(1)
    pythoncom.CoInitialize()
    log("UIA COM 已完全重置")


# ══════════════════════════════════════════════════════════════════
#                     后台进程
# ══════════════════════════════════════════════════════════════════
def run_background():
    """--background 模式"""
    kernel32 = ctypes.windll.kernel32
    hwnd_console = kernel32.GetConsoleWindow()

    if hwnd_console:
        ctypes.windll.user32.ShowWindow(hwnd_console, 6)
        log("CMD 已最小化到任务栏")

    h_mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    err = ctypes.get_last_error()
    if not h_mutex:
        log(f"后台互斥体创建失败! Error={err}")
        return
    log(f"后台互斥体已持有 (h={h_mutex}, err={err})")

    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    log(f"PID 文件已写入: {os.getpid()}")

    import pythoncom
    pythoncom.CoInitialize()
    log(f"=== 后台监听进程启动 PID={os.getpid()} v0.2.4 ===")

    import faulthandler
    faulthandler.enable()  # access violation 时打印 C 层堆栈到 stderr

    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    root.title("WeChatNotifierBG")

    records = {}
    if RECORDS_FILE.exists():
        try:
            records = json.loads(RECORDS_FILE.read_text(encoding="utf-8"))
            log(f"加载 {len(records)} 条历史记录")
        except:
            pass

    empty_count = 0
    com_error_count = 0
    reset_cycle = 0
    MAX_EMPTY_BEFORE_RESET = 50
    MAX_COM_ERRORS = 5

    def _check_exit():
        try:
            s = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if s.get("exit_requested"):
                s.pop("exit_requested", None)
                SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
                log("收到退出请求，停止后台监听")
                root.quit()
                return
        except:
            pass
        root.after(500, _check_exit)

    def _poll():
        nonlocal empty_count, records, com_error_count, reset_cycle

        # ── 窗口预检: FindWindowW ~0ms ──
        wechat_hwnd = _wechat_window_handle()
        if not wechat_hwnd:
            root.after(int(SCAN_INTERVAL * 1000), _poll)
            return

        next_interval = int(SCAN_INTERVAL * 1000)


        try:
            sessions = read_sessions(wechat_hwnd)
            if sessions is None:
                com_error_count += 1
                log(f"read=None ({com_error_count}/{MAX_COM_ERRORS})")
                if com_error_count >= MAX_COM_ERRORS:
                    reset_cycle += 1
                    log(f"连续 {com_error_count} 次 COM 断连，触发 UIA 重置 (#{reset_cycle})")
                    reset_uia()
                    com_error_count = 0
                    next_interval = 0
                    if reset_cycle >= 3:
                        log(f"重置已连续 {reset_cycle} 次，延长等待 5s")
                        time.sleep(5)
            elif not sessions:
                empty_count += 1
                if empty_count >= MAX_EMPTY_BEFORE_RESET:
                    log(f"连续 {empty_count} 次空会话，触发 UIA 完全重置")
                    reset_uia()
                    empty_count = 0
            else:
                if reset_cycle > 0:
                    log(f"COM 已恢复 (历经 {reset_cycle} 次重置)")
                reset_cycle = 0
                empty_count = 0
                com_error_count = 0
                for s in sessions:
                    user = s["user"]
                    msg = s["msg"]
                    if user in BLACK_LIST:
                        records[user] = msg
                        continue
                    if "免打扰" in s.get("raw", ""):
                        records[user] = msg
                        continue
                    if records.get(user) == msg:
                        continue
                    if not s["unread"]:
                        continue
                    un = s["unread"].replace("条未读", "")
                    title = f"{user}({un})" if un.isdigit() else user
                    body = msg[:50] + ("..." if len(msg) > 50 else "")
                    log(f"弹窗: {title} -> {body}")
                    _show_toast(root, title, body)
                    records[user] = msg
                RECORDS_FILE.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            log(f"poll异常: {traceback.format_exc()}")
        root.after(next_interval, _poll)

    root.after(int(SCAN_INTERVAL * 1000), _poll)
    root.after(500, _check_exit)

    if hwnd_console:
        ctypes.windll.user32.ShowWindow(hwnd_console, 0)
        log("CMD 已从任务栏隐身")

    log("进入 mainloop")
    root.mainloop()

    try: PID_FILE.unlink()
    except: pass
    try:
        import pythoncom
        pythoncom.CoUninitialize()
        log("主线程 COM 已清理")
    except: pass
    log("=== 后台监听进程已退出 ===")


# ══════════════════════════════════════════════════════════════════
#                    启动后台子进程
# ══════════════════════════════════════════════════════════════════
def launch_background():
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            SYNCHRONIZE = 0x00100000
            h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, old_pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                log(f"后台进程 PID={old_pid} 仍在运行，跳过启动")
                return False
        except:
            pass

    if getattr(sys, "frozen", False):
        exe = sys.executable
    else:
        exe = sys.executable.replace("python.exe", "pythonw.exe")

    args = [exe, "--background"]
    if not getattr(sys, "frozen", False):
        args = [exe, str(Path(__file__).resolve()), "--background"]

    CREATE_NO_WINDOW = 0x08000000

    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        subprocess.Popen(
            args,
            startupinfo=si,
            creationflags=CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log("后台进程已启动（无窗口）")
    except Exception as e:
        log(f"后台进程启动失败: {e}")
        return False

    # Wait for background worker to write its PID, then clean up any stray bootloader process
    for _ in range(30):  # max 3 seconds
        time.sleep(0.1)
        if PID_FILE.exists():
            try:
                bg_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
                if bg_pid != os.getpid():
                    _cleanup_stray_processes(bg_pid)
                break
            except:
                pass

    return True


def _cleanup_stray_processes(keep_pid):
    """Terminate PyInstaller bootloader processes that aren't the console or background worker."""
    current_pid = os.getpid()
    exe_name = Path(sys.executable).name
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().split("\n"):
            parts = line.replace('"', "").split(",")
            if len(parts) >= 2:
                try:
                    pid = int(parts[1].strip())
                    if pid not in (current_pid, keep_pid):
                        log(f"清理多余进程 PID={pid}")
                        ctypes.windll.kernel32.TerminateProcess(
                            ctypes.windll.kernel32.OpenProcess(1, False, pid), 0
                        )
                except (ValueError, OSError):
                    pass
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════
#                     控制台菜单
# ══════════════════════════════════════════════════════════════════
def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")

BANNER = """
  =====================================================
             微信消息通知器 v0.2.4  控制台版
                     作者: 不理笨笨
  ====================================================="""


def _build_narrator_warning():
    """Build NARRATOR_WARNING with centered title and CJK-aware alignment."""
    def _dw(s):
        w = 0
        for ch in s:
            w += 2 if unicodedata.east_asian_width(ch) in 'FW' else 1
        return w

    content = [
        "!! 重要提醒 !!",
        "",
        "如果工具无法正常捕获消息, 请按以下步骤:",
        "本操作只需进行一次即可永久解决问题",
        "1. 彻底退出微信",
        '2. 点击开始菜单，在 "Windows 轻松使用"',
        "    展开后即可看到讲述人并点击启动",
        "3. 重新启动并登录微信",
        "4. 登录完成后可关闭讲述人",
        "",
        "微信可以最小化到任务栏，但不要关闭",
        "关闭本窗口不影响通知器后台运行",
    ]
    lines = ["  +" + "=" * 51 + "+"]
    # Title centered in 47-char content area (box width 55: "  |  " (5) + content(47) + "  |" (3) = 55)
    title = content[0]
    dw_title = _dw(title)
    padL = (47 - dw_title) // 2
    padR = 47 - dw_title - padL
    lines.append("  |  " + " " * padL + title + " " * padR + "  |")
    for line in content[1:]:
        dw = _dw(line)
        pad = 47 - dw
        lines.append("  |  " + line + " " * max(0, pad) + "  |")
    lines.append("  +" + "=" * 51 + "+")
    return "\n".join(lines)


NARRATOR_WARNING = "\n" + _build_narrator_warning()

def console_loop(is_primary):
    """主菜单：1-4 切换快捷键  5 开机自启  6 退出"""

    def _dw(s):
        w = 0
        for ch in s:
            w += 2 if unicodedata.east_asian_width(ch) in 'FW' else 1
        return w

    def _pad(s, target=44):
        """Pad to target display width (prefix 2 spaces already accounted)"""
        dw = _dw(s)
        return s + " " * max(0, target - dw)

    def show_main():
        clear_screen()
        print(BANNER)
        print(NARRATOR_WARNING)
        print()
        shortcut = get_shortcut_combo()
        auto_start = get_auto_start()
        as_status = "已开启" if auto_start else "已关闭"
        presets = list(SHORTCUT_PRESETS.keys())
        p = presets  # ["Alt+X", "Ctrl+Alt+W", "Alt+W"]
        divider = "  " + "=" * 53
        print(divider)
        print("  " + _pad(f"唤醒微信快捷键(当前: {shortcut}) ", 53))
        opt_line = f"[1] {p[0]}   [2] {p[1]}  [3] {p[2]}  [4] 关闭快捷键"
        print("  " + _pad(opt_line, 53))
        print("  " + _pad(f"[5] 开机自启[{as_status}]", 53))
        print("  " + _pad("[6] 彻底退出通知器(停止后台监听)", 53))
        print(divider)

    show_main()
    n_presets = len(SHORTCUT_PRESETS)

    while True:
        try:
            choice = input("  请输入选项 [1-6] > ").strip()
        except (EOFError, KeyboardInterrupt, RuntimeError):
            print()
            print("  控制面板已关闭，通知器后台继续运行。")
            return

        if choice.isdigit():
            idx = int(choice)
            presets = list(SHORTCUT_PRESETS.keys())
            if 1 <= idx <= n_presets:
                set_shortcut_combo(presets[idx - 1])
                print(f"  已切换为: {presets[idx - 1]}")
            elif idx == n_presets + 1:
                set_shortcut_combo("关闭")
                print("  快捷键已关闭")
            elif idx == n_presets + 2:
                auto_start = get_auto_start()
                new_state = not auto_start
                set_auto_start(new_state)
                status_str = "已启用（VBS计划任务，登录时无CMD闪现）" if new_state else "已禁用"
                print(f"  开机自启: {status_str}")
            elif idx == n_presets + 3:
                print()
                print("  正在退出通知器...")
                s = load_settings()
                s["exit_requested"] = True
                save_settings(s)
                log("已向后台进程发送退出请求")
                time.sleep(1)
                return
            else:
                print("  无效选项")
        else:
            print("  无效选项")

        time.sleep(0.5)
        show_main()


# ══════════════════════════════════════════════════════════════════
#                          主入口
# ══════════════════════════════════════════════════════════════════
def main():
    if "--background" in sys.argv:
        run_background()
        return

    if getattr(sys, "frozen", False):
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleTitleW("微信消息通知器 v0.2.4")

    is_primary = True
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            SYNCHRONIZE = 0x00100000
            h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, old_pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                is_primary = False
                log(f"已有后台进程 PID={old_pid}，不再启动新的")
        except:
            pass

    if is_primary:
        log(f"=== 微信通知器 v0.2.4 首次启动 (PID={os.getpid()}) ===")
        if launch_background():
            time.sleep(1)
    else:
        log(f"=== 已有实例在运行，仅打开控制面板 (PID={os.getpid()}) ===")

    console_loop(is_primary=is_primary)
    log("控制面板已关闭")


if __name__ == "__main__":
    main()
