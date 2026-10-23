<p align="center">
  <img src="assets/app-icon-64.png" width="64" height="64" alt="微信图片顺序发送助手 Logo">
</p>

# 微信图片顺序发送助手 · v3.6.0

面向 Windows 桌面微信的图片队列工具。按照 **图片 → 客户端状态核实 → 图片名称** 逐张处理，支持暂停、人工核实和历史任务恢复。

![蓝白桌面工作台，使用离线示例图片](docs/images/desktop.png)

## 这一版改了什么

- **蓝白桌面工作台**：借鉴 Windows 7 的桌面层次与 Google 风格的简洁排版，集中展示接收对象、图片队列和发送进度；预览、日志与高级设置按需展开。
- **原创程序图标**：叠放照片与发送箭头组成蓝白 Logo，应用于窗口、任务栏和打包资源。
- **列表就是发送顺序**：点击文件名或发送名称表头会调整实际队列；自定义名称跟随对应文件，序号同步更新。
- **恢复时锁定接收对象**：任务开始后保存接收对象、顺序与名称。历史任务继续使用记录中的会话，修改这些内容需新建任务。
- **发送期间保护队列**：禁止导入、改名、移除和调整顺序；扫描结果过期后不会覆盖当前任务。
- **减少界面等待**：扫描、连接检查和预览加载放到后台；速度统计只计算本轮新完成的图片。

Windows 7 指的是视觉方向，**运行与打包面向 Windows 10/11**。

## 使用前提与确认范围

真实发送需要已登录的 Windows 桌面微信、可辨认的目标聊天窗口及可读取的本地图片。发送时程序需要操作微信前台窗口与系统剪贴板，请留出操作时间。

默认 `CF_HDROP` 模式把原文件路径放入剪贴板，由微信读取源文件，不改写本地原图。微信如何压缩、展示或传输文件由客户端决定；程序不承诺接收端文件与源文件逐字节相同。`CF_DIB` 是静态位图兼容模式，不能保留动画。

程序通过本地客户端画面、上传状态和文字状态判断是否可以继续。**客户端确认不等于接收方已经收到或阅读**。微信版本、显示缩放、窗口遮挡和网络状况均可能影响识别，无法确认时应暂停并人工核实。

部分微信界面无法通过无障碍接口读取文字时，程序会尝试 Windows 本地 OCR。识别需要 Windows 已安装可用的 OCR 语言组件，优先使用中文识别语言；缺少组件或无法可靠识别时仍会暂停。OCR 截图只在本机处理，不上传到云端识别服务。

v3.6.0 已完成离线回归、演示队列、界面检查，以及当前电脑微信“文件传输助手”的两张人工样张与对应名称实发。实测采用深色微信界面、125% 显示缩放：两张图片均通过画面匹配；名称识别曾触发暂停，调整本地 OCR 后通过只读复核，再人工标记完成，没有重发。此结果尚不能证明整批无人干预发送，也不代表所有微信版本均兼容或有固定速度保证。

## 快速开始

### 从源码运行

准备 Windows 10/11、Python 3.10–3.12（含 Tk）。在项目根目录执行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

也可以依次双击 `安装依赖.cmd`、`启动程序.cmd`。

### 离线演示与只读诊断

```powershell
# 演示界面，不连接真实微信
.\.venv\Scripts\python.exe main.py --demo

# 自动生成临时样张并运行离线队列
.\.venv\Scripts\python.exe main.py --demo --headless

# 只读检查微信窗口与版本，不发送消息
.\.venv\Scripts\python.exe main.py --diagnose

# 将诊断保存在临时目录，分享前先检查隐私内容
.\.venv\Scripts\python.exe main.py --diagnose --diagnose-output "$env:TEMP\WeChatImageSender-diagnostic.json"
```

### 正常发送

1. 打开微信并进入接收对象的聊天窗口，再启动程序。
2. 导入图片或文件夹，检查列表序号、图片预览与待发送名称；可以拖入文件导入。
3. 确认接收对象及微信使用的发送快捷键，再点击“开始发送”。
4. 需要暂停时点击停止或按 **F8**。等待当前步骤安全结束后，再操作队列或恢复任务。

恢复方法与核实选项见 [使用说明](使用说明.md)。

## 历史任务与数据

任务 JSON 默认保存在 `%LOCALAPPDATA%\WeChatImageSender\jobs`；没有该环境变量时使用用户目录中的 `.wechat_image_sender\jobs`。记录包含接收对象、本地路径、图片名称、摘要与处理状态，不保存原图副本。

“新建任务”只重置界面队列，保留已有任务记录。恢复任务需要源图片仍然存在且通过摘要检查。状态不明的条目必须先人工核实，程序不会直接把它当作未发送重新执行。

任务记录、诊断文件、微信截图和个人图片均属于本地数据，不应提交到公开仓库。反馈问题时使用人工样张，并隐藏会话名称、头像、聊天内容和本地私人路径。

## 项目结构

```text
main.py                       程序入口：正常、演示和诊断模式
wechat_sender/
  gui.py                      窗口整合与桌面交互
  gui_layout.py               蓝白布局、主题和图片预览
  gui_workflow.py             队列编辑、后台扫描、恢复和运行状态
  job_store.py                顺序发送状态机与任务记录
  wechat_adapter.py           微信交互及离线演示适配器
  windows_ocr.py              Windows 本地文字识别回退
  image_utils.py              扫描、摘要、命名和画面判断
  clipboard_win32.py          Windows 剪贴板处理
  accessibility.py            Windows 无障碍接口
assets/                       程序 Logo 与图标
tests/                        离线回归测试
docs/                         开发说明、变更记录与公开示例截图
scripts/export_single_file.py 可选单文件导出工具
WeChatImageSender.spec         PyInstaller 打包入口
build.ps1                     本地测试、打包与压缩
WeChatImageSender_v3.py        兼容启动入口，转到 main.py
```

根目录的 `WeChatImageSender_v3.py` 需要完整项目，不再保存一份重复实现。需要独立脚本时，运行 `python scripts/export_single_file.py`，生成文件及所需图标位于 `dist/standalone/`；该目录不纳入源码管理。

## 测试与打包

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

默认发行输出目录为 `releases/3.6.0/`。使用打包后的程序时应完整解压并保留配套资源，不要只复制 `.exe`。本地构建成功不代表 GitHub Release 已发布。

贡献与测试边界见 [开发说明](docs/DEVELOPMENT.md)，版本改动见 [变更记录](docs/CHANGELOG.md)。
