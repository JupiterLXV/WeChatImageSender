from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from .core import DemoBackend, Journal, NeedsReview, SenderError, Stopped, run_queue, scan_folder
from .compatibility import APP_VERSION, DOWNLOAD_URL, UPDATE_REMINDER

PHASES = {'pending': '等待发送', 'image_attempting': '图片待核实',
          'image_submitted': '等待图片发送完成', 'image_confirmed': '图片完成，等待名称',
          'name_attempting': '名称待核实', 'name_submitted': '核对文件名', 'done': '完成'}


def app_data() -> Path:
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'WeChatImageSender'


class Application:
    def __init__(self, root: tk.Tk, demo=False):
        self.root = root
        self.demo = demo
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.worker = None
        self.binding = None
        self.photos = []
        self.journal = None
        self.sessions = []
        self.close_requested = False
        self.controls = []
        self.folder = tk.StringVar()
        self.recursive = tk.BooleanVar(value=False)
        self.extension = tk.BooleanVar(value=True)
        self.timeout = tk.StringVar(value='90')
        self.interval = tk.StringVar(value='1')
        self.send_shortcut = tk.StringVar(value='Enter')
        self.target = tk.StringVar(value='尚未选择聊天')
        self.status = tk.StringVar(value='先选择图片文件夹，再选择微信聊天。')
        self.progress_label = tk.StringVar(value='0 / 0 张完成')
        self.version_status = tk.StringVar(value='演示模式不检测微信版本。' if demo else '等待检测微信版本……')
        self.allow_activation = tk.BooleanVar(value=False)
        self.root.title(f'微信图片顺序发送助手 {APP_VERSION}' + (' · 演示模式' if demo else ''))
        window_height = min(940, self.root.winfo_screenheight() - 100)
        self.root.geometry(f'1040x{window_height}')
        self.root.minsize(950, min(820, window_height))
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.build()
        if demo:
            self.binding = {'title': '演示联系人（不会发送消息）', 'account': '演示',
                            'key': 'demo', 'demo': True}
            self.target.set(self.binding['title'])
        else:
            self.root.after(250, self.check_wechat)
        self.root.after(100, self.drain)

    def button(self, parent, text, command, **kw):
        button = ttk.Button(parent, text=text, command=command, **kw)
        self.controls.append(button)
        return button

    def build(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10))
        style.configure('TFrame', background='#f5f7fa')
        style.configure('TLabel', background='#f5f7fa', foreground='#172a3a')
        style.configure('TLabelframe', background='#f5f7fa')
        style.configure('TLabelframe.Label', font=('Microsoft YaHei UI', 11, 'bold'), background='#f5f7fa')
        style.configure('TButton', padding=(10, 6))
        style.configure('Accent.TButton', background='#147d64', foreground='white')
        style.map('Accent.TButton', background=[('active', '#0d6852'), ('disabled', '#bcc9c4')])
        style.configure('Treeview', rowheight=28, font=('Microsoft YaHei UI', 9))
        style.configure('Treeview.Heading', font=('Microsoft YaHei UI', 9, 'bold'))
        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='图片 → 确认完成 → 图片名称',
                  font=('Microsoft YaHei UI', 20, 'bold')).pack(anchor='w')
        ttk.Label(outer, text=UPDATE_REMINDER, wraplength=960).pack(anchor='w', pady=(4, 12))
        version_bar = ttk.Frame(outer)
        version_bar.pack(fill='x', pady=(0, 8))
        self.button(version_bar, '检测微信版本', self.check_wechat).pack(side='right')
        self.button(version_bar, '微信官方下载', lambda: webbrowser.open(DOWNLOAD_URL)).pack(side='right', padx=6)
        ttk.Label(version_bar, textvariable=self.version_status, wraplength=650,
                  foreground='#946200').pack(side='left', fill='x', expand=True)
        if self.demo:
            ttk.Label(outer, text='演示模式：只模拟队列和上传状态，不连接微信。',
                      foreground='#946200').pack(anchor='w', pady=(0, 8))

        files = ttk.LabelFrame(outer, text='1  选择图片', padding=10)
        files.pack(fill='x', pady=(0, 10))
        row = ttk.Frame(files)
        row.pack(fill='x')
        ttk.Entry(row, textvariable=self.folder, state='readonly').pack(side='left', fill='x', expand=True)
        self.button(row, '选择文件夹', self.choose_folder).pack(side='left', padx=(8, 0))
        opts = ttk.Frame(files)
        opts.pack(fill='x', pady=(8, 0))
        for title, variable in [('包含子文件夹', self.recursive), ('名称包含扩展名（如 .jpg）', self.extension)]:
            box = ttk.Checkbutton(opts, text=title, variable=variable, command=self.options_changed)
            box.pack(side='left', padx=(0, 15))
            self.controls.append(box)
        self.button(opts, '重新读取', self.scan).pack(side='right')
        ttk.Label(files, text='按文件名自然排序：1、2、10。支持常见静态图片；不修改原文件。',
                  foreground='#566778').pack(anchor='w', pady=(6, 0))

        targets = ttk.LabelFrame(outer, text='2  选择好友或群聊', padding=10)
        targets.pack(fill='x', pady=(0, 10))
        if not self.demo:
            consent = ttk.Checkbutton(targets, text='允许临时启用微信辅助功能（查看说明后同意，仅本次运行有效）',
                                      variable=self.allow_activation, command=self.activation_changed)
            consent.pack(anchor='w', pady=(0, 6))
            self.controls.append(consent)
        row = ttk.Frame(targets)
        row.pack(fill='x')
        self.session_box = ttk.Combobox(row, state='readonly', width=32)
        self.session_box.pack(side='left', fill='x', expand=True)
        self.controls.append(self.session_box)
        self.button(row, '刷新最近会话', self.refresh_sessions).pack(side='left', padx=6)
        self.button(row, '使用所选会话', self.open_session).pack(side='left')
        self.button(row, '使用微信当前聊天', self.bind_current).pack(side='left', padx=(6, 0))
        ttk.Label(targets, textvariable=self.target, font=('Microsoft YaHei UI', 11, 'bold'),
                  foreground='#147d64').pack(anchor='w', pady=(8, 0))
        ttk.Label(targets, text='列表没有目标时，先在微信打开该聊天，再点击“使用微信当前聊天”。',
                  foreground='#566778').pack(anchor='w', pady=(4, 0))

        settings = ttk.Frame(outer)
        settings.pack(fill='x', pady=(0, 8))
        ttk.Label(settings, text='单条确认超时（秒）').pack(side='left')
        timeout = ttk.Spinbox(settings, from_=10, to=600, textvariable=self.timeout, width=6)
        timeout.pack(side='left', padx=(6, 18))
        ttk.Label(settings, text='每张间隔（秒）').pack(side='left')
        interval = ttk.Spinbox(settings, from_=0, to=60, textvariable=self.interval, width=6)
        interval.pack(side='left', padx=6)
        self.controls.extend([timeout, interval])
        ttk.Label(settings, text='微信发送快捷键').pack(side='left', padx=(18, 6))
        shortcut = ttk.Combobox(settings, textvariable=self.send_shortcut,
                               values=('Enter', 'Ctrl+Enter'), state='readonly', width=12)
        shortcut.pack(side='left')
        self.controls.append(shortcut)
        self.shortcut_box = shortcut

        # Reserve the controls and logs first, allowing the list to shrink.
        footer = ttk.Frame(outer)
        footer.pack(side='bottom', fill='x')
        listing = ttk.Frame(outer)
        listing.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(listing, columns=('order', 'image', 'name', 'state'), show='headings', height=7)
        for key, title, width in [('order', '序号', 50), ('image', '图片文件', 300),
                                  ('name', '发送的名称', 270), ('state', '状态', 195)]:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=45, stretch=key != 'order')
        self.tree.tag_configure('done', foreground='#147d64')
        self.tree.tag_configure('review', foreground='#a75b00')
        bar = ttk.Scrollbar(listing, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side='left', fill='both', expand=True)
        bar.pack(side='right', fill='y')

        progress = ttk.Frame(footer)
        progress.pack(fill='x', pady=(8, 6))
        self.progress = ttk.Progressbar(progress, maximum=1)
        self.progress.pack(side='left', fill='x', expand=True)
        ttk.Label(progress, textvariable=self.progress_label).pack(side='left', padx=(10, 0))
        self.actions = ttk.Frame(footer)
        self.actions.pack(fill='x', pady=(0, 6))
        self.button(self.actions, '开始 / 继续', self.start, style='Accent.TButton').pack(side='left')
        self.stop_button = ttk.Button(self.actions, text='停止（F8）', command=self.request_stop, state='disabled')
        self.stop_button.pack(side='left', padx=6)
        self.button(self.actions, '处理待核实项', self.review).pack(side='left')
        self.button(self.actions, '恢复历史任务', self.restore).pack(side='left', padx=6)
        self.button(self.actions, '新建任务', self.reset).pack(side='right')
        self.button(self.actions, '打开记录目录', lambda: os.startfile(app_data() / 'jobs')).pack(side='right', padx=6)
        ttk.Label(footer, textvariable=self.status, wraplength=900, foreground='#4a5868').pack(fill='x', pady=(0, 5))
        self.log = scrolledtext.ScrolledText(footer, height=4, font=('Microsoft YaHei UI', 9),
                                           relief='flat', background='#eaf0f5', state='disabled')
        self.log.pack(fill='x')
        ttk.Label(footer, text='发送期间请保持微信在前台，不要切换聊天或从其他设备同时发消息。F8 可随时停止。',
                  foreground='#566778', font=('Microsoft YaHei UI', 9)).pack(anchor='w', pady=(6, 0))
        app_data().joinpath('jobs').mkdir(parents=True, exist_ok=True)
        self.root.bind('<F8>', lambda event: self.request_stop())

    def append_log(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', f'{time.strftime("%H:%M:%S")}  {text}\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def busy(self):
        return self.worker is not None and self.worker.is_alive()

    def activation_changed(self):
        if self.allow_activation.get():
            accepted = messagebox.askyesno('临时启用辅助功能',
                '微信 4.1.15 默认可能不公开聊天控件。程序会校验已验证版本的官方模块指纹，'
                '临时修改运行中微信的 1 个辅助功能标记字节。\n\n'
                '这可能造成微信不稳定。正常退出助手时尝试恢复；若异常退出，请重启微信恢复。'
                '不修改安装文件，不访问聊天数据库。\n\n是否同意在本次运行中启用？', parent=self.root)
            self.allow_activation.set(accepted)
        else:
            from .accessibility import restore_all
            restore_all()

    def launch(self, operation):
        if self.busy():
            return
        self.stop.clear()
        for control in self.controls:
            control.configure(state='disabled')
        self.stop_button.configure(state='normal')
        allow_activation = self.allow_activation.get()

        def runner():
            try:
                if self.demo:
                    operation(None)
                else:
                    import uiautomation as uia
                    from .wechat import WeChatBackend
                    with uia.UIAutomationInitializerInThread():
                        operation(WeChatBackend(allow_activation=allow_activation, stop=self.stop))
            except (SenderError, OSError, ValueError) as exc:
                self.events.put(('error', str(exc)))
            except Exception as exc:
                self.events.put(('error', f'{type(exc).__name__}：{exc}。已停止；请核实当前消息。'))
            finally:
                self.events.put(('idle', None))

        self.worker = threading.Thread(target=runner, name='wechat-worker', daemon=True)
        self.worker.start()

    def can_replace(self):
        if self.journal and any(p.phase != 'pending' for p in self.journal.photos):
            messagebox.showinfo('已有发送记录', '请先完成或处理当前任务，再点击“新建任务”。已发送内容不会自动重复。')
            return False
        return True

    def choose_folder(self):
        if not self.can_replace():
            return
        folder = filedialog.askdirectory(title='选择要发送的图片文件夹')
        if folder:
            self.folder.set(folder)
            self.scan()

    def options_changed(self):
        if self.journal:
            self.status.set('当前任务的图片和名称已固定；选项将在新建任务后生效。')
            return
        if self.folder.get():
            self.scan()

    def scan(self):
        if not self.folder.get() or not self.can_replace():
            return
        folder, recursive, extension = Path(self.folder.get()), self.recursive.get(), self.extension.get()
        self.status.set('正在读取和校验图片……')
        self.photos = []
        self.journal = None
        self.update_tree()
        self.launch(lambda _: self.events.put(('photos', scan_folder(folder, recursive, extension, self.stop))))

    def refresh_sessions(self):
        if self.demo:
            self.status.set('演示模式已选择模拟联系人。')
            return
        self.status.set('正在读取最近会话……')
        self.launch(lambda backend: self.events.put(('sessions', backend.sessions())))

    def check_wechat(self):
        if self.demo:
            self.version_status.set('演示模式不检测微信版本，也不连接微信。')
            return
        if self.busy():
            return
        self.version_status.set('正在检测微信版本……')
        self.launch(lambda backend: self.events.put(('version', backend.version_report())))

    def accept_binding(self, binding):
        if self.journal and binding['key'] != self.journal.data['binding']['key']:
            raise SenderError('历史任务的微信登录会话或接收对象不同。微信重启、重新登录或切换账号后不能自动接续旧任务；请核实旧记录后新建剩余任务。')
        self.binding = binding
        if self.journal:
            self.journal.data['binding'] = binding
            self.journal.save()
        version = f' · 微信 {binding["version"]}' if 'version' in binding else ''
        self.target.set(f'接收对象：{binding["title"]} · 已锁定本次微信登录会话{version}')
        self.status.set('目标已绑定。核对图片列表后，点击“开始 / 继续”。')
        self.append_log('目标已绑定：' + binding['title'])

    def open_session(self):
        if self.demo:
            return
        index = self.session_box.current()
        if index < 0:
            messagebox.showinfo('选择聊天', '请先刷新并选择最近会话。')
            return
        session = self.sessions[index]

        def action(backend):
            backend.connect()
            self.events.put(('binding', backend.open_session(session)))
        self.launch(action)

    def bind_current(self):
        if self.demo:
            return
        self.launch(lambda backend: self.events.put(('binding', backend.bind_current())))

    def start(self):
        if not self.photos or self.binding is None:
            messagebox.showinfo('尚未准备好', '请先读取图片文件夹并选择聊天。')
            return
        try:
            timeout, interval = float(self.timeout.get()), float(self.interval.get())
            if not 10 <= timeout <= 600 or not 0 <= interval <= 60:
                raise ValueError()
        except ValueError:
            messagebox.showerror('参数错误', '超时请填 10～600 秒，间隔请填 0～60 秒。')
            return
        if self.journal and any(p.phase not in {'pending', 'image_confirmed', 'done'} for p in self.photos):
            self.review()
            return
        if all(p.phase == 'done' for p in self.photos):
            messagebox.showinfo('任务已完成', '所有图片和名称均已处理。需要重新发送时请新建任务。')
            return
        if self.journal is None:
            self.binding['send_shortcut'] = self.send_shortcut.get()
            try:
                self.journal = Journal.create(app_data() / 'jobs', self.binding, self.photos, self.demo)
                self.photos = self.journal.photos
            except OSError as exc:
                messagebox.showerror('无法保存记录', f'尚未发送。请先解决记录目录的读写问题：\n{exc}')
                return
        elif self.binding is not None:
            self.journal.data['binding']['send_shortcut'] = self.send_shortcut.get()
            try:
                self.journal.save()
            except OSError as exc:
                messagebox.showerror('无法保存记录', f'尚未继续发送。请先解决记录目录的读写问题：\n{exc}')
                return
        journal = self.journal
        self.status.set('即将切换到微信并开始。请保持微信在前台；按 F8 可停止。')
        self.append_log('开始任务：' + journal.path.name)

        def action(backend):
            if self.demo:
                backend = DemoBackend()
            else:
                backend.attach(journal.data['binding'])
            run_queue(journal, backend, self.stop, lambda k, v: self.events.put((k, v)),
                      timeout=timeout, interval=interval)
        self.launch(action)

    def request_stop(self):
        if self.busy():
            self.stop.set()
            self.status.set('已请求停止。已点击发送的消息可能仍在微信中继续上传。')

    def review(self):
        if not self.journal:
            messagebox.showinfo('无需核实', '没有待核实的发送记录。')
            return
        uncertain = [(i, p) for i, p in enumerate(self.photos)
                     if p.phase not in {'pending', 'image_confirmed', 'done'}]
        if not uncertain:
            messagebox.showinfo('无需核实', '没有待核实项，可直接继续任务。')
            return
        index, photo = uncertain[0]
        window = tk.Toplevel(self.root)
        window.title('核实当前消息后再继续')
        window.geometry('610x290')
        window.transient(self.root)
        window.grab_set()
        body = ttk.Frame(window, padding=20)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text=f'第 {index + 1} 张：{Path(photo.path).name}',
                  font=('Microsoft YaHei UI', 12, 'bold'), wraplength=560).pack(anchor='w')
        image_phase = photo.phase.startswith('image_')
        explanation = ('程序未确认图片发送完成，文件名尚未提交。\n'
                       '请在微信核实图片已发送成功、没有转圈或红色失败标记，再选择操作。'
                       if image_phase else '图片已确认；文件名的提交结果待核实。\n请在微信查看该图片之后是否已有完全一致的文件名。')
        ttk.Label(body, text=explanation, wraplength=560).pack(anchor='w', pady=14)

        def decide(decision):
            if decision == 'not_sent' and not messagebox.askyesno(
                    '核实后重试', '请确认该条消息没有发送成功，也没有仍在上传；并清理输入框中的待发送草稿。\n重试可能产生重复消息，仍要重试吗？', parent=window):
                return
            try:
                self.journal.resolve(index, decision)
            except (SenderError, OSError) as exc:
                messagebox.showerror('核实结果未保存', str(exc), parent=window)
                return
            self.append_log(f'第 {index + 1} 张：用户核实结果 {decision}')
            self.update_tree()
            self.status.set('核实结果已保存。请重新绑定原聊天，然后点击“开始 / 继续”。')
            window.destroy()
        ttk.Button(body, text='图片已成功，仅补发名称' if image_phase else '文件名已成功，标记本张完成',
                   command=lambda: decide('image_sent' if image_phase else 'both_sent')).pack(fill='x', pady=3)
        ttk.Button(body, text='确认本条没有发出，允许重试',
                   command=lambda: decide('not_sent')).pack(fill='x', pady=3)
        ttk.Button(body, text='还在发送或不确定，保持暂停', command=window.destroy).pack(fill='x', pady=3)

    def restore(self):
        if not self.can_replace():
            return
        filename = filedialog.askopenfilename(title='恢复历史发送任务', initialdir=app_data() / 'jobs',
                                              filetypes=[('任务记录', '*.json')])
        if not filename:
            return
        try:
            journal = Journal.load(Path(filename))
            if bool(journal.data['demo']) != self.demo:
                raise SenderError('演示任务与真实发送任务不能互相恢复。')
            self.journal = journal
            self.photos = journal.photos
            self.binding = journal.data['binding'] if self.demo else None
            self.folder.set(str(Path(self.photos[0].path).parent) if self.photos else '')
            self.target.set('恢复目标：' + journal.data['binding']['title'] + '；请重新绑定原聊天')
            self.update_tree()
            self.status.set('任务已恢复。仅支持同一次微信登录会话内绑定原聊天；微信重启或重新登录后须核实旧记录并新建剩余任务。')
        except Exception as exc:
            messagebox.showerror('恢复失败', str(exc))

    def reset(self):
        if self.journal and any(p.phase != 'done' for p in self.photos):
            if not messagebox.askyesno('保留旧任务并新建', '当前任务未完成，记录将保留。新任务会重新发送所选图片。\n是否新建？'):
                return
        self.photos = []
        self.journal = None
        self.folder.set('')
        self.update_tree()
        self.status.set('新任务已就绪，请选择图片文件夹。')

    def update_tree(self):
        self.tree.delete(*self.tree.get_children())
        for i, photo in enumerate(self.photos):
            tag = 'done' if photo.phase == 'done' else ('review' if photo.phase not in {'pending', 'image_confirmed'} else '')
            self.tree.insert('', 'end', iid=str(i), values=(i + 1, Path(photo.path).name,
                             photo.name, PHASES.get(photo.phase, photo.phase)), tags=(tag,))
        done = sum(p.phase == 'done' for p in self.photos)
        self.progress.configure(maximum=max(1, len(self.photos)), value=done)
        self.progress_label.set(f'{done} / {len(self.photos)} 张完成')

    def drain(self):
        try:
            from .winutil import user32
            if user32.GetAsyncKeyState(0x77) & 0x8000:  # Global F8, including when WeChat is active.
                self.request_stop()
        except Exception:
            pass
        while True:
            try:
                event, value = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                if event == 'photos':
                    self.photos = value
                    self.update_tree()
                    self.status.set(f'已校验 {len(value)} 张图片，发送顺序如下。')
                elif event == 'sessions':
                    self.sessions = value
                    self.session_box.configure(values=[f'{i + 1}. {s["title"]}' for i, s in enumerate(value)])
                    self.status.set(f'已读取 {len(value)} 个最近会话。')
                elif event == 'binding':
                    self.accept_binding(value)
                elif event == 'version':
                    self.version_status.set(value['message'])
                    self.append_log(value['message'])
                elif event in {'item', 'phase'}:
                    self.update_tree()
                    self.tree.see(str(value[0]))
                    self.tree.selection_set(str(value[0]))
                    self.status.set(f'第 {value[0] + 1} 张 · {PHASES[value[1]]}')
                    self.append_log(self.status.get())
                elif event == 'status':
                    self.status.set(value)
                    self.append_log(value)
                elif event == 'complete':
                    self.status.set(f'任务完成：已处理 {value} 张图片及名称。')
                    self.append_log(self.status.get())
                elif event == 'error':
                    self.status.set(value)
                    self.append_log(value)
                    self.update_tree()
                elif event == 'idle':
                    for control in self.controls:
                        control.configure(state='readonly' if control in (self.session_box, self.shortcut_box) else 'normal')
                    self.stop_button.configure(state='disabled')
            except Exception as exc:
                self.status.set(str(exc))
                self.append_log(str(exc))
        if self.close_requested and not self.busy():
            self.root.destroy()
            return
        self.root.after(100, self.drain)

    def close(self):
        if self.busy():
            self.close_requested = True
            self.request_stop()
            self.status.set('正在停止任务并保存进度，稍后关闭……')
        else:
            self.root.destroy()
