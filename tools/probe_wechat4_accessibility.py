"""Temporarily enable the verified accessibility flag and save a redacted tree."""
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from sender.accessibility import enable, restore_all
from sender.wechat import WeChatBackend
from tools.inspect_wechat4 import inspect

backend = WeChatBackend()
windows = backend._windows()
if len(windows) != 1:
    raise RuntimeError('Expected exactly one main WeChat window')
window, _, version = windows[0]
try:
    enable(window.ProcessId, window.NativeWindowHandle, version)
    result = inspect()
    path = PROJECT / 'data' / 'wechat4-tree.json'
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'nodes': len(result), 'output': str(path)}))
finally:
    restore_all()
