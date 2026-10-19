from __future__ import annotations

import hashlib
import ctypes
import json
import re
import threading
import time
from pathlib import Path
from ctypes import wintypes

import uiautomation as uia

from .core import NeedsReview, Observation, SenderError, State, Stopped
from .compatibility import aid_matches, check_version, editor_chat_candidates, TARGET_VERSION
from .qt_status import Signal, message_state
from .winutil import copy_image, copy_text, process_path, user32, version_of

MESSAGE_NAMES = {'消息', 'Messages'}
SESSION_NAMES = {'会话', 'Chats', 'Sessions'}
SEARCH_NAMES = {'搜索', 'Search'}
IMAGE_NAMES = {'[图片]', '图片', '[Image]', 'Image'}
BUSY = {'发送中', '正在发送', '正在上传', '上传中', '取消发送',
        'Sending', 'Sending...', 'Uploading', 'Cancel sending'}
FAILED = {'发送失败', '重新发送', '重发', '未发送', '发送失败，点击重新发送',
          'Failed', 'Send failed', 'Resend', 'Not sent'}
DONE = {'已发送', '发送成功', '发送完成', 'Sent', 'Delivered'}
OFFLINE = {'网络连接已断开', '网络连接不可用', '无法连接到服务器', '连接已断开',
           'Disconnected', 'Unable to connect to server'}


def rid(control) -> str:
    return '.'.join(str(i) for i in control.GetRuntimeId())


def walk(control, depth=18, budget=1000, skip_lists=False):
    stack = [(control, 0)]
    count = 0
    while stack:
        item, level = stack.pop()
        count += 1
        if count > budget:
            raise NeedsReview('微信界面控件过多，无法完整检查，已暂停。')
        yield item
        if skip_lists and item.ControlTypeName == 'ListControl':
            continue
        if level < depth:
            stack.extend((c, level + 1) for c in reversed(item.GetChildren()))
        elif item.GetChildren():
            raise NeedsReview('微信界面结构超出可检查范围，已暂停。')


def valid_rect(control):
    rect = control.BoundingRectangle
    return rect.right > rect.left and rect.bottom > rect.top and rect.left > -30000


class WeChatBackend:
    """WeChat 4.1.15 Qt UI adapter. Unknown states never acknowledge images."""

    def __init__(self, allow_activation=False, stop=None):
        uia.SetGlobalSearchTimeout(1)
        self.window = None
        self.message_list = None
        self.editor = None
        self.send_button = None
        self._version = ''
        self._path = ''
        self.send_shortcut = 'Enter'
        self.allow_activation = allow_activation
        self.stop = stop
        self._source_images = {}

    def _windows(self):
        candidates = []
        handles = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        def collect(hwnd, unused):
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if cls.value == 'WeChatMainWndForPC' or re.fullmatch(r'Qt\d+QWindowIcon', cls.value):
                handles.append((hwnd, cls.value))
            return True
        user32.EnumWindows(callback_type(collect), 0)
        for hwnd, cls in handles:
            try:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                path = process_path(pid.value)
                if Path(path).name.lower() not in {'wechat.exe', 'weixin.exe'}:
                    continue
                title = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, title, 256)
                if cls.startswith('Qt') and not re.fullmatch(r'(?:微信|Weixin|WeChat)(?:\s*[（(]\d+[）)])?', title.value):
                    continue
                window = uia.ControlFromHandle(hwnd)
                candidates.append((window, path, version_of(path)))
            except (SenderError, OSError):
                continue
        return candidates

    def version_report(self):
        windows = self._windows()
        if not windows:
            return {'supported': False, 'version': '',
                    'message': '未检测到微信主窗口。使用前请更新到官方最新版本（本程序适配 4.1.15）并登录。'}
        supported = [entry for entry in windows if check_version(entry[2]).supported]
        chosen = supported or windows
        if len(chosen) != 1:
            return {'supported': False, 'version': '', 'message': '检测到多个微信窗口，请只保留需要发送的账号窗口。'}
        report = check_version(chosen[0][2])
        return {'supported': report.supported, 'version': report.version, 'message': report.message}

    def connect(self, hwnd=None, allow_activation=None):
        roots = self._windows()
        if hwnd is not None:
            roots = [entry for entry in roots if entry[0].NativeWindowHandle == hwnd]
        else:
            compatible = [entry for entry in roots if check_version(entry[2]).supported]
            if compatible:
                roots = compatible
        if not roots:
            raise SenderError('未找到微信主窗口，请更新到官方最新版本（本程序适配 4.1.15）并登录后重试。')
        if len(roots) != 1:
            raise SenderError('检测到多个微信主窗口，请只保留需要发送的账号窗口。')
        self.window, self._path, self._version = roots[0]
        compatibility = check_version(self._version)
        if not compatibility.supported:
            raise SenderError(compatibility.message)
        activate_accessibility = self.allow_activation if allow_activation is None else allow_activation
        if activate_accessibility:
            from .accessibility import enable
            handle = self.window.NativeWindowHandle
            enable(self.window.ProcessId, handle, self._version)
            self.window = uia.ControlFromHandle(handle)
        if not any(c.ClassName == 'mmui::MainWindow' for c in walk(self.window, depth=8, budget=200, skip_lists=True)):
            raise NeedsReview('微信 4.1.15 当前没有公开可读取的无障碍控件，无法核对聊天及上传状态。'
                              '请先在助手中同意临时启用辅助功能；已同意仍无法读取时，请重新登录微信后再检测。')
        return {'version': self._version, 'path': self._path,
                'hwnd': self.window.NativeWindowHandle, 'pid': self.window.ProcessId}

    def activate(self):
        hwnd = self.window.NativeWindowHandle
        user32.ShowWindow(hwnd, 9)
        self.window.SetActive()
        for _ in range(20):
            if user32.GetForegroundWindow() == hwnd and valid_rect(self.window):
                return
            time.sleep(0.05)
        raise NeedsReview('微信未能切到前台，请手动打开微信后再试。')

    def _chrome(self):
        return list(walk(self.window, depth=40, budget=1600, skip_lists=True))

    def _layout(self):
        nodes = self._chrome()
        lists = [c for c in nodes if not c.IsOffscreen and valid_rect(c)
                 and (aid_matches(c.AutomationId, 'chat_message_list')
                      or c.ClassName == 'mmui::RecyclerListView')]
        if len(lists) != 1:
            raise NeedsReview('请在微信主窗口打开要发送的好友或群聊，再点击“使用当前聊天”。')
        self.message_list = lists[0]
        mr = self.message_list.BoundingRectangle
        edits = [c for c in nodes if c.ControlTypeName == 'EditControl' and not c.IsOffscreen
                 and c.ClassName == 'mmui::ChatInputField'
                 and aid_matches(c.AutomationId, 'chat_input_field')
                 and c.BoundingRectangle.left >= mr.left - 4
                 and c.BoundingRectangle.top >= mr.bottom - 4]
        buttons = [c for c in nodes if c.ControlTypeName == 'ButtonControl'
                   and c.Name in {'发送(S)', '发送', 'Send(S)', 'Send'}
                   and c.BoundingRectangle.top >= mr.bottom]
        if len(edits) != 1 or len(buttons) > 1:
            raise NeedsReview('无法唯一识别微信 4.1.15 的聊天输入框，请打开聊天并加宽微信窗口。')
        self.editor = edits[0]
        self.send_button = buttons[0] if buttons else None
        # The chat heading is above the message list; group size is a separate text node.
        titles = [c for c in nodes if c.ControlTypeName == 'TextControl' and c.Name.strip()
                  and c.BoundingRectangle.left >= mr.left
                  and c.BoundingRectangle.right <= mr.right
                  and c.BoundingRectangle.bottom <= mr.top + 3
                  and c.BoundingRectangle.top >= self.window.BoundingRectangle.top
                  and not c.Name.strip().startswith(('(', '（'))]
        titles.sort(key=lambda c: (c.BoundingRectangle.left, c.BoundingRectangle.top))
        candidates = editor_chat_candidates(self.editor.Name)
        matched_titles = {c.Name.strip() for c in titles if c.Name.strip() in candidates}
        if len(matched_titles) != 1:
            raise NeedsReview('无法交叉核对聊天标题与输入框的目标名称。请打开目标聊天并加宽微信窗口。')
        title = matched_titles.pop()
        if title.endswith(('...', '…')):
            raise NeedsReview('聊天名称被截断，请加宽微信窗口后重试。')
        # 4.1.15 does not expose the account avatar in its accessibility tree.
        # Bind to this process lifetime + main-window instance; never claim a
        # nickname is an account ID or silently resume across another login.
        from .winutil import process_identity
        account = process_identity(self.window.ProcessId) + ':' + rid(self.window)
        for c in nodes:
            if c.ControlTypeName in {'TextControl', 'ButtonControl'} and c.Name.strip() in OFFLINE:
                raise NeedsReview('微信显示网络连接已断开，已暂停。')
        return title, account

    def sessions(self):
        if self.window is None:
            self.connect()
        lists = self._session_lists()
        if len(lists) != 1:
            raise SenderError('无法读取最近会话，请直接在微信打开聊天后使用当前聊天。')
        sessions = []
        for row in lists[0].GetChildren():
            if row.ClassName != 'mmui::ChatSessionCell':
                continue
            title = (row.Name or '').split('\n')[0].strip()
            if title:
                sessions.append({'title': title, 'id': rid(row)})
        return sessions

    def _session_lists(self):
        return [c for c in self._chrome() if aid_matches(c.AutomationId, 'session_list')
                and not c.IsOffscreen and valid_rect(c)]

    def open_session(self, session):
        self.activate()
        lists = self._session_lists()
        rows = [c for box in lists for c in box.GetChildren() if rid(c) == session['id']]
        if len(rows) != 1:
            raise NeedsReview('最近会话已变化，请刷新后重新选择。')
        # The guarded click verifies that Qt owns the click point.
        from .qt_input import click_control
        click_control(self.window, rows[0])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                binding = self.bind_current()
                if binding['title'] == session['title']:
                    return binding
            except SenderError:
                pass
            time.sleep(0.1)
        raise NeedsReview('打开的聊天名称与所选会话不一致，未发送任何内容。请手动打开后重新绑定。')

    def bind_current(self):
        if self.window is None:
            self.connect()
        self.activate()
        title, account = self._layout()
        # Duplicate display names cannot be disambiguated safely by a title check.
        sessions = self.sessions()
        if sum(s['title'] == title for s in sessions) > 1:
            raise NeedsReview('最近会话中存在同名聊天，请先设置唯一备注名后再发送。')
        key = hashlib.sha256(f'{account}\0{title}'.encode()).hexdigest()
        return {'title': title, 'account': account, 'key': key,
                'hwnd': self.window.NativeWindowHandle, 'pid': self.window.ProcessId,
                'list_id': rid(self.message_list), 'editor_id': rid(self.editor),
                'version': self._version, 'adapter': 'qt-4.1.15', 'demo': False}

    def attach(self, binding):
        if binding.get('adapter') != 'qt-4.1.15':
            raise NeedsReview('这是旧版微信的目标记录，请在 4.1.15 中重新绑定原账号和聊天后继续。')
        self.send_shortcut = binding.get('send_shortcut', 'Enter')
        self.connect(binding['hwnd'])
        self.activate()
        self.assert_target(binding)

    def assert_target(self, binding):
        if binding.get('demo'):
            raise SenderError('禁止把演示任务发送到真实微信。')
        if not user32.IsWindow(binding['hwnd']) or self.window.ProcessId != binding['pid']:
            raise NeedsReview('微信进程已变化，已暂停。')
        if user32.IsIconic(binding['hwnd']) or user32.GetForegroundWindow() != binding['hwnd']:
            raise NeedsReview('微信失去前台焦点或已最小化，已暂停。请保持微信在前台。')
        # Qt may replace its provider while opening/closing a popup. Reacquire
        # the root; persisted chat/editor/message RuntimeIds must still agree.
        self.window = uia.ControlFromHandle(binding['hwnd'])
        title, account = self._layout()
        if (title != binding['title'] or account != binding['account']
                or rid(self.message_list) != binding['list_id']
                or rid(self.editor) != binding['editor_id']):
            raise NeedsReview('当前聊天或账号与绑定目标不一致，已停止，未继续发送。')

    def _rows(self):
        return [c for c in self.message_list.GetChildren()
                if c.ClassName != 'mmui::ChatItemView'
                and (c.ControlTypeName == 'ListItemControl' or c.ClassName.startswith('mmui::Chat'))]

    def _outgoing(self, row):
        if self._version.startswith('4.1.15'):
            # Qt does not expose avatars/direction on rows. Newly submitted
            # images must additionally pass a message-specific completion check.
            return row.ClassName in {'mmui::ChatImageItemView', 'mmui::ChatTextItemView'}
        right = self.message_list.BoundingRectangle.right
        left = self.message_list.BoundingRectangle.left
        avatars = [c for c in walk(row, depth=12, budget=200)
                   if 'Avatar' in c.ClassName and not c.IsOffscreen
                   and 15 <= c.BoundingRectangle.width() <= 100
                   and 15 <= c.BoundingRectangle.height() <= 100]
        return any(c.BoundingRectangle.left > left + (right - left) * 0.80 for c in avatars)

    def _input_value(self):
        pattern = self.editor.GetValuePattern()
        if pattern is not None:
            return pattern.Value or ''
        legacy = self.editor.GetLegacyIAccessiblePattern()
        if legacy is not None and legacy.Value is not None:
            return legacy.Value
        raise NeedsReview('微信输入框不提供可验证的内容，不能安全提交。')

    def _submit(self, binding, stop, kind, payload):
        self.assert_target(binding)
        if self._input_value().strip() or any(
                c.ControlTypeName == 'ImageControl' and not c.IsOffscreen
                for c in walk(self.editor, depth=8, budget=100)):
            raise NeedsReview('当前聊天输入框有草稿，请先处理草稿再开始；程序不会清空草稿。')
        baseline = [rid(row) for row in self._rows()]
        if kind == 'image':
            copy_image(payload)
        else:
            copy_text(payload)
        if stop.is_set():
            raise Stopped('已停止，尚未粘贴内容。')
        self.assert_target(binding)
        self.editor.SetFocus()
        if not self.editor.HasKeyboardFocus:
            from .qt_input import click_control
            click_control(self.window, self.editor, stop=stop)
            if not self.editor.HasKeyboardFocus:
                raise NeedsReview('输入框没有获得焦点。')
        self.editor.SendKeys('{Ctrl}v', waitTime=0)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if stop.is_set():
                raise Stopped('已停止，粘贴内容保留在微信草稿中。')
            self.assert_target(binding)
            value = self._input_value()
            if kind == 'text' and value == payload:
                break
            if kind == 'image' and (value.strip() in IMAGE_NAMES or '\ufffc' in value
                                    or any(c.ControlTypeName == 'ImageControl'
                                           for c in walk(self.editor, depth=4, budget=30))):
                break
            stop.wait(0.1)
        else:
            raise NeedsReview('无法确认输入框已载入完整图片或文字。内容保留在草稿中，未点击发送。')
        if stop.is_set():
            raise Stopped('已停止，尚未点击发送。')
        self.assert_target(binding)
        if self.send_button is not None and not self.send_button.IsEnabled:
            raise NeedsReview('微信发送按钮不可用。')
        # Send once through the configured WeChat shortcut. A successful keypress
        # is NOT an acknowledgement; observe() must find this exact new message.
        self._invoke_send(stop)
        return {'kind': kind, 'before': baseline, 'id': None,
                'text': payload if kind == 'text' else '', 'seen_busy': False,
                'image_path': str(payload) if kind == 'image' else ''}

    def _invoke_send(self, stop=None):
        shortcuts = {'Enter': '{Enter}', 'Ctrl+Enter': '{Ctrl}{Enter}'}
        if self.send_shortcut not in shortcuts:
            raise SenderError('未知发送快捷键，请选择 Enter 或 Ctrl+Enter。')
        self.editor.SetFocus()
        if not self.editor.HasKeyboardFocus:
            raise NeedsReview('微信输入框未获得焦点，未提交。')
        if stop is not None and stop.is_set():
            raise Stopped('已停止，内容保留在草稿中。')
        self.editor.SendKeys(shortcuts[self.send_shortcut], waitTime=0)

    def submit_image(self, path, binding, stop):
        return self._submit(binding, stop, 'image', path)

    def submit_text(self, text, binding, stop):
        return self._submit(binding, stop, 'text', text)

    def observe(self, token, binding):
        if self.stop is not None and self.stop.is_set():
            raise Stopped('已停止，已提交的图片仍可能继续上传。')
        self.assert_target(binding)
        rows = self._rows()
        current = {rid(c): c for c in rows}
        if token['id'] is None:
            before = set(token['before'])
            if before and not before.intersection(current):
                raise NeedsReview('消息列表被重新加载，无法可靠匹配本次发送的消息。')
            own_new = [row for key, row in current.items() if key not in before and self._outgoing(row)]
            if len(own_new) > 1:
                raise NeedsReview('检测到多条新发出消息，无法唯一对应当前图片。请勿在其他设备同时发送。')
            if not own_new:
                # A stable temporary ID avoids treating an absent bubble as a completion signal.
                return Observation('', State.UNKNOWN, '等待本次发送的消息出现在聊天中')
            row = own_new[0]
            if token['kind'] == 'image' and row.Name.strip() not in IMAGE_NAMES:
                raise NeedsReview('新消息没有显示为图片，已停止，未发送文件名。')
            if token['kind'] == 'text' and row.Name != token['text']:
                raise NeedsReview('新文字消息与预期文件名不完全一致，已停止。')
            token['id'] = rid(row)
        if token['id'] not in current:
            raise NeedsReview('当前消息已不在可见列表中，不能把消息消失判断为成功。')
        row = current[token['id']]
        if not valid_rect(row) or row.IsOffscreen:
            raise NeedsReview('当前消息已滚出可见区域，不能确认发送状态。')
        if token['kind'] == 'image' and row.Name.strip() not in IMAGE_NAMES:
            raise NeedsReview('图片消息控件内容发生变化，已暂停。')
        if token['kind'] == 'text' and row.Name != token['text']:
            raise NeedsReview('文件名消息控件内容发生变化，已暂停。')
        nodes = list(walk(row, depth=14, budget=300))
        signals = [Signal(c.ControlTypeName, c.Name, c.ItemStatus or '',
                          not c.IsOffscreen and valid_rect(c), is_message=(index == 0))
                   for index, c in enumerate(nodes)]
        state = message_state(signals)
        if state == State.FAILED:
            return Observation(token['id'], State.FAILED, '微信显示本条消息发送失败')
        if state == State.SENDING:
            token['seen_busy'] = True
            return Observation(token['id'], State.SENDING, '微信显示正在发送，继续等待')
        if state == State.SENT:
            return Observation(token['id'], State.SENT, '微信明确显示本条消息已发送')
        if token['kind'] == 'image':
            if token.get('image_path'):
                from PIL import ImageGrab
                from .qt_visual import prepare_source, image_state
                path = token['image_path']
                if path not in self._source_images:
                    # Keep only the current image, not an entire folder in memory.
                    self._source_images = {path: prepare_source(path)}
                rect, box = row.BoundingRectangle, self.message_list.BoundingRectangle
                if rect.top < box.top or rect.bottom > box.bottom or rect.left < box.left or rect.right > box.right:
                    raise NeedsReview('图片消息没有完整显示在聊天区域内，请勿滚动消息列表。')
                dpi = user32.GetDpiForWindow(binding['hwnd'])
                frame = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom))
                result = image_state(frame, self._source_images[path], dpi, self._version)
                self.assert_target(binding)
                if result.state == State.SENDING:
                    token['seen_busy'] = True
                    return Observation(token['id'], State.SENDING, result.detail)
                if result.state == State.FAILED:
                    return Observation(token['id'], State.FAILED, result.detail)
                if result.state == State.CLEAR and token['seen_busy']:
                    return Observation(token['id'], State.CLEAR,
                                       '已观察到本条图片上传，当前图片匹配且上传指示已消失；等待状态稳定')
                if result.state == State.CLEAR and result.matched:
                    from .qt_menu import read_image_menu, completed_image_menu
                    items = read_image_menu(self, binding, row, self._source_images[path], dpi)
                    if completed_image_menu(items, result.state, self._version):
                        return Observation(token['id'], State.SENT,
                                           '本条图片内容匹配，上传指示已清空，且完成后菜单操作均已可用')
                if result.state == State.UNKNOWN:
                    return Observation(token['id'], State.UNKNOWN, result.detail)
            return Observation(token['id'], State.UNKNOWN,
                               '未捕捉到本条图片的上传过程或明确完成状态，保持未确认；超时后需人工核实')
        if token['kind'] == 'text':
            return Observation(token['id'], State.SENT, '文件名已提交，并在本次发出的文字消息中核对一致')
        return Observation(token['id'], State.UNKNOWN,
                           '图片/文字已出现在本机聊天中，但微信未暴露完成状态；继续等待，超时将暂停')

    def diagnose(self):
        info = self.version_report()
        info.update(target_version=TARGET_VERSION, ui_readable=False, sessions_readable=False,
                    completion_mode='explicit-status-or-upload-transition-or-complete-menu',
                    live_validation='filehelper-staged-image-and-caption-verified')
        if not info['supported']:
            return info
        try:
            info.update(self.connect(allow_activation=False))
            info['ui_readable'] = True
            info['sessions_readable'] = bool(self.sessions())
            info['minimized'] = bool(user32.IsIconic(info['hwnd']))
        except SenderError as exc:
            info['message'] = str(exc)
        return info
