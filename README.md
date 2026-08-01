# 微信消息通知器 WeChatNotifier

> 窗口置顶 Toast 弹窗，实时捕获微信消息通知，无需手机，不依赖微信官方 API。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-lightgrey.svg)]()

## 这是什么？

一个 Windows 桌面工具，通过 **UI Automation（UIA）** 实时读取微信电脑版会话列表，当有人发来新消息时，在屏幕右下角弹出半透明 Toast 通知 —— 类似 macOS 的通知中心体验。

**微信不用置顶，最小化到任务栏即可正常工作。**

**如果开启讲述人重启微信依旧不可用则无法使用**  
**经测试微信是否走MMUI框架为绑定账号的灰度测试**  
**作者自己的微信可在朋友电脑上登录使用 但朋友的微信经过无数尝试皆不可用**  

---

## 功能特性

| 特性 | 说明 |
|------|------|
| Toast 弹窗 | 基于 tkinter 自绘的半透明圆角通知卡片，显示联系人 + 未读数 + 消息预览 |
| 点击跳转 | 点击弹窗自动激活微信窗口（模拟快捷键） |
| 零依赖微信 API | 不调用微信任何官方接口，不注入微信进程，纯 UIA 树读取 |
| 后台常驻 | `--background` 模式最小化 CMD 窗口后隐形运行，托盘无图标（极简设计） |
| 快捷键唤醒 | 支持 Alt+X / Ctrl+Alt+W / Alt+W 三种预设，可关闭 |
| 开机自启 | VBS + 计划任务实现，登录时无 CMD 窗口闪现 |
| COM 容灾 | 自动检测 COM/RPC 断连，触发 CoInitialize 重置恢复 |
| 黑名单过滤 | 自动忽略服务号、公众号、文件传输助手、微信团队等系统账号 |
| 日志去重 | 重复消息智能压制（COM/UIA 错误消息例外，确保调试透明） |

---

## 工作原理

```
  read_sessions() ── UIA 树解析 ──► XTableView(Name="会话") → ChatSessionCell
        │
        ▼
  对比 chat-records.json 去重
        │
        ▼
  _show_toast() ── tkinter Toplevel 弹窗
        │
        ▼
  用户点击 → 模拟快捷键激活微信
```

**核心技术栈**：`uiautomation` + `pywin32` + `tkinter`

**UIA 解析策略**：通过 `XTableView(Name="会话")` 控件读取 `ChatSessionCell` 子元素，兼容 `mmui::MainWindow`（标准版）和 `Qt*QWindowIcon`（Qt 渲染版）。初次搜索未命中时自动触发深度重搜。

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

---

## 控制面板

```
  =====================================================
             微信消息通知器 v0.2.4  控制台版
                 
  =====================================================

  [1] Alt+X    [2] Ctrl+Alt+W   [3] Alt+W   [4] 关闭快捷键
  [5] 开机自启
  [6] 彻底退出通知器
```

| 选项 | 功能 |
|------|------|
| 1-3 | 切换唤醒微信的快捷键 |
| 4 | 关闭快捷键（点击弹窗不再激活微信） |
| 5 | 切换开机自启（通过 Windows 计划任务，无 CMD 闪现） |
| 6 | 彻底退出后台监听进程 |

---

## 从源码构建 EXE

如果你想打包成独立可执行文件（无需安装 Python）：

```bash
pyinstaller --onedir --console \
    --icon data\wechat_new.ico \
    --exclude-module PIL._avif \
    --exclude-module PIL._webp \
    --exclude-module PIL._imagingcms \
    --exclude-module win32ui \
    --exclude-module lzma \
    --name WeChatNotifier_v0.2.4 \
    wechat_notifier.py
```

**打包注意事项**：
- 必须使用 `--console` 子系统（程序依赖 `GetConsoleWindow()` 实现 CMD 隐身）
- 推荐 `--onedir` 模式（免解压，启动更快）
- 排除 5 个冗余模块可减少安装包约 9.4MB

---

## 已知限制

| 限制 | 说明 |
|------|------|
| 微信不能缩到系统托盘 | Qt 框架在微信缩到托盘时会完全剪枝 UIA 子控件树（`GetChildren()` 返回 0），导致无法读取会话 |
| 正常最小化不影响 | 最小化到任务栏（`IsIconic=1, IsWindowVisible=1`）时 UIA 树保持完整 |
| COM RPC 偶尔僵死 | 极少数情况下 UIA COM 调用会陷入 60 秒阻塞，框架已内置自动重置机制 |
| 仅支持 Windows | 依赖 COM / UIA / Win32 API |
| 仅测试微信 4.1.x | UI 结构随微信版本变化，新版可能需调整 UIA 解析逻辑 |

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
- [pystray](https://github.com/moses-palmer/pystray) - 系统托盘支持
- [Pillow](https://python-pillow.org/) - 图像处理
