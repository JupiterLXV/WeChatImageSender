# 微信图片顺序发送助手 v3.0.0（零内存补丁 · 原图句柄加速版）

Windows 桌面程序：选择图片文件夹和好友／群聊，按 **图片 → 确认上传完成 → 图片名称** 逐张顺序发送。

## v3.0.0 核心架构升级说明（相比 2.0.x 的四大改进）

1. **彻底移除 `Weixin.dll` 内存补丁，跨版本通用兼容**：
   - 2.0.x 版本依赖校验 `Weixin.dll` 的 SHA-256 并强行修改 1 个内存字节来开启 Qt 辅助功能，微信一旦小版本升级即失效，且存在崩溃风险。
   - **3.0.0 新方案**：采用 Windows 官方标准无障碍唤醒协议（`SystemParametersInfoW(SPI_SETSCREENREADER)` + `WM_GETOBJECT` 信号 + `QT_ACCESSIBILITY=1` 环境变量），**零内存修改**即可唤醒微信 3.9.x 与 4.1.x+ 的无障碍控件树，不再锁死单一小版本号。
2. **新增 `CF_HDROP` 原文件剪贴板引擎（默认推荐）+ `CF_DIB` 位图双模切换**：
   - 2.0.x 仅支持将图片解码为静态位图（`CF_DIB`）粘贴，耗时耗内存且触发微信二次重压缩。
   - **3.0.0 新方案**：默认采用与 Windows 资源管理器“复制文件”完全一致的 **`CF_HDROP`（`DROPFILES` 结构）** 剪贴板通道，剪贴板仅传递几十字节的文件路径，微信直接读取原图文件发送，支持保留 GIF 动图，速度提升 5~10 倍；同时保留 `CF_DIB` 静态位图模式供兼容切换。
3. **非侵入式“多尺度模板匹配 + 帧间动态差分（Frame-Diff）”确认上传完成，告别右键菜单弹窗**：
   - 2.0.x 在未捕捉到转圈时会模拟右键点击图片弹出菜单并临时修改渲染窗口样式，容易闪烁或受分辨率影响。
   - **3.0.0 新方案**：全程只读分析聊天消息区域截图——先通过多尺度归一化相关匹配（自适应 100%~200% DPI 缩放及宽图/竖图）定位新增缩略图，再计算缩略图左侧状态区的**连续帧间像素差分（Spinner Motion Score）**与**红色失败感叹号检测**。当上传动画消失、状态区连续静止且无红色感叹号时放行图片名称，**全程零右键点击、零窗口样式修改**。
4. **内置 GitHub Actions 云端自动构建工作流（`.github/workflows/release.yml`）**：
   - 将源码上传至 GitHub 仓库后，GitHub 云端 Windows Server 会自动运行 100% 单元测试并调用 PyInstaller 打包生成免安装的 `WeChatImageSender-Windows-x64.zip` 发行包。

## 快速开始

### 方式一：直接下载免安装发行包
在 GitHub 仓库的 **Actions** 或 **Releases** 页面下载 `WeChatImageSender-Windows-x64.zip`，完整解压后双击 `WeChatImageSender.exe` 即可运行（保留同目录的 `_internal` 文件夹，无需安装 Python）。

### 方式二：从源码运行（Windows 10/11，Python 3.10~3.12）
双击 **`安装依赖.cmd`** 自动创建虚拟环境并安装依赖，完成后双击 **`启动程序.cmd`** 即可打开图形界面。

命令行操作：
```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

运行离线测试、演示模式与只读诊断：
```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe main.py --demo
.\.venv\Scripts\python.exe main.py --demo --headless
.\.venv\Scripts\python.exe main.py --diagnose --diagnose-output diagnostic.json
```

本地一键打包 `.exe`：
双击 **`一键打包EXE.cmd`** 或运行：
```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

## 状态不明时与断点续传恢复

程序严格遵循 **先写日志、后发消息** 的事务原则（任务记录保存在 `%LOCALAPPDATA%\WeChatImageSender\jobs`）。若遇到网络波动、焦点切走或状态未确认，队列会立即暂停并标记为 `NEEDS_MANUAL_VERIFY`，绝不盲目重复发送。点击界面上的 **“处理待核实项...”** 可按实际情况选择：
- **图片已成功发出 → 仅补发图片名称**
- **图片与名称均已发出 → 标记本张完成**
- **确认未发出（且已清理草稿）→ 重新发送本张**
