# 微信消息通知器 WeChatNotifier

> 窗口置顶 Toast 弹窗，实时捕获微信消息通知，无需手机，不依赖微信官方 API。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-lightgrey.svg)]()
[![Version](https://img.shields.io/github/v/release/balababa1/WeChatNotifier.svg)]()

## 这是什么？

一个 Windows 桌面工具，通过 **UI Automation（UIA）** 实时读取微信电脑版会话列表，当有人发来新消息时，在屏幕右下角弹出半透明 Toast 通知 —— 类似 macOS 的通知中心体验。

**微信不用置顶，最小化到任务栏即可正常工作。**

读取会话列表依赖微信侧 Qt 无障碍开关。程序启动抓到微信进程后会先判定该开关，未开启时自动开启并回读确认，通常无需手动操作。

**若微信未向 UIA 暴露控件树，仍可能无法读取会话**

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
| 红包提醒 | 独立红包样式弹窗并播放提示音，不受未读数、免打扰、时间窗限制 |
| 免打扰群聊 | 普通消息在免打扰会话中静默，红包照常提醒；会话名自动剔除「消息免打扰」标签 |
| 文件事件唤醒 | 监听微信 db_storage 的 session / message 目录变化触发扫描 |
| Emoji 混排 | 按 emoji / 文字分段度量绘制，Segoe UI Emoji 与雅黑混排，超宽自动省略 |
| 点击跳转 | 点击弹窗自动激活微信窗口（模拟快捷键） |
| 零 API 依赖 | 不调用微信任何官方接口，不做代码注入，不挂钩通信，纯 UIA 树读取 |
| 控件树自愈 | 启动抓到微信进程后判定 Qt 无障碍开关，未开启自动开启并回读校验；只在启动与换进程时判定一次，失败按冷却重试 |
| 记录原子落盘 | 会话缓存 5 秒静默窗口合并写入，先写临时文件再原子替换，退出前强制落盘 |
| 只读诊断 | `--gate-status` 打印开关判定的完整过程，只读不写，便于反馈问题 |
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
  启动 → 捕获 weixin.exe → 判定 Qt 无障碍开关（为 1 跳过 / 不为 1 写入并回读）
        │
        ▼
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

**扫描节拍**：

| 常量 | 取值 | 作用 |
|------|------|------|
| `DEBOUNCE_SEC` | 0.3 | 事件合并窗口 |
| `BURST_SCANS` / `BURST_GAP_MS` | 3 / 300 | 唤醒后跟进扫描，等微信把库变化渲染进 UIA 树 |
| `FALLBACK_SCAN_SEC` | 120 | 无事件也强制扫一次，防漏 |
| `IDLE_TICK_MS` | 100 | 空闲只查内存标志，不碰 UIA |
| `NO_WECHAT_GAP_MS` | 2000 | 微信窗口不存在时的探测间隔 |

---

## Qt 无障碍开关的实现方式

`wechat_uia.py` 只做一件事：定位微信 4.x 里 `QAccessible` 的 active byte，先读它的值。

- 值为 1：该字节已激活，跳过写入，直接进入抓取流程
- 值不为 1（0 或读不到）：写入 1 并回读确认，再进入抓取流程

不改启动参数、不注入代码、不设钩子、不挂 DLL；只改运行中进程里的 1 个字节，微信重启后进程内存清零，下次启动会重新判定一次（幂等）。

开关字节地址按 Weixin.dll 版本收录，未收录的版本走特征扫描；进程特征与扫描结果缓存在 `~/.wechatauto/gate_cache.json`，换版本（目录名 / 大小 / mtime 变化）自动失效。

---

## 环境要求

- **操作系统**：Windows 10 / 11（64 位）
- **微信**：Windows 桌面版（4.1.x / 4.2.x 测试通过）

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

### 3. 辅助功能开关

> 读取微信控件树依赖微信侧 Qt 无障碍开关。程序启动抓到微信进程后会自动判定该开关，未开启时写入并回读确认。

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
          微信消息通知器  v0.2.10
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
| `chat-records.json` | 会话缓存，用于未读数对比判定新消息；5 秒静默窗口合并写入，临时文件原子替换 |
| `bg_pid.txt` | 后台进程 PID，用于单实例与残留清理 |
| `debug.log` | 仅记录弹窗事件；超过 1MB 轮转为 `debug.log.1` |
| `error.log` | 异常记录（时间、位置、traceback）；超过 512KB 轮转 |
| `notify.wav` | 红包提示音（随仓库提供） |

---

## 从源码构建 EXE

推荐直接用仓库内的 spec 文件：

```bash
python -m PyInstaller --noconfirm --clean WeChatNotifier_v0.2.10.spec
```

产物位于 `dist\WeChatNotifier_v0.2.10\`。

**打包注意事项**：
- 必须使用 `--console` 子系统（程序依赖 `GetConsoleWindow()` 实现控制台隐身）
- 推荐 `--onedir` 模式（免解压，启动更快）
- spec 已声明打包 `data/notify.wav` 与 `data/wechat_new.ico`，缺图标或音频会静音或使用默认图标
- spec 已排除 `PIL.ImageQt`、`PIL.ImageTk`、`PIL.ImageGrab`、`PIL._avif`、`PIL._webp`、`PIL.ImageSequence`、`PIL.PdfParser` 等冗余模块

---

## 已知限制

| 限制 | 说明 |
|------|------|
| 微信不能缩到系统托盘 | Qt 框架在微信缩到托盘时会剪枝 UIA 树，主窗口从根节点消失，`GetChildren()` 取不到，导致无法读取会话 |
| 正常最小化不影响 | 最小化到任务栏（`IsIconic=1, IsWindowVisible=1`）时 UIA 树保持完整 |
| 仅支持 Windows | 依赖 COM / UIA / Win32 分层窗口 |
| 仅测试微信 4.1.x / 4.2.x | UI 结构随微信版本变化，新版可能需调整 UIA 解析逻辑 |
| 开关定位依赖版本 | 开关字节地址按 Weixin.dll 版本收录，未收录的版本走特征扫描 |

---

## 常见问题

**Q：红包弹窗出现了但没有声音？**
检查 `data/notify.wav` 是否存在。源码运行时需与脚本同级，打包后需在 `_internal/data/` 下。缺文件只静音，不报错。

**Q：免打扰群里为什么普通消息不弹、红包却弹？**
这是设计行为：普通通道遇到免打扰会话直接跳过，红包通道独立且不受免打扰限制。

**Q：抓不到会话怎么排查？**
在程序目录执行 `WeChatNotifier --gate-status`（实际文件名以发布包为准），把输出贴到 issue。该参数只读，不会改动微信进程任何数据。

**Q：为什么 `data/debug.log` 没生成？**
正式版 `DEBUG_LOG = False`，诊断日志不落盘；只有真实弹窗时才写一行记录。排查问题请看 `data/error.log`。

**Q：关掉 CMD 窗口后还能收到通知吗？**
可以。后台进程已通过 `FreeConsole()` 彻底脱离控制台，关闭窗口不会终止监听。

---

## 免责声明

本工具（WeChatNotifier）是一个**独立的第三方开源项目**，与腾讯、微信及其关联公司**没有任何关系**，也未获得其授权或认可。

- 本工具通过 Windows 公开的 UI Automation 框架访问微信的界面元素，不注入代码、不拦截或改写任何消息内容与网络通信。
- 为让微信向 UIA 暴露控件树，程序会在必要时把微信 Qt 无障碍开关对应的**单个标志字节**置为开启（先读后写、回读校验，已是开启状态则不写）。除此之外不修改微信进程的任何数据。该操作不等同于修改微信客户端文件，但请你知悉后再决定是否使用。
- 使用本工具不会修改微信客户端、不会绕过任何安全机制、不会收集或上传任何用户数据到第三方服务器。
- 本工具的所有数据处理均在用户本地计算机完成（`data/` 目录下的 JSON 配置文件）。
- 使用本工具可能违反微信的《软件许可及服务协议》，请用户在充分了解风险后自行决定是否使用。
- 作者对因使用本工具导致的任何问题（包括但不限于账号封禁、数据丢失、软件异常）**不承担任何责任**。

---

## 开源许可

MIT License - 详见 [LICENSE](LICENSE)

---

