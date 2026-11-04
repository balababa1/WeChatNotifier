# 微信消息通知器 WeChatNotifier

> 窗口置顶 Toast 弹窗，实时捕获微信消息通知，无需手机，不依赖微信官方 API。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-lightgrey.svg)]()
[![Version](https://img.shields.io/badge/Version-v0.2.7-22d3ee.svg)]()

## 这是什么？

一个 Windows 桌面工具，通过 **UI Automation（UIA）** 实时读取微信电脑版会话列表，当有人发来新消息时，在屏幕右下角弹出半透明 Toast 通知 —— 类似 macOS 的通知中心体验。

**微信不用置顶，最小化到任务栏即可正常工作。**

**如果开启讲述人重启微信依旧不可用则无法使用**
**经测试微信是否走MMUI框架为绑定账号的灰度测试**
**作者自己的微信可在朋友电脑上登录使用 但朋友的微信经过无数尝试皆不可用**

---

## 测试效果
<img width="415" height="166" alt="微信图片_2026-08-01_125945_170" src="https://github.com/user-attachments/assets/c6d28c9a-3fd7-4784-955c-bc355155047c" />
<img width="427" height="196" alt="微信图片_2026-08-01_125940_505" src="https://github.com/user-attachments/assets/79a8114d-0056-4ffe-9461-fa49edbacb2b" />
<img width="466" height="278" alt="微信图片_2026-08-01_125958_892" src="https://github.com/user-attachments/assets/eb46376d-53f1-45db-b28f-52c3a0b5b03b" />

---

## 功能特性

| 特性 | 说明 |
|------|------|
| Toast 弹窗 | PIL 超采样渲染 + Win32 分层窗口，半透明圆角卡片，显示联系人 + 未读数 + 消息预览 |
| 红包提醒（v0.2.7 新增） | 独立红包样式弹窗（金色封口条 + 「開」钮）并播放提示音，不受未读数、免打扰、时间窗限制 |
| 免打扰群聊（v0.2.7 新增） | 普通消息在免打扰会话中静默，红包照常提醒；会话名自动剔除「消息免打扰」标签 |
| 文件事件唤醒（v0.2.7） | 监听微信 db_storage 的 session / message 目录变化触发扫描，替代 0.1 秒空转轮询 |
| Emoji 混排 | 按 emoji / 文字分段度量绘制，Segoe UI Emoji 与雅黑混排，超宽自动省略 |
| 点击跳转 | 点击弹窗自动激活微信窗口（模拟快捷键） |
| 零依赖微信 API | 不调用微信任何官方接口，不注入微信进程，纯 UIA 树读取 |
| 后台常驻 | `--background` 模式彻底脱离控制台运行，托盘无图标（极简设计） |
| 快捷键唤醒 | 支持 Alt+X / Ctrl+Alt+W / Alt+W 三种预设，可关闭 |
| 开机自启 | VBS + 计划任务实现，登录时无 CMD 窗口闪现 |
| COM 容灾 | 异常检测后非阻塞退避 30 秒重置 CoInitialize，界面不卡死 |
| 黑名单过滤 | 自动忽略服务号、公众号、文件传输助手、折叠的聊天、服务通知、微信团队等系统账号 |
| 异常可见化 | 异常统一写入 `data/error.log`（含 traceback，512KB 轮转），不再静默吞错 |
| 诊断日志分层 | `DEBUG_LOG = False`，正式版诊断日志零落盘，debug.log 仅保留弹窗记录 |

---

## 红包提醒与免打扰规则

红包通道与普通消息通道**完全解耦**，判定依据是会话列表消息中的类型标签 `[微信红包]`：

- 私聊：`[微信红包] 恭喜发财，大吉大利`
- 群聊：`发送者昵称: [微信红包] ...`
- 领取提示（如「XX领取了你的红包」）不含该标签，不会误弹

| 场景 | 普通消息 | 红包 |
|------|----------|------|
| 免打扰群聊 | 静默，不弹窗 | **照常弹红包窗 + 提示音** |
| 未读数为 1 | 按抑制规则处理 | **弹窗**（不受未读数限制） |
| 深夜 / 任意时段 | 不做时间抑制 | 不做时间抑制 |
| 同一句「恭喜发财，大吉大利」连发 | 按未读变化判定 | 靠未读数增加识别，同文案也不漏 |

提示音读取 `data/notify.wav`（源码运行读脚本同级 `data/`，打包后读 `_internal/data/`）。找不到文件时弹窗照常出现、仅静音，不报错。

---

## 工作原理

```
  _FileWatcher（ReadDirectoryChangesW）
        │  监听 db_storage\session、db_storage\message
        ▼
  事件去抖 0.3s → 跟进扫描 3 轮
        │
        ▼
  read_sessions() ── UIA 树解析 ──► XTableView(Name="会话") → ChatSessionCell
        │
        ▼
  _is_new_msg() 对比 chat-records.json 的未读数判定是否新消息
        │
        ├── 含 [微信红包] ──► _show_redpacket_toast() + 提示音
        │
        └── 普通消息 ──► 免打扰静默 → 未读校验 → _show_toast()
        │
        ▼
  用户点击 → 模拟快捷键激活微信
```

**核心技术栈**：`uiautomation` + `pywin32` + `tkinter`（窗口与事件循环）+ `Pillow`（渲染）

**UIA 解析策略**：通过 `XTableView(Name="会话")` 控件读取 `ChatSessionCell` 子元素，兼容 `mmui::MainWindow`（标准版）和 `Qt*QWindowIcon`（Qt 渲染版）。首次深搜成功后缓存控件路径，后续走快路径；快路径异常先原地重试 1 次，仍失败才丢弃缓存重新深搜（一次深搜约 2 秒，成本是命中路径的 600 倍）。

**扫描节拍**：

| 常量 | 取值 | 作用 |
|------|------|------|
| `DEBOUNCE_SEC` | 0.3 | 事件合并窗口 |
| `BURST_SCANS` / `BURST_GAP_MS` | 3 / 300 | 唤醒后跟进扫描，等微信把库变化渲染进 UIA 树 |
| `FALLBACK_SCAN_SEC` | 120 | 无事件也强制扫一次，防漏 |
| `IDLE_TICK_MS` | 100 | 空闲只查内存标志，不碰 UIA |
| `NO_WECHAT_GAP_MS` | 2000 | 微信窗口不存在时的探测间隔 |

---

## 环境要求

- **操作系统**：Windows 10 / 11（64 位）
- **微信**：Windows 桌面版（4.1.x 测试通过）
- **辅助功能**：必须启用 Windows 讲述人（一次性操作，见下方说明）

---

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/balababa1/WeChatNotifier.git
cd WeChatNotifier
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 启用 Windows 讲述人（仅需一次）

> 这是 UIA 技术的前提条件 —— Windows 的 UI Automation 框架需要辅助功能处于激活状态才能读取第三方应用的 UI 树。

1. 彻底退出微信
2. 点击「开始」菜单 → 搜索「讲述人」→ 点击启动
3. 重新启动并登录微信
4. 登录完成后可关闭讲述人（UIA 状态会保持）

**此操作仅需执行一次，重启电脑后无需重复。**

### 4. 运行

```bash
python wechat_notifier.py
```

程序启动后：
- CMD 控制台显示主菜单，可配置快捷键和开机自启
- 后台进程自动启动，开始监听微信消息
- 关闭控制台窗口不影响后台监听

**调试弹窗效果**：`python wechat_notifier.py --demo-toast` 直接弹出一个演示弹窗，不必等真实消息。

---

## 控制面板

```
  =====================================================
          微信消息通知器  v0.2.7
                 作者: 不理笨笨
  =====================================================
   微信保持登录即可，可最小化，请勿退出微信
   关闭本窗口不影响后台监听

   当前唤醒键: Ctrl+Alt+W
   [1] Alt+X   [2] Ctrl+Alt+W   [3] Alt+W   [4] 关闭
   [5] 开机自启: 已关闭
   [6] 退出通知器
  =====================================================
```

| 选项 | 功能 |
|------|------|
| 1-3 | 切换唤醒微信的快捷键 |
| 4 | 关闭快捷键（点击弹窗不再激活微信） |
| 5 | 切换开机自启（通过 Windows 计划任务，无 CMD 闪现） |
| 6 | 彻底退出后台监听进程 |

---

## 数据文件

所有运行时文件都在 `data/` 目录（不纳入版本控制）：

| 文件 | 用途 |
|------|------|
| `settings.json` | 快捷键、开机自启、退出请求等配置 |
| `chat-records.json` | 会话缓存，用于未读数对比判定新消息 |
| `bg_pid.txt` | 后台进程 PID，用于单实例与残留清理 |
| `debug.log` | 仅记录弹窗事件；超过 1MB 轮转为 `debug.log.1` |
| `error.log` | 异常记录（时间、位置、traceback）；超过 512KB 轮转 |
| `notify.wav` | 红包提示音（随仓库提供） |

---

## 从源码构建 EXE

推荐直接用仓库内的 spec 文件：

```bash
python -m PyInstaller --noconfirm --clean WeChatNotifier_v0.2.7.spec
```

产物位于 `dist\WeChatNotifier_v0.2.7\`。

**打包注意事项**：
- 必须使用 `--console` 子系统（程序依赖 `GetConsoleWindow()` 实现控制台隐身）
- 推荐 `--onedir` 模式（免解压，启动更快）
- spec 已声明打包 `data/notify.wav` 与 `data/wechat_new.ico`，缺图标或音频会静音或使用默认图标
- spec 已排除 `PIL.ImageQt`、`PIL.ImageTk`、`PIL.ImageGrab`、`PIL._avif`、`PIL._webp`、`PIL.ImageSequence`、`PIL.PdfParser` 等冗余模块

---

## 已知限制

| 限制 | 说明 |
|------|------|
| 微信不能缩到系统托盘 | Qt 框架在微信缩到托盘时会完全剪枝 UIA 子控件树（`GetChildren()` 返回 0），导致无法读取会话 |
| 正常最小化不影响 | 最小化到任务栏（`IsIconic=1, IsWindowVisible=1`）时 UIA 树保持完整 |
| COM RPC 偶尔僵死 | 极少数情况下 UIA COM 调用会陷入长阻塞，现以非阻塞退避 30 秒重置，退避期间事件仍被记录 |
| 事件唤醒依赖 db 落盘 | 微信把消息写库后才触发唤醒，比纯轮询多一个落盘延迟；文字消息实测无明显差异 |
| 仅支持 Windows | 依赖 COM / UIA / Win32 分层窗口 |
| 仅测试微信 4.1.x | UI 结构随微信版本变化，新版可能需调整 UIA 解析逻辑 |
| 红包识别依赖标签 | 若微信改掉 `[微信红包]` 类型标签，需同步调整 `_is_redpacket()` |

---

## 常见问题

**Q：红包弹窗出现了但没有声音？**
检查 `data/notify.wav` 是否存在。源码运行时需与脚本同级，打包后需在 `_internal/data/` 下。缺文件只静音，不报错。

**Q：免打扰群里为什么普通消息不弹、红包却弹？**
这是设计行为：普通通道遇到免打扰会话直接跳过，红包通道独立且不受免打扰限制。

**Q：为什么 `data/debug.log` 没生成？**
正式版 `DEBUG_LOG = False`，诊断日志不落盘；只有真实弹窗时才写一行记录。排查问题请看 `data/error.log`。

**Q：关掉 CMD 窗口后还能收到通知吗？**
可以。后台进程已通过 `FreeConsole()` 彻底脱离控制台，关闭窗口不会终止监听。

---

## 免责声明

本工具（WeChatNotifier）是一个**独立的第三方开源项目**，与腾讯、微信及其关联公司**没有任何关系**，也未获得其授权或认可。

- 本工具通过 Windows 公开的 UI Automation 框架**只读**访问微信的界面元素，不修改、不注入、不拦截微信进程的任何数据或行为。
- 使用本工具不会修改微信客户端、不会绕过任何安全机制、不会收集或上传任何用户数据到第三方服务器。
- 本工具的所有数据处理均在用户本地计算机完成（`data/` 目录下的 JSON 配置文件）。
- 使用本工具可能违反微信的《软件许可及服务协议》，请用户在充分了解风险后自行决定是否使用。
- 作者对因使用本工具导致的任何问题（包括但不限于账号封禁、数据丢失、软件异常）**不承担任何责任**。

---

## 开源许可

MIT License - 详见 [LICENSE](LICENSE)

---

## 致谢

- [uiautomation](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows) - Windows UIA Python 绑定
- [Pillow](https://python-pillow.org/) - 弹窗底图与文字层渲染
- [pywin32](https://github.com/mhammond/pywin32) - Win32 分层窗口与进程管理
