#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
微信 4.x Qt 无障碍 gate（active byte）判定与热激活 —— v0.2.8 新增层
v0.2.9: rip_xrefs 去 numpy 化（纯 Python + C 层正则），无 numpy 环境下候选排序与向量版一致

方案来源：fanyuantaier/wechatauto-replica 的 tools/wake_uia.py
只做一件事：定位 Weixin.dll 里 QAccessible 的 active byte，先读它的值
  - 值为 1 → 该字节已激活，跳过修改，直接进入原有抓取流程
  - 值不为 1（0 / 读不到）→ 写入 1 并回读确认，再进入原有抓取流程

不改启动参数、不注入代码、不设钩子、不挂 DLL；只改运行中进程里的 1 个字节，
微信重启后进程内存清零，下次启动时本模块会再判定一次（幂等）。

本模块不 import uiautomation，不做控件树校验：
  校验交给调用方（通知器主线程的 read_sessions 本身就是最好的校验），
  避免在子线程与主线程同时使用 uiautomation 全局单例而产生 COM 冲突。
缓存文件与 wake_uia.py 共用（~/.wechatauto/gate_cache.json），命中即零扫描。
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import struct
from ctypes import wintypes

# 与 wake_uia.py 共用同一份缓存：换版本（目录名/大小/mtime 变化）自动失效
GATE_CACHE_FILE = os.path.join(os.path.expanduser("~"), ".wechatauto",
                               "gate_cache.json")

# 已知 active byte 位置：只作扫描失败时的兜底，扫描结果优先
QACCESSIBLE_ACTIVE_RVA_BY_VERSION = {
    "4.2.0.13": 0x0B50ABC8,
    "4.1.11.22": 0x0A1E7DB8,
    "4.1.13.65": 0x0AE2B0C8,
    "4.1.15.13": 0x0B135C38,
    "4.1.15.53": 0x0B3959D8,
}
QACCESSIBLE_CORE_STRING = b"qt.accessibility.core"

# test rcx,rcx / jz / cmp byte ptr [rip+disp32],0 / jz —— gate 检查的标准形状
QACCESSIBLE_GATE_PATTERN = re.compile(
    rb"\x48\x85\xc9\x0f\x84....\x80\x3d(?P<disp>.{4})"
    rb"\x00\x0f\x84",
    re.DOTALL,
)

IMAGE_SCN_MEM_EXECUTE = 0x20000000
IMAGE_SCN_MEM_WRITE = 0x80000000
TH32CS_SNAPMODULE, TH32CS_SNAPMODULE32 = 0x00000008, 0x00000010
PROCESS_ACCESS = 0x0400 | 0x0010 | 0x0020 | 0x0008   # QUERY|VM_READ|VM_WRITE|VM_OP
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_MODULE_NAME32, MAX_PATH = 255, 260

# 状态常量（供调用方判断，不要把英文串直接写进面板）
ST_ALREADY = "already"      # 值本来就是 1，跳过修改
ST_PATCHED = "patched"      # 值不是 1，已写入 1 并回读确认
ST_SKIPPED = "skipped"      # 控件树已可用，未读取字节（省掉 198MB 扫描）
ST_READONLY = "readonly"    # 只读模式：值不为 1 但没有写入
ST_NO_GATE = "no_gate"      # 这个版本的 DLL 找不到 gate 字节
ST_NO_PROC = "no_proc"      # 读不到微信进程（权限/位数不一致）
ST_FAILED = "failed"        # 写失败或回读不为 1


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("th32ModuleID", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("GlblcntUsage", ctypes.c_ulong),
        ("ProccntUsage", ctypes.c_ulong),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_ubyte)),
        ("modBaseSize", ctypes.c_ulong),
        ("hModule", ctypes.c_void_p),
        ("szModule", ctypes.c_wchar * (MAX_MODULE_NAME32 + 1)),
        ("szExePath", ctypes.c_wchar * MAX_PATH),
    ]


# ---------------------------------------------------------------------- 缓存
def dll_identity(dll_path):
    """Weixin.dll 身份 = 版本目录名 + 大小 + mtime；换版本自动失效。"""
    try:
        st = os.stat(dll_path)
        return "%s|%d|%d" % (os.path.basename(os.path.dirname(dll_path)),
                             st.st_size, int(st.st_mtime))
    except OSError:
        return dll_path


def load_cache(path=None):
    try:
        with open(path or GATE_CACHE_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(data, path=None):
    path = path or GATE_CACHE_FILE
    try:
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass


# ------------------------------------------------------------------------ PE
def pe_sections(data):
    try:
        pe_off = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe_off:pe_off + 4] != b"PE\0\0":
            return []
        coff = pe_off + 4
        count = struct.unpack_from("<H", data, coff + 2)[0]
        opt_size = struct.unpack_from("<H", data, coff + 16)[0]
        sec_off = coff + 20 + opt_size
        out = []
        for i in range(count):
            off = sec_off + i * 40
            name = data[off:off + 8].split(b"\0", 1)[0].decode("ascii", "ignore")
            vsize, rva, raw_size, raw_ptr = struct.unpack_from("<IIII", data, off + 8)
            out.append({"name": name, "rva": rva, "vsize": vsize,
                        "raw_size": raw_size, "raw_ptr": raw_ptr,
                        "chars": struct.unpack_from("<I", data, off + 36)[0]})
        return out
    except Exception:
        return []


def section_for_rva(sections, rva):
    for s in sections:
        if s["rva"] <= rva < s["rva"] + max(s["vsize"], s["raw_size"]):
            return s
    return None


def offset_to_rva(sections, offset):
    for s in sections:
        if s["raw_ptr"] <= offset < s["raw_ptr"] + s["raw_size"]:
            return s["rva"] + offset - s["raw_ptr"]
    return None


_RIP_LEA_MODRM_RE = re.compile(rb"\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d]")


def rip_xrefs(data, sections, target_rva):
    """可执行段里用 RIP 相对寻址引用 target_rva 的 LEA 指令 RVA。

    纯 Python 实现，不依赖 numpy：正则先在 C 层筛出「8D + RIP 型 ModRM」双字节，
    再对每个命中解 disp32 算目标地址，取命中位置换算成 RVA。
    仅用于给 gate 候选按「距 qt.accessibility.core 字符串多远」排序，
    缓存未命中时才跑一次（196MB 实测约 1~2 秒），不影响候选集合本身。
    """
    hits = []
    n = len(data)
    if n < 16:
        return hits
    for s in sections:
        if not (s["chars"] & IMAGE_SCN_MEM_EXECUTE):
            continue
        start = s["raw_ptr"]
        end = min(n, start + s["raw_size"])
        if end - start < 8:
            continue
        # LEA 后 disp 相对「本条指令末尾」：p + disp 应与该常量比较
        k = target_rva - s["rva"] + start - 6
        plain = []
        rexed = []
        for m in _RIP_LEA_MODRM_RE.finditer(data, start, end):
            pos = m.start()
            if pos + 6 > n:
                continue
            disp = struct.unpack_from("<i", data, pos + 2)[0]
            if pos + disp != k:
                continue
            if pos <= end - 8:
                plain.append(s["rva"] + pos - start)
            if pos >= start + 1 and pos <= end - 7 and 0x40 <= data[pos - 1] <= 0x4F:
                rexed.append(s["rva"] + pos - start - 1)
        hits += plain
        hits += rexed
    return hits


def scan_candidates(dll_path, cache_path=None):
    """按可信度排好的 gate RVA 候选；「扫过了但没有」这个负结果也落盘。"""
    cache_path = cache_path or GATE_CACHE_FILE
    identity = dll_identity(dll_path)
    entry = load_cache(cache_path).get(identity) or {}
    if isinstance(entry.get("candidates"), list):
        return [int(x) for x in entry["candidates"]]

    try:
        with open(dll_path, "rb") as f:
            data = f.read()
    except OSError:
        return []
    sections = pe_sections(data)
    if not sections:
        return []

    core_off = data.find(QACCESSIBLE_CORE_STRING)
    core_rva = offset_to_rva(sections, core_off) if core_off >= 0 else None
    core_xrefs = rip_xrefs(data, sections, core_rva) if core_rva is not None else []

    found = []
    for m in QACCESSIBLE_GATE_PATTERN.finditer(data):
        match_rva = offset_to_rva(sections, m.start())
        disp_rva = offset_to_rva(sections, m.start("disp"))
        if match_rva is None or disp_rva is None:
            continue
        sec = section_for_rva(sections, match_rva)
        if not sec or not (sec["chars"] & IMAGE_SCN_MEM_EXECUTE):
            continue
        cmp_rva = disp_rva - 2
        target_rva = cmp_rva + 7 + struct.unpack("<i", m.group("disp"))[0]
        tgt = section_for_rva(sections, target_rva)
        if not tgt or not (tgt["chars"] & IMAGE_SCN_MEM_WRITE):
            continue
        dist = min(abs(match_rva - x) for x in core_xrefs) if core_xrefs else 0x7FFFFFFF
        found.append((dist, target_rva))

    cache = load_cache(cache_path)
    if not found:
        cache.setdefault(identity, {})["candidates"] = []
        save_cache(cache, cache_path)
        return []
    found.sort(key=lambda x: x[0])
    near = [rva for dist, rva in found if dist <= 0x20000]
    ordered = list(dict.fromkeys(near or [rva for _d, rva in found]))
    cache.setdefault(identity, {})["candidates"] = ordered
    save_cache(cache, cache_path)
    return ordered


def candidates_for(dll_path, cache_path=None):
    """顺序：上次真正生效过的地址 > 特征扫描 > 版本兜底表。"""
    cache_path = cache_path or GATE_CACHE_FILE
    out = []
    cache = load_cache(cache_path)
    verified = (cache.get(dll_identity(dll_path)) or {}).get("verified")
    if isinstance(verified, int):
        out.append(verified)
    for rva in scan_candidates(dll_path, cache_path):
        if rva not in out:
            out.append(rva)
    fallback = QACCESSIBLE_ACTIVE_RVA_BY_VERSION.get(
        os.path.basename(os.path.dirname(dll_path)))
    if fallback and fallback not in out:
        out.append(fallback)
    return out


def mark_verified(dll_path, rva, cache_path=None):
    """主线程确认控件树真的可用后，把生效地址固化成首选候选。"""
    cache_path = cache_path or GATE_CACHE_FILE
    try:
        cache = load_cache(cache_path)
        cache.setdefault(dll_identity(dll_path), {})["verified"] = int(rva)
        save_cache(cache, cache_path)
    except Exception:
        pass


# ---------------------------------------------------------------------- 进程
def iter_modules(pid):
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for first in (k32.Module32FirstW, k32.Module32NextW):
        first.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
        first.restype = wintypes.BOOL
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if ctypes.cast(snap, ctypes.c_void_p).value == INVALID_HANDLE_VALUE:
        return
    try:
        e = MODULEENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        nxt = k32.Module32FirstW(snap, ctypes.byref(e))
        while nxt:
            base = ctypes.cast(e.modBaseAddr, ctypes.c_void_p).value or 0
            yield base, int(e.modBaseSize), e.szModule, e.szExePath
            nxt = k32.Module32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)


def weixin_dll(pid):
    """返回 (模块基址, Weixin.dll 磁盘路径)；微信没加载 Weixin.dll 时返回 None。"""
    for base, _size, name, path in iter_modules(pid):
        if name.lower() == "weixin.dll":
            return base, path
    return None


def open_process(pid):
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    return k32.OpenProcess(PROCESS_ACCESS, False, pid)


def close_handle(handle):
    try:
        ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass


def read_byte(handle, address):
    k32 = ctypes.windll.kernel32
    k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    k32.ReadProcessMemory.restype = wintypes.BOOL
    buf = (ctypes.c_ubyte * 1)()
    got = ctypes.c_size_t(0)
    ok = k32.ReadProcessMemory(handle, ctypes.c_void_p(address), buf, 1, ctypes.byref(got))
    return int(buf[0]) if ok and got.value == 1 else None


def write_byte(handle, address, value):
    k32 = ctypes.windll.kernel32
    k32.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    k32.WriteProcessMemory.restype = wintypes.BOOL
    buf = (ctypes.c_ubyte * 1)(value & 0xFF)
    done = ctypes.c_size_t(0)
    ok = k32.WriteProcessMemory(handle, ctypes.c_void_p(address), buf, 1, ctypes.byref(done))
    return bool(ok and done.value == 1)


# ------------------------------------------------------------------ 判定入口
def check_and_activate(pid, logger=print, tree_ready=False, tree_ready_probe=None,
                       cancel=None, cache_path=None, dry_run=False):
    """读 gate 字节：为 1 跳过修改，不为 1 写入 1 并回读。

    参数
      pid              微信进程 PID
      logger           日志回调（用通知器的 log 落盘 + 面板可见）
      tree_ready       调用方是否已经能读到 mmui 会话列表；为 True 时只报告不改写，
                       顺带省掉 198MB 的 DLL 扫描（正常路径 0 开销）
      tree_ready_probe 可调用对象：扫描 DLL 之前再问一次"控件树可用了吗"，
                       已可用且本地无缓存候选则直接跳过，省掉首次扫描
      cancel           threading.Event，置位则尽快收工（微信已退出/切号）
      cache_path       缓存文件覆盖（测试用）

    返回 dict：state（ST_* 之一） / value（写前读到的值） / rva / dll / msg
    """
    result = {"state": ST_FAILED, "value": None, "rva": None, "dll": "", "msg": ""}
    if cancel is not None and cancel.is_set():
        result["msg"] = "已取消"
        return result

    mod = weixin_dll(pid)
    if not mod:
        result["state"] = ST_NO_PROC
        result["msg"] = "微信进程里没有 Weixin.dll（未登录 / 位数不一致）"
        return result
    base, dll_path = mod
    result["dll"] = dll_path

    if tree_ready:
        # 主线程已经能读到会话列表 = 控件树本就物化，无需碰进程内存，
        # 也不必为"读值"去扫 198MB 的 Weixin.dll
        result["state"] = ST_SKIPPED
        result["msg"] = "控件树已可用，跳过判定与修改"
        return result

    # 本地已有该版本候选 → 直接读值（毫秒级）；没有候选才可能触发 205MB 扫描，
    # 此时若主线程已经能读到会话列表，就完全没必要扫
    cached = (load_cache(cache_path or GATE_CACHE_FILE)
              .get(dll_identity(dll_path)) or {}).get("candidates")
    has_cache = isinstance(cached, list) and len(cached) > 0
    if not has_cache and tree_ready_probe is not None:
        try:
            if tree_ready_probe():
                result["state"] = ST_SKIPPED
                result["msg"] = "控件树已可用，跳过判定与扫描"
                return result
        except Exception:
            pass

    rvas = candidates_for(dll_path, cache_path)
    if not rvas:
        result["state"] = ST_NO_GATE
        result["msg"] = "该版本 Weixin.dll 未匹配到 gate 特征，也无版本兜底"
        return result

    handle = open_process(pid)
    if not handle:
        result["state"] = ST_NO_PROC
        result["msg"] = ("OpenProcess 失败（err=%d）：脚本与微信权限/完整性级别不一致，"
                         "不要以管理员身份运行" % ctypes.windll.kernel32.GetLastError())
        return result

    try:
        for rva in rvas:
            if cancel is not None and cancel.is_set():
                result["msg"] = "已取消"
                return result
            address = base + rva
            current = read_byte(handle, address)
            logger("[UIA] gate 候选 0x%X 当前值=%s" % (rva, "读不到" if current is None else current))
            if current is None:
                continue
            result["value"] = current
            result["rva"] = rva
            if current == 1:
                result["state"] = ST_ALREADY
                result["msg"] = "gate 已为 1，跳过修改"
                return result
            # 值不为 1 → 走修改流程
            if dry_run:
                logger("[UIA] 只读模式：gate 值=%s，按流程本应写入 1，本次不写" % current)
                result["state"] = ST_READONLY
                result["msg"] = "只读模式：gate 值=%s，未修改" % current
                return result
            if not write_byte(handle, address, 1):
                logger("[UIA] gate 0x%X 写入失败（内存保护？）" % rva)
                continue
            back = read_byte(handle, address)
            if back == 1:
                result["state"] = ST_PATCHED
                result["msg"] = "gate %s -> 1 写入成功" % current
                return result
            logger("[UIA] gate 0x%X 回读=%s，未生效，尝试下一个候选" % (rva, back))
        result["state"] = ST_FAILED
        result["msg"] = "所有候选均未写入成功"
        return result
    finally:
        close_handle(handle)


def restore_off(pid, logger=print, cache_path=None):
    """把 gate 写回 0（收起控件树），仅用于手动排障，正常流程不调用。"""
    mod = weixin_dll(pid)
    if not mod:
        return ST_NO_PROC
    base, dll_path = mod
    handle = open_process(pid)
    if not handle:
        return ST_NO_PROC
    try:
        for rva in candidates_for(dll_path, cache_path):
            if read_byte(handle, base + rva) == 1:
                write_byte(handle, base + rva, 0)
                logger("[UIA] gate 0x%X: 1 -> 0" % rva)
                return "off"
        return "idle"
    finally:
        close_handle(handle)
