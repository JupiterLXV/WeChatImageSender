"""Open only File Transfer Assistant through a verified search. Sends nothing."""
import json
from pathlib import Path
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
import uiautomation as uia
from sender.compatibility import aid_matches
from sender.wechat import WeChatBackend, walk
from sender.qt_input import click_control
from sender.accessibility import restore_all
from sender.winutil import dpi_aware, copy_text


def open_filehelper(backend):
    target = '文件传输助手'
    backend.connect()
    backend.activate()
    try:
        binding = backend.bind_current()
        if binding['title'] == target:
            return binding
    except Exception:
        pass
    candidates = [s for s in backend.sessions() if s['title'] == target]
    if len(candidates) == 1:
        return backend.open_session(candidates[0])
    searches = [c for c in backend._chrome() if c.ControlTypeName == 'EditControl'
                and c.Name == '搜索' and not c.IsOffscreen]
    if len(searches) != 1:
        raise RuntimeError('Search field is not uniquely available')
    search = searches[0]
    click_control(backend.window, search)
    if not search.HasKeyboardFocus:
        raise RuntimeError('Search field did not receive focus')
    copy_text(target)
    search.SendKeys('{Ctrl}a{Ctrl}v', waitTime=0)
    candidates = []
    for _ in range(30):
        boxes = [c for c in backend._chrome() if aid_matches(c.AutomationId, 'search_list')]
        candidates = [c for box in boxes for c in box.GetChildren()
                      if c.Name.split('\n')[0].strip() == target
                      and 'search_item_' in c.AutomationId]
        if len(candidates) == 1:
            break
        time.sleep(.1)
    if len(candidates) != 1:
        raise RuntimeError('File Transfer Assistant search result is not unique')
    click_control(backend.window, candidates[0])
    time.sleep(.3)
    binding = backend.bind_current()
    if binding['title'] != target:
        raise RuntimeError('Opened chat does not match File Transfer Assistant')
    return binding


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    dpi_aware()
    try:
        backend = WeChatBackend(allow_activation=True)
        binding = open_filehelper(backend)
        print(json.dumps({'target': binding['title'], 'version': binding['version'],
                          'input_empty': not backend._input_value()}, ensure_ascii=False))
    finally:
        restore_all()
