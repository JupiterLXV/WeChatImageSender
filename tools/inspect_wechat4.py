"""Read-only Weixin UI inspection. Names/identifiers are redacted by default."""
import json
import sys
from pathlib import Path

import uiautomation as uia

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sender.winutil import dpi_aware, process_path, version_of

KNOWN = {'微信', 'Weixin', 'WeChat', '文件传输助手', '搜索', '消息', '聊天', '会话', '发送',
         '发送(S)', '图片', '[图片]', '发送中', '正在发送', '正在上传', '上传中', '已发送',
         '发送完成', '发送成功', '发送失败', '重新发送', '登录', '进入微信', '头像', '我的头像',
         '按住鼠标 语音输入文字', '文件传输助手按住鼠标 语音输入文字'}


def inspect():
    dpi_aware()
    output = []
    for root in uia.GetRootControl().GetChildren():
        try:
            path = process_path(root.ProcessId)
        except Exception:
            continue
        if Path(path).name.lower() != 'weixin.exe':
            continue
        version = version_of(path)
        stack = [(root, 0)]
        count = 0
        while stack and count < 1800:
            node, depth = stack.pop()
            count += 1
            name = node.Name or ''
            aid = node.AutomationId or ''
            for prefix in ('session_item_', 'search_item_'):
                if prefix in aid:
                    aid = aid.split(prefix, 1)[0] + prefix + '<redacted>'
            output.append({'depth': depth, 'type': node.ControlTypeName, 'class': node.ClassName,
                           'name': name if name in KNOWN else f'<text:{len(name)}>',
                           'id': aid, 'rect': str(node.BoundingRectangle),
                           'status': node.ItemStatus if node.ItemStatus in KNOWN else '',
                           'offscreen': node.IsOffscreen,
                           'hwnd': node.NativeWindowHandle if depth == 0 else None,
                           'version': version if depth == 0 else None})
            if depth < 40:
                children = node.GetChildren()
                if node.ControlTypeName == 'ListControl':
                    children = children[-2:]
                stack.extend((c, depth + 1) for c in reversed(children))
    return output


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps(inspect(), ensure_ascii=False, indent=2))
