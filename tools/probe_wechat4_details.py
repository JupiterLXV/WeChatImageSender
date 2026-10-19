"""Inspect only control semantics in the currently open File Transfer chat."""
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from sender.accessibility import enable, restore_all
from sender.wechat import WeChatBackend, walk, rid
import uiautomation as uia

sys.stdout.reconfigure(encoding='utf-8')
b = WeChatBackend()
w, _, version = b._windows()[0]
try:
    enable(w.ProcessId, w.NativeWindowHandle, version)
    root = uia.ControlFromHandle(w.NativeWindowHandle)
    nodes = list(walk(root, depth=40, budget=2000, skip_lists=True))
    headings = [c for c in nodes if c.AutomationId.endswith('current_chat_name_label')]
    if len(headings) != 1 or headings[0].Name != '文件传输助手':
        raise RuntimeError('Only the authorized File Transfer chat may be inspected')
    result=[]
    candidates=[c for c in nodes if c.AutomationId.endswith('session_list') or c.AutomationId.endswith('chat_message_list')]
    for control in candidates:
        for row in control.GetChildren()[-3:]:
            item={'class': row.ClassName, 'kind': row.ControlTypeName, 'name_length':len(row.Name),
                  'children':len(row.GetChildren()), 'rid':rid(row), 'rect':str(row.BoundingRectangle),
                  'status':row.ItemStatus, 'help':row.HelpText,
                  'value_pattern':bool(row.GetValuePattern()),
                  'selection_pattern':bool(row.GetSelectionItemPattern()),
                  'invoke_pattern':bool(row.GetInvokePattern())}
            p=row.GetLegacyIAccessiblePattern()
            if p:
                item['legacy']={key:getattr(p,key) for key in ('Role','State','Value','Description','DefaultAction')}
            result.append(item)
    print(json.dumps({'root_class':root.ClassName,'root_hwnd':root.NativeWindowHandle,
                      'rows':result},ensure_ascii=False,indent=2))
finally:
    restore_all()
