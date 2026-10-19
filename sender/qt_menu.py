"""Read a pinned image's context menu; never invoke a menu action."""
import ctypes
from ctypes import wintypes
import time

import uiautomation as uia

from .core import NeedsReview, Stopped, State
from .qt_input import click_point
from .winutil import user32


def completed_image_menu(items, visual_state, version):
    """Measured 4.1.15.53 signature, gated by a clear matched-image snapshot.

During the authorized upload the menu exposed only forward/multiselect/open/
delete. Copy/edit/favorite/quote/save appeared after the upload finished.
Never infer completion from forward alone or from absence of a cancel item.
"""
    if version != '4.1.15.53' or visual_state != State.CLEAR:
        return False
    labels = [item['name'] for item in items]
    if len(set(labels)) != len(labels):
        return False
    if set(labels) & {'发送中', '取消发送', '重新发送', '重发', '发送失败'}:
        return False
    enabled = {item['name'] for item in items if item['enabled']}
    return {'复制', '编辑', '收藏', '引用', '另存为...', '用窗口打开'} <= enabled


def _menus(pid):
    from .wechat import rid, walk
    handles = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def collect(hwnd, unused):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            handles.append(hwnd)
        return True
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows(callback_type(collect), 0)
    found = {}
    for hwnd in handles:
        root = uia.ControlFromHandle(hwnd)
        if root.ClassName == 'mmui::XMenu' and not root.IsOffscreen:
            found[rid(root)] = (root, list(walk(root, depth=8, budget=100)))
    return found


def read_image_menu(backend, binding, row, source, dpi):
    from .wechat import rid
    backend.assert_target(binding)
    if backend.stop is not None and backend.stop.is_set():
        raise Stopped('已停止，尚未检查消息菜单。')
    if _menus(binding['pid']):
        raise NeedsReview('微信已有打开的菜单，请先关闭后再继续。')
    identity = rid(row)
    rect = row.BoundingRectangle
    scale = dpi / 96
    thumb = source.copy()
    thumb.thumbnail((round(240 * scale), round(240 * scale)))
    x = rect.right - round(70 * scale) - thumb.width / 2
    y = rect.top + round(8 * scale) + thumb.height / 2
    # The caller must first verify this source thumbnail in this exact row.
    if not (rect.left < x < rect.right and rect.top < y < rect.bottom):
        raise NeedsReview('图片菜单定位超出当前消息范围。')
    click_point(binding['hwnd'], binding['pid'], x, y, right=True, stop=backend.stop)
    menus = {}
    try:
        for _ in range(6):
            menus = _menus(binding['pid'])
            if menus:
                break
            time.sleep(.05)
        if not menus:
            return []
        if len(menus) != 1:
            raise NeedsReview('出现多个菜单，不能对应到当前图片。')
        menu, nodes = next(iter(menus.values()))
        mr = menu.BoundingRectangle
        if not (mr.left - 40 * scale <= x <= mr.right + 40 * scale
                and mr.top - 40 * scale <= y <= mr.bottom + 40 * scale):
            raise NeedsReview('菜单位置与当前图片不匹配。')
        if rid(row) != identity:
            raise NeedsReview('图片控件在检查菜单时被替换。')
        return [{'name': n.Name.strip(), 'enabled': bool(n.IsEnabled)} for n in nodes
                if n.ControlTypeName == 'MenuItemControl' and n.ClassName == 'mmui::XMenuView'
                and not n.IsOffscreen]
    finally:
        if menus:
            foreground = user32.GetForegroundWindow()
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(foreground, ctypes.byref(owner))
            if owner.value != binding['pid']:
                raise NeedsReview('检查菜单时焦点被切换，已停止。')
            uia.SendKeys('{Esc}', waitTime=0)
            time.sleep(.08)
            if _menus(binding['pid']):
                raise NeedsReview('图片菜单未能关闭，已停止。')
        backend.assert_target(binding)
