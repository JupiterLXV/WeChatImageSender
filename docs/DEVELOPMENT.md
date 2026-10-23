# 开发说明

本项目面向 Windows 10/11 桌面微信。v3.6.0 的 Windows 7 风格是视觉参考，不代表 Windows 7 运行兼容性。主要维护模块化源码，避免在兼容入口中复制实现。

## 环境与启动

在项目根目录创建 Python 3.10–3.12 虚拟环境并安装依赖：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py --demo
```

日常开发优先使用离线演示。`--demo --headless` 自动生成临时样张与任务，不连接真实微信；`--diagnose` 只读检查微信窗口和版本。真实模式会操作剪贴板和微信窗口，需要事先准备明确的测试会话与样张。

## 代码分工

- `main.py`：启动参数、演示与诊断入口。
- `wechat_sender/gui.py`：界面整合、Windows 热键、拖放和微信窗口识别。
- `gui_layout.py`：蓝白视觉、布局、图标、预览及窗口关闭交互。
- `gui_workflow.py`：队列顺序、编辑锁定、后台扫描、历史恢复和运行状态。
- `job_store.py`：发送步骤、任务持久化与人工决策。
- `wechat_adapter.py`：真实微信和离线演示实现。
- `windows_ocr.py`：Windows 本地 OCR 回退，优先选择已安装的中文识别语言；缺少运行组件、语言组件或识别失败时返回不可用。
- `image_utils.py`：图片读取、摘要、命名和客户端画面判断。
- `clipboard_win32.py`、`accessibility.py`：Windows 平台支持。

修改 Logo 时同时检查 `assets/app-icon.png`、小尺寸 PNG、ICO，以及窗口、任务栏和 PyInstaller 资源。`docs/images/desktop.png` 必须使用离线人工样张，不得包含真实微信信息。

## 需要保持的行为

1. 图片按队列序号发送。表头排序必须修改实际队列，并保留文件与自定义名称的对应关系。
2. 已保存的任务继续使用其接收对象与顺序；运行中禁止编辑队列。旧扫描结果不得覆盖新任务或恢复的任务。
3. 对可能产生消息的步骤先记录状态。操作结果不明时暂停并人工核实，不按“尚未完成”直接自动重发。
4. 停止请求应保留到工作线程处理，等待确认过程应能响应停止。关闭窗口时等待工作线程退出后再释放适配器。
5. Tk 控件更新在主线程执行。扫描、连接诊断和图片解码等较慢操作放入后台；COM/UI Automation 工作线程需正确初始化。
6. 运行速度以本轮新完成条目为基准，暂停后冻结耗时。不要用已有完成数美化恢复任务的速度。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe main.py --demo --headless
.\.venv\Scripts\python.exe main.py --demo --diagnose
```

新增回归应覆盖实际风险，例如排序与发送不一致、恢复后会话错位、提交后异常、停止信号丢失和旧后台结果覆盖。测试使用临时目录、人工样张与模拟适配器，不应依赖真实联系人、私人图片或真实消息。

界面改动还需打开离线演示，检查最小窗口、缩放、折叠预览、日志、队列操作与任务状态。单元测试通过不能替代真实微信验收。v3.6.0 在当前电脑的深色微信、125% 显示缩放下向文件传输助手发送了两张人工样张与名称：图片画面匹配通过，文字识别曾暂停，调整 OCR 后只读复核通过并人工标记完成，未重发。仍需进一步验证整批无人干预发送与其他客户端环境。

## 构建与兼容入口

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

构建使用 `WeChatImageSender.spec`，默认发行产物输出到 `releases/3.6.0/`。仅复制 `.exe` 可能缺少运行依赖，应完整交付输出目录或压缩包。以当前构建脚本及 CI 日志确认产物，不把本地构建成功描述成 GitHub Release 已发布。

根目录 `WeChatImageSender_v3.py` 是转到 `main.py` 的兼容启动入口，需要项目其他模块。需要独立脚本时执行：

```powershell
python scripts/export_single_file.py
```

导出脚本及所需图标放在 `dist/standalone/`，属于生成产物，不手工维护或提交；修改模块后重新导出。

## 提交与问题反馈

提交前运行相关测试，并检查差异中是否混入个人数据。排除 `.venv`、缓存、构建产物、本地任务记录、诊断输出、微信截图与用户图片。反馈缺陷请提供人工复现步骤、运行环境和脱敏结果。

说明应区分离线测试、界面检查、只读诊断和真实发送验收。性能与兼容性结论需要注明实际测试环境，不使用“所有版本兼容”“固定倍数加速”等未经验证的承诺。
