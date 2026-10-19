"""Version-specific interpretation of an observed WeChat image-message tree.

Verified on 3.9.12.55: the transfer indicator is an unnamed button, sibling of
the image content pane. A transfer/error button blocks completion regardless
of its glyph or label. Unknown layouts never produce a completion result.
"""
from __future__ import annotations

from dataclasses import dataclass

from .core import State


@dataclass(frozen=True)
class Node:
    kind: str
    name: str
    visible: bool
    children: tuple['Node', ...] = ()


def image_state(root: Node, version: str, account: str) -> State:
    if version != '3.9.12.55' or root.kind != 'ListItemControl' or root.name != '[图片]' or not root.visible:
        return State.UNKNOWN
    if len(root.children) != 1 or root.children[0].kind != 'PaneControl':
        return State.UNKNOWN
    parts = root.children[0].children
    if len(parts) != 3 or [p.kind for p in parts] != ['PaneControl', 'PaneControl', 'ButtonControl']:
        return State.UNKNOWN
    if not parts[2].visible or not account or parts[2].name != account:
        return State.UNKNOWN
    outer = parts[1]
    if len(outer.children) != 1 or outer.children[0].kind != 'PaneControl':
        return State.UNKNOWN
    content = outer.children[0].children
    # A spinning upload indicator and an unlabeled retry icon are both blockers.
    if any(n.kind == 'ButtonControl' for n in content):
        return State.SENDING
    if len(content) != 1 or content[0].kind != 'PaneControl' or not content[0].visible:
        return State.UNKNOWN
    stack = [content[0]]
    image_buttons = []
    while stack:
        node = stack.pop()
        if node.kind == 'ButtonControl':
            image_buttons.append(node)
        elif node.kind != 'PaneControl':
            return State.UNKNOWN
        stack.extend(node.children)
    if len(image_buttons) != 1 or image_buttons[0].name or not image_buttons[0].visible:
        return State.UNKNOWN
    return State.SENT
