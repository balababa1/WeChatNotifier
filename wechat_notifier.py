"""
微信消息通知器 - CMD控制台版
v0.2.9: rip_xrefs 去 numpy 化（字节级纯 Python 实现），无 numpy 环境也不再
        多出一个距目标 59.9MB 的无关 gate 候选，候选集合与有 numpy 时一致
v0.2.8: 新增 UIA gate 判定层（启动抓到 weixin.exe 先查 Qt 无障碍标志字节，
        为 1 跳过修改、不为 1 才写入 1，再执行原有抓取流程）
v0.2.7: 正式版（异常可见化 + 清理死常量 + 控制台面板精简 + 去诊断日志）
作者: 不理笨笨
"""
import os, sys, re, json, time, traceback, ctypes, subprocess, threading, unicodedata
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
VERSION      = "v0.2.10"
BLACK_LIST   = {"服务号", "公众号", "文件传输助手", "折叠的聊天", "服务通知", "微信团队"}
TIME_WORDS   = {"昨天", "今天", "上午", "下午", "刚刚"}

SHORTCUT_PRESETS = {
    "Alt+X":       {"mods": [0x12],           "key": 0x58},
    "Ctrl+Alt+W":  {"mods": [0x11, 0x12],     "key": 0x57},
    "Alt+W":       {"mods": [0x12],           "key": 0x57},
}
DEFAULT_SHORTCUT = "Ctrl+Alt+W"

MUTEX_NAME = "Global\\WeChatNotifier_BG_v4"
RESET_BACKOFF_MS = 30000   # 连续多次 COM 重置后的非阻塞退避间隔
PID_FILE = DATA_DIR / "bg_pid.txt"


# ── Toast 弹窗配置 ──
NOTIFY_W   = 380
NOTIFY_H   = 110
MARGIN     = 20
DURATION   = 5000
CORNER_R   = 16


# ══════════════════════════════════════════════════════════════════
#                          日志
# ══════════════════════════════════════════════════════════════════
# 调试日志开关：测试版 True；正式版置 False 后诊断日志全部静默、不落盘
DEBUG_LOG = False

_LOG_MAX_SIZE = 1 * 1024 * 1024  # 1MB 轮转阈值
_LOG_BACKUP   = DATA_DIR / "debug.log.1"

ERROR_LOG     = DATA_DIR / "error.log"
_ERR_MAX_SIZE = 512 * 1024       # error.log 超 512KB 轮转为 error.log.1


def _note_err(where, exc=None):
    """异常可见化：把原本被静默吞掉的异常追加写入 data/error.log。

    仅在异常真正发生时调用，正常路径零开销；写日志自身再失败则彻底放弃，
    绝不让"记录异常"变成新的异常源。单文件超 _ERR_MAX_SIZE 轮转为 .1。
    """
    try:
        if exc is not None:
            detail = "".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__)).strip()
        else:
            detail = traceback.format_exc().strip()
        try:
            if ERROR_LOG.exists() and ERROR_LOG.stat().st_size >= _ERR_MAX_SIZE:
                _bak = ERROR_LOG.with_suffix(".log.1")
                if _bak.exists():
                    _bak.unlink()
                ERROR_LOG.replace(_bak)
        except Exception:
            pass
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"{where}: {detail}\n")
    except Exception:
        pass


def _write_log(line):
    """原始写入（不带时间戳），调用方负责格式化"""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        _note_err("_write_log", e)

def _rotate_log():
    """debug.log 超过阈值时轮转为 debug.log.1"""
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size >= _LOG_MAX_SIZE:
            try:
                if _LOG_BACKUP.exists():
                    _LOG_BACKUP.unlink()
            except Exception as e:
                _note_err("_rotate_log.old_unlink", e)
            LOG_FILE.replace(_LOG_BACKUP)
    except Exception as e:
        _note_err("_rotate_log", e)

def log(msg):
    """写入 data/debug.log：带时间戳、超 1MB 轮转。"""
    try:
        _rotate_log()
        _write_log(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")
    except Exception as e:
        _note_err("log", e)

def logd(msg):
    """诊断日志：仅 DEBUG_LOG=True（测试版）时落盘，正式版完全静默。"""
    if not DEBUG_LOG:
        return
    try:
        _rotate_log()
        _write_log(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}][D] {msg}")
    except Exception as e:
        _note_err("logd", e)


# ══════════════════════════════════════════════════════════════════
#                        设置管理
# ══════════════════════════════════════════════════════════════════
def load_settings():
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            _note_err("load_settings", e)
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
    except Exception as e:
        _note_err("_clean_registry_auto_start", e)

def get_auto_start():
    try:
        # 只关心返回码，丢掉输出可省一次管道解码（并规避 GBK 解码异常）
        result = subprocess.run(
            ["schtasks", "/query", "/tn", TASK_NAME],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5
        )
        return result.returncode == 0
    except Exception as e:
        _note_err("get_auto_start", e)
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
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15
            )
            if result.returncode == 0:
                subprocess.run(
                    ["schtasks", "/change", "/tn", TASK_NAME, "/settings", "HIDDEN"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10
                )
                return True
            else:
                return False
        else:
            result = subprocess.run(
                ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10
            )
            return True
    except Exception as e:
        _note_err("set_auto_start", e)
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
#   PIL 超采样渲染 + 分层窗口（每像素 alpha，消除圆角锯齿）
#   4 倍尺寸绘制 → LANCZOS 缩放 → UpdateLayeredWindow 贴 32bpp BGRA
# ══════════════════════════════════════════════════════════════════
_SS = 4                        # 超采样倍率
_bg_cache = {}                 # (kind, w, h) -> RGBA Image（静态底图缓存）
_text_cache = {}               # (kind, w, h, title, body) -> RGBA Image（文字层缓存）
_TEXT_CACHE_MAX = 128
_font_cache = {}
_FONT_CANDIDATES = {
    "normal": ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyh.ttf"),
    "bold":   ("C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyhbd.ttf"),
}

# ── 弹窗配色（红包 / 普通）──
RP_RED, RP_GOLD, RP_GOLD_D = "#D9443A", "#F5D07A", "#E3B65A"
RP_GOLD_L, RP_TITLE, RP_BODY, RP_SEAL_FG = "#FBE7B4", "#FFE9A8", "#FFF3DC", "#B0341F"
NT_BG, NT_HEADER, NT_TITLE, NT_BODY = "#1C1C1E", "#8E8E93", "#FFFFFF", "#AEAEB2"

RP_ALPHA = int(round(0.97 * 255))   # 红包弹窗整体不透明度（原 -alpha 0.97）
NT_ALPHA = int(round(0.93 * 255))   # 普通弹窗整体不透明度（原 -alpha 0.93）


def _enable_dpi_awareness():
    """在 Tk 初始化前声明 DPI 感知，使 Tk 坐标 / GetSystemMetrics /
    GetDeviceCaps 三者单位一致（均为物理像素）。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
        return True
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return True
    except Exception:
        return False


def _rgba(hexstr):
    h = hexstr.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)


def _pt2px(pt, s):
    """Tk 字号（点）→ 像素（96 DPI 下 1pt = 4/3 px）"""
    return max(1, int(round(pt * s * 96.0 / 72.0)))


def _load_font(bold, px):
    key = (bool(bold), int(px))
    f = _font_cache.get(key)
    if f is not None:
        return f
    from PIL import ImageFont
    f = None
    for path in _FONT_CANDIDATES["bold" if bold else "normal"]:
        try:
            f = ImageFont.truetype(path, int(px))
            break
        except Exception:
            f = None
    if f is None:
        try:
            f = ImageFont.load_default()
        except Exception:
            f = None
    _font_cache[key] = f
    return f


# ── emoji 支持：Segoe UI Emoji 分段渲染 ──
# 雅黑不含 emoji 字形，直接绘制会显示为空心方框；
# 此处按 Unicode 码段把文本拆成「文字段 + emoji 段」，
# emoji 段改用 seguiemj.ttf，并与文字段共用同一条基线（anchor="ls"），
# 解决两种字体 ascent/descent 不一致导致的垂直错位。
# 字体走系统绝对路径、不参与 PyInstaller 打包；缺失时自动回退为原单字体渲染。
_emoji_font_cache = {}
_EMOJI_FONT_CANDIDATES = (
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "seguiemj.ttf"),
    "C:/Windows/Fonts/seguiemj.ttf",
)


def _load_emoji_font(px):
    """加载 Segoe UI Emoji；不可用时返回 None（调用方回退纯雅黑渲染）"""
    px = int(px)
    if px <= 0:
        return None
    f = _emoji_font_cache.get(px)
    if f is not None:
        return f
    from PIL import ImageFont
    f = None
    for path in _EMOJI_FONT_CANDIDATES:
        try:
            f = ImageFont.truetype(path, px)
            break
        except Exception:
            f = None
    _emoji_font_cache[px] = f
    return f


def _is_emoji_char(ch):
    """emoji / 符号区判定（箭头、几何图形、dingbats、emoji 主体、区域指示符等）"""
    o = ord(ch)
    return (
        0x2190 <= o <= 0x21FF or      # 箭头
        0x2300 <= o <= 0x23FF or      # 杂项技术符号
        0x2460 <= o <= 0x24FF or      # 带圈数字字母
        0x25A0 <= o <= 0x27BF or      # 几何图形 / 杂项符号 / dingbats
        0x2B00 <= o <= 0x2BFF or      # 杂项符号与箭头
        0x1F000 <= o <= 0x1FAFF or    # 麻将 / 扑克 / 补充符号 / emoji 主体
        0x1F1E6 <= o <= 0x1F1FF       # 区域指示符（国旗）
    )


def _split_runs(text):
    """把文本拆成 [(片段, 是否 emoji), ...] 连续段；
    U+FE0F / U+FE0E 变体选择符与 ZWJ 并入前一段，避免被孤立渲染。"""
    runs = []
    cur = []
    cur_emoji = False
    for ch in text:
        if ch in ("\ufe0f", "\ufe0e", "\u200d"):
            cur.append(ch)
            continue
        e = _is_emoji_char(ch)
        if cur and e != cur_emoji:
            runs.append(("".join(cur), cur_emoji))
            cur = []
        cur_emoji = e
        cur.append(ch)
    if cur:
        runs.append(("".join(cur), cur_emoji))
    return runs


def _has_emoji(text):
    return any(_is_emoji_char(ch) for ch in text)


def _text_width(draw, text, font):
    """按分段字体度量单行宽度（emoji 段用 emoji 字体）"""
    if not text or font is None:
        return 0
    try:
        f_emoji = _load_emoji_font(getattr(font, "size", 0)) if _has_emoji(text) else None
        if f_emoji is None:
            return draw.textlength(text, font=font)
        total = 0
        for run, e in _split_runs(text):
            total += draw.textlength(run, font=(f_emoji if e else font))
        return total
    except Exception:
        try:
            return draw.textlength(text, font=font)
        except Exception:
            return 0


def _draw_text_mixed(d, x, y, text, f_text, fill, anchor="la"):
    """分段绘制单行文本：文字段用 f_text，emoji 段换 Segoe UI Emoji，
    两段共用同一基线以保证垂直对齐（anchor 支持 la / lm / ls）。"""
    if not text or f_text is None:
        return
    f_emoji = _load_emoji_font(getattr(f_text, "size", 0)) if _has_emoji(text) else None
    if f_emoji is None:
        d.text((x, y), text, font=f_text, fill=fill, anchor=anchor)
        return
    try:
        asc, desc = f_text.getmetrics()
    except Exception:
        asc, desc = 0, 0
    if anchor == "lm":
        y_base = y + (asc - desc) // 2
    elif anchor == "la":
        y_base = y + asc
    else:
        y_base = y
    for run, e in _split_runs(text):
        f = f_emoji if e else f_text
        d.text((x, y_base), run, font=f, fill=fill, anchor="ls")
        x += d.textlength(run, font=f)


def _fit_text(draw, text, font, max_w):
    """单行宽度截断（超出补 ...），避免文字溢出弹窗（emoji 段按 emoji 字体度量）"""
    if font is None:
        return text
    try:
        if _text_width(draw, text, font) <= max_w:
            return text
        while text and _text_width(draw, text + "...", font) > max_w:
            text = text[:-1]
        return text + "..."
    except Exception:
        return text


def _render_redpacket_bg(w, h, s):
    """红包静态底图：圆角主体 + 金色封口条 + 金色圆形「開」钮（4x 超采样 → LANCZOS）"""
    key = ("rp_bg", w, h)
    cached = _bg_cache.get(key)
    if cached is not None:
        return cached
    from PIL import Image, ImageDraw
    S = _SS
    W, H = w * S, h * S
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 主体圆角
    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=int(CORNER_R * s) * S,
                        fill=_rgba(RP_RED))
    # 顶部金色封口条 + 下沿描边
    x0 = int(CORNER_R * s) * S
    bar_h = max(3, int(6 * s)) * S
    d.rectangle([x0, 0, W - 1 - x0, bar_h], fill=_rgba(RP_GOLD))
    lw = max(1, int(1 * s)) * S
    d.rectangle([x0, bar_h, W - 1 - x0, bar_h + lw - 1], fill=_rgba(RP_GOLD_D))
    # 左侧金色圆形「開」钮
    cx, cy = int(52 * s) * S, H // 2
    rad = int(26 * s) * S
    d.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=_rgba(RP_GOLD_L),
              outline=_rgba(RP_GOLD_D), width=max(2, int(2 * s)) * S)
    ir = rad - int(4 * s) * S
    d.ellipse([cx - ir, cy - ir, cx + ir, cy + ir], outline=_rgba(RP_GOLD),
              width=max(1, int(1 * s)) * S)
    f_seal = _load_font(True, _pt2px(15, s) * S)
    if f_seal is not None:
        d.text((cx, cy), "開", font=f_seal, fill=_rgba(RP_SEAL_FG), anchor="mm")
    img = img.resize((w, h), Image.LANCZOS)
    _bg_cache[key] = img
    return img


def _render_redpacket_text(w, h, s, title, body):
    """红包文字层（标题 + 正文，ImageFont 渲染，透明底）"""
    key = ("rp_txt", w, h, title, body)
    cached = _text_cache.get(key)
    if cached is not None:
        return cached
    from PIL import Image, ImageDraw
    S = _SS
    W, H = w * S, h * S
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    tx = (int(52 * s) + int(26 * s) + int(16 * s)) * S
    cy = H // 2
    max_w = W - tx - int(16 * s) * S
    f_t = _load_font(True, _pt2px(12, s) * S)
    f_b = _load_font(False, _pt2px(10, s) * S)
    if f_t is not None:
        _draw_text_mixed(d, tx, cy - int(16 * s) * S,
                         _fit_text(d, title[:26], f_t, max_w),
                         f_t, _rgba(RP_TITLE), "lm")
    if f_b is not None:
        bt = body[:42] + ("..." if len(body) > 42 else "")
        _draw_text_mixed(d, tx, cy + int(14 * s) * S,
                         _fit_text(d, bt, f_b, max_w),
                         f_b, _rgba(RP_BODY), "lm")
    img = img.resize((w, h), Image.LANCZOS)
    if len(_text_cache) > _TEXT_CACHE_MAX:
        _text_cache.clear()
    _text_cache[key] = img
    return img


def _render_toast_bg(w, h, s):
    """普通弹窗静态底图：深灰圆角主体"""
    key = ("nt_bg", w, h)
    cached = _bg_cache.get(key)
    if cached is not None:
        return cached
    from PIL import Image, ImageDraw
    S = _SS
    W, H = w * S, h * S
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=int(CORNER_R * s) * S,
                        fill=_rgba(NT_BG))
    img = img.resize((w, h), Image.LANCZOS)
    _bg_cache[key] = img
    return img


def _render_toast_text(w, h, s, title, body):
    """普通弹窗文字层（来源标签 + 标题 + 正文）"""
    key = ("nt_txt", w, h, title, body)
    cached = _text_cache.get(key)
    if cached is not None:
        return cached
    from PIL import Image, ImageDraw
    S = _SS
    W, H = w * S, h * S
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad_x = int(16 * s) * S
    max_w = W - pad_x - int(12 * s) * S
    f_h = _load_font(False, _pt2px(9, s) * S)
    f_t = _load_font(True, _pt2px(12, s) * S)
    f_b = _load_font(False, _pt2px(10, s) * S)
    if f_h is not None:
        d.text((pad_x, int(12 * s) * S), "微信", font=f_h,
               fill=_rgba(NT_HEADER), anchor="la")
    if f_t is not None:
        _draw_text_mixed(d, pad_x, int(37 * s) * S,
                         _fit_text(d, title[:30], f_t, max_w),
                         f_t, _rgba(NT_TITLE), "la")
    if f_b is not None:
        bt = body[:60] + ("..." if len(body) > 60 else "")
        _draw_text_mixed(d, pad_x, int(58 * s) * S,
                         _fit_text(d, bt, f_b, max_w),
                         f_b, _rgba(NT_BODY), "la")
    img = img.resize((w, h), Image.LANCZOS)
    if len(_text_cache) > _TEXT_CACHE_MAX:
        _text_cache.clear()
    _text_cache[key] = img
    return img


def _compose(bg, layer):
    """静态底图 + 文字层合成（copy 避免污染缓存）"""
    img = bg.copy()
    img.alpha_composite(layer)
    return img


# ── 分层窗口所需的 Win32 声明 ──
WS_EX_LAYERED = 0x00080000
GWL_EXSTYLE = -20
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
BI_RGB = 0
DIB_RGB_COLORS = 0

from ctypes import wintypes as _wt

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32


class _POINT(ctypes.Structure):
    _fields_ = [("x", _wt.LONG), ("y", _wt.LONG)]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", _wt.LONG), ("cy", _wt.LONG)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", _wt.DWORD), ("biWidth", _wt.LONG), ("biHeight", _wt.LONG),
                ("biPlanes", _wt.WORD), ("biBitCount", _wt.WORD),
                ("biCompression", _wt.DWORD), ("biSizeImage", _wt.DWORD),
                ("biXPelsPerMeter", _wt.LONG), ("biYPelsPerMeter", _wt.LONG),
                ("biClrUsed", _wt.DWORD), ("biClrImportant", _wt.DWORD)]


class _RGBQUAD(ctypes.Structure):
    _fields_ = [("rgbBlue", ctypes.c_ubyte), ("rgbGreen", ctypes.c_ubyte),
                ("rgbRed", ctypes.c_ubyte), ("rgbReserved", ctypes.c_ubyte)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", _RGBQUAD * 1)]


def _setup_win32_api():
    u, g = _user32, _gdi32
    u.GetDC.argtypes = [_wt.HWND]
    u.GetDC.restype = _wt.HDC
    u.ReleaseDC.argtypes = [_wt.HWND, _wt.HDC]
    u.ReleaseDC.restype = ctypes.c_int
    u.GetParent.argtypes = [_wt.HWND]
    u.GetParent.restype = _wt.HWND
    u.GetWindowLongW.argtypes = [_wt.HWND, ctypes.c_int]
    u.GetWindowLongW.restype = _wt.LONG
    u.SetWindowLongW.argtypes = [_wt.HWND, ctypes.c_int, _wt.LONG]
    u.SetWindowLongW.restype = _wt.LONG
    u.UpdateLayeredWindow.argtypes = [_wt.HWND, _wt.HDC, ctypes.POINTER(_POINT),
                                      ctypes.POINTER(_SIZE), _wt.HDC, ctypes.POINTER(_POINT),
                                      _wt.DWORD, ctypes.POINTER(_BLENDFUNCTION), _wt.DWORD]
    u.UpdateLayeredWindow.restype = _wt.BOOL
    g.CreateCompatibleDC.argtypes = [_wt.HDC]
    g.CreateCompatibleDC.restype = _wt.HDC
    g.CreateDIBSection.argtypes = [_wt.HDC, ctypes.POINTER(_BITMAPINFO), _wt.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), _wt.HANDLE, _wt.DWORD]
    g.CreateDIBSection.restype = _wt.HANDLE
    g.SelectObject.argtypes = [_wt.HDC, _wt.HANDLE]
    g.SelectObject.restype = _wt.HANDLE
    g.DeleteObject.argtypes = [_wt.HANDLE]
    g.DeleteObject.restype = _wt.BOOL
    g.DeleteDC.argtypes = [_wt.HDC]
    g.DeleteDC.restype = _wt.BOOL


_setup_win32_api()


class _LayeredToast(object):
    """分层弹窗：Tk Toplevel(overrideredirect + topmost) 加 WS_EX_LAYERED，
    用 UpdateLayeredWindow 贴 32bpp BGRA 位图，实现真正的每像素 alpha 圆角。"""

    def __init__(self, root, image, x, y, const_alpha=255):
        import tkinter as tk
        self.image = image
        self.w, self.h = image.size
        self.const_alpha = int(const_alpha)
        self.pos = (int(x), int(y))
        self._surfaces = []
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.geometry(f"{self.w}x{self.h}+{self.pos[0]}+{self.pos[1]}")
        self.win.update_idletasks()
        inner = self.win.winfo_id()
        parent = _user32.GetParent(inner)
        # 顶层 wrapper + 内层 TkChild 一并分层，确保任意一层都不残留不透明底色
        for hwnd, is_top in ((parent, True), (inner, False)):
            if hwnd:
                self._attach(hwnd, is_top)
        self.blit(*self.pos)

    def _attach(self, hwnd, is_top):
        try:
            ex = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
            hdc_dst = _user32.GetDC(0)
            hdc_src = _gdi32.CreateCompatibleDC(hdc_dst)
            bmi = _BITMAPINFO()
            hdr = bmi.bmiHeader
            hdr.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            hdr.biWidth = self.w
            hdr.biHeight = -self.h          # 负值 = top-down
            hdr.biPlanes = 1
            hdr.biBitCount = 32
            hdr.biCompression = BI_RGB
            bits = ctypes.c_void_p()
            hbmp = _gdi32.CreateDIBSection(hdc_dst, ctypes.byref(bmi), DIB_RGB_COLORS,
                                           ctypes.byref(bits), None, 0)
            _user32.ReleaseDC(0, hdc_dst)
            if not hbmp or not bits:
                return False
            data = self.image.tobytes("raw", "BGRA")
            ctypes.memmove(bits, data, len(data))
            _gdi32.SelectObject(hdc_src, hbmp)
            self._surfaces.append((hwnd, hdc_src, hbmp, bool(is_top)))
            return True
        except Exception as e:
            return False

    def blit(self, x=None, y=None):
        if x is None or y is None:
            x, y = self.pos
        self.pos = (int(x), int(y))
        hdc_dst = _user32.GetDC(0)
        ok = False
        try:
            for hwnd, hdc_src, hbmp, is_top in self._surfaces:
                px, py = self.pos if is_top else (0, 0)
                pt_src = _POINT(0, 0)
                pt_dst = _POINT(int(px), int(py))
                size = _SIZE(self.w, self.h)
                bf = _BLENDFUNCTION(AC_SRC_OVER, 0, self.const_alpha, AC_SRC_ALPHA)
                if _user32.UpdateLayeredWindow(hwnd, hdc_dst, ctypes.byref(pt_dst),
                                               ctypes.byref(size), hdc_src, ctypes.byref(pt_src),
                                               0, ctypes.byref(bf), ULW_ALPHA):
                    ok = True
        finally:
            _user32.ReleaseDC(0, hdc_dst)
        return ok

    def move(self, x, y):
        try:
            self.win.geometry(f"{self.w}x{self.h}+{int(x)}+{int(y)}")
            self.win.update_idletasks()
        except Exception:
            pass
        self.blit(x, y)

    def winfo_exists(self):
        try:
            return bool(self.win.winfo_exists())
        except Exception:
            return False

    def destroy(self):
        for _hwnd, hdc_src, hbmp, _is_top in self._surfaces:
            try:
                _gdi32.DeleteObject(hbmp)
                _gdi32.DeleteDC(hdc_src)
            except Exception:
                pass
        self._surfaces = []
        try:
            self.win.destroy()
        except Exception:
            pass


def _toast_alive(t):
    try:
        return bool(t.winfo_exists())
    except Exception:
        return False


def _place_toast(t, x, y, w, h):
    try:
        if isinstance(t, _LayeredToast):
            t.move(x, y)
        else:
            t.geometry(f"{w}x{h}+{int(x)}+{int(y)}")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════
def _get_scale(root):
    """直接取 Windows 系统 DPI 缩放比，不再从屏幕尺寸推算"""
    try:
        import ctypes
        _u32 = ctypes.windll.user32
        _g32 = ctypes.windll.gdi32
        # 64 位下 HWND/HDC 必须显式声明为指针类型，
        # 否则默认 c_int 会截断高位句柄 → OverflowError，且 ReleaseDC 永不执行造成 GDI 泄漏
        _u32.GetDC.restype = ctypes.c_void_p
        _u32.GetDC.argtypes = [ctypes.c_void_p]
        _u32.ReleaseDC.restype = ctypes.c_int
        _u32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        _g32.GetDeviceCaps.restype = ctypes.c_int
        _g32.GetDeviceCaps.argtypes = [ctypes.c_void_p, ctypes.c_int]
        hdc = _u32.GetDC(None)
        if not hdc:
            raise OSError("GetDC 返回空句柄")
        try:
            dpi = _g32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
        finally:
            _u32.ReleaseDC(None, hdc)          # 异常路径也必须释放，杜绝 GDI 泄漏
        return max(0.5, min(2.0, dpi / 96.0))
    except Exception as e:
        _note_err("_get_scale", e)
        # 兜底：仍用屏幕尺寸推算
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        w_ratio = sw / 2560.0
        h_ratio = sh / 1440.0
        return max(0.5, min(2.0, min(w_ratio, h_ratio)))

def _show_toast(root, title, body):
    """普通消息弹窗（分层窗口 + PIL 超采样，右下角，位置与红包弹窗一致）"""
    s = _get_scale(root)
    nw = int(NOTIFY_W * s)
    nh = int(NOTIFY_H * s)
    margin = int(MARGIN * s)
    bottom_off = int(60 * s)
    gap = int(8 * s)

    # 用 GetSystemMetrics 取物理分辨率，避免 Tkinter DPI 感知不一致
    SM_CXSCREEN = 0
    SM_CYSCREEN = 1
    screen_w = ctypes.windll.user32.GetSystemMetrics(SM_CXSCREEN)
    screen_h = ctypes.windll.user32.GetSystemMetrics(SM_CYSCREEN)

    if not hasattr(root, "active_toasts"):
        root.active_toasts = []

    root.active_toasts = [w for w in root.active_toasts if _toast_alive(w)]

    x = screen_w - nw - margin
    base_y = screen_h - nh - bottom_off

    offset = nh + gap
    for i, existing in enumerate(root.active_toasts):
        new_y = base_y - (len(root.active_toasts) - i) * offset
        _place_toast(existing, x, new_y, nw, nh)

    y = base_y

    img = _compose(_render_toast_bg(nw, nh, s),
                   _render_toast_text(nw, nh, s, title, body))
    tw = _LayeredToast(root, img, x, y, const_alpha=NT_ALPHA)

    def close():
        try:
            tw.destroy()
        except Exception:
            pass
        if hasattr(root, "active_toasts"):
            root.active_toasts = [w for w in root.active_toasts if _toast_alive(w)]
            for i, existing in enumerate(root.active_toasts):
                new_y = base_y - (len(root.active_toasts) - 1 - i) * offset
                _place_toast(existing, x, new_y, nw, nh)

    def on_click(e=None):
        activate_wechat_by_shortcut()
        close()

    try:
        tw.win.bind("<Button-1>", on_click)
    except Exception:
        pass

    root.active_toasts.append(tw)
    tw.win.after(DURATION, close)


# ══════════════════════════════════════════════════════════════════
#                  微信红包样式弹窗
# ══════════════════════════════════════════════════════════════════

# ── 红包提示音（仅红包消息，异步播放，失败静默）─────────────────
_RP_SOUND = {"path": None, "resolved": False}


def _sound_path():
    """定位红包提示音文件。打包后位于 _internal/data/notify.wav；
    源码运行时位于脚本同级 data/notify.wav。找不到返回空串。"""
    name = "notify.wav"
    cands = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cands.append(os.path.join(base, "data", name))
        cands.append(os.path.join(base, name))
    try:
        cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", name))
    except Exception:
        pass
    try:
        cands.append(os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "data", name))
    except Exception:
        pass
    for p in cands:
        try:
            if os.path.exists(p):
                return p
        except Exception:
            continue
    return ""


def _play_redpacket_sound():
    """播放红包提示音（winsound 异步，单次 <0.1ms，异常静默）。
    仅由 _show_redpacket_toast 调用；普通消息弹窗 _show_toast 不调用。"""
    try:
        import winsound
        if not _RP_SOUND["resolved"]:
            _RP_SOUND["path"] = _sound_path()
            _RP_SOUND["resolved"] = True
        p = _RP_SOUND["path"]
        if p:
            winsound.PlaySound(
                p, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT
            )
    except Exception:
        pass


def _is_redpacket(msg):
    """判断会话列表消息是否为微信红包。
    微信红包在会话列表中的消息类型标签为「[微信红包]」（私聊：'[微信红包] 恭喜发财，大吉大利'；
    群聊：'发送者昵称: [微信红包] ...'）。领取提示类消息（如'XX领取了你的红包'）不含该标签。"""
    if not msg:
        return False
    return "[微信红包]" in msg


def _show_redpacket_toast(root, title, body):
    """微信红包样式弹窗（分层窗口 + PIL 超采样，右下角，位置与普通弹窗一致）"""
    s = _get_scale(root)
    nw = int(380 * s)
    nh = int(110 * s)
    margin = int(20 * s)
    bottom_off = int(60 * s)
    gap = int(8 * s)

    SM_CXSCREEN = 0
    SM_CYSCREEN = 1
    screen_w = ctypes.windll.user32.GetSystemMetrics(SM_CXSCREEN)
    screen_h = ctypes.windll.user32.GetSystemMetrics(SM_CYSCREEN)

    if not hasattr(root, "active_toasts"):
        root.active_toasts = []
    root.active_toasts = [w for w in root.active_toasts if _toast_alive(w)]

    x = screen_w - nw - margin
    base_y = screen_h - nh - bottom_off
    offset = nh + gap
    for i, existing in enumerate(root.active_toasts):
        new_y = base_y - (len(root.active_toasts) - i) * offset
        _place_toast(existing, x, new_y, nw, nh)

    y = base_y

    img = _compose(_render_redpacket_bg(nw, nh, s),
                   _render_redpacket_text(nw, nh, s, title, body))
    tw = _LayeredToast(root, img, x, y, const_alpha=RP_ALPHA)

    # 仅红包消息：弹窗出现即异步播放提示音（异常静默，绝不影响弹窗）
    _play_redpacket_sound()

    def close():
        try:
            tw.destroy()
        except Exception:
            pass
        if hasattr(root, "active_toasts"):
            root.active_toasts = [w for w in root.active_toasts if _toast_alive(w)]

    def on_click(e=None):
        activate_wechat_by_shortcut()
        close()

    try:
        tw.win.bind("<Button-1>", on_click)
    except Exception:
        pass

    root.active_toasts.append(tw)
    tw.win.after(DURATION, close)


# ══════════════════════════════════════════════════════════════════
#                     UIA 消息解析
# ══════════════════════════════════════════════════════════════════



_wechat_hwnd_cache = [0]


def _wechat_window_handle():
    """快速嗅探微信主窗口 HWND（命中缓存 ~0ms，无需 COM），返回 0 表示未找到。

    注意: 微信 4.x 的 Win32 层类名是 Qt5xxxxQWindowIcon、标题是当前聊天对象名（动态变化），
    与 UIA 层的 mmui::MainWindow / Weixin 并不一致，因此**不能**用标题匹配定位:
    4.2.x 进程里同时存在多个标题都为 "Weixin" 的隐藏辅助窗口（如 Qt51514QWindowToolSaveBits
    394x70），按标题命中的正是它们，ControlFromHandle 之后拿到的是子级为 0 的 QLabel，
    表现为"静默读不到会话"。现改为按进程名枚举全部顶层窗口后**按特征打分取最高分**，
    辅助窗口 / 托盘消息窗口 / 输入法窗口直接剔除（见 issue #1）。
    """
    u = ctypes.windll.user32

    # 0) 缓存命中（微信未重启时 HWND 不变）
    h = _wechat_hwnd_cache[0]
    try:
        if h and u.IsWindow(h):
            return h
    except Exception as e:
        _note_err("_wechat_window_handle.cache", e)
    _wechat_hwnd_cache[0] = 0

    try:
        import ctypes.wintypes as wt
        k = ctypes.windll.kernel32
        found = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        # 这些窗口的标题同样是 "Weixin"，必须靠类名剔除
        _BAD_CLS = ("ToolSaveBits", "TrayIcon", "SystemMessage",
                    "PowerMessage", "IME", "MSCTF")

        def _cls_of(hwnd):
            b = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(hwnd, b, 256)
            return b.value or ""

        def _cb(hwnd, _lparam):
            pid = wt.DWORD(0)
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return True
            hp = k.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
            if not hp:
                return True
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wt.DWORD(1024)
                if not k.QueryFullProcessImageNameW(hp, 0, buf, ctypes.byref(size)):
                    return True
                n = buf.value.lower()
                if not (n.endswith("\\weixin.exe") or n.endswith("\\wechat.exe")):
                    return True
            except Exception as e:
                _note_err("_wechat_window_handle.enum", e)
                return True
            finally:
                k.CloseHandle(hp)

            cls = _cls_of(hwnd)
            if cls and any(x in cls for x in _BAD_CLS):
                return True          # 辅助 / 消息 / 输入法窗口，直接排除

            score = 0
            if cls:
                if cls.endswith("QWindowIcon"):
                    score += 100     # 微信 4.x 主窗口
                if cls.startswith("Qt"):
                    score += 20
            if u.IsWindowVisible(hwnd):
                score += 50
            if not u.IsIconic(hwnd):
                score += 30
            # 尺寸只加分、绝不作过滤条件: 主窗口最小化到任务栏时 rect 会缩成 160x28 且 IsIconic=True
            r = wt.RECT()
            if u.GetWindowRect(hwnd, ctypes.byref(r)):
                if (r.right - r.left) >= 300 and (r.bottom - r.top) >= 300:
                    score += 20
            tl = ctypes.create_unicode_buffer(256)
            u.GetWindowTextW(hwnd, tl, 256)
            if tl.value in ("Weixin", "微信"):
                score += 10
            found.append((score, hwnd))
            return True

        u.EnumWindows(WNDENUMPROC(_cb), 0)
        if found:
            found.sort(key=lambda x: x[0], reverse=True)
            best = found[0][1]
            _wechat_hwnd_cache[0] = best
            return best

        # 兜底: 旧版微信(3.x)只有一个标题为 Weixin / 微信 的主窗口，此时标题匹配才安全
        for title in ("Weixin", "微信"):
            hh = u.FindWindowW(None, title)
            if hh:
                _wechat_hwnd_cache[0] = hh
                return hh
    except Exception as e:
        _note_err("_wechat_window_handle", e)
    return 0


def _is_time_line(p):
    """判断是否为微信会话列表的时间行（纯时间格式）
    避免消息内容中的时间词（今天/下午/上午等）被误判为时间行
    """
    p = p.strip()
    if not p:
        return False
    # 纯时间: 20:30 / 9:05
    if re.match(r"^\d{1,2}:\d{2}$", p):
        return True
    # 时间词: 昨天/今天/上午/下午/刚刚
    if p in TIME_WORDS:
        return True
    # 昨天/今天/星期X (+ 时间)
    if re.match(r"^(昨天|今天|星期[一二三四五六日天])(\s+\d{1,2}:\d{2})?$", p):
        return True
    # 上午/下午 + 时间
    if re.match(r"^(上午|下午)\s+\d{1,2}:\d{2}$", p):
        return True
    # X月X日
    if re.match(r"^\d{1,2}月\d{1,2}日$", p):
        return True
    # MM/DD、M/D（微信会话列表跨天日期，如 "09/22"）
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})$", p)
    if m and 1 <= int(m.group(1)) <= 12 and 1 <= int(m.group(2)) <= 31:
        return True
    # YYYY/MM/DD、YYYY-MM-DD
    if re.match(r"^\d{4}[/-]\d{1,2}[/-]\d{1,2}$", p):
        return True
    # 日期 + 免打扰标签混排（如 "09/22 消息免打扰"）
    if re.match(r"^\d{1,2}[/-]\d{1,2}\s+(消息免打扰|免打扰)$", p):
        return True
    if re.match(r"^\d{1,2}月\d{1,2}日\s+(消息免打扰|免打扰)$", p):
        return True
    return False


def parse_session(cell_name):
    """解析 mmui::ChatSessionCell 的 Name 属性
    格式: "会话名\\n已置顶\\n[N条] 消息内容\\n时间\\n[标签...]"
    返回: {"user":..., "msg":..., "unread":..., "time":..., "raw":...} 或 None
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
    time_label = ""
    for i in range(len(parts) - 1, start - 1, -1):
        if _is_time_line(parts[i]):
            end = i
            time_label = parts[i].strip()
            break

    # 逐段拼接消息，并剔除混入的会话标签（如 "MM/DD 消息免打扰" 中的 "消息免打扰"）
    msg_parts = []
    for p in parts[start:end]:
        p = p.strip()
        for label in ("消息免打扰", "免打扰"):
            p = p.replace(label, "")
        p = p.strip()
        if p:
            msg_parts.append(p)
    msg = " ".join(msg_parts)
    msg = re.sub(r"\[\d+条\]", "", msg).strip()

    return {"user": user, "unread": unread, "msg": msg, "time": time_label, "raw": raw}


def _unread_num(unread_text):
    """从未读标签解析未读条数。'3条未读'→3；'[10条] 某人: xxx'→10；无未读→-1。
    未读数递增视为"来了新消息"，用于识别同文案的重复消息
    （微信 UIA 不提供秒级时间戳与消息 ID，未读数是唯一可用的"新消息"信号）。"""
    if not unread_text:
        return -1
    m = re.search(r"(\d+)\s*条", unread_text)
    return int(m.group(1)) if m else -1


def _legacy_msg_text(stored):
    """兼容旧版记录格式（'时间标签|消息文本' 或 纯文本），提取其中的消息文本部分。"""
    if not isinstance(stored, str):
        return None
    if "|" in stored:
        head, tail = stored.split("|", 1)
        if _is_time_line(head):
            return tail
    return stored


def _is_new_msg(prev, msg, cur_u):
    """判断该会话本轮是否来了新消息（决定要不要弹窗）。
    prev : 上一轮记录 {"m": 消息文本, "u": 未读数}，兼容旧格式字符串 / None
    msg  : 本轮消息文本
    cur_u: 本轮未读数（无未读为 -1）
    判据 : 消息文本变化 或 未读数增加；未读数未知（旧记录/首次见到）时只认文本变化。
    注意 : 不使用会话时间标签——微信会话列表的时间标签会随时间自然演化
           （"19:07"→"昨天 19:07"→"09/22"），纳入比较会造成周期性误弹，
           而"未读数增加"已能覆盖同一文案的重复消息。
    """
    if isinstance(prev, dict):
        prev_m, prev_u = prev.get("m"), prev.get("u")
    elif isinstance(prev, str):
        prev_m, prev_u = _legacy_msg_text(prev), None
    else:
        prev_m, prev_u = prev, None
    return (prev_m != msg) or (prev_u is not None and cur_u > prev_u)


# ── 会话列表控件缓存 ──
# 实测微信控件树极少重建（4 小时内主窗口 0 次丢失、会话列表控件仅 1-2 次失效），
# 命中缓存可省掉每轮 searchDepth=20 的深搜，单轮耗时由 ~1.4s 降至 ~0.4-0.6s。
# 只缓存控件对象本身；GetChildren 返回的会话项每轮都重新读取（内容会变）。
# 缓存失效（Exists 返回 False / 抛异常）自动回退到完整深搜，且失败轮不更新去重记录，
# 因此不会漏消息，只会延迟 1-2 秒。
# ── 空结果诊断（issue #1 补充建议）──
# 原实现读不到会话时完全静默（error.log 为空），用户无法自查。
# 现按"连续 N 轮"限频落盘: 正常轮次零日志，只有持续失败才写一次，不会刷屏。
_EMPTY_STREAK = [0]
_EMPTY_WARN_AT = 5


def _note_empty(reason):
    """连续 _EMPTY_WARN_AT 轮读不到会话时，把原因写进 error.log（限频）。"""
    _EMPTY_STREAK[0] += 1
    n = _EMPTY_STREAK[0]
    if n == _EMPTY_WARN_AT or (n > _EMPTY_WARN_AT and n % (20 * _EMPTY_WARN_AT) == 0):
        _note_err("read_sessions 连续 %d 轮读不到会话: %s" % (n, reason))


def _clear_empty():
    _EMPTY_STREAK[0] = 0


def _enum_session_cells(table):
    """枚举会话表下的 mmui::ChatSessionCell 并逐项解析，返回解析成功的会话列表。"""
    out = []
    for c in table.GetChildren():
        if c.ControlTypeName == "ListItemControl" and c.ClassName == "mmui::ChatSessionCell":
            p = parse_session(c.Name)
            if p:
                out.append(p)
    return out


_cached_table = None
_last_cache_hit = False    # 上一轮 read_sessions 是否走了缓存快路径（仅用于日志观测）


def read_sessions(hwnd=0):
    import uiautomation as auto
    global _cached_table, _last_cache_hit
    _last_cache_hit = False
    auto.SetGlobalSearchTimeout(1)
    import pythoncom

    # ── 快路径: 命中缓存的会话列表控件，直接枚举会话项（省掉每轮深搜）──
    #    异常(控件失效 / COM 抖动)时先原地重试 1 次，仍失败才丢缓存：
    #    掉缓存要重新完整深搜约 2s，成本是命中路径的 600 倍，不轻易丢弃。
    if _cached_table is not None:
        for _attempt in (1, 2):
            try:
                if _cached_table.Exists(0):
                    _sessions = _enum_session_cells(_cached_table)
                    if _sessions:
                        _last_cache_hit = True
                        _clear_empty()
                        return _sessions
                break              # Exists 为 False: UI 已重建，重试无意义，直接落完整流程
            except Exception as e:
                if _attempt == 2:
                    logd(f"[缓存] 快路径连续失败，丢弃缓存: {e!r}")
                    _cached_table = None
                    break
                logd(f"[缓存] 快路径异常，重试 1 次: {e!r}")
                time.sleep(0.02)

    # ── HWND 直连快路径（issue #1 核心修复）──
    # 调用方已用 _wechat_window_handle() 精确定位主窗口，直接 ControlFromHandle 即可，
    # 不必从桌面根控件枚举。原流程优先 root.GetChildren(): 它会逐个唤醒所有顶层窗口的
    # UIA provider，只要其中一个无响应就整轮阻塞（实测 60s 不返回且**不抛异常**），
    # 于是下面 except 里"改走 HWND 直连"的兜底永远不可达。故把 HWND 直连提到最前，
    # root.GetChildren() 降级为最后兜底。
    if hwnd:
        try:
            _mw = auto.ControlFromHandle(hwnd)
            _mcls = (_mw.ClassName or "") if _mw is not None else ""
            if _mcls == "mmui::MainWindow":
                _t = _mw.Control(searchDepth=20, Name="会话", ClassName="mmui::XTableView")
                if _t is not None and _t.Exists(0):
                    _ss = _enum_session_cells(_t)
                    if _ss:
                        _cached_table = _t
                        _clear_empty()
                        return _ss
                    _note_empty("直连会话表存在但无会话项")
                else:
                    _note_empty("直连未找到会话表 mmui::XTableView")
            else:
                _note_empty("HWND 直连类名不是 mmui::MainWindow: %r" % (_mcls,))
        except Exception as e:
            logd(f"[HWND] 快路径失败，回落原流程: {e!r}")

    try:
        # ── COM 健康检查: Exists(0) ~0ms 试探连通性，断连直接 None ──
        try:
            root = auto.GetRootControl()
            if not root.Exists(0):
                logd("[COM] 根控件不可用")
                return None
        except Exception as e:
            logd(f"[COM] 根控件异常: {e!r}")
            return None

        mainwin = None
        try:
            for c in root.GetChildren():
                cn = c.ClassName or ""
                # 只认 mmui::MainWindow。微信 4.x 的顶层窗口有两套类名且必须分开看:
                #   Win32 层 = Qt51514QWindowIcon（顶层窗口由 Qt 承载）、标题 = 当前聊天对象名（动态变化）
                #   UIA  层 = mmui::MainWindow（控件树由 mmui 框架提供，完整可枚举）
                # 两者共存，不构成"Qt 渲染所以读不到控件"的关系，故不能用 Win32 类名做匹配，
                # 也不再保留 "QWindowIcon" in cn 的兼容分支。
                if cn == "mmui::MainWindow":
                    mainwin = c
                    break
        except Exception as e:
            # 枚举根窗口失败: 典型为 COM 断开(-2147220991)，退回 HWND 直连
            logd(f"[窗口] 枚举根窗口异常，改走 HWND 直连: {e!r}")
            if hwnd:
                try:
                    mainwin = auto.ControlFromHandle(hwnd)
                except Exception as e2:
                    logd(f"[窗口] HWND 直连失败: {e2!r}")
                    return None
            if not mainwin:
                return None

        if not mainwin:
            logd("[窗口] 未找到微信主窗口")
            _note_empty("根控件下未找到 mmui::MainWindow")
            return None

        if not mainwin.Exists(0):
            logd("[窗口] 主窗口句柄已失效")
            _note_empty("主窗口句柄已失效")
            return None

        # ── 注意: 不要钻入 MMUIRenderSubWindowHW ──
        # 4.2.0.13 实测控件树归属:
        #   主窗口 mmui::MainWindow 下 2 个子元素
        #     ├─ MMUIRenderSubWindowHW (PaneControl)  直接子级数 = 0，空壳
        #     └─ QWidget               (GroupControl) 3 个子级，会话表在这条分支下
        #   会话表父链: XTableView ← ChatSessionList ← ... ← QWidget ← ... ← MainWindow（共 14 层）
        # 从 MMUIRenderSubWindowHW 起搜 depth=20 必然搜不到（1.0s 超时返回空）。
        # 旧代码在"只有 1 个子元素且类名含 MMUIRenderSubWindowHW"时钻入该空壳，
        # 结果只会直接搜不到会话列表，属确定性错误分支，已删除。

        # ── 定位会话列表: 实测真实深度 14 层，searchDepth=20 一次到位 ──
        #    (原 searchDepth=30 的重搜段为冗余：20 能命中的场景 30 不会多命中，
        #     20 命不中的场景 30 同样命不中，已删除。)
        table = None
        try:
            table = mainwin.Control(searchDepth=20, Name="会话", ClassName="mmui::XTableView")
        except Exception as e:
            logd(f"[定位] 会话表搜索异常: {e!r}")
        if table is None or not table.Exists(0):
            try:
                table = mainwin.Control(searchDepth=20, Name="会话")
            except Exception as e:
                logd(f"[定位] 会话表(仅按名称)搜索异常: {e!r}")
                table = None
        if table is None or not table.Exists(0):
            logd("[定位] 未找到会话列表控件")
            _note_empty("主窗口下未找到会话列表控件")
            return []

        _cached_table = table          # 缓存控件对象，供后续轮次走快路径
        try:
            children = list(table.GetChildren())
        except Exception as e:
            logd(f"[定位] 枚举会话项异常，丢弃缓存: {e!r}")
            _cached_table = None
            return None

        sessions = []
        for c in children:
            if c.ControlTypeName == "ListItemControl" and c.ClassName == "mmui::ChatSessionCell":
                p = parse_session(c.Name)
                if p:
                    sessions.append(p)
        if sessions:
            _clear_empty()
        else:
            _note_empty("会话表枚举结果为空")
        return sessions

    except Exception as e:
        logd(f"[read_sessions] 未捕获异常: {e!r}")
        return None


# ══════════════════════════════════════════════════════════════════
#      UIA gate 判定层（v0.2.8 新增，方案来自 wechatauto-replica）
# ══════════════════════════════════════════════════════════════════
# 每次抓到 weixin.exe 时，先问一次 Weixin.dll 里 QAccessible 的 active byte：
#   值 == 1  → 已激活，跳过修改，直接走原有抓取流程
#   值 != 1  → 该字节写 1 并回读确认，再走原有抓取流程
# 判定跑在后台线程（首次无缓存要扫 Weixin.dll 的 200MB），绝不占住 Tk 主循环；
# 只改运行中进程的 1 个字节，微信重启后 PID 变化会重新判定一次。
_uia_gate = {}            # pid -> {state, value, rva, dll, msg, ts, tree_ready, confirmed}
_uia_gate_lock = threading.Lock()
_UIA_GATE_RETRY = 3600.0  # 判定失败后同一 PID 的重试冷却（秒）
_UIA_GATE_RETRYABLE = ("failed", "no_gate", "no_proc")


def _wechat_pid_of(hwnd):
    """窗口句柄 → 进程 PID（微信重启后 PID 变化，gate 判定按 PID 归档）。"""
    try:
        import ctypes.wintypes as wt
        pid = wt.DWORD(0)
        ctypes.windll.user32.GetWindowThreadProcessId(wt.HWND(hwnd), ctypes.byref(pid))
        return int(pid.value) or 0
    except Exception as e:
        _note_err("_wechat_pid_of", e)
        return 0


def _kick_uia_gate(hwnd, tree_ready=False):
    """非阻塞入口：同一微信 PID 只判定一次，失败项按冷却重试。"""
    pid = _wechat_pid_of(hwnd)
    if not pid:
        return
    spawn = False
    with _uia_gate_lock:
        rec = _uia_gate.get(pid)
        if rec is None:
            _uia_gate[pid] = {"state": "pending", "value": None, "rva": None,
                              "dll": "", "msg": "", "ts": time.time(),
                              "tree_ready": bool(tree_ready), "confirmed": False}
            spawn = True
        else:
            if tree_ready:
                rec["tree_ready"] = True
            if rec.get("state") == "pending":
                return
            if (rec.get("state") in _UIA_GATE_RETRYABLE
                    and time.time() - rec.get("ts", 0) > _UIA_GATE_RETRY):
                rec.update({"state": "pending", "ts": time.time()})
                spawn = True
    if spawn:
        threading.Thread(target=_uia_gate_worker, args=(pid,), daemon=True).start()


def _uia_gate_tree_ready(pid):
    with _uia_gate_lock:
        rec = _uia_gate.get(pid) or {}
        return bool(rec.get("tree_ready"))


def _uia_gate_worker(pid):
    """后台线程：读值 →（不为 1 时）写 1 → 回读确认。不碰 uiautomation。"""
    try:
        import wechat_uia
    except Exception as e:
        _note_err("_uia_gate_worker.import", e)
        with _uia_gate_lock:
            _uia_gate[pid] = {"state": "failed", "value": None, "rva": None, "dll": "",
                              "msg": f"wechat_uia 模块不可用: {e!r}", "ts": time.time(),
                              "tree_ready": False, "confirmed": False}
        log(f"[UIA] gate 判定跳过：wechat_uia 模块不可用（{e!r}）")
        return
    try:
        res = wechat_uia.check_and_activate(
            pid,
            logger=log,
            tree_ready=_uia_gate_tree_ready(pid),
            tree_ready_probe=lambda: _uia_gate_tree_ready(pid),
        )
    except Exception as e:
        _note_err("_uia_gate_worker.run", e)
        res = {"state": "failed", "value": None, "rva": None, "dll": "", "msg": repr(e)}

    with _uia_gate_lock:
        prev = _uia_gate.get(pid) or {}
        res.update({"ts": time.time(),
                    "tree_ready": bool(prev.get("tree_ready")),
                    "confirmed": bool(prev.get("confirmed"))})
        _uia_gate[pid] = res

    state = res.get("state")
    if state == "already":
        log(f"[UIA] gate 值=1，跳过修改（{res.get('msg')}）")
    elif state == "patched":
        log(f"[UIA] gate 值={res.get('value')}，已写入 1（RVA 0x{res.get('rva') or 0:X}）")
    elif state == "skipped":
        log(f"[UIA] {res.get('msg')}")
    else:
        log(f"[UIA] gate 判定未生效：{res.get('msg')}")


def _uia_gate_confirm(hwnd):
    """主线程确实抓到会话后调用：把生效地址固化成首选候选（下次免扫描）。"""
    pid = _wechat_pid_of(hwnd)
    if not pid:
        return
    with _uia_gate_lock:
        rec = _uia_gate.get(pid)
        if not rec or rec.get("state") != "patched" or rec.get("confirmed"):
            return
        rec["confirmed"] = True
        rva, dll = rec.get("rva"), rec.get("dll")
    if not rva or not dll:
        return
    try:
        import wechat_uia
        wechat_uia.mark_verified(dll, rva)
        log(f"[UIA] 控件树已可用，生效地址 0x{rva:X} 已固化到缓存")
    except Exception as e:
        _note_err("_uia_gate_confirm", e)


def reset_uia(reason=""):
    """重建 UIA/COM 连接。不再 sleep：阻塞点在 Tk 主线程，会让弹窗与退出检测全停，
    退避改由调用方用 root.after 非阻塞安排。"""
    import pythoncom
    global _cached_table
    _cached_table = None       # COM 重连后旧控件引用必然失效，一并清掉
    logd(f"[COM] 重建 UIA 连接 | {reason}")
    try:
        pythoncom.CoUninitialize()
    except Exception as e:
        logd(f"[COM] CoUninitialize 异常: {e!r}")
    try:
        pythoncom.CoInitialize()
    except Exception as e:
        logd(f"[COM] CoInitialize 异常: {e!r}")


# ══════════════════════════════════════════════════════════════════
#              文件事件唤醒（方案A：监听微信数据库目录）
# ══════════════════════════════════════════════════════════════════
# 原理: db_storage\{session,message} 下的 .db-wal 变化即"有新数据落盘"。
# 用 ReadDirectoryChangesW 阻塞等事件（内核回调，零轮询开销），
# 300ms 去抖把一次写入产生的多文件事件合并成一次唤醒，再驱动 _poll。
# 判定逻辑与弹窗逻辑完全不变，只替换驱动源。
DEBOUNCE_SEC      = 0.3     # 事件合并窗口
BURST_SCANS       = 3       # 唤醒后跟进扫描轮数（等微信把 db 变化渲染进 UI 树）
BURST_GAP_MS      = 300
IDLE_TICK_MS      = 100     # 空闲时检查唤醒标志的间隔（纯内存判断，不碰 UIA）
FALLBACK_SCAN_SEC = 120     # 兜底：超过该秒数无任何事件也强制扫一次（实测微信静默期写入很稀疏）
NO_WECHAT_GAP_MS  = 2000    # 微信窗口不存在时的探测间隔

FILE_LIST_DIRECTORY        = 0x0001
FILE_SHARE_ALL             = 0x0007
OPEN_EXISTING              = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
NOTIFY_FILTER = 0x0001 | 0x0002 | 0x0008 | 0x0010   # 文件名/目录名/大小/最后写入


def _find_db_storage():
    r"""定位微信 db_storage: 用户目录 Documents\xwechat_files\<wxid>\db_storage"""
    try:
        base = Path.home() / "Documents" / "xwechat_files"
        if not base.exists():
            return None
        cands = [d for d in base.iterdir()
                 if (d / "db_storage" / "session").is_dir() and (d / "db_storage" / "message").is_dir()]
        if not cands:
            return None
        cands.sort(key=lambda d: (d / "db_storage").stat().st_mtime, reverse=True)
        return cands[0] / "db_storage"
    except Exception as e:
        logd(f"[唤醒] 定位 db_storage 失败: {e!r}")
        return None


def _is_candidate_event(names):
    """事件过滤（依据 WAL 探针实测指纹）:
    - message_*.db-wal  → 候选写入，唤醒
    - *.db 主库 / .material / 仅 session.db-wal 孤立事件 → checkpoint 或心跳尾巴，忽略
    注: 10 秒心跳的 3 文件形态与真消息同形，事件层分辨不掉，只能靠去抖降频。"""
    for n in names:
        low = n.lower()
        if low.endswith(".db-wal") and "message" in low:
            return True
    return False


def _parse_notify_names(buf, size):
    """解析 FILE_NOTIFY_INFORMATION 链"""
    names = []
    offset = 0
    try:
        while offset < size:
            nxt = ctypes.c_uint32.from_buffer(buf, offset).value
            nlen = ctypes.c_uint32.from_buffer(buf, offset + 8).value
            if nlen:
                raw = ctypes.string_at(ctypes.addressof(buf) + offset + 12, nlen)
                names.append(raw.decode("utf-16-le", "ignore"))
            if not nxt:
                break
            offset += nxt
    except Exception as e:
        logd(f"[唤醒] 事件解析异常: {e!r}")
    return names


_wake_lock = threading.Lock()
_wake_state = {"ts": 0.0, "count": 0, "sample": ""}


def _mark_wake(names):
    with _wake_lock:
        _wake_state["ts"] = time.time()
        _wake_state["count"] += 1
        if names:
            _wake_state["sample"] = names[0]


class _FileWatcher:
    """单目录阻塞监听线程：只负责攒事件，不做任何 UIA 操作。"""

    def __init__(self, path):
        self.path = str(path)
        self.h = None
        self.alive = True

    def start(self):
        threading.Thread(target=self._run, daemon=True,
                         name=f"watch:{Path(self.path).name}").start()

    def stop(self):
        self.alive = False
        try:
            if self.h:
                ctypes.windll.kernel32.CloseHandle(self.h)
        except Exception:
            pass

    def _run(self):
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        k32.ReadDirectoryChangesW.restype = ctypes.c_int
        k32.ReadDirectoryChangesW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                              ctypes.c_int, ctypes.c_uint32,
                                              ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p, ctypes.c_void_p]

        while self.alive:
            h = k32.CreateFileW(self.path, FILE_LIST_DIRECTORY, FILE_SHARE_ALL, None,
                                OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
            if not h or h == 0xFFFFFFFFFFFFFFFF or h == -1:
                logd(f"[唤醒] 打开目录失败 {self.path}: err={ctypes.get_last_error()}")
                time.sleep(5)
                continue
            self.h = h
            logd(f"[唤醒] 开始监听 {self.path}")
            buf = ctypes.create_string_buffer(64 * 1024)
            returned = ctypes.c_uint32(0)
            while self.alive:
                ok = k32.ReadDirectoryChangesW(h, buf, len(buf), 1, NOTIFY_FILTER,
                                               ctypes.byref(returned), None, None)
                if not ok:
                    if self.alive:
                        logd(f"[唤醒] 读取事件失败 {Path(self.path).name}: "
                             f"err={ctypes.get_last_error()}，1s 后重连")
                        time.sleep(1)
                    break
                names = _parse_notify_names(buf, returned.value)
                if names and _is_candidate_event(names):
                    _mark_wake(names)
            try:
                k32.CloseHandle(h)
            except Exception:
                pass
            self.h = None


# ══════════════════════════════════════════════════════════════════
#                     后台进程
# ══════════════════════════════════════════════════════════════════
def run_background():
    """--background 模式"""
    kernel32 = ctypes.windll.kernel32
    hwnd_console = kernel32.GetConsoleWindow()

    # ── 关键修复（问题②）──
    # 原实现用 SW_MINIMIZE(6)：控制台只是缩到任务栏，仍挂在当前控制台会话上，
    # 用户手动关掉那个 cmd 窗口时，Windows 广播 CTRL_CLOSE_EVENT，后台就此被杀。
    # 现改为 SW_HIDE(0) 隐藏 + FreeConsole() 彻底脱离控制台：
    # 进程不再拥有控制台窗口，关窗口不再可能连累后台（stdout/stderr 已重定向到 error.log）。
    if hwnd_console:
        ctypes.windll.user32.ShowWindow(hwnd_console, 0)
        kernel32.FreeConsole()

    # ── 单实例保护（issue #1 问题③）──
    # CreateMutexW 的语义是: 不论互斥体是新建还是已存在，只要调用成功就返回**有效句柄**，
    # 所以原实现只判返回值是否为空，这个分支永远不会命中 —— 单实例保护实际等于没有，
    # 重复启动会出现多个 --background 进程同时弹同一批消息、并互相覆盖 bg_pid.txt。
    # 正确做法是查 GetLastError() == ERROR_ALREADY_EXISTS(183)。
    # 另: ctypes.windll.kernel32 的 CreateMutexW 默认 restype=c_int，64 位下句柄会被截断，
    #     且只有 WinDLL(..., use_last_error=True) 才能可靠读到 last error。
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateMutexW.restype = ctypes.c_void_p
    _k32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    h_mutex = _k32.CreateMutexW(None, False, MUTEX_NAME)
    if not h_mutex:
        return
    if ctypes.get_last_error() == 183:      # ERROR_ALREADY_EXISTS: 已有实例在跑，静默退出
        return

    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")

    import pythoncom
    pythoncom.CoInitialize()

    # 后台模式会隐藏控制台，traceback 与 faulthandler 的 C 层崩溃栈原本全丢，
    # 统一重定向到 data/error.log 留证（不参与正常日志轮转）。
    _err_fp = None
    try:
        _err_fp = open(ERROR_LOG, "a", encoding="utf-8", buffering=1)
        sys.stderr = _err_fp
        sys.stdout = _err_fp
    except Exception:
        _err_fp = None

    import faulthandler
    faulthandler.enable(file=sys.stderr)  # access violation 时打印 C 层堆栈到 error.log

    # 声明 DPI 感知（必须在 Tk 初始化前）
    _enable_dpi_awareness()

    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    root.title("WeChatNotifierBG")

    # 预热渲染缓存：静态底图只画一次，避免首条消息额外承担字体加载/底图绘制开销
    try:
        _s = _get_scale(root)
        _render_redpacket_bg(int(380 * _s), int(110 * _s), _s)
        _render_toast_bg(int(NOTIFY_W * _s), int(NOTIFY_H * _s), _s)
    except Exception:
        pass

    # ── 持久化去重记录：{会话: {"m": 消息文本, "u": 上轮未读数}} ──
    # 兼容旧格式（"时间标签|文本" 或 纯文本字符串）：只提取文本部分沿用，
    # 未读数标记为未知(None)，首轮不据此判定，因此不会吞掉升级瞬间的新消息。
    records = {}
    # 写盘节流：保存"上次已落盘"的原始文本，内容未变则不再写盘
    _last_saved = [""]

    # ── 落盘优化：紧凑序列化 + 延迟合并 + 原子替换 ──
    # 判重与弹窗全程只读内存 records，落盘仅是给下次启动留基线快照，
    # 因此可以把短时间内的多次变化合并成一次写，不影响任何弹窗时序。
    REC_SAVE_MIN_GAP = 5.0      # 变化后延迟多少秒落盘（合并窗口）
    _rec_pending = [None]       # 待落盘的序列化文本（None = 无待写）
    _rec_timer = [None]         # 延迟写盘的 after id

    def _flush_records(_=None):
        """把待写快照原子落盘。写失败只记诊断，绝不影响判重与弹窗。"""
        _rec_timer[0] = None
        buf = _rec_pending[0]
        if buf is None or buf == _last_saved[0]:
            _rec_pending[0] = None
            return
        try:
            tmp = RECORDS_FILE.with_name(RECORDS_FILE.name + ".tmp")
            tmp.write_text(buf, encoding="utf-8")
            os.replace(tmp, RECORDS_FILE)   # 原子替换：杜绝半截 JSON 导致的记录清空
            _last_saved[0] = buf
            _rec_pending[0] = None
        except Exception as e:
            _note_err("_flush_records", e)

    def _mark_records_dirty(buf):
        """记录新快照，并确保延迟窗口到期后必定落盘（不依赖下一轮轮询）。"""
        _rec_pending[0] = buf
        if _rec_timer[0] is None:
            try:
                _rec_timer[0] = root.after(int(REC_SAVE_MIN_GAP * 1000), _flush_records)
            except Exception as e:
                _note_err("_mark_records_dirty", e)

    if RECORDS_FILE.exists():
        try:
            _last_saved[0] = RECORDS_FILE.read_text(encoding="utf-8")
            _raw_records = json.loads(_last_saved[0])
            if isinstance(_raw_records, dict):
                for _k, _v in _raw_records.items():
                    if isinstance(_v, dict):
                        records[_k] = {"m": _v.get("m", ""), "u": _v.get("u", None)}
                    elif isinstance(_v, str):
                        records[_k] = {"m": _legacy_msg_text(_v), "u": None}
        except Exception as e:
            logd(f"[记录] chat-records.json 读取失败(按空记录继续): {e!r}")

    # 冷却机制已移除：旧版无冷却、按纯文本判重时从不漏消息，
    # 而 2 秒冷却会把落在窗口内的消息永久丢弃；新消息改由"文本变化 或 未读数增加"精确识别。

    empty_count = 0
    com_error_count = 0
    reset_cycle = 0
    MAX_EMPTY_BEFORE_RESET = 50
    MAX_COM_ERRORS = 5

    # ── 方案A 驱动源: 目录事件 → 去抖 → 扫描 ──
    _last_scan_ts  = [0.0]
    _burst_left    = [0]
    _backoff_until = [0.0]
    _watchers = []
    _db_storage = _find_db_storage()
    if _db_storage:
        for _sub in ("session", "message"):
            _d = _db_storage / _sub
            if _d.is_dir():
                _w = _FileWatcher(_d)
                _w.start()
                _watchers.append(_w)
        logd(f"[唤醒] 监听根目录 {_db_storage} ｜ 子目录 {[Path(w.path).name for w in _watchers]}")
    else:
        logd("[唤醒] 未找到 db_storage，退化为兜底定时扫描")

    def _consume_wake():
        """去抖: 最后一个事件距今 >= DEBOUNCE_SEC 才算一次有效唤醒"""
        with _wake_lock:
            ts = _wake_state["ts"]
            if ts <= 0 or time.time() - ts < DEBOUNCE_SEC:
                return False
            cnt = _wake_state["count"]
            sample = _wake_state["sample"]
            _wake_state["ts"] = 0.0
            _wake_state["count"] = 0
            _wake_state["sample"] = ""
        logd(f"[唤醒] 触发扫描（合并 {cnt} 次事件，样本 {sample}）")
        return True

    def _wait_next():
        """空闲等待: 有事件→扫; 事件后跟进 burst; 长时间无事件→兜底扫"""
        now = time.time()
        if now < _backoff_until[0]:
            root.after(int((_backoff_until[0] - now) * 1000) + 10, _wait_next)
            return
        if _consume_wake():
            _burst_left[0] = BURST_SCANS
            root.after(0, _poll)
            return
        if _burst_left[0] > 0:
            _burst_left[0] -= 1
            root.after(BURST_GAP_MS, _poll)
            return
        if now - _last_scan_ts[0] >= FALLBACK_SCAN_SEC:
            logd("[唤醒] 长时间无事件，执行兜底扫描")
            root.after(0, _poll)
            return
        root.after(IDLE_TICK_MS, _wait_next)

    def _check_exit():
        try:
            s = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if s.get("exit_requested"):
                s.pop("exit_requested", None)
                SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
                logd("[退出] 收到 exit_requested，注销后台进程")
                try:
                    _flush_records()    # 退出前兜底落盘，避免丢最后几秒的判重基准
                except Exception as e:
                    _note_err("_check_exit.flush", e)
                root.quit()
                return
        except Exception as e:
            logd(f"[退出] 检测失败(忽略): {e!r}")
        root.after(2000, _check_exit)   # 2000ms 足够，500ms 属无谓唤醒

    def _poll():
        nonlocal empty_count, records, com_error_count, reset_cycle

        # ── 窗口预检: FindWindowW ~0ms ──
        wechat_hwnd = _wechat_window_handle()
        if not wechat_hwnd:
            _last_scan_ts[0] = time.time()
            _burst_left[0] = 0
            root.after(NO_WECHAT_GAP_MS, _wait_next)   # 微信未运行: 低频探测，不空转
            return

        # ── v0.2.8: 抓到 weixin.exe 就判定 gate（后台线程，非阻塞）──
        # 值已是 1 则什么都不做；不为 1 才走修改流程。判定不影响本轮抓取节奏。
        _kick_uia_gate(wechat_hwnd)

        # 本轮结束后的调度方式: normal(等下一次文件事件) / immediate(立即重扫) / backoff(COM 退避)
        next_action = "normal"

        try:
            _scan_t0 = time.time()
            sessions = read_sessions(wechat_hwnd)
            _scan_cost = time.time() - _scan_t0
            # 慢轮观测：走缓存快路径时单轮约 0.4-0.6s，不产生日志；
            # 出现该日志即说明本轮走了完整深搜（首次建缓存 / UI 重建 / 缓存失效）。
            if _scan_cost >= 1.0:
                logd("[慢轮] read_sessions %.2fs | 缓存: %s"
                     % (_scan_cost, "命中" if _last_cache_hit else "未命中(完整深搜)"))
            if sessions is None:
                com_error_count += 1
                logd(f"[COM] 本轮无结果 ({com_error_count}/{MAX_COM_ERRORS})")
                if com_error_count >= MAX_COM_ERRORS:
                    reset_cycle += 1
                    reset_uia(f"连续 {com_error_count} 轮无结果，第 {reset_cycle} 次重置")
                    com_error_count = 0
                    if reset_cycle >= 3:
                        # 原为 time.sleep(5) 阻塞主线程（弹窗/退出检测全停），改非阻塞退避
                        next_action = "backoff"
                        logd(f"[COM] 连续 {reset_cycle} 次重置，退避 {RESET_BACKOFF_MS}ms 后重试")
                    else:
                        next_action = "immediate"
            elif not sessions:
                empty_count += 1
                if empty_count >= MAX_EMPTY_BEFORE_RESET:
                    reset_uia(f"连续 {empty_count} 轮会话列表为空")
                    empty_count = 0
            else:
                reset_cycle = 0
                empty_count = 0
                com_error_count = 0
                # ── v0.2.8: 控件树可用 = 判定层已生效；告知 gate 线程免去无谓扫描，
                # 并把「确实生效的地址」固化进缓存，下次启动零扫描 ──
                _kick_uia_gate(wechat_hwnd, tree_ready=True)
                _uia_gate_confirm(wechat_hwnd)
                for s in sessions:
                    user = s["user"]
                    msg = s["msg"]
                    cur_u = _unread_num(s["unread"])

                    # ── 新消息判定：文本变化 或 未读数增加（详见 _is_new_msg）──
                    is_new = _is_new_msg(records.get(user), msg, cur_u)

                    # 无论是否弹窗都刷新为最新状态，保证下一轮比较基准正确
                    records[user] = {"m": msg, "u": cur_u}

                    if not is_new:
                        continue
                    if user in BLACK_LIST:
                        continue

                    # ── 红包通道：消息含 "[微信红包]" 即弹红包样式 ──
                    # 完全解耦于抑制逻辑：不受未读数量限制、不受免打扰静默限制、无任何时间窗；
                    # 同文案红包靠"未读数增加"识别，重复内容同样不漏。
                    if _is_redpacket(msg):
                        un = s["unread"].replace("条未读", "")
                        title = f"{user}({un})" if un.isdigit() else user
                        try:
                            _show_redpacket_toast(root, title, msg)
                            log(f"红包弹窗: {title} -> {msg[:50]}")
                        except Exception as e:
                            logd(f"[弹窗] 红包弹窗失败: {e!r}")
                        continue

                    # ── 普通通道：免打扰会话静默；需有未读 ──
                    if "免打扰" in s.get("raw", ""):
                        continue
                    if not s["unread"]:
                        continue
                    un = s["unread"].replace("条未读", "")
                    title = f"{user}({un})" if un.isdigit() else user
                    body = msg[:50] + ("..." if len(msg) > 50 else "")
                    try:
                        _show_toast(root, title, body)
                        log(f"弹窗: {title} -> {body}")
                    except Exception as e:
                        logd(f"[弹窗] 普通弹窗失败: {e!r}")
                # 写盘节流：内容与上次落盘相同则跳过；有变化只打脏标记，
                # 由 _flush_records 合并写盘（紧凑格式，体积约为 indent=2 的一半）
                _buf = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
                if _buf != _last_saved[0]:
                    _mark_records_dirty(_buf)
        except Exception as e:
            logd(f"[_poll] 未捕获异常: {e!r}")

        _last_scan_ts[0] = time.time()
        if next_action == "backoff":
            # COM 连续重置后的非阻塞退避（不再 time.sleep 卡死主线程）
            _backoff_until[0] = time.time() + RESET_BACKOFF_MS / 1000.0
        root.after(0, _poll if next_action == "immediate" else _wait_next)

    root.after(0, _poll)        # 首轮立即扫描，建立基线
    root.after(500, _check_exit)

    if hwnd_console:
        ctypes.windll.user32.ShowWindow(hwnd_console, 0)

    root.mainloop()

    try:
        _flush_records()            # 进程收尾兜底落盘
    except Exception as e:
        _note_err("run_background.final_flush", e)

    for _w in _watchers:
        try:
            _w.stop()
        except Exception:
            pass

    try: PID_FILE.unlink()
    except Exception as e:
        _note_err("run_background.unlink_pid", e)
    try:
        import pythoncom
        pythoncom.CoUninitialize()
    except Exception as e:
        _note_err("run_background.CoUninitialize", e)


# ══════════════════════════════════════════════════════════════════
#                    启动后台子进程
# ══════════════════════════════════════════════════════════════════
def _pid_alive(pid):
    """判断 PID 对应的通知器进程是否仍在运行。

    仅用 OpenProcess(SYNCHRONIZE) 判断，在进程已退出但内核对象尚未回收
    （僵尸进程）时仍会返回有效句柄，从而把陈旧 PID 误判为"存活"，阻塞新的
    后台进程启动。因此这里用 WaitForSingleObject 判断句柄是否已 signaled，
    并校验可执行文件名，避免 PID 被其他进程复用造成误判。
    """
    if not pid or pid <= 0:
        return False
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.WaitForSingleObject.restype = ctypes.c_uint32
        k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.QueryFullProcessImageNameW.restype = ctypes.c_int
        k32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]

        SYNCHRONIZE = 0x00100000
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        WAIT_TIMEOUT = 0x00000102

        h = k32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            if k32.WaitForSingleObject(h, 0) != WAIT_TIMEOUT:
                return False  # 已退出（含僵尸）
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return False  # 无法确认镜像名，按"非本程序"处理（宁可重启也不静默失效）
            name = os.path.basename(buf.value).lower()
            return name.startswith("wechatnotifier")
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def launch_background():
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            if _pid_alive(old_pid):
                return False
        except Exception as e:
            _note_err("launch_background.read_pid", e)

    if getattr(sys, "frozen", False):
        exe = sys.executable
    else:
        exe = sys.executable.replace("python.exe", "pythonw.exe")

    args = [exe, "--background"]
    if not getattr(sys, "frozen", False):
        args = [exe, str(Path(__file__).resolve()), "--background"]

    # ── 关键修复（问题①）──
    # 原实现用 CREATE_NO_WINDOW：它只是"不显示窗口"，子进程仍挂在父进程的
    # 控制台会话上，关闭控制台窗口时 Windows 会向同一控制台的进程广播
    # CTRL_CLOSE_EVENT，导致后台监听被连带杀死。
    # 现改为 DETACHED_PROCESS（不继承父控制台）+ CREATE_NEW_PROCESS_GROUP，
    # 并附加 CREATE_BREAKAWAY_FROM_JOB 以脱离父进程所属的 Job Object，
    # 从而关闭控制台窗口后后台仍可继续运行。
    DETACHED_PROCESS          = 0x00000008
    CREATE_NEW_PROCESS_GROUP  = 0x00000200
    CREATE_BREAKAWAY_FROM_JOB = 0x01000000

    def _spawn(flags):
        return subprocess.Popen(
            args,
            creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    try:
        try:
            _spawn(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB)
        except OSError:
            # 父 Job Object 未开启 breakaway 权限时降级重试（仍脱离控制台）
            _spawn(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
    except Exception as e:
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
            except Exception as e:
                _note_err("launch_background.wait_pid", e)

    return True


def _cleanup_stray_processes(keep_pid):
    """Terminate PyInstaller bootloader processes that aren't the console or background worker."""
    current_pid = os.getpid()
    exe_name = Path(sys.executable).name
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
            encoding="mbcs", errors="replace",
        )
        for line in result.stdout.strip().split("\n"):
            parts = line.replace('"', "").split(",")
            if len(parts) >= 2:
                try:
                    pid = int(parts[1].strip())
                    if pid not in (current_pid, keep_pid):
                        ctypes.windll.kernel32.TerminateProcess(
                            ctypes.windll.kernel32.OpenProcess(1, False, pid), 0
                        )
                except (ValueError, OSError):
                    pass
    except Exception as e:
        _note_err("_cleanup_stray_processes", e)


# ══════════════════════════════════════════════════════════════════
#                     控制台菜单
# ══════════════════════════════════════════════════════════════════
def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")

_BOX_W = 55


def _dw(s):
    """计算字符串显示宽度（中文/全角字符按 2 计）"""
    return sum(2 if unicodedata.east_asian_width(ch) in "FW" else 1 for ch in s)


def _center(s, width=_BOX_W):
    """按显示宽度居中，右侧补空格保证框线不错位"""
    pad = max(0, width - _dw(s))
    left = pad // 2
    return "  " + " " * left + s + " " * (pad - left)


BANNER = "\n".join([
    "  " + "=" * _BOX_W,
    _center(f"微信消息通知器  {VERSION}"),
    _center("作者: 不理笨笨"),
    "  " + "=" * _BOX_W,
    "   微信保持登录即可，可最小化，请勿退出微信",
    "   关闭本窗口不影响后台监听",
])


def console_loop(is_primary):
    """主菜单：1-3 切快捷键  4 关快捷键  5 开机自启  6 退出"""

    def show_main():
        clear_screen()
        print()
        print(BANNER)
        print("  " + "-" * _BOX_W)
        shortcut = get_shortcut_combo()
        as_status = "已开启" if get_auto_start() else "已关闭"
        presets = list(SHORTCUT_PRESETS.keys())
        opt_line = "".join(f"[{i + 1}] {name}   " for i, name in enumerate(presets))
        opt_line += f"[{len(presets) + 1}] 关闭"
        print("   当前唤醒键: " + shortcut)
        print("   " + opt_line)
        print("   [5] 开机自启: " + as_status)
        print("   [6] 退出通知器")
        print("  " + "=" * _BOX_W)
        print()

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
                status_str = "已开启" if new_state else "已关闭"
                print(f"  开机自启: {status_str}")
            elif idx == n_presets + 3:
                print()
                print("  正在退出通知器...")
                s = load_settings()
                s["exit_requested"] = True
                save_settings(s)
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
def run_demo_toast():
    """调试入口（--demo-toast）：直接弹出演示弹窗，用于核对渲染效果。"""
    _enable_dpi_awareness()
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    root.title("WeChatNotifierDemo")
    root.after(300, lambda: _show_redpacket_toast(
        root, "无畏契约", "精灵:[微信红包]恭喜发财大吉大利"))
    root.after(1200, lambda: _show_toast(
        root, "文件传输助手", "这是一条普通消息弹窗测试"))
    root.after(15000, root.destroy)
    root.mainloop()


def run_gate_status():
    """诊断入口（--gate-status）：只读打印 gate 判定结果，一个字节都不写。"""
    _enable_dpi_awareness()
    hwnd = _wechat_window_handle()
    if not hwnd:
        print("  微信未运行：找不到 weixin.exe 主窗口")
        return 2
    pid = _wechat_pid_of(hwnd)
    print("  通知器版本    : %s" % VERSION)
    print("  微信 PID      : %d" % pid)
    try:
        import wechat_uia as U
    except Exception as e:
        print("  wechat_uia 模块不可用: %r" % e)
        return 3
    mod = U.weixin_dll(pid)
    if not mod:
        print("  读不到 Weixin.dll 模块基址（权限不足？不要用管理员身份运行）")
        return 4
    print("  Weixin.dll    : %s" % mod[1])
    print("  模块基址      : 0x%X" % mod[0])
    print("  ---- 以下为只读判定，不修改任何字节 ----")
    res = U.check_and_activate(pid, logger=lambda m: print("  " + m), dry_run=True)
    print("  判定状态      : %s" % res.get("state"))
    print("  gate 当前值   : %s" % res.get("value"))
    print("  命中 RVA      : %s" % (("0x%X" % res["rva"]) if res.get("rva") else "—"))
    print("  结论          : %s" % res.get("msg"))
    if res.get("state") == U.ST_ALREADY:
        print("  -> 值为 1：通知器启动时会跳过修改，直接走原有抓取流程")
    elif res.get("state") == U.ST_READONLY:
        print("  -> 值不为 1：通知器启动时会先把该字节写成 1，再走原有抓取流程")
    else:
        print("  -> 判定未生效，通知器会照常抓取，只是拿不到这层保险")
    return 0


def main():
    if "--background" in sys.argv:
        run_background()
        return

    if "--demo-toast" in sys.argv:
        run_demo_toast()
        return

    if "--gate-status" in sys.argv:
        sys.exit(run_gate_status())

    if getattr(sys, "frozen", False):
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleTitleW("微信消息通知器 " + VERSION)

    is_primary = True
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            if _pid_alive(old_pid):
                is_primary = False
        except Exception as e:
            _note_err("main.read_pid", e)

    if is_primary:
        if launch_background():
            time.sleep(1)

    console_loop(is_primary=is_primary)


if __name__ == "__main__":
    main()
