"""Inspect an image context menu in File Transfer Assistant; never select an action."""
import ctypes
import json
import sys
import time
from ctypes import wintypes
from pathlib import Path

import uiautomation as uia

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from sender.accessibility import enable, restore_all
from sender.wechat import WeChatBackend, walk
from sender.winutil import user32, dpi_aware
from sender.qt_input import click_control

sys.stdout.reconfigure(encoding='utf-8')
dpi_aware()
b = WeChatBackend()
w, _, version = b._windows()[0]
user32.SetWindowPos.argtypes=[wintypes.HWND,wintypes.HWND,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,wintypes.UINT]
user32.SetWindowPos(w.NativeWindowHandle,None,100,80,1100,800,0x14)
try:
    enable(w.ProcessId, w.NativeWindowHandle, version)
    w = uia.ControlFromHandle(w.NativeWindowHandle)
    w.SetActive()
    nodes = list(walk(w, depth=40, budget=2000, skip_lists=True))
    headings = [c for c in nodes if c.AutomationId.endswith('current_chat_name_label')]
    if len(headings) != 1 or headings[0].Name != '文件传输助手':
        sessions=[c for c in nodes if c.AutomationId=='session_list']
        helpers=[c for s in sessions for c in s.GetChildren() if c.AutomationId.endswith('session_item_文件传输助手') or c.Name.split('\n')[0].strip()=='文件传输助手']
        if len(helpers)==1:
            click_control(w,helpers[0])
            time.sleep(0.4)
            nodes=list(walk(w,depth=40,budget=2000,skip_lists=True))
            headings=[c for c in nodes if c.AutomationId.endswith('current_chat_name_label')]
    if len(headings) != 1 or headings[0].Name != '文件传输助手':
        print('headings',len(headings),'sessions',len(sessions),'helpers',len(helpers))
        print('search_controls',[(c.ControlTypeName,c.ClassName,c.Name=='搜索') for c in nodes if 'Search' in c.ClassName or c.Name=='搜索'])
        raise RuntimeError('Expected the authorized File Transfer chat')
    box = next(c for c in nodes if c.AutomationId == 'chat_message_list')
    row = [r for r in box.GetChildren() if r.ClassName == 'mmui::ChatImageItemView'][-1]
    handles=[]
    callback_type=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    def collect(hwnd, data):
        name=ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd,name,256)
        if name.value in {'MMUIRenderSubWindowHW','MMUIRenderSubWindow'}:
            handles.append(hwnd)
        return True
    user32.EnumChildWindows.argtypes=[wintypes.HWND,callback_type,wintypes.LPARAM]
    collect(w.NativeWindowHandle,0)
    user32.EnumChildWindows(w.NativeWindowHandle,callback_type(collect),0)
    if len(handles)!=1:
        raise RuntimeError(f'Expected one rendering window, found {len(handles)}')
    hwnd=handles[0]
    origin=wintypes.RECT()
    user32.GetWindowRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect(hwnd,ctypes.byref(origin))
    r=row.BoundingRectangle
    row.CaptureToImage(str(PROJECT/'data'/'wechat4-menu-row.png'))
    x, y = r.right - 125 - origin.left, r.top + 45 - origin.top
    user32.GetWindowLongPtrW.argtypes=[wintypes.HWND,ctypes.c_int]
    user32.GetWindowLongPtrW.restype=ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes=[wintypes.HWND,ctypes.c_int,ctypes.c_ssize_t]
    user32.SetWindowLongPtrW.restype=ctypes.c_ssize_t
    user32.WindowFromPoint.argtypes=[wintypes.POINT]
    user32.WindowFromPoint.restype=wintypes.HWND
    style=user32.GetWindowLongPtrW(hwnd,-20)
    try:
        user32.SetWindowLongPtrW(hwnd,-20,style & ~0x20)
        point=wintypes.POINT(int(x+origin.left),int(y+origin.top))
        hit=user32.WindowFromPoint(point)
        owner=wintypes.DWORD()
        user32.GetWindowThreadProcessId(hit,ctypes.byref(owner))
        if owner.value!=w.ProcessId:
            raise RuntimeError('The click point belongs to another application')
        user32.SetCursorPos(point.x,point.y)
        user32.mouse_event(8,0,0,0,0)
        user32.mouse_event(16,0,0,0,0)
        time.sleep(0.2)
    finally:
        user32.SetWindowLongPtrW(hwnd,-20,style)
    time.sleep(0.4)
    records=[]
    for root in uia.GetRootControl().GetChildren():
        if root.ProcessId != w.ProcessId:
            continue
        for node in walk(root,depth=40,budget=2000,skip_lists=True):
            if 'Menu' in node.ControlTypeName or 'Menu' in node.ClassName:
                records.append({'type':node.ControlTypeName,'class':node.ClassName,'name':node.Name,
                                'enabled':node.IsEnabled,'rect':str(node.BoundingRectangle)})
    print(json.dumps(records,ensure_ascii=False,indent=2))
    uia.SendKeys('{Esc}',waitTime=0)
finally:
    restore_all()
