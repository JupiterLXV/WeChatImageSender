"""Conservative status interpretation for Qt message accessibility snapshots.

The 3.9 image-tree disappearance rule is deliberately not reused. This module
only accepts explicit accessibility status. The independent visual fallback
requires matching image content and a positively observed upload transition.
"""
from dataclasses import dataclass

from .core import State

FAILED = {'发送失败', '重新发送', '重发', '未发送', '发送失败，点击重新发送',
          'Failed', 'Send failed', 'Resend', 'Not sent'}
BUSY = {'发送中', '发送中...', '正在发送', '正在上传', '上传中', '取消发送',
        'Sending', 'Sending...', 'Uploading', 'Cancel sending'}
DONE = {'已发送', '发送成功', '发送完成', 'Sent', 'Delivered'}


@dataclass(frozen=True)
class Signal:
    kind: str
    name: str = ''
    status: str = ''
    visible: bool = True
    is_message: bool = False


def message_state(signals: list[Signal]) -> State:
    # Names from message text and avatars are content, never success signals.
    # ItemStatus is accepted only from the row itself or a status/progress node.
    semantic = {signal.status.strip() for signal in signals if signal.visible and
                (signal.is_message or signal.kind in {'ProgressBarControl', 'StatusBarControl'})
                and signal.status}
    indicator_names = {signal.name.strip() for signal in signals if signal.visible and
                       signal.kind in {'ProgressBarControl', 'StatusBarControl'} and signal.name}
    retry_names = {signal.name.strip() for signal in signals if signal.visible and
                   signal.kind == 'ButtonControl' and signal.name}
    if (semantic | indicator_names | retry_names) & FAILED:
        return State.FAILED
    if (semantic | indicator_names | retry_names) & BUSY or any(
            s.visible and s.kind == 'ProgressBarControl' for s in signals):
        return State.SENDING
    if semantic & DONE or indicator_names & DONE:
        return State.SENT
    return State.UNKNOWN
