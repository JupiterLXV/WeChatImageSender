"""Version policy is shared by the GUI, command-line checks and send guard."""
from __future__ import annotations

import re
from dataclasses import dataclass

APP_VERSION = '2.0.1'
TARGET_VERSION = '4.1.15'
DOWNLOAD_URL = 'https://pc.weixin.qq.com/'
UPDATE_REMINDER = '使用前请将电脑微信更新到官方最新版本。本程序适配 Windows 微信 4.1.15。'


@dataclass(frozen=True)
class Compatibility:
    version: str
    supported: bool
    message: str


def check_version(version: str) -> Compatibility:
    text = (version or '').strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:\.\d+)?', text):
        return Compatibility(text, False, '无法读取微信版本，请更新并登录微信 4.1.15 后重新检测。')
    parts = tuple(int(part) for part in text.split('.'))
    target = (4, 1, 15)
    if parts[:3] < target:
        return Compatibility(text, False,
                             f'检测到微信 {text}，请先将微信更新到官方最新版本（本程序适配 4.1.15）再使用。')
    if parts[:3] > target:
        return Compatibility(text, False,
                             f'检测到微信 {text}，高于本程序的适配版本 4.1.15，请先更新发送助手。')
    return Compatibility(text, True, f'检测到微信 {text}；发送前还需通过聊天界面和状态读取检查。')


def aid_matches(value: str, segment: str) -> bool:
    """Qt may qualify an automation id with its parent path."""
    return (value or '') == segment or (value or '').endswith('.' + segment)


def editor_chat_candidates(name: str) -> set[str]:
    # Preserve the original candidate so a real name ending with the hint is
    # never silently changed. A separate header/selected session must agree.
    name = (name or '').strip()
    candidates = {name} if name else set()
    for pattern in (r'(?:(?:按住|长按)鼠标\s*)?语音输入(?:文字)?$', r'语音输入(?:文字)?$'):
        match = re.search(pattern, name)
        if match and name[:match.start()].strip():
            candidates.add(name[:match.start()].strip())
    return candidates
