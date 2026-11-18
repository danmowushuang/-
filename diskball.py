# -*- coding: utf-8 -*-
"""DiskBall（磁盘小球）—— 桌面磁盘占用实时监测原型 v0.1

功能：
  1. 桌面悬浮圆形小球，实时显示各盘占用（环形进度）
  2. 点击小球 → 打开详情面板：每个盘的总量/已用/剩余 + 目录大小排行
  3. 详情面板里可以一层层点进文件夹，看清"大头 / 小头"
  4. 选中条目 → 移到回收站（可还原），系统关键目录禁止删除
依赖：Python 3.9+ / tkinter / Pillow
"""

import os
import sys
import json
import time
import math
import ctypes
import shutil
import threading
import queue
import subprocess
from ctypes import wintypes
from concurrent.futures import ThreadPoolExecutor, as_completed

import tkinter as tk
from PIL import Image, ImageDraw, ImageFont, ImageTk

# ---------------------------------------------------------------- 视觉常量
BG       = "#0b111a"   # 窗口底色
PANEL    = "#0f1724"
CARD     = "#141e2c"
CARD_HL  = "#1c2739"
LINE     = "#22303f"
TEXT     = "#e8eff8"
MUTED    = "#8ba0b8"
DIM      = "#5e7186"
ACCENT   = "#4cc2ff"
GREEN    = "#34d399"
AMBER    = "#fbbf24"
RED      = "#f87171"
KEY      = "#101010"   # 透明色键（小球的"消失"背景）

DESIGN_SIZE = 168      # 小球设计尺寸（逻辑像素）

CFG_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "DiskBall")
CFG_FILE = os.path.join(CFG_DIR, "config.json")
RECT_FILE = os.path.join(CFG_DIR, "rects.json")
PID_FILE = os.path.join(CFG_DIR, "run.pid")
LOCK_FILE = os.path.join(CFG_DIR, "instance.lock")
STRUCT_FILE = os.path.join(CFG_DIR, "structure.json")
AUDIT_FILE = os.path.join(CFG_DIR, "audit.log")
ERROR_LOG = os.path.join(CFG_DIR, "error.log")

MIN_BIG_FILE = 100 * 1024 * 1024   # "大文件"模式的门槛：100MB
MAX_ROWS = 200


# ---------------------------------------------------------------- 小工具
def fmt_size(n):
    if n is None:
        return "—"
    n = float(n)
    if n < 1024:
        return "%d B" % int(n)
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit)
    return "%.1f PB" % n


def fmt_pct(p):
    if p is None:
        return "—"
    if p >= 10:
        return "%.0f%%" % p
    return "%.1f%%" % p


def usage_color(pct):
    if pct >= 85:
        return RED
    if pct >= 70:
        return AMBER
    return GREEN


# 环上分段配色（依次给最大的目录）
SEG_COLORS = ("#f2c14e", "#f2703f", "#ef72a4", "#9b8cf2", "#4c9cf2", "#4ecb8f", "#5ad1d8")
SEG_OTHER = "#7d90a6"   # 其余零碎项
SEG_SYS = "#46586c"     # 系统占用 / 读不到的部分
SEG_FREE = "#243141"    # 剩余空间


def brighten(hex_color, ratio=0.35):
    try:
        h = hex_color.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except Exception:
        return hex_color
    r = int(r + (255 - r) * ratio)
    g = int(g + (255 - g) * ratio)
    b = int(b + (255 - b) * ratio)
    return "#%02x%02x%02x" % (r, g, b)


def elide(s, n=34):
    if len(s) <= n:
        return s
    keep = (n - 1) // 2
    return s[:keep] + "…" + s[-keep:]


def _draw_check(canvas, on, px):
    """自绘复选框。"""
    canvas.delete("all")
    if on:
        canvas.create_rectangle(px(1), px(1), px(17), px(17), fill=ACCENT, outline="")
        canvas.create_line(px(4.5), px(9), px(8), px(13), px(14), px(5),
                           fill="#08131c", width=max(2, px(2)))
    else:
        canvas.create_rectangle(px(1), px(1), px(17), px(17), fill="", outline="#3a4d66")


def colorref(hexcolor):
    h = hexcolor.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return b << 16 | g << 8 | r


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "DiskBall"


def _launcher_exe():
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        exe = exe[:-len("python.exe")] + "pythonw.exe"
    return exe


def _script_path():
    return os.path.abspath(__file__)


def autostart_enabled():
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY)
        try:
            val, _t = winreg.QueryValueEx(k, RUN_VALUE)
            return bool(val)
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(k)
    except Exception:
        return False


def set_autostart(on):
    """开机自启动：写 HKCU 的 Run 键（不需要管理员，随时可撤）。"""
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        try:
            if on:
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ,
                                  '"%s" "%s"' % (_launcher_exe(), _script_path()))
            else:
                try:
                    winreg.DeleteValue(k, RUN_VALUE)
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(k)
        return True
    except Exception:
        return False


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def audit_write(action, detail):
    """把每一次删除/清理写进审计日志：时间、动作、对象。"""
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write("%s\t%s\t%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), action, detail))
    except Exception:
        pass


def enable_dpi():
    """让窗口用物理像素渲染，避免 125%/150% 缩放下的糊边。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # Per-Monitor
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def alive(pid):
    try:
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


_lock_handle = None


def acquire_single_instance():
    """用文件独占锁判断单实例：进程退出锁自动释放，不会因 PID 被回收而误判。"""
    global _lock_handle
    try:
        f = open(LOCK_FILE, "a+")
    except Exception:
        return True
    try:
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except Exception:
        try:
            f.close()
        except Exception:
            pass
        return False
    _lock_handle = f
    return True


# ---------------------------------------------------------------- 磁盘
class Drive:
    def __init__(self, root, total, free, dtype):
        self.root = root
        self.total = total
        self.free = free
        self.used = max(0, total - free)
        self.dtype = dtype

    @property
    def letter(self):
        return self.root[0]

    @property
    def pct(self):
        return (self.used / self.total * 100.0) if self.total else 0.0


def refresh_usage(d):
    try:
        u = shutil.disk_usage(d.root)
        d.total, d.free = u.total, u.free
        d.used = max(0, u.total - u.free)
    except OSError:
        pass
    return d


def list_drives():
    out = []
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        mask = 1
    for i in range(26):
        if not (mask & (1 << i)):
            continue
        root = "%s:\\" % chr(65 + i)
        try:
            t = ctypes.windll.kernel32.GetDriveTypeW(root)
        except Exception:
            t = 3
        if t not in (2, 3, 4):      # 可移动 / 固定 / 网络
            continue
        try:
            u = shutil.disk_usage(root)
        except OSError:
            continue
        # 标签
        try:
            name_buf = ctypes.create_unicode_buffer(261)
            ctypes.windll.kernel32.GetVolumeInformationW(
                root, name_buf, 261, None, None, None, None, 0)
            label = name_buf.value or ""
        except Exception:
            label = ""
        d = Drive(root, u.total, u.free, t)
        d.label = label
        if not hasattr(d, "label"):
            d.label = ""
        out.append(d)
    return out


# ---------------------------------------------------------------- 扫描
SKIP_ATTR = 0x400   # FILE_ATTRIBUTE_REPARSE_POINT


def dir_size(path, should_stop=None):
    """递归统计目录大小；跳过符号链接/联接点，出错忽略。"""
    total = 0
    stack = [path]
    while stack:
        if should_stop is not None and should_stop():
            return total
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                        if getattr(st, "st_file_attributes", 0) & SKIP_ATTR:
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += st.st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


# ---------------------------------------------------------------- 删除到回收站
class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", ctypes.c_bool),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


def recycle_paths(paths):
    """用 Shell 接口删除到回收站（可还原）。返回 (成功?, 信息)。"""
    if not paths:
        return False, "没有选中任何项"
    FO_DELETE = 3
    FOF_ALLOWUNDO = 0x0040
    FOF_NOCONFIRMATION = 0x0010
    FOF_SILENT = 0x0004
    FOF_NOERRORUI = 0x0400
    text = "\0".join(paths)
    buf = ctypes.create_unicode_buffer(len(text) + 2)
    buf[:len(text)] = text
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = ctypes.cast(buf, ctypes.c_wchar_p)
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
    try:
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    except Exception as ex:
        return False, str(ex)
    if rc != 0:
        return False, "系统拒绝删除（错误码 %d），可能被占用或无权限。" % rc
    if op.fAnyOperationsAborted:
        return False, "操作被中断。"
    return True, "已移入回收站"


# ---------------------------------------------------------------- 安全护栏
SUB_TREE_BLOCK = (
    "c:\\windows",
    "c:\\system volume information",
    "c:\\$recycle.bin",
    "c:\\recovery",
    "c:\\perflogs",
    "c:\\boot",
    "c:\\efi",
)
HOME = os.path.expanduser("~")


def _protected_exact():
    s = {
        "c:\\windows",
        "c:\\program files",
        "c:\\program files (x86)",
        "c:\\programdata",
        "c:\\users",
        os.path.join(HOME).lower(),
        os.path.dirname(os.path.abspath(__file__)).lower(),
    }
    for n in ("Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music", "OneDrive"):
        s.add(os.path.join(HOME, n).lower())
    return s


PROTECTED_EXACT = _protected_exact()


def is_protected_path(p):
    """返回 (是否保护, 原因)。"""
    try:
        path = os.path.normpath(p)
    except Exception:
        return True, "路径不合法"
    low = path.lower().rstrip("\\")
    if len(low) == 2 and low[1] == ":":
        return True, "整块磁盘不能删除"
    if low in PROTECTED_EXACT:
        return True, "这是系统或用户目录的根节点，请删除里面的具体内容"
    for bad in SUB_TREE_BLOCK:
        if low == bad or low.startswith(bad + "\\"):
            return True, "系统目录受保护，不能从这里删除"
    return False, ""


# ---------------------------------------------------------------- 推荐清理
def _local_appdata(sub):
    base = os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local")
    return os.path.join(base, sub)


class SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong),
                ("i64Size", ctypes.c_longlong),
                ("i64NumItems", ctypes.c_longlong)]


def recycle_bin_info():
    """回收站总占用（字节）与条目数。"""
    try:
        info = SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(SHQUERYRBINFO)
        if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) == 0:
            return int(info.i64Size), int(info.i64NumItems)
    except Exception:
        pass
    return 0, 0


def empty_recycle_bin():
    SHERB_NOCONFIRMATION = 0x1
    SHERB_NOPROGRESSUI = 0x2
    SHERB_NOSOUND = 0x4
    try:
        ctypes.windll.shell32.SHEmptyRecycleBinW(
            None, None, SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND)
        return True
    except Exception:
        return False


def _rm_file(path):
    """删文件；只读属性先解除。"""
    try:
        os.remove(path)
        return True
    except OSError:
        try:
            os.chmod(path, 0o666)
            os.remove(path)
            return True
        except Exception:
            return False


def _purge_dir(path):
    """清空目录内容（保留目录本身）。联接点/符号链接只删链接自身，绝不进入目标。

    这是整个清理链路里最关键的一道护栏：%TEMP% 里可能存在指向别处的 junction，
    一旦顺着它递归删除，就会把用户别的目录一起删掉。
    """
    removed = skipped = 0
    try:
        entries = list(os.scandir(path))
    except OSError:
        return 0, 1
    for e in entries:
        try:
            st = e.stat(follow_symlinks=False)
            reparse = bool(getattr(st, "st_file_attributes", 0) & SKIP_ATTR)
            if e.is_dir(follow_symlinks=False):
                if reparse:
                    os.rmdir(e.path)          # 只摘掉链接本身
                    removed += 1
                    continue
                r, s = _purge_dir(e.path)
                removed += r
                skipped += s
                try:
                    os.rmdir(e.path)
                except OSError:
                    skipped += 1
            else:
                if _rm_file(e.path):
                    removed += 1
                else:
                    skipped += 1
        except Exception:
            skipped += 1
    return removed, skipped


def purge_paths(paths):
    """永久删除可再生缓存（目录只删内容，保留目录本身）。返回 (成功数, 跳过数)。"""
    removed = skipped = 0
    for p in paths:
        try:
            if not os.path.exists(p):
                continue
            if os.path.isdir(p) and not os.path.islink(p):
                st = os.lstat(p)
                if getattr(st, "st_file_attributes", 0) & SKIP_ATTR:
                    os.rmdir(p)               # 传入的是链接，只删链接
                    removed += 1
                    continue
                r, s = _purge_dir(p)
                removed += r
                skipped += s
            else:
                if _rm_file(p):
                    removed += 1
                else:
                    skipped += 1
        except Exception:
            skipped += 1
    return removed, skipped


def build_clean_candidates():
    """生成"推荐清理"候选清单。risk: safe / admin / irreversible"""
    cands = []
    temp = os.environ.get("TEMP") or os.environ.get("TMP")
    if temp and os.path.isdir(temp):
        cands.append(dict(id="user_temp", title="用户临时文件",
                          desc="%s  各种程序运行时的临时文件" % temp,
                          kind="empty", paths=[temp], risk="safe", checked=True))
    wt = r"C:\Windows\Temp"
    if os.path.isdir(wt):
        cands.append(dict(id="win_temp", title="系统临时文件",
                          desc=r"C:\Windows\Temp  受保护的文件会自动跳过",
                          kind="empty", paths=[wt], risk="safe", checked=True))
    explorer = _local_appdata(r"Microsoft\Windows\Explorer")
    files = []
    if os.path.isdir(explorer):
        try:
            for n in os.listdir(explorer):
                if n.startswith("thumbcache_") or n.startswith("iconcache_"):
                    files.append(os.path.join(explorer, n))
        except OSError:
            pass
    if files:
        cands.append(dict(id="thumb", title="缩略图与图标缓存",
                          desc="资源管理器会自己重建，删掉只会短暂变慢",
                          kind="files", paths=files, risk="safe", checked=True))
    upd = r"C:\Windows\SoftwareDistribution\Download"
    if os.path.isdir(upd):
        cands.append(dict(id="win_update", title="Windows 更新下载缓存",
                          desc="已装完的更新安装包残留，清理需要管理员权限",
                          kind="empty", paths=[upd], risk="admin", checked=True))
    dumps = []
    if os.path.isdir(r"C:\Windows\Minidump"):
        dumps.append(r"C:\Windows\Minidump")
    if os.path.isfile(r"C:\Windows\MEMORY.DMP"):
        dumps.append(r"C:\Windows\MEMORY.DMP")
    cr = _local_appdata("CrashDumps")
    if os.path.isdir(cr):
        dumps.append(cr)
    if dumps:
        cands.append(dict(id="crash", title="崩溃转储文件",
                          desc="蓝屏/崩溃时的内存快照，只有排障时才需要",
                          kind="mixed", paths=dumps, risk="safe", checked=True))
    for cid, path, title in (("pip", _local_appdata(r"pip\Cache"), "Python pip 缓存"),
                             ("npm", _local_appdata("npm-cache"), "npm 缓存"),
                             ("yarn", _local_appdata(r"Yarn\Cache"), "Yarn 缓存")):
        if os.path.isdir(path):
            cands.append(dict(id=cid, title=title,
                              desc="开发时下载的包缓存，删掉下次安装会重新下载",
                              kind="empty", paths=[path], risk="safe", checked=True))
    size, num = recycle_bin_info()
    cands.append(dict(id="recycle", title="回收站",
                      desc="里面是 %d 个已删除项；清空后无法再还原" % num,
                      kind="recycle", paths=[], risk="irreversible",
                      checked=False, count=num, known_size=size))
    return cands


def build_report_items():
    """只报告、不提供删除的项。"""
    items = []
    hib = r"C:\hiberfil.sys"
    try:
        if os.path.isfile(hib):
            items.append(dict(id="hib", title="休眠文件 hiberfil.sys",
                              desc="可在管理员命令行执行 powercfg /h off 关闭休眠后释放（会失去休眠功能）",
                              size=os.path.getsize(hib)))
    except OSError:
        pass
    wo = r"C:\Windows.old"
    if os.path.isdir(wo):
        try:
            items.append(dict(id="winold", title="旧系统备份 Windows.old",
                              desc="升级前的系统备份，建议用 Windows 磁盘清理工具删除",
                              size=dir_size(wo)))
        except OSError:
            pass
    return items


def candidate_size(c):
    if c["kind"] == "recycle":
        return c.get("known_size", 0)
    total = 0
    for p in c["paths"]:
        try:
            if os.path.isdir(p):
                total += dir_size(p)
            elif os.path.isfile(p):
                total += os.path.getsize(p)
        except OSError:
            pass
    return total


# ---------------------------------------------------------------- 环形图渲染
_font_cache = {}


def ui_font(size, bold=True):
    key = (int(size), bold)
    if key in _font_cache:
        return _font_cache[key]
    cands = ["msyhbd.ttc", "msyh.ttc", "segoeuib.ttf", "arialbd.ttf"] if bold else \
            ["msyh.ttc", "segoeui.ttf", "arial.ttf"]
    f = None
    for name in cands:
        p = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name)
        if os.path.exists(p):
            try:
                f = ImageFont.truetype(p, int(size))
                break
            except Exception:
                f = None
    if f is None:
        try:
            f = ImageFont.load_default(int(size))
        except Exception:
            f = ImageFont.load_default()
    _font_cache[key] = f
    return f


def ring_photo(size, pct, color, width=9, track="#233247", bgcolor=KEY, cap=True):
    """生成一个圆环图（抗锯齿），返回 ImageTk.PhotoImage。"""
    ss = 3
    W = int(size) * ss
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = (W - width * ss) / 2.0 - ss
    box = (W / 2 - r, W / 2 - r, W / 2 + r, W / 2 + r)
    wpx = int(width * ss)
    d.arc(box, -90, 270, fill=track, width=wpx)
    sweep = max(0.0, min(100.0, pct)) * 3.6
    if sweep > 0.4:
        d.arc(box, -90, -90 + sweep, fill=color, width=wpx)
        if cap:
            import math
            for ang in (-90, -90 + sweep):
                a = math.radians(ang)
                cx = W / 2 + r * math.cos(a)
                cy = W / 2 + r * math.sin(a)
                rr = wpx / 2.0
                d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=color)
    out = img.resize((int(size), int(size)), Image.LANCZOS)
    bg = Image.new("RGB", out.size, bgcolor)
    bg.paste(out, (0, 0), out)
    return ImageTk.PhotoImage(bg)


def ring_photo_seg(size, parts, width=9, track="#233247", bgcolor=KEY):
    """按比例分段着色的圆环。parts = [(占比, 颜色), ...]，没占满的部分用 track 补。"""
    ss = 3
    W = int(size) * ss
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = (W - width * ss) / 2.0 - ss
    box = (W / 2 - r, W / 2 - r, W / 2 + r, W / 2 + r)
    wpx = int(width * ss)
    d.arc(box, -90, 270, fill=track, width=wpx)
    acc = 0.0
    for frac, color in parts:
        sweep = max(0.0, float(frac)) * 360.0
        if sweep <= 0.6:
            acc += sweep
            continue
        a0 = -90.0 + acc
        a1 = a0 + sweep
        pad = 0.9 if sweep > 8 else 0.15
        d.arc(box, a0 + pad, a1 - pad, fill=color, width=wpx)
        acc += sweep
    out = img.resize((int(size), int(size)), Image.LANCZOS)
    bg = Image.new("RGB", out.size, bgcolor)
    bg.paste(out, (0, 0), out)
    return ImageTk.PhotoImage(bg)


# ---------------------------------------------------------------- 通用小部件
class SlimScroll(tk.Canvas):
    """细长自绘滚动条。"""

    def __init__(self, parent, target, **kw):
        super().__init__(parent, width=8, bg=BG, highlightthickness=0, bd=0, **kw)
        self.target = target
        self._first, self._last = 0.0, 1.0
        self._drag_y = None
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag_y", None))
        target.configure(yscrollcommand=self._sync)

    def _sync(self, first, last):
        self._first, self._last = float(first), float(last)
        self._redraw()

    def _redraw(self):
        self.delete("all")
        h = self.winfo_height()
        if h < 10:
            return
        if self._last - self._first >= 0.999:
            return
        y0 = self._first * h
        y1 = max(self._last * h, y0 + 28)
        self.create_rectangle(2, y0, self.winfo_width() - 2, y1,
                              fill="#2c3c51", outline="")

    def _press(self, event):
        self._drag_y = (event.y, self.target.yview()[0])
        # 点击空白区域 → 直接跳转
        h = self.winfo_height() or 1
        y0 = self._first * h
        y1 = max(self._last * h, y0 + 28)
        if not (y0 <= event.y <= y1):
            self.target.yview_moveto(max(0.0, event.y / h - 0.05))
            self._drag_y = (event.y, self.target.yview()[0])

    def _drag(self, event):
        if not self._drag_y:
            return
        h = self.winfo_height() or 1
        span = self._last - self._first
        dy = event.y - self._drag_y[0]
        new = self._drag_y[1] + dy / h * (1.0 - span) * 0 + dy / h
        self.target.yview_moveto(max(0.0, min(1.0 - span, new)))


def mk_button(parent, text, command, kind="ghost", px=lambda v: v, **kw):
    """扁平按钮：ghost / primary / danger。"""
    palette = {
        "ghost":   (CARD, CARD_HL, TEXT),
        "primary": ("#123243", "#173c52", ACCENT),
        "danger":  ("#331419", "#421a20", "#ff9a9a"),
    }[kind]
    bg, hover, fg = palette
    lbl = tk.Label(parent, text=text, bg=bg, fg=fg, cursor="hand2",
                   font=("Microsoft YaHei UI", -px(12)),
                   padx=px(14), pady=px(7))
    lbl._bg, lbl._hover = bg, hover
    lbl.bind("<Enter>", lambda e: lbl.configure(bg=hover))
    lbl.bind("<Leave>", lambda e: lbl.configure(bg=lbl._bg))
    lbl.bind("<Button-1>", lambda e: command())
    return lbl


# ---------------------------------------------------------------- 小球
class BallApp(tk.Tk):
    DESIGN = DESIGN_SIZE

    def px(self, v):
        return int(round(v * self.scale))

    def __init__(self):
        super().__init__()
        self.scale = max(1.0, self.winfo_fpixels("1i") / 96.0)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=KEY)
        try:
            self.attributes("-transparentcolor", KEY)
        except tk.TclError:
            pass

        self.size = self.px(self.DESIGN)
        self.canvas = tk.Canvas(self, width=self.size, height=self.size,
                                bg=KEY, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.img_id = self.canvas.create_image(self.size // 2, self.size // 2)

        cfg0 = load_json(CFG_FILE, {})
        design = cfg0.get("size")
        if design not in (96, 112, 132, 168):
            design = 112
        self.design = design
        self.size = self.px(design)
        self.canvas.configure(width=self.size, height=self.size)
        self.canvas.coords(self.img_id, self.size // 2, self.size // 2)
        self._size_var = tk.IntVar(value=self.design)
        self._auto_var = tk.BooleanVar(value=autostart_enabled())

        self.drives = []
        self.main = None
        self.pct = 0.0
        self.target = 0.0
        self.hover = False
        self.detail = None
        self.tip = None
        self._tip_job = None
        self._photo = None
        self._drag_from = None
        self._moved = False
        # 环上分段（按目录大小着色）
        self.segments = []
        self.hover_seg = None
        self.struct_items = None
        self.struct_ts = 0
        self._struct_busy = False
        self._struct_token = 0
        self._sq = queue.Queue()
        self._tip_key = None
        self._tip_state = None
        self._mouse = (0, 0)
        self._demo_hover = False

        cfg = load_json(CFG_FILE, {})
        self._cfg = cfg
        self._place_from_cfg(cfg)
        self._bind()
        self.after(300, self._load_struct_cache)

        self.after(10, self.refresh_drives)
        self.after(60, self._animate)
        self.after(400, self.dump_rects)
        self.after(1200, self.scan_structure)
        self.after(250, self._poll_struct)
        self.after(1500, self._tick)

    # ---- 位置
    def _place_from_cfg(self, cfg):
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = cfg.get("x")
        y = cfg.get("y")
        if not isinstance(x, int) or not (0 <= x <= sw - 100):
            x = sw - self.size - self.px(28)
        if not isinstance(y, int) or not (0 <= y <= sh - 100):
            y = self.px(90)
        self.geometry("+%d+%d" % (x, y))
        self.lift()

    def _save_pos(self):
        cfg = dict(self._cfg)
        cfg["x"] = self.winfo_x()
        cfg["y"] = self.winfo_y()
        save_json(CFG_FILE, cfg)
        self._cfg = cfg
        self.dump_rects()

    def dump_rects(self):
        data = {"ball": [self.winfo_x(), self.winfo_y(), self.size, self.size]}
        if self.detail is not None and self.detail.winfo_exists():
            d = self.detail
            data["detail"] = [d.winfo_rootx(), d.winfo_rooty(),
                              d.winfo_width(), d.winfo_height()]
        save_json(RECT_FILE, data)

    # ---- 事件
    def _bind(self):
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Button-3>", self._menu)
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Enter>", self._enter)
        self.canvas.bind("<Leave>", self._leave)

    def _press(self, e):
        self._drag_from = (e.x_root, e.y_root, self.winfo_x(), self.winfo_y())
        self._moved = False
        self._hide_tip()

    def _drag(self, e):
        if not self._drag_from:
            return
        x0, y0, wx, wy = self._drag_from
        dx, dy = e.x_root - x0, e.y_root - y0
        if abs(dx) > 3 or abs(dy) > 3:
            self._moved = True
        self.geometry("+%d+%d" % (wx + dx, wy + dy))

    def _release(self, e):
        if self._moved:
            self._save_pos()
        else:
            self.toggle_detail()
        self._drag_from = None
        self.dump_rects()

    def _menu(self, e):
        m = tk.Menu(self, tearoff=0, bg=CARD, fg=TEXT, activebackground=CARD_HL,
                    activeforeground=TEXT, bd=0)
        m.add_command(label="  查看存储详情  ", command=self.open_detail)
        m.add_command(label="  立即刷新      ", command=self.force_refresh)
        sizes = tk.Menu(m, tearoff=0, bg=CARD, fg=TEXT, activebackground=CARD_HL,
                        activeforeground=TEXT, bd=0)
        for label, val in (("迷你 96", 96), ("小 112", 112), ("中 132", 132), ("大 168", 168)):
            sizes.add_radiobutton(label="  %s  " % label, value=val,
                                  variable=self._size_var,
                                  command=lambda v=val: self.set_size(v))
        m.add_cascade(label="  小球大小      ", menu=sizes)
        m.add_checkbutton(label="  开机自启动    ", variable=self._auto_var,
                          command=self._toggle_autostart)
        m.add_separator()
        m.add_command(label="  退出          ", command=self.quit_app)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _toggle_autostart(self):
        want = bool(self._auto_var.get())
        set_autostart(want)
        self._auto_var.set(autostart_enabled())

    def _enter(self, e):
        self.hover = True
        self._render()
        self._schedule_tip()
    def _leave(self, e):
        self.hover = False
        self.hover_seg = None
        self._render()
        self._hide_tip()

    def _motion(self, e):
        self._mouse = (e.x_root, e.y_root)
        if self._drag_from:
            return
        idx = self._seg_at(e.x, e.y)
        if idx == self.hover_seg:
            if idx is not None:
                self._position_tip()
            return
        self.hover_seg = idx
        self.hover = True
        self._render()
        if idx is not None:
            self._cancel_tip()
            self._show_seg_tip(idx)
        else:
            self._hide_tip()
            self._schedule_tip()

    def _seg_at(self, x, y):
        """按鼠标位置判断落在哪一段上（角度 + 半径双重判定）。"""
        if not self.segments:
            return None
        S = float(self.size)
        cx = cy = S / 2.0
        dx, dy = x - cx, y - cy
        dist = math.hypot(dx, dy)
        r = 0.366 * S
        wpx = max(3.0, 0.054 * S)
        if abs(dist - r) > wpx / 2.0 + self.px(3):
            return None
        ang = (math.degrees(math.atan2(dy, dx)) + 90.0) % 360.0
        for i, sg in enumerate(self.segments):
            if sg["start"] <= ang < sg["end"]:
                return i
        return None

    # ---- 悬浮提示
    def _schedule_tip(self):
        self._cancel_tip()
        self._tip_job = self.after(400, self._show_drive_tip)

    def _cancel_tip(self):
        if self._tip_job:
            try:
                self.after_cancel(self._tip_job)
            except Exception:
                pass
            self._tip_job = None

    def _hide_tip(self):
        self._cancel_tip()
        self._tip_state = None
        if self.tip is not None:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None

    def _show_drive_tip(self):
        if not self.hover or not self.drives:
            return
        state = ("drive", tuple(round(d.pct, 1) for d in self.drives))
        if self.tip is not None and self._tip_state == state:
            return
        self._hide_tip()
        self._tip_key = "drive"
        self._tip_state = state
        tip = tk.Toplevel(self)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tip.configure(bg=LINE)
        box = tk.Frame(tip, bg=PANEL)
        box.pack(padx=1, pady=1)
        head = tk.Label(box, text="  实时占用  ", bg=PANEL, fg=DIM,
                        font=("Microsoft YaHei UI", -self.px(10)))
        head.pack(anchor="w", padx=self.px(6), pady=(self.px(6), 0))
        for d in self.drives:
            row = tk.Frame(box, bg=PANEL)
            row.pack(fill="x", padx=self.px(10), pady=self.px(2))
            dot = tk.Canvas(row, width=self.px(8), height=self.px(8), bg=PANEL,
                            highlightthickness=0)
            dot.create_oval(1, 1, self.px(7), self.px(7), fill=usage_color(d.pct), outline="")
            dot.pack(side="left", pady=(self.px(3), 0))
            tk.Label(row, text="%s:" % d.letter, bg=PANEL, fg=TEXT,
                     font=("Microsoft YaHei UI", -self.px(11), "bold")).pack(side="left")
            tk.Label(row, text=" 已用 %s  剩 %s" % (fmt_pct(d.pct), fmt_size(d.free)),
                     bg=PANEL, fg=MUTED,
                     font=("Microsoft YaHei UI", -self.px(10))).pack(side="left")
        tk.Label(box, text="  单击查看大头 / 小头  ", bg=PANEL, fg=DIM,
                 font=("Microsoft YaHei UI", -self.px(9))).pack(anchor="w", padx=self.px(6),
                                                               pady=(self.px(4), self.px(6)))
        x = self.winfo_x() + self.size + self.px(4)
        y = self.winfo_y() + self.px(14)
        if x + self.px(260) > self.winfo_screenwidth():
            x = self.winfo_x() - self.px(230)
        tip.geometry("+%d+%d" % (x, y))
        self.tip = tip

    def _show_seg_tip(self, idx):
        """鼠标停在环上某一段时的小浮框：色块 + 名称 + 体积 + 占比。"""
        if not (0 <= idx < len(self.segments)):
            return
        sg = self.segments[idx]
        drv = self._main_drive()
        total = float(drv.total) if (drv and drv.total) else 0.0
        state = ("seg", idx, round(sg["size"] / 1048576.0, 1), len(self.segments))
        if self.tip is not None and self._tip_state == state:
            self._position_tip()
            return
        self._hide_tip()
        self._tip_key = "seg:%d" % idx
        self._tip_state = state
        tip = tk.Toplevel(self)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tip.configure(bg=LINE)
        box = tk.Frame(tip, bg=PANEL)
        box.pack(padx=1, pady=1)
        head = tk.Frame(box, bg=PANEL)
        head.pack(fill="x", padx=self.px(9), pady=(self.px(7), self.px(1)))
        dot = tk.Canvas(head, width=self.px(9), height=self.px(9), bg=PANEL,
                        highlightthickness=0)
        dot.create_oval(1, 1, self.px(8), self.px(8), fill=sg["color"], outline="")
        dot.pack(side="left", pady=(self.px(3), 0))
        tk.Label(head, text="  " + elide(sg["name"], 18), bg=PANEL, fg=TEXT,
                 font=("Microsoft YaHei UI", -self.px(11), "bold")).pack(side="left")
        detail = fmt_size(sg["size"])
        if total > 0:
            detail += "   占全盘 %.1f%%" % (sg["size"] / total * 100.0)
        tk.Label(box, text="  " + detail, bg=PANEL, fg=MUTED,
                 font=("Microsoft YaHei UI", -self.px(10))).pack(
            anchor="w", padx=self.px(9), pady=(0, self.px(7)))
        self.tip = tip
        self._position_tip()

    def _position_tip(self):
        if self.tip is None:
            return
        try:
            self.tip.update_idletasks()
            w = self.tip.winfo_reqwidth()
            h = self.tip.winfo_reqheight()
        except Exception:
            return
        mx, my = self._mouse
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        # 贴在球的外侧，避免小框盖住环和中间的数字
        bx, by = self.winfo_x(), self.winfo_y()
        x = bx + self.size + self.px(6)
        y = my - self.px(18)
        if x + w > sw - self.px(6):
            x = bx - w - self.px(6)
        if y + h > sh - self.px(6):
            y = sh - h - self.px(6)
        if y < self.px(6):
            y = self.px(6)
        self.tip.geometry("+%d+%d" % (max(0, x), max(0, y)))

    # ---- 数据
    def refresh_drives(self, first=False):
        prev = {d.root: d for d in self.drives}
        drives = list_drives()
        merged = []
        for d in drives:
            if d.root in prev:
                d.label = prev[d.root].label
            merged.append(d)
        self.drives = merged
        if merged:
            roots = [d.root for d in merged]
            if self.main not in roots:
                sysroot = (os.environ.get("SystemDrive") or "C:") + "\\"
                self.main = sysroot if sysroot in roots else roots[0]
            d = self._main_drive()
            self.target = d.pct if d else 0.0
        self._build_segments()
        self._render()
        if self.detail is not None and self.detail.winfo_exists():
            self.detail.refresh_cards()

    def _main_drive(self):
        for d in self.drives:
            if d.root == self.main:
                return d
        return self.drives[0] if self.drives else None

    def _tick(self):
        self.refresh_drives()
        if self.tip is not None and self.hover:
            if self._tip_key and self._tip_key.startswith("seg"):
                if self.hover_seg is not None:
                    self._show_seg_tip(self.hover_seg)
            else:
                self._show_drive_tip()
        if not self._struct_busy and (time.time() - (self.struct_ts or 0)) > 900:
            self.scan_structure()
        self.after(2000, self._tick)

    def force_refresh(self):
        self.refresh_drives()
        if self.detail is not None and self.detail.winfo_exists():
            self.detail.scan()
        self.invalidate_structure()

    # ---- 环上分段数据：当前盘根目录下各文件夹的体积
    def scan_structure(self):
        drv = self._main_drive()
        if not drv:
            self.after(1500, self.scan_structure)
            return
        if self._struct_busy:
            return
        self._struct_busy = True
        self._struct_token += 1
        baseline = list(self.struct_items or [])
        threading.Thread(target=self._struct_worker,
                         args=(drv.root, self._struct_token, baseline), daemon=True).start()

    def _struct_worker(self, root, token, baseline):
        """统计根目录各项体积。以 baseline（上次结果/缓存）为底稿，逐项更新，
        这样重扫过程中环不会塌成一大片灰。"""
        items = [dict(x) for x in (baseline or []) if x.get("path")]
        pos = {it["path"]: i for i, it in enumerate(items)}
        try:
            entries = []
            with os.scandir(root) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                        is_dir = e.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    if getattr(st, "st_file_attributes", 0) & SKIP_ATTR:
                        continue
                    entries.append((e.name, e.path, is_dir, st.st_size))
        except OSError:
            self._sq.put(("done", token, items))
            return
        for name, path, is_dir, size in entries:
            if not is_dir:
                if path in pos:
                    items[pos[path]]["size"] = size
                else:
                    pos[path] = len(items)
                    items.append({"name": name, "path": path, "size": size})
        self._sq.put(("partial", token, [dict(x) for x in items]))
        dirs = [(n, p) for n, p, is_dir, _s in entries if is_dir]
        if dirs:
            with ThreadPoolExecutor(max_workers=4) as ex:
                futs = {ex.submit(dir_size, p): (n, p) for n, p in dirs}
                for f in as_completed(futs):
                    if token != self._struct_token:
                        return
                    n, p = futs[f]
                    try:
                        size = f.result()
                    except Exception:
                        size = 0
                    if p in pos:
                        items[pos[p]]["size"] = size
                        items[pos[p]]["name"] = n
                    else:
                        pos[p] = len(items)
                        items.append({"name": n, "path": p, "size": size})
                    self._sq.put(("partial", token, [dict(x) for x in items]))
        alive_paths = {p for _n, p, _d, _s in entries}
        items = [x for x in items if x.get("path") in alive_paths]
        self._sq.put(("done", token, items))

    def _poll_struct(self):
        changed = False
        finished = False
        try:
            while True:
                msg = self._sq.get_nowait()
                if len(msg) < 3 or msg[1] != self._struct_token:
                    continue
                if msg[0] == "partial":
                    self.struct_items = msg[2]
                    changed = True
                elif msg[0] == "done":
                    self.struct_items = msg[2]
                    self._struct_busy = False
                    self.struct_ts = time.time()
                    changed = True
                    finished = True
                    self._save_struct_cache(self.main)
        except queue.Empty:
            pass
        try:
            if changed:
                self._build_segments()
                self._render()
            mode = os.environ.get("DISKBALL_DEMO_HOVER")
            if changed and mode and not self._demo_hover and self.segments:
                self._demo_hover = True
                self.hover = True
                idx = min(1, len(self.segments) - 1)
                sg = self.segments[idx]
                mid = math.radians((sg["start"] + sg["end"]) / 2.0 - 90.0)
                r = 0.366 * self.size
                x = self.size / 2.0 + r * math.cos(mid)
                y = self.size / 2.0 + r * math.sin(mid)
                if mode == "2":     # 走真实的鼠标命中逻辑
                    self.canvas.event_generate(
                        "<Motion>", x=int(x), y=int(y),
                        rootx=int(self.winfo_rootx() + x),
                        rooty=int(self.winfo_rooty() + y))
                else:               # 直接指定某一段，便于截图
                    self.hover_seg = idx
                    self._mouse = (self.winfo_rootx() + int(x) + self.px(10),
                                   self.winfo_rooty() + int(y) + self.px(10))
                    self._render()
                    self._show_seg_tip(idx)
        except Exception:
            pass
        finally:
            self.after(250, self._poll_struct)

    def invalidate_structure(self, delay=800):
        self.struct_ts = 0
        self.after(delay, self.scan_structure)

    def _load_struct_cache(self):
        """启动时先用上次的结果把彩色环画出来，不必等几分钟扫描。"""
        if self.struct_items:
            return
        data = load_json(STRUCT_FILE, None)
        if not isinstance(data, dict):
            return
        items = data.get("items") or []
        root = data.get("root") or ""
        if root and self.main and root.upper() != self.main.upper():
            return
        if not items:
            return
        self.struct_items = items
        self._build_segments()
        self._render()

    def _save_struct_cache(self, root):
        items = self.struct_items or []
        if not items:
            return
        try:
            top = sorted(items, key=lambda x: -(x.get("size") or 0))[:120]
            save_json(STRUCT_FILE, {"root": root, "ts": time.time(),
                                    "items": [{"name": i.get("name"),
                                               "path": i.get("path"),
                                               "size": int(i.get("size") or 0)}
                                              for i in top]})
        except Exception:
            pass

    def _build_segments(self):
        """把根目录各项按体积切成环上的色段（含“其他/系统/剩余”）。"""
        drv = self._main_drive()
        items = self.struct_items or []
        if not drv or not items or not drv.total:
            self.segments = []
            self.hover_seg = None
            return
        total = float(drv.total)
        used = float(drv.used or 0)
        parts = []
        for it in items:
            size = it.get("size") or 0
            if size >= total * 0.012:      # 不足 1.2% 的零碎项并进“其他”
                parts.append({"name": it.get("name") or "?", "path": it.get("path"),
                              "size": float(size)})
        parts.sort(key=lambda x: -x["size"])
        top, tail = parts[:7], parts[7:]
        segs = []
        for i, it in enumerate(top):
            segs.append({"name": it["name"], "path": it["path"], "size": it["size"],
                         "color": SEG_COLORS[i % len(SEG_COLORS)]})
        rest = sum(x["size"] for x in tail)
        if rest > 0:
            segs.append({"name": "其他小项", "path": None, "size": rest, "color": SEG_OTHER})
        known = sum(s["size"] for s in segs)
        gap = used - known
        if gap > total * 0.008:
            segs.append({"name": "系统 / 未统计", "path": None, "size": gap, "color": SEG_SYS})
        free = max(0.0, total - used)
        if free > 0:
            segs.append({"name": "剩余空间", "path": None, "size": free,
                         "color": SEG_FREE, "free": True})
        span = sum(s["size"] for s in segs)
        if span <= 0:
            self.segments = []
            return
        acc = 0.0
        for s in segs:
            s["start"] = acc
            acc += s["size"] / span * 360.0
            s["end"] = min(360.0, acc)
        segs[-1]["end"] = 360.0
        self.segments = segs
        if self.hover_seg is not None and self.hover_seg >= len(segs):
            self.hover_seg = None

    # ---- 动效
    def _animate(self):
        if abs(self.pct - self.target) > 0.06:
            self.pct += (self.target - self.pct) * 0.25
            self._render()
        elif self.pct != self.target:
            self.pct = self.target
            self._render()
        self.after(33, self._animate)

    # ---- 绘制小球（按目标尺寸等比布局，缩小时自动省略次要信息）
    def _render(self):
        S = self.size
        ss = 3
        W = S * ss
        img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        cx = cy = W / 2.0

        # 底盘
        disc_fill = (26, 36, 52, 255) if self.hover else (18, 26, 38, 255)
        d.ellipse((0.030 * W, 0.030 * W, 0.970 * W, 0.970 * W), fill=disc_fill,
                  outline=(58, 82, 112, 255), width=max(1, int(0.008 * W)))
        d.arc((0.054 * W, 0.054 * W, 0.946 * W, 0.946 * W), 200, 340,
              fill=(120, 160, 200, 40), width=max(1, int(0.008 * W)))

        drv = self._main_drive()
        pct = self.pct
        color = usage_color(drv.pct if drv else 0)

        # 环形：按目录大小分段着色（无数据时退回单色进度环）
        r = 0.366 * W
        wpx = max(3, int(0.054 * W))
        box = (cx - r, cy - r, cx + r, cy + r)
        if self.segments:
            for i, sg in enumerate(self.segments):
                a0 = -90.0 + sg["start"]
                a1 = -90.0 + sg["end"]
                span_deg = sg["end"] - sg["start"]
                pad = 1.5 if span_deg >= 10 else (0.9 if span_deg >= 4 else 0.15)
                s0, s1 = a0 + pad, a1 - pad
                if s1 - s0 < 0.06:
                    s1 = s0 + 0.06
                col = sg["color"]
                if self.hover_seg == i:
                    col = brighten(col, 0.4)
                d.arc(box, s0, s1, fill=col, width=wpx)
                arclen = math.radians(s1 - s0) * r
                if arclen > wpx * 1.2 and not sg.get("free"):
                    rr = wpx / 2.0
                    for ang in (s0, s1):
                        a = math.radians(ang)
                        px_, py_ = cx + r * math.cos(a), cy + r * math.sin(a)
                        d.ellipse((px_ - rr, py_ - rr, px_ + rr, py_ + rr), fill=col)
        else:
            d.arc(box, -90, 270, fill=(35, 50, 71, 255), width=wpx)
            sweep = max(0.0, min(100.0, pct)) * 3.6
            if sweep > 0.6:
                d.arc(box, -90, -90 + sweep, fill=color, width=wpx)
                for ang in (-90.0, -90.0 + sweep):
                    a = math.radians(ang)
                    px_, py_ = cx + r * math.cos(a), cy + r * math.sin(a)
                    rr = wpx / 2.0
                    d.ellipse((px_ - rr, py_ - rr, px_ + rr, py_ + rr), fill=color)

        small = self.design < 118
        if drv:
            d.text((cx, 0.357 * W), "%s:" % drv.letter, font=ui_font(0.084 * W, True),
                   fill=(206, 220, 238), anchor="mm")
            d.text((cx, (0.47 if small else 0.506) * W), "%.0f%%" % drv.pct,
                   font=ui_font((0.155 if small else 0.143) * W, True),
                   fill=color, anchor="mm")
            if not small:
                d.text((cx, 0.630 * W), "剩 %s" % fmt_size(drv.free),
                       font=ui_font(0.058 * W, False), fill=(140, 160, 182), anchor="mm")
        else:
            d.text((cx, cy), "无磁盘", font=ui_font(0.072 * W, True),
                   fill=(140, 160, 182), anchor="mm")

        # 多盘标记（尺寸太小时省略）
        n = len(self.drives)
        if n and not small:
            gap = 0.054 * W
            start = cx - (n - 1) * gap / 2
            for i, dr in enumerate(self.drives):
                dx = start + i * gap
                dy = 0.810 * W
                rr = 0.0155 * W
                on = dr.root == self.main
                d.ellipse((dx - rr, dy - rr, dx + rr, dy + rr),
                          fill=usage_color(dr.pct) if on else (48, 64, 86),
                          outline=(120, 150, 180) if on else None,
                          width=max(1, int(0.006 * W)))

        out = img.resize((S, S), Image.LANCZOS)
        bg = Image.new("RGB", (S, S), KEY)
        bg.paste(out, (0, 0), out)
        self._photo = ImageTk.PhotoImage(bg)
        self.canvas.itemconfigure(self.img_id, image=self._photo)

    # ---- 尺寸切换
    def set_size(self, design):
        old = self.size
        self.design = design
        self.size = self.px(design)
        self.canvas.configure(width=self.size, height=self.size)
        self.canvas.coords(self.img_id, self.size // 2, self.size // 2)
        x = self.winfo_x() + (old - self.size) // 2
        y = self.winfo_y() + (old - self.size) // 2
        self.geometry("%dx%d+%d+%d" % (self.size, self.size, x, y))
        cfg = dict(self._cfg)
        cfg["size"] = design
        save_json(CFG_FILE, cfg)
        self._cfg = cfg
        try:
            self._size_var.set(design)
        except Exception:
            pass
        self._render()
        self.dump_rects()

    # ---- 详情窗
    def open_detail(self):
        self._hide_tip()
        if self.detail is not None and self.detail.winfo_exists():
            self.detail.deiconify()
            self.detail.lift()
            self.detail.focus_force()
            return
        self.detail = DetailWindow(self)
        self.dump_rects()

    def toggle_detail(self):
        if self.detail is not None and self.detail.winfo_exists():
            self.detail.on_close()
        else:
            self.open_detail()

    def quit_app(self):
        self._hide_tip()
        self._save_pos()
        try:
            os.remove(PID_FILE)
        except Exception:
            pass
        self.destroy()


# ---------------------------------------------------------------- 行控件
class Row:
    """目录/文件列表里的一行。"""

    H = 46

    def __init__(self, parent, w, item, max_size, total_size, on_toggle, on_open, on_menu):
        self.w = w
        self.item = item
        self.path = item["path"]
        self.selected = False
        self.max_size = max_size or 1
        self.total = total_size or 0
        self.frame = tk.Frame(parent, bg=BG, height=w.px(self.H))
        self.frame.pack(fill="x")
        self.frame.pack_propagate(False)

        # 复选框
        self.chk = tk.Canvas(self.frame, width=w.px(18), height=w.px(18), bg=BG,
                             highlightthickness=0, cursor="hand2")
        self.chk.pack(side="left", padx=(w.px(12), w.px(10)))
        self.chk.bind("<Button-1>", lambda e: on_toggle(self))
        self._draw_chk()

        # 图标
        self.ico = tk.Canvas(self.frame, width=w.px(18), height=w.px(18), bg=BG,
                             highlightthickness=0)
        self.ico.pack(side="left", padx=(0, w.px(10)))
        self._draw_icon()

        # 名称（最近新增模式下带时间）
        note = item.get("note")
        title = elide(item["name"], 34)
        if note:
            title = "%s   %s" % (title, note)
        self.name = tk.Label(self.frame, text=title, bg=BG, fg=TEXT,
                             font=("Microsoft YaHei UI", -w.px(12)), anchor="w")
        self.name.pack(side="left", fill="x", expand=True)

        # 占比标签
        self.pctl = tk.Label(self.frame, text="", bg=BG, fg=MUTED, width=7, anchor="e",
                             font=("Microsoft YaHei UI", -w.px(11)))
        self.pctl.pack(side="right", padx=(w.px(8), w.px(14)))
        # 大小
        self.size = tk.Label(self.frame, text="扫描中…" if item.get("size") is None else fmt_size(item["size"]),
                             bg=BG, fg=TEXT if item.get("size") else DIM, width=10, anchor="e",
                             font=("Microsoft YaHei UI", -w.px(11), "bold"))
        self.size.pack(side="right", padx=(w.px(6), 0))
        # 条
        self.bar = tk.Canvas(self.frame, width=w.px(w.BAR_W), height=w.px(6), bg=BG,
                             highlightthickness=0)
        self.bar.pack(side="right", padx=(w.px(10), w.px(6)))
        self._draw_bar()

        for widget in (self.frame, self.name, self.size, self.pctl, self.bar, self.ico):
            widget.bind("<Button-1>", lambda e: on_toggle(self))
            widget.bind("<Double-Button-1>", lambda e: on_open(self))
            widget.bind("<Button-3>", lambda e: on_menu(self, e))
            widget.bind("<Enter>", self._enter)
            widget.bind("<Leave>", self._leave)

    def _draw_icon(self):
        c = self.ico
        c.delete("all")
        p = self.w.px
        if self.item["is_dir"]:
            c.create_rectangle(p(1), p(5), p(17), p(16), fill="#3d5673", outline="")
            c.create_rectangle(p(1), p(2), p(9), p(6), fill="#4c6b8d", outline="")
        else:
            c.create_rectangle(p(3), p(1), p(15), p(17), fill="#2f4257", outline="#54718f")
            c.create_line(p(3), p(12), p(15), p(12), fill="#54718f")

    def _bar_fill(self):
        size = self.item.get("size")
        if not size:
            return 0.0
        span = self.max_size or 1
        return max(0.02, min(1.0, size / float(span)))

    def _draw_bar(self):
        c = self.bar
        c.delete("all")
        p = self.w.px
        h = p(6)
        c.create_rectangle(0, 0, p(self.w.BAR_W), h, fill="#1b2634", outline="")
        if self.item.get("size"):
            c.create_rectangle(0, 0, p(self.w.BAR_W) * self._bar_fill(), h,
                               fill=self.item.get("color", ACCENT), outline="")

    def set_max(self, max_size, total_size):
        self.max_size = max_size
        self.total = total_size
        size = self.item.get("size")
        if size is not None:
            self.size.configure(text=fmt_size(size), fg=TEXT)
            pct = (size / float(total_size) * 100.0) if total_size else None
            self.pctl.configure(text=fmt_pct(pct), fg=(RED if pct and pct > 8 else MUTED))
        self._draw_bar()

    def set_selected(self, on):
        self.selected = on
        bg = "#16283a" if on else BG
        self.frame.configure(bg=bg)
        for wdg in (self.name, self.size, self.pctl, self.bar, self.ico, self.chk):
            wdg.configure(bg=bg)
        self._draw_chk()
        self._draw_bar()

    def _draw_chk(self):
        c = self.chk
        c.delete("all")
        p = self.w.px
        if self.selected:
            c.create_rectangle(p(1), p(1), p(17), p(17), fill=ACCENT, outline="")
            c.create_line(p(4.5), p(9), p(8), p(13), p(14), p(5), fill="#08131c",
                          width=max(2, p(2)))
        else:
            c.create_rectangle(p(1), p(1), p(17), p(17), fill="", outline="#3a4d66")

    def _enter(self, e):
        if not self.selected:
            self.frame.configure(bg=CARD)
            for wdg in (self.name, self.size, self.pctl, self.bar, self.ico, self.chk):
                wdg.configure(bg=CARD)

    def _leave(self, e):
        if not self.selected:
            self.frame.configure(bg=BG)
            for wdg in (self.name, self.size, self.pctl, self.bar, self.ico, self.chk):
                wdg.configure(bg=BG)

    def destroy(self):
        self.frame.destroy()


# ---------------------------------------------------------------- 确认框
class Confirm(tk.Toplevel):
    def __init__(self, parent, title, lines, ok_text, danger=True):
        super().__init__(parent)
        self.result = False
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=LINE)
        w = parent.px(430)
        body = tk.Frame(self, bg=PANEL)
        body.pack(padx=1, pady=1)
        tk.Label(body, text=title, bg=PANEL, fg=TEXT,
                 font=("Microsoft YaHei UI", -parent.px(14), "bold")).pack(
            anchor="w", padx=parent.px(22), pady=(parent.px(18), parent.px(8)))
        for ln in lines:
            tk.Label(body, text=ln, bg=PANEL, fg=MUTED, justify="left", anchor="w",
                     wraplength=w - parent.px(44),
                     font=("Microsoft YaHei UI", -parent.px(11))).pack(
                anchor="w", padx=parent.px(22))
        btns = tk.Frame(body, bg=PANEL)
        btns.pack(fill="x", padx=parent.px(22), pady=(parent.px(16), parent.px(16)))
        b1 = mk_button(btns, "取消", self._no, "ghost", parent.px)
        b1.pack(side="right")
        b2 = mk_button(btns, ok_text, self._yes, "danger" if danger else "primary", parent.px)
        b2.pack(side="right", padx=(0, parent.px(8)))
        self.update_idletasks()
        px_, py_ = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        ww, wh = self.winfo_width(), self.winfo_height()
        self.geometry("+%d+%d" % (px_ + (pw - ww) // 2, py_ + (ph - wh) // 3))
        self.grab_set()
        self.transient(parent)

    def _yes(self):
        self.result = True
        self.destroy()

    def _no(self):
        self.result = False
        self.destroy()


# ---------------------------------------------------------------- 详情窗
class DetailWindow(tk.Toplevel):
    BAR_W = 168
    RECENT_DAYS = 30
    MIN_RECENT = 20 * 1024 * 1024      # 只看 20MB 以上的新增文件
    MAX_RECENT = 300
    # 这些目录里"新增"的多是系统更新/缓存产生的，不是用户新装的东西
    SKIP_DIRS = {"$recycle.bin", "system volume information", "recovery", "perflogs",
                 "windows", "temp", "tmp", "winsxs", "softwaredistribution",
                 "node_modules", "__pycache__", ".git", "cache", "cache2", "code cache"}

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.scale = app.scale
        self.title("磁盘小球 · 存储详情")
        self.configure(bg=BG)
        self.withdraw()
        w, h = self.px(1120), self.px(720)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2),
                                       max(0, (sh - h) // 2 - self.px(20))))
        self.minsize(self.px(960), self.px(620))
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.drives = app.drives
        self.cur = app.main if app.main else (self.drives[0].root if self.drives else "C:\\")
        self.cur_path = self.cur
        self.history = []
        self.mode = "dir"            # dir | big
        self.items = []
        self.rows = {}
        self.selected = set()
        self.max_size = 1
        self.q = queue.Queue()
        self.token = 0
        self.cancelled = -1
        self.scanning = False
        self.scanned_files = 0
        self._drive_refs = {}
        self.clean_cands = []
        self.clean_sel = set()
        self.clean_cards = {}
        self.clean_sizes = {}
        self.clean_panel = None

        self._build()
        self.refresh_cards()
        self._apply_titlebar()
        self.bind("<Escape>", lambda e: self.on_close())
        self.bind("<F5>", lambda e: self.scan())
        self.deiconify()
        self.lift()
        self.after(120, self._ensure_visible)
        self.after(700, self._ensure_visible)
        self.after(200, lambda: self.scan())
        self.after(100, self._poll_loop)
        self.after(150, app.dump_rects)
        if os.environ.get("DISKBALL_DEMO_CLEAN") == "1":
            self.after(1500, lambda: self.set_mode("clean"))
        if os.environ.get("DISKBALL_DEMO_RECENT") == "1":
            self.after(1500, lambda: self.set_mode("recent"))

    def px(self, v):
        return int(round(v * self.scale))

    def _ensure_visible(self):
        """偶发被系统最小化/隐藏时，强制恢复成一个正常窗口。"""
        try:
            if self.state() in ("iconic", "withdrawn"):
                self.deiconify()
            self.lift()
        except Exception:
            pass

    def _apply_titlebar(self):
        try:
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            dwm = ctypes.windll.dwmapi
            one = ctypes.c_int(1)
            dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(one), ctypes.sizeof(one))
            cap = ctypes.c_int(colorref(BG))
            dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(cap), ctypes.sizeof(cap))
            fg = ctypes.c_int(colorref(TEXT))
            dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(fg), ctypes.sizeof(fg))
        except Exception:
            pass

    # ------------------------------------------------------------ 布局
    def _build(self):
        F = lambda s, b=False: ("Microsoft YaHei UI", -self.px(s), "bold" if b else "normal")

        # 头部
        head = tk.Frame(self, bg=BG, height=self.px(70))
        head.pack(fill="x")
        head.pack_propagate(False)
        left = tk.Frame(head, bg=BG)
        left.pack(side="left", padx=(self.px(24), 0), pady=self.px(14))
        tk.Label(left, text="磁盘小球", bg=BG, fg=TEXT, font=F(17, True)).pack(side="left")
        tk.Label(left, text="   实时存储占用 · 找出大头", bg=BG, fg=DIM, font=F(10)).pack(side="left")

        right = tk.Frame(head, bg=BG)
        right.pack(side="right", padx=self.px(24))
        self.sum_label = tk.Label(right, text="", bg=BG, fg=MUTED, font=F(11))
        self.sum_label.pack(side="right", padx=(self.px(14), 0))
        mk_button(right, "重新扫描", self.refresh_all, "ghost", self.px).pack(side="right")

        # 主体
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=self.px(20), pady=(0, self.px(14)))

        # 左：磁盘卡片
        self.left = tk.Frame(body, bg=BG, width=self.px(252))
        self.left.pack(side="left", fill="y")
        self.left.pack_propagate(False)
        tk.Label(self.left, text="存储介质", bg=BG, fg=DIM, font=F(10)).pack(
            anchor="w", padx=self.px(14), pady=(self.px(4), self.px(8)))
        self.cards_host = tk.Frame(self.left, bg=BG)
        self.cards_host.pack(fill="x", padx=self.px(14))
        for d in self.drives:
            self._build_card(d)
        legend = tk.Frame(self.left, bg=BG)
        legend.pack(side="bottom", fill="x", padx=self.px(14), pady=self.px(10))
        tk.Label(legend, text="主盘环 = 各目录占比 · 其它盘 = 紧张程度", bg=BG, fg=DIM,
                 font=F(9)).pack(anchor="w")

        # 右：内容
        self.right = tk.Frame(body, bg=BG)
        self.right.pack(side="left", fill="both", expand=True, padx=(self.px(18), 0))

        # 工具栏
        bar = tk.Frame(self.right, bg=BG, height=self.px(38))
        bar.pack(fill="x")
        bar.pack_propagate(False)
        self.back_btn = tk.Label(bar, text="↑ 上级", bg=CARD, fg=MUTED, cursor="hand2",
                                 font=F(11), padx=self.px(10), pady=self.px(4))
        self.back_btn.pack(side="left", padx=(0, self.px(8)))
        self.back_btn.bind("<Button-1>", lambda e: self.go_up())
        self.path_label = tk.Label(bar, text="", bg=BG, fg=TEXT, font=F(11), anchor="w")
        self.path_label.pack(side="left", fill="x", expand=True)
        self.status = tk.Label(bar, text="", bg=BG, fg=ACCENT, font=F(10), anchor="e")
        self.status.pack(side="right", padx=(self.px(10), 0))

        # 模式切换
        seg = tk.Frame(self.right, bg=BG, height=self.px(36))
        seg.pack(fill="x", pady=(self.px(6), self.px(4)))
        seg.pack_propagate(False)
        self.seg_box = tk.Frame(seg, bg=CARD)
        self.seg_box.pack(side="left")
        self.seg_dir = tk.Label(self.seg_box, text="  按目录  ", bg=CARD, fg=MUTED, cursor="hand2",
                                font=F(11), pady=self.px(3))
        self.seg_dir.pack(side="left")
        self.seg_big = tk.Label(self.seg_box, text="  大文件 100MB+  ", bg=CARD, fg=MUTED,
                                cursor="hand2", font=F(11), pady=self.px(3))
        self.seg_big.pack(side="left")
        self.seg_recent = tk.Label(self.seg_box, text="  最近新增  ", bg=CARD, fg=MUTED,
                                   cursor="hand2", font=F(11), pady=self.px(3))
        self.seg_recent.pack(side="left")
        self.seg_dir.bind("<Button-1>", lambda e: self.set_mode("dir"))
        self.seg_big.bind("<Button-1>", lambda e: self.set_mode("big"))
        self.seg_recent.bind("<Button-1>", lambda e: self.set_mode("recent"))
        self.seg_clean = tk.Label(self.seg_box, text="  推荐清理  ", bg=CARD, fg=MUTED,
                                  cursor="hand2", font=F(11), pady=self.px(3))
        self.seg_clean.pack(side="left")
        self.seg_clean.bind("<Button-1>", lambda e: self.set_mode("clean"))
        self.hint = tk.Label(seg, text="单击选中 · 双击进入文件夹 · 右键更多操作",
                             bg=BG, fg=DIM, font=F(9))
        self.hint.pack(side="right")

        # 表头
        self.th = tk.Frame(self.right, bg=BG, height=self.px(30))
        self.th.pack(fill="x")
        self.th.pack_propagate(False)
        tk.Label(self.th, text="名称", bg=BG, fg=DIM, font=F(10)).pack(side="left", padx=(self.px(42), 0))
        self.th_pct = tk.Label(self.th, text="占本盘", bg=BG, fg=DIM, font=F(10), width=7,
                               anchor="e")
        self.th_pct.pack(side="right", padx=(self.px(8), self.px(14)))
        tk.Label(self.th, text="大小", bg=BG, fg=DIM, font=F(10), width=10, anchor="e").pack(
            side="right", padx=(self.px(6), 0))
        tk.Label(self.th, text="相对大小", bg=BG, fg=DIM, font=F(10)).pack(side="right",
                                                                  padx=(self.px(10), self.px(6)))

        # 列表
        self.listwrap = tk.Frame(self.right, bg=BG)
        self.listwrap.pack(fill="both", expand=True)
        listwrap = self.listwrap
        self.canvas = tk.Canvas(listwrap, bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.sbar = SlimScroll(listwrap, self.canvas)
        self.sbar.pack(side="right", fill="y", padx=(self.px(6), 0))
        self.inner = tk.Frame(self.canvas, bg=BG)
        self.win_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")

        def _cfg(e):
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
            self.canvas.itemconfigure(self.win_id, width=self.canvas.winfo_width())
        self.inner.bind("<Configure>", _cfg)
        self.canvas.bind("<Configure>", _cfg)
        self.canvas.bind("<Enter>", lambda e: self.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.unbind_all("<MouseWheel>"))

        # 底部
        foot = tk.Frame(self, bg=BG, height=self.px(64))
        foot.pack(fill="x", padx=self.px(20), pady=(0, self.px(10)))
        foot.pack_propagate(False)
        self.sel_label = tk.Label(foot, text="未选择任何项", bg=BG, fg=MUTED, font=F(11))
        self.sel_label.pack(side="left", padx=(self.px(4), 0))
        self.del_btn = mk_button(foot, "移到回收站", self.delete_selected, "danger", self.px)
        self.del_btn.pack(side="right")
        self.del_btn.configure(state="disabled")
        self.open_btn = mk_button(foot, "在资源管理器中打开", self.open_current, "ghost", self.px)
        self.open_btn.pack(side="right", padx=(0, self.px(10)))

        self._build_clean_page()
        self._switch_view()
        self._sync_seg()

    # ------------------------------------------------------------ 推荐清理页
    def _build_clean_page(self):
        F = lambda s, b=False: ("Microsoft YaHei UI", -self.px(s), "bold" if b else "normal")
        self.clean_panel = tk.Frame(self.right, bg=BG)

        strip = tk.Frame(self.clean_panel, bg=CARD)
        strip.pack(fill="x", pady=(self.px(4), self.px(10)))
        left = tk.Frame(strip, bg=CARD)
        left.pack(side="left", padx=self.px(16), pady=self.px(12))
        tk.Label(left, text="勾选后预计释放", bg=CARD, fg=DIM, font=F(10)).pack(anchor="w")
        self.clean_total = tk.Label(left, text="扫描中…", bg=CARD, fg=ACCENT, font=F(21, True))
        self.clean_total.pack(anchor="w")
        tk.Label(strip,
                 text="这些都是可再生的缓存/临时文件，直接删除才能真正腾出空间（不进回收站）；\n"
                      "回收站单独放在最后一项，清空后无法还原。",
                 bg=CARD, fg=MUTED, font=F(10), justify="left").pack(
            side="left", padx=self.px(26), pady=self.px(6))

        wrap = tk.Frame(self.clean_panel, bg=BG)
        wrap.pack(fill="both", expand=True)
        cv = tk.Canvas(wrap, bg=BG, highlightthickness=0, bd=0)
        cv.pack(side="left", fill="both", expand=True)
        sb = SlimScroll(wrap, cv)
        sb.pack(side="right", fill="y", padx=(self.px(6), 0))
        inner = tk.Frame(cv, bg=BG)
        wid = cv.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>", lambda e: cv.itemconfigure(wid, width=cv.winfo_width()))
        cv.bind("<Enter>", lambda e: self.bind_all("<MouseWheel>",
                                                   lambda ev: cv.yview_scroll(int(-ev.delta / 120 * 3), "units")))
        cv.bind("<Leave>", lambda e: self.unbind_all("<MouseWheel>"))
        self.clean_canvas = cv
        self.clean_inner = inner

    def _chip(self, parent, text, tone):
        colors = {"safe": ("#123126", "#5ddba0"), "admin": ("#33280f", "#f5c45a"),
                  "irreversible": ("#331419", "#ff9a9a"), "report": ("#1b2735", "#8ba0b8")}
        bg, fg = colors.get(tone, colors["report"])
        tk.Label(parent, text=text, bg=bg, fg=fg, font=("Microsoft YaHei UI", -self.px(9)),
                 padx=self.px(8), pady=self.px(2)).pack(side="right", padx=(0, self.px(10)))

    def _render_clean(self):
        for wdg in self.clean_inner.winfo_children():
            wdg.destroy()
        self.clean_cards = {}
        for c in self.clean_cands:
            self._clean_card(c)
        reports = build_report_items()
        if reports:
            tk.Label(self.clean_inner, text="仅报告 · 不在这里删除", bg=BG, fg=DIM,
                     font=("Microsoft YaHei UI", -self.px(10))).pack(
                anchor="w", pady=(self.px(12), self.px(6)))
            for r in reports:
                self._report_card(r)
        self._refresh_clean_footer()

    def _clean_card(self, c):
        F = lambda s, b=False: ("Microsoft YaHei UI", -self.px(s), "bold" if b else "normal")
        card = tk.Frame(self.clean_inner, bg=CARD, cursor="hand2")
        card.pack(fill="x", pady=(0, self.px(6)))
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=self.px(14), pady=self.px(10))

        chk = tk.Canvas(inner, width=self.px(18), height=self.px(18), bg=CARD,
                        highlightthickness=0, cursor="hand2")
        chk.pack(side="left", padx=(0, self.px(12)))
        _draw_check(chk, c.get("checked", False), self.px)

        size_l = tk.Label(inner, text="统计中…", bg=CARD, fg=TEXT,
                          font=F(12, True), width=9, anchor="e")
        size_l.pack(side="right", padx=(self.px(6), self.px(8)))
        tone = {"safe": "safe", "admin": "admin", "irreversible": "irreversible"}[c["risk"]]
        chip_txt = {"safe": "安全可删", "admin": "需管理员", "irreversible": "不可恢复"}[c["risk"]]
        self._chip(inner, chip_txt, tone)

        text = tk.Frame(inner, bg=CARD)
        text.pack(side="left", fill="x", expand=True)
        tk.Label(text, text=c["title"], bg=CARD, fg=TEXT, font=F(11, True),
                 anchor="w").pack(fill="x")
        tk.Label(text, text=c["desc"], bg=CARD, fg=DIM, font=F(9), anchor="w").pack(fill="x")

        self.clean_cards[c["id"]] = {"card": card, "inner": inner, "chk": chk, "size": size_l, "c": c}
        if c.get("checked"):
            self.clean_sel.add(c["id"])

        def toggle(_e=None, cid=c["id"]):
            if cid in self.clean_sel:
                self.clean_sel.discard(cid)
                new = False
            else:
                self.clean_sel.add(cid)
                new = True
            _draw_check(chk, new, self.px)
            self._refresh_clean_footer()
        for w in (card, inner, chk, size_l, text):
            w.bind("<Button-1>", toggle)
        for w in text.winfo_children():
            w.bind("<Button-1>", toggle)

    def _report_card(self, r):
        F = lambda s, b=False: ("Microsoft YaHei UI", -self.px(s), "bold" if b else "normal")
        card = tk.Frame(self.clean_inner, bg="#101a26")
        card.pack(fill="x", pady=(0, self.px(6)))
        inner = tk.Frame(card, bg="#101a26")
        inner.pack(fill="x", padx=self.px(14), pady=self.px(9))
        size_l = tk.Label(inner, text=fmt_size(r.get("size") or 0), bg="#101a26", fg=MUTED,
                          font=F(11, True), width=9, anchor="e")
        size_l.pack(side="right")
        self._chip(inner, "仅报告", "report")
        text = tk.Frame(inner, bg="#101a26")
        text.pack(side="left", fill="x", expand=True)
        tk.Label(text, text=r["title"], bg="#101a26", fg="#c3d2e2", font=F(11, True),
                 anchor="w").pack(fill="x")
        tk.Label(text, text=r["desc"], bg="#101a26", fg=DIM, font=F(9), anchor="w",
                 wraplength=self.px(560), justify="left").pack(fill="x")

    def _refresh_clean_footer(self):
        total = sum(self.clean_sizes.get(cid, 0) for cid in self.clean_sel)
        n = len(self.clean_sel)
        self.clean_total.configure(text=fmt_size(total) if total else "0 B")
        if n:
            self.sel_label.configure(text="已勾选 %d 项 · 预计释放 %s" % (n, fmt_size(total)), fg=TEXT)
            self.del_btn.configure(state="normal", fg="#ff9a9a", bg="#331419")
        else:
            self.sel_label.configure(text="只清可再生的缓存，回收站需单独勾选", fg=MUTED)
            self.del_btn.configure(state="disabled", fg=DIM, bg=CARD)

    def scan_clean(self):
        self.token += 1
        token = self.token
        self.cancelled = token
        self.clean_cands = build_clean_candidates()
        self.clean_sel = set()
        self.clean_sizes = {}
        self._render_clean()
        self.clean_total.configure(text="扫描中…")
        self.status.configure(text="正在统计可清理的空间…", fg=ACCENT)
        threading.Thread(target=self._measure_clean, args=(token,), daemon=True).start()

    def _measure_clean(self, token):
        for c in self.clean_cands:
            if token != self.cancelled:
                return
            try:
                size = candidate_size(c)
            except Exception:
                size = 0
            self.q.put(("clean_size", token, c["id"], size))
        self.q.put(("clean_done", token))

    def _switch_view(self):
        if self.mode == "clean":
            self.th.pack_forget()
            self.listwrap.pack_forget()
            self.clean_panel.pack(fill="both", expand=True)
            self.back_btn.pack_forget()
            self.open_btn.pack_forget()
            self.del_btn.configure(text="清理选中项")
            self.del_btn.bind("<Button-1>", lambda e: self.clean_selected())
            self.hint.configure(text="建议清理会跳过正在被占用的文件")
            self._refresh_clean_footer()
        else:
            self.clean_panel.pack_forget()
            self.th.pack(fill="x")
            self.listwrap.pack(fill="both", expand=True)
            self.back_btn.pack(side="left", padx=(0, self.px(8)), before=self.path_label)
            self.open_btn.pack(side="right", padx=(0, self.px(10)))
            self.del_btn.configure(text="移到回收站")
            self.del_btn.bind("<Button-1>", lambda e: self.delete_selected())
            self.hint.configure(text="单击选中 · 双击进入文件夹 · 右键更多操作")
            self._update_sel()

    def refresh_all(self):
        if self.mode == "clean":
            self.scan_clean()
        else:
            self.scan()

    def _build_card(self, d):
        F = lambda s, b=False: ("Microsoft YaHei UI", -self.px(s), "bold" if b else "normal")
        card = tk.Frame(self.cards_host, bg=CARD, cursor="hand2")
        card.pack(fill="x", pady=(0, self.px(8)))
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=self.px(12), pady=self.px(10))
        ring = tk.Canvas(inner, width=self.px(48), height=self.px(48), bg=CARD,
                         highlightthickness=0)
        ring.pack(side="left")
        txt = tk.Frame(inner, bg=CARD)
        txt.pack(side="left", padx=(self.px(10), 0), fill="x", expand=True)
        title = tk.Label(txt, text="%s:" % d.letter, bg=CARD, fg=TEXT, font=F(12, True), anchor="w")
        title.pack(fill="x")
        pct_l = tk.Label(txt, text="", bg=CARD, fg=MUTED, font=F(10), anchor="w")
        pct_l.pack(fill="x")
        sub_l = tk.Label(txt, text="", bg=CARD, fg=DIM, font=F(9), anchor="w")
        sub_l.pack(fill="x")

        self._drive_refs[d.root] = {"card": card, "ring": ring, "pct": pct_l, "sub": sub_l,
                                    "title": title, "text": txt, "inner": inner}

        for wdg in (card, inner, ring, title, pct_l, sub_l, txt):
            wdg.bind("<Button-1>", lambda e, root=d.root: self.select_drive(root))
        self._draw_card(d.root)

    def _draw_card(self, root):
        ref = self._drive_refs.get(root)
        if not ref:
            return
        d = next((x for x in self.drives if x.root == root), None)
        if d is None:
            return
        sel = (root == self.cur)
        bg = "#1b2a3d" if sel else CARD
        for k in ("card", "inner", "ring", "text"):
            ref[k].configure(bg=bg)
        ref["pct"].configure(bg=bg)
        ref["sub"].configure(bg=bg)
        ref["title"].configure(bg=bg, fg=TEXT if sel else "#c3d2e2")
        if sel:
            ref["pct"].configure(fg=usage_color(d.pct))
        else:
            ref["pct"].configure(fg=MUTED)
        ref["pct"].configure(text="已用 %s  ·  剩 %s" % (fmt_pct(d.pct), fmt_size(d.free)))
        ref["sub"].configure(text="%s / %s" % (fmt_size(d.used), fmt_size(d.total)))
        ref["ring"].delete("all")
        parts = self._seg_parts(d)
        if parts:
            ref["ring"].image = ring_photo_seg(self.px(48), parts, width=self.px(7),
                                               bgcolor=bg, track="#2c3f58")
        else:
            ref["ring"].image = ring_photo(self.px(48), d.pct, usage_color(d.pct),
                                           width=self.px(7), bgcolor=bg, track="#2c3f58")
        ref["ring"].create_image(self.px(24), self.px(24), image=ref["ring"].image)

    def _seg_parts(self, d):
        """主盘的圆环按目录分色，其它盘还是按紧张程度单色。"""
        segs = getattr(self.app, "segments", None)
        if not segs or not d.total or d.root != getattr(self.app, "main", None):
            return None
        parts = [(sg["size"] / float(d.total), sg["color"]) for sg in segs if not sg.get("free")]
        return parts or None

    def refresh_cards(self):
        for d in list(self.drives):
            refresh_usage(d)
            self._draw_card(d.root)
        self._update_summary()

    def _update_summary(self):
        d = next((x for x in self.drives if x.root == self.cur), None)
        if d:
            used = fmt_size(sum(x.used for x in self.drives))
            total = fmt_size(sum(x.total for x in self.drives))
            self.sum_label.configure(text="全部磁盘：%s 已用 / %s 总容量" % (used, total))

    # ------------------------------------------------------------ 交互
    def select_drive(self, root):
        if root == self.cur:
            return
        self.cur = root
        self.cur_path = root
        self.history = []
        for r in self._drive_refs:
            self._draw_card(r)
        self.selected.clear()
        self._update_sel()
        self._update_path()
        self.scan()

    def go_up(self):
        if not self.history:
            return
        self.cur_path = self.history.pop()
        self.selected.clear()
        self._update_sel()
        self._update_path()
        self.scan()

    def set_mode(self, mode):
        if mode == self.mode:
            return
        self.mode = mode
        self.selected.clear()
        self.clean_sel = set()
        self._update_sel()
        self._sync_seg()
        self.th_pct.configure(text="占新增" if mode == "recent" else "占本盘")
        self._switch_view()
        if mode == "clean":
            self._update_path()
            self.path_label.configure(text="清理建议")
            self.scan_clean()
        elif mode == "recent":
            # 最近新增：默认看整个当前盘
            root = "%s\\" % self.cur_path[0].upper()
            if root not in [d.root for d in self.drives]:
                root = self.cur_path
            self.cur_path = root
            self.history = []
            self.scan()
        else:
            self._update_path()
            self.scan()

    def _sync_seg(self):
        on = ("#173243", ACCENT)
        off = (CARD, MUTED)
        for lbl, key in ((self.seg_dir, "dir"), (self.seg_big, "big"),
                         (self.seg_recent, "recent"), (self.seg_clean, "clean")):
            if self.mode == key:
                lbl.configure(bg=on[0], fg=on[1])
            else:
                lbl.configure(bg=off[0], fg=off[1])

    def _update_path(self):
        if self.mode == "recent":
            self.path_label.configure(
                text="最近 %d 天新增的文件 · %s" % (self.RECENT_DAYS, self.cur_path))
            self.back_btn.configure(fg=DIM)
            return
        d = next((x for x in self.drives if self.cur_path.upper().startswith(x.letter)), None)
        self.path_label.configure(text=elide(self.cur_path, 70))
        self.back_btn.configure(fg=TEXT if self.history else DIM)

    def _wheel(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120 * 3), "units")

    # ------------------------------------------------------------ 扫描
    def scan(self):
        self.token += 1
        token = self.token
        self.cancelled = token
        self.scanning = True
        self.scanned_files = 0
        self.items = []
        self.max_size = 1
        self._clear_rows()
        self.selected.clear()
        self._update_sel()
        self._update_path()
        if self.mode == "dir":
            threading.Thread(target=self._scan_dir, args=(self.cur_path, token), daemon=True).start()
        elif self.mode == "big":
            threading.Thread(target=self._scan_big, args=(self.cur_path, token), daemon=True).start()
        else:
            threading.Thread(target=self._scan_recent, args=(self.cur_path, token), daemon=True).start()

    def _stop(self, token):
        return token != self.cancelled

    def _scan_dir(self, path, token):
        try:
            entries = []
            with os.scandir(path) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    entries.append({
                        "name": e.name,
                        "path": e.path,
                        "is_dir": e.is_dir(follow_symlinks=False),
                        "size": (None if e.is_dir(follow_symlinks=False) else st.st_size),
                        "reparse": bool(getattr(st, "st_file_attributes", 0) & SKIP_ATTR),
                    })
        except OSError as ex:
            self.q.put(("error", token, "无法读取该目录：%s" % ex))
            return
        if self._stop(token):
            return
        entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        self.q.put(("reset", token, entries))

        dirs = [x for x in entries if x["is_dir"] and not x["reparse"]]
        total = len(entries)
        done = 0
        found = sum((x["size"] or 0) for x in entries if not x["is_dir"])
        # 文件先上报
        for x in entries:
            if not x["is_dir"]:
                self.q.put(("size", token, x["path"], x["size"] or 0))
        done = len([x for x in entries if not x["is_dir"]])
        self.q.put(("prog", token, done, total, found))

        if dirs:
            with ThreadPoolExecutor(max_workers=4) as ex:
                futs = {ex.submit(dir_size, x["path"], lambda: self._stop(token)): x for x in dirs}
                for f in as_completed(futs):
                    if self._stop(token):
                        return
                    x = futs[f]
                    try:
                        size = f.result()
                    except Exception:
                        size = 0
                    done += 1
                    found += size
                    self.q.put(("size", token, x["path"], size))
                    self.q.put(("prog", token, done, total, found))
        if not self._stop(token):
            self.q.put(("done", token, found))

    def _scan_big(self, path, token):
        found = []
        count = 0
        stack = [path]
        while stack:
            if self._stop(token):
                return
            cur = stack.pop()
            try:
                with os.scandir(cur) as it:
                    for e in it:
                        count += 1
                        if count % 500 == 0:
                            self.q.put(("bigprog", token, count, found))
                        try:
                            st = e.stat(follow_symlinks=False)
                            if getattr(st, "st_file_attributes", 0) & SKIP_ATTR:
                                continue
                            if e.is_dir(follow_symlinks=False):
                                stack.append(e.path)
                            elif st.st_size >= MIN_BIG_FILE:
                                found.append({"name": e.name, "path": e.path, "is_dir": False,
                                              "size": st.st_size})
                                found.sort(key=lambda x: -x["size"])
                                del found[80:]
                        except OSError:
                            pass
            except OSError:
                pass
        if not self._stop(token):
            self.q.put(("bigdone", token, count, found))

    def _scan_recent(self, path, token):
        """扫整个盘，挑出最近 N 天新建、20MB 以上的文件，按时间从新到旧。"""
        cutoff = time.time() - self.RECENT_DAYS * 86400
        found = []
        count = 0
        last_post = 0.0
        stack = [path]
        while stack:
            if self._stop(token):
                return
            cur = stack.pop()
            try:
                with os.scandir(cur) as it:
                    for e in it:
                        count += 1
                        try:
                            st = e.stat(follow_symlinks=False)
                            if getattr(st, "st_file_attributes", 0) & SKIP_ATTR:
                                continue
                            if e.is_dir(follow_symlinks=False):
                                if e.name.lower() in self.SKIP_DIRS:
                                    continue
                                stack.append(e.path)
                            elif st.st_size >= self.MIN_RECENT and st.st_ctime >= cutoff:
                                found.append({"name": e.name, "path": e.path, "is_dir": False,
                                              "size": st.st_size, "ctime": st.st_ctime})
                                found.sort(key=lambda x: -x["ctime"])
                                del found[self.MAX_RECENT:]
                        except OSError:
                            pass
                        now = time.time()
                        if now - last_post > 2.0:
                            last_post = now
                            self.q.put(("recentprog", token, count, list(found)))
            except OSError:
                pass
        if not self._stop(token):
            self.q.put(("recentdone", token, count, found))

    def _poll_loop(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] in ("reset", "size", "prog", "done", "error", "bigprog", "bigdone",
                              "recentprog", "recentdone",
                              "deleted", "clean_size", "clean_done", "cleaned"):
                    if msg[1] != self.token:
                        continue
                    self._handle(msg)
        except queue.Empty:
            pass
        try:
            self.after(90, self._poll_loop)
        except tk.TclError:
            pass

    def _handle(self, msg):
        kind = msg[0]
        if kind == "reset":
            self._render_rows(msg[2])
            self.status.configure(text="扫描中…")
        elif kind == "size":
            path, size = msg[2], msg[3]
            for it in self.items:
                if it["path"] == path:
                    it["size"] = size
                    break
            row = self.rows.get(path)
            if row:
                row.set_max(self.max_size, self._drive_total())
        elif kind == "prog":
            done, total, found = msg[2], msg[3], msg[4]
            self.status.configure(text="扫描中 %d/%d · 已计 %s" % (done, total, fmt_size(found)))
            self.scanning = True
        elif kind == "done":
            self.scanning = False
            self._finalize(msg[2])
        elif kind == "error":
            self.scanning = False
            self.status.configure(text=msg[2], fg=RED)
        elif kind == "bigprog":
            self.scanned_files = msg[2]
            self._render_big(msg[3], scanning=True, count=msg[2])
        elif kind == "bigdone":
            self.scanning = False
            self._render_big(msg[3], scanning=False, count=msg[2])
        elif kind == "recentprog":
            self.scanned_files = msg[2]
            self._render_recent(msg[3], scanning=True, count=msg[2])
        elif kind == "recentdone":
            self.scanning = False
            self._render_recent(msg[3], scanning=False, count=msg[2])
        elif kind == "deleted":
            ok, text = msg[2], msg[3]
            if ok:
                self._after_delete(text)
            else:
                self.status.configure(text=text, fg=RED)
        elif kind == "clean_size":
            cid, size = msg[2], msg[3]
            self.clean_sizes[cid] = size
            ref = self.clean_cards.get(cid)
            if ref:
                ref["size"].configure(text=fmt_size(size), fg=TEXT if size else DIM)
            self._refresh_clean_footer()
        elif kind == "clean_done":
            total = sum(self.clean_sizes.values())
            picked = sum(self.clean_sizes.get(cid, 0) for cid in self.clean_sel)
            self.status.configure(text="统计完成 · 全部 %s，已勾选 %s" % (fmt_size(total), fmt_size(picked)),
                                  fg=MUTED)
            if os.environ.get("DISKBALL_DEMO_SCROLL") == "1":
                self.clean_canvas.yview_moveto(1.0)
        elif kind == "cleaned":
            self._after_cleaned(msg[2], msg[3], msg[4])

    def _drive_total(self):
        for d in self.drives:
            if d.root.upper().startswith(self.cur_path[0].upper()):
                return d.total
        return 0

    # ------------------------------------------------------------ 渲染
    def _clear_rows(self):
        for r in self.rows.values():
            r.destroy()
        self.rows = {}
        for child in self.inner.winfo_children():
            child.destroy()
        self.count_cache = 0

    def _render_rows(self, items):
        self._clear_rows()
        self.items = items
        self.max_size = max([x["size"] or 0 for x in items] + [1]) if items else 1
        total = self._drive_total()
        if not items:
            self._empty("这个目录是空的，或者没有权限读取。")
            return
        for it in items:
            self._add_row(it, total)
        self._update_summary_line()

    def _add_row(self, it, total):
        row = Row(self.inner, self, it, self.max_size, total,
                  on_toggle=self._toggle, on_open=self._open_row, on_menu=self._row_menu)
        self.rows[it["path"]] = row
        row.set_max(self.max_size, total)
        if it["size"] is None:
            row.size.configure(text="统计中…", fg=DIM)

    def _render_big(self, found, scanning, count):
        self._clear_rows()
        self.items = found
        total = self._drive_total()
        self.max_size = max([x["size"] for x in found] + [1])
        if not found:
            self._empty("还没发现大于 %s 的文件（已扫描 %s 个文件）" %
                        (fmt_size(MIN_BIG_FILE), "{:,}".format(count)))
        else:
            for it in found:
                self._add_row(it, total)
            self._update_summary_line()
        self.status.configure(
            text=("扫描中… 已扫 %s 个文件" % "{:,}".format(count)) if scanning
            else "共 %d 个大于 %s 的文件" % (len(found), fmt_size(MIN_BIG_FILE)),
            fg=ACCENT if scanning else MUTED)

    def _render_recent(self, found, scanning, count):
        self._clear_rows()
        shown = found[:60] if scanning else found      # 扫描中只画前 60 条，避免一直重建
        for it in shown:
            it["note"] = time.strftime("%m-%d %H:%M", time.localtime(it.get("ctime") or 0))
        self.items = found
        total = sum(x["size"] for x in found) or 1      # 占比按“本批新增”算
        self.max_size = max([x["size"] for x in found] + [1])
        if not shown:
            self._empty("还没发现最近 %d 天新增的文件（已扫描 %s 个）" %
                        (self.RECENT_DAYS, "{:,}".format(count)))
        else:
            for it in shown:
                self._add_row(it, total)
            self._update_summary_line()
        self.status.configure(
            text=("扫描中… 已扫 %s 个文件" % "{:,}".format(count)) if scanning
            else "最近 %d 天新增 %d 个（≥%s）" % (self.RECENT_DAYS, len(found), fmt_size(self.MIN_RECENT)),
            fg=ACCENT if scanning else MUTED)

    def _empty(self, text):
        tk.Label(self.inner, text=text, bg=BG, fg=DIM,
                 font=("Microsoft YaHei UI", -self.px(12))).pack(pady=self.px(40))

    def _update_summary_line(self):
        total = self._drive_total()
        sizes = [x for x in self.items if x.get("size")]
        used_here = sum(x["size"] for x in sizes)
        if self.mode == "dir":
            prefix = "本目录合计 %s" % fmt_size(used_here)
        elif self.mode == "recent":
            prefix = "最近 %d 天新增合计 %s" % (self.RECENT_DAYS, fmt_size(used_here))
        else:
            prefix = "大于 %s 的文件合计 %s" % (fmt_size(MIN_BIG_FILE), fmt_size(used_here))
        extra = ""
        if sizes:
            sizes.sort(key=lambda x: -x["size"])
            top3 = sum(x["size"] for x in sizes[:3])
            share3 = (top3 / float(used_here) * 100) if used_here else 0
            top = sizes[0]
            extra = " · 前 3 项占 %.0f%% · 大头：%s %s" % (
                share3, elide(top["name"], 14), fmt_size(top["size"]))
        self.status.configure(text="%d 项 · %s%s" % (len(self.items), prefix, extra), fg=MUTED)

    def _finalize(self, total_bytes):
        sizes = [(x, x.get("size") or 0) for x in self.items]
        sizes.sort(key=lambda t: -t[1])
        self.max_size = max([s for _, s in sizes] + [1])
        total = self._drive_total()
        # 排序后重排
        for _x, _s in sizes:
            r = self.rows.get(_x["path"])
            if r:
                r.frame.pack_forget()
        for x, _s in sizes:
            r = self.rows.get(x["path"])
            if r:
                r.set_max(self.max_size, total)
                r.frame.pack(fill="x")
        self._update_summary_line()
        self.canvas.yview_moveto(0)
        if os.environ.get("DISKBALL_DEMO_SELECT") == "1" and not getattr(self, "_demo_sel", False):
            self._demo_sel = True
            for x, _s in sizes[:2]:
                r = self.rows.get(x["path"])
                if r:
                    self._toggle(r)

    def _toggle(self, row):
        p = row.path
        if p in self.selected:
            self.selected.discard(p)
            row.set_selected(False)
        else:
            self.selected.add(p)
            row.set_selected(True)
        self._update_sel()

    def _update_sel(self):
        n = len(self.selected)
        if not n:
            self.sel_label.configure(text="未选择任何项", fg=MUTED)
            self.del_btn.configure(state="disabled", fg=DIM, bg=CARD)
        else:
            total = sum((it.get("size") or 0) for it in self.items if it["path"] in self.selected)
            self.sel_label.configure(text="已选 %d 项 · %s" % (n, fmt_size(total)), fg=TEXT)
            self.del_btn.configure(state="normal", fg="#ff9a9a", bg="#331419")

    def _open_row(self, row):
        it = row.item
        if it["is_dir"]:
            self.history.append(self.cur_path)
            self.cur_path = it["path"]
            self.selected.clear()
            self._update_sel()
            self._update_path()
            self.scan()
        else:
            self.reveal(it["path"])

    def _row_menu(self, row, e):
        m = tk.Menu(self, tearoff=0, bg=CARD, fg=TEXT, activebackground=CARD_HL,
                    activeforeground=TEXT, bd=0)
        it = row.item
        if it["is_dir"]:
            m.add_command(label="  打开文件夹      ", command=lambda: self._open_row(row))
        m.add_command(label="  在资源管理器中显示  ", command=lambda: self.reveal(it["path"]))
        m.add_command(label="  复制路径            ",
                      command=lambda: self._copy(it["path"]))
        m.add_separator()
        m.add_command(label="  移到回收站          ", command=lambda: self._delete_one(it))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _copy(self, path):
        try:
            self.clipboard_clear()
            self.clipboard_append(path)
        except Exception:
            pass

    def reveal(self, path):
        try:
            if os.path.isdir(path):
                os.startfile(path)
            else:
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        except Exception as ex:
            self.status.configure(text="打不开：%s" % ex, fg=RED)

    def open_current(self):
        try:
            os.startfile(self.cur_path)
        except Exception as ex:
            self.status.configure(text="打不开：%s" % ex, fg=RED)

    # ------------------------------------------------------------ 删除
    def _delete_one(self, it):
        p = it["path"]
        prot, why = is_protected_path(p)
        if prot:
            Confirm(self, "不能删除", [why, "已跳过：%s" % elide(p, 46)], "知道了", False)
            return
        dlg = Confirm(self, "移到回收站？",
                      ["文件：%s" % elide(it["name"], 40),
                       "大小：%s" % fmt_size(it.get("size") or 0),
                       "文件会进入回收站，需要时可以还原。"],
                      "移到回收站")
        self.wait_window(dlg)
        if not dlg.result:
            return
        threading.Thread(target=self._do_delete, args=([p],), daemon=True).start()

    def delete_selected(self):
        if not self.selected:
            return
        paths = list(self.selected)
        items = [it for it in self.items if it["path"] in self.selected]
        blocked = [(p, is_protected_path(p)) for p in paths if is_protected_path(p)[0]]
        if blocked:
            lines = ["以下位置受保护，已跳过："] + [elide(p, 44) for p, _ in blocked[:3]]
            Confirm(self, "部分位置不能删除", lines, "知道了", False)
            return
        total = sum((it.get("size") or 0) for it in items)
        names = [elide(it["name"], 30) for it in items[:4]]
        if len(items) > 4:
            names.append("… 共 %d 项" % len(items))
        dlg = Confirm(self, "确认移到回收站？",
                      ["将删除 %d 项，合计 %s" % (len(items), fmt_size(total)),
                       "、".join(names),
                       "删除后会进入回收站，可以随时还原。"],
                      "移到回收站")
        self.wait_window(dlg)
        if not dlg.result:
            return
        threading.Thread(target=self._do_delete, args=(paths,), daemon=True).start()

    def _do_delete(self, paths):
        audit_write("回收站", " | ".join(paths))
        ok, msg = recycle_paths(paths)
        self.q.put(("deleted", self.token, ok, msg))

    def _after_delete(self, msg):
        self.selected.clear()
        self._update_sel()
        self.status.configure(text=msg, fg=GREEN)
        self.app.refresh_drives()
        self.app.invalidate_structure(1500)
        self.refresh_cards()
        self.scan()

    # ------------------------------------------------------------ 清理执行
    def clean_selected(self):
        if not self.clean_sel:
            return
        selected = [c for c in self.clean_cands if c["id"] in self.clean_sel]
        total = sum(self.clean_sizes.get(c["id"], 0) for c in selected)
        lines = ["将直接删除：%s" % "、".join(c["title"] for c in selected),
                 "合计 %s（不进回收站，删了才能真腾出空间）" % fmt_size(total)]
        if any(c["risk"] == "irreversible" for c in selected):
            lines.append("⚠ 其中包含回收站，清空后无法恢复。")
        if any(c["risk"] == "admin" for c in selected):
            lines.append("部分项需要管理员权限，没权限的文件会自动跳过。")
        dlg = Confirm(self, "确认清理？", lines, "开始清理")
        self.wait_window(dlg)
        if not dlg.result:
            return
        threading.Thread(target=self._do_clean, args=(selected,), daemon=True).start()

    def _do_clean(self, selected):
        try:
            before = shutil.disk_usage(self.cur).free
        except OSError:
            before = 0
        removed = skipped = 0
        for c in selected:
            try:
                if c["kind"] == "recycle":
                    audit_write("清空回收站", "全部")
                    empty_recycle_bin()
                else:
                    audit_write("清理", "%s\t%s" % (c["id"], " | ".join(c["paths"])))
                    r, s = purge_paths(c["paths"])
                    removed += r
                    skipped += s
            except Exception:
                skipped += 1
        try:
            after = shutil.disk_usage(self.cur).free
        except OSError:
            after = before
        self.q.put(("cleaned", self.token, max(0, after - before), removed, skipped))

    def _after_cleaned(self, freed, removed, skipped):
        self.app.refresh_drives()
        self.app.invalidate_structure(1500)
        self.refresh_cards()
        self.scan_clean()
        self.status.configure(
            text="清理完成 · 释放 %s（删除 %d 项，跳过 %d 个占用中）" % (fmt_size(freed), removed, skipped),
            fg=GREEN)

    # ------------------------------------------------------------ 关闭
    def on_close(self):
        self.token += 1
        self.cancelled = self.token
        try:
            self.destroy()
        except Exception:
            pass
        if self.app.detail is self:
            self.app.detail = None
        self.app.dump_rects()


# ---------------------------------------------------------------- 入口
def main():
    enable_dpi()

    errpath = os.environ.get("DISKBALL_ERRFILE")
    if errpath:
        try:
            sys.stderr = open(errpath, "w", buffering=1, encoding="utf-8")
            sys.stdout = sys.stderr
        except Exception:
            pass

    if not acquire_single_instance():
        sys.exit(0)
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

    app = BallApp()
    if os.environ.get("DISKBALL_DEMO") == "1":
        app.after(700, app.open_detail)
    try:
        app.mainloop()
    except Exception:
        import traceback
        try:
            os.makedirs(CFG_DIR, exist_ok=True)
            with open(ERROR_LOG, "a", encoding="utf-8") as f:
                f.write("\n===== %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
                traceback.print_exc(file=f)
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
