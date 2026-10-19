from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from PIL import Image

EXTENSIONS = {'.jpg', '.jpeg', '.png', '.bmp', '.webp', '.tif', '.tiff', '.gif'}


class SenderError(RuntimeError):
    pass


class Stopped(SenderError):
    pass


class NeedsReview(SenderError):
    pass


class State(str, Enum):
    UNKNOWN = 'unknown'
    SENDING = 'sending'
    SENT = 'sent'
    FAILED = 'failed'
    CLEAR = 'clear'  # Only usable after a positive sending signal for this exact row.


@dataclass(frozen=True)
class Observation:
    row_id: str
    state: State
    detail: str = ''


class CompletionGate:
    """A thumbnail, empty input, elapsed time, or missing row is never an ACK."""

    def __init__(self, stable_seconds: float = 1.5):
        self.row_id: str | None = None
        self.saw_sending = False
        self.since: float | None = None
        self.stable_seconds = stable_seconds

    def feed(self, observation: Observation, now: float) -> bool:
        if not observation.row_id and observation.state == State.UNKNOWN and self.row_id is None:
            return False
        if self.row_id is None:
            self.row_id = observation.row_id
        if not observation.row_id or observation.row_id != self.row_id:
            raise NeedsReview('消息标识发生变化，无法确认这一条消息的发送状态。')
        if observation.state == State.FAILED:
            raise NeedsReview(observation.detail or '微信显示发送失败，已停止。')
        if observation.state == State.SENDING:
            self.saw_sending = True
            self.since = None
            return False
        ready = observation.state == State.SENT or (
            observation.state == State.CLEAR and self.saw_sending
        )
        if not ready:
            self.since = None
            return False
        if self.since is None:
            self.since = now
        return now - self.since >= self.stable_seconds


@dataclass
class Photo:
    path: str
    name: str
    sha256: str
    size: int
    phase: str = 'pending'
    token: dict | None = None
    proof: str = ''


def file_hash(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def natural_key(value: str):
    return tuple((0, int(p)) if p.isdigit() else (1, p.casefold())
                 for p in re.split(r'(\d+)', value))


def scan_folder(folder: Path, recursive: bool, include_extension: bool,
                stop: threading.Event | None = None) -> list[Photo]:
    folder = folder.resolve(strict=True)
    if not folder.is_dir():
        raise SenderError('请选择一个存在的图片文件夹。')
    paths = folder.rglob('*') if recursive else folder.iterdir()
    ordered = sorted((p for p in paths if p.is_file() and p.suffix.lower() in EXTENSIONS),
                     key=lambda p: (natural_key(str(p.relative_to(folder))), str(p)))
    photos = []
    for path in ordered:
        if stop is not None and stop.is_set():
            raise Stopped('已停止读取文件夹。')
        if any(ord(c) < 32 for c in path.name):
            raise SenderError(f'文件名包含换行等控制字符：{path.name!r}')
        try:
            with Image.open(path) as image:
                if getattr(image, 'n_frames', 1) != 1:
                    raise SenderError(f'{path.name} 是动图或多页图片，请先转换为单张静态图片。')
                image.verify()
        except SenderError:
            raise
        except Exception as exc:
            raise SenderError(f'图片无法读取：{path.name}（{exc}）') from exc
        photos.append(Photo(str(path.resolve()), path.name if include_extension else path.stem,
                            file_hash(path), path.stat().st_size))
    if not photos:
        raise SenderError('文件夹中没有支持的静态图片。支持 JPG、PNG、BMP、WEBP、TIFF、静态 GIF。')
    return photos


class Journal:
    """One immutable queue with atomic, write-before-send checkpoints."""

    def __init__(self, path: Path, data: dict):
        self.path = path
        self.data = data
        self.photos = [Photo(**p) for p in data['photos']]

    @classmethod
    def create(cls, directory: Path, binding: dict, photos: list[Photo], demo=False):
        path = directory / f'{time.strftime("%Y%m%d-%H%M%S")}-{uuid.uuid4().hex[:8]}.json'
        journal = cls(path, {'schema': 1, 'binding': binding, 'demo': demo,
                             'created': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                             'photos': [asdict(p) for p in photos], 'events': []})
        journal.save()
        return journal

    @classmethod
    def load(cls, path: Path):
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('schema') != 1 or not isinstance(data.get('photos'), list):
            raise SenderError('不支持的任务记录格式。')
        journal = cls(path, data)
        phases = {'pending', 'image_attempting', 'image_submitted', 'image_confirmed',
                  'name_attempting', 'name_submitted', 'done'}
        if any(photo.phase not in phases for photo in journal.photos):
            raise SenderError('任务记录含未知阶段，不能自动恢复。')
        return journal

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data['photos'] = [asdict(p) for p in self.photos]
        temporary = self.path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as handle:
            json.dump(self.data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def checkpoint(self, photo: Photo, phase: str, **fields):
        previous = asdict(photo)
        photo.phase = phase
        for key, value in fields.items():
            setattr(photo, key, value)
        try:
            self.save()
        except Exception:
            # A failed disk write must not leave a more permissive in-memory phase.
            for key, value in previous.items():
                setattr(photo, key, value)
            raise

    def resolve(self, index: int, decision: str):
        photo = self.photos[index]
        if decision == 'image_sent':
            if photo.phase not in {'image_attempting', 'image_submitted'}:
                raise SenderError('当前阶段不能使用“仅补发名称”。')
            phase = 'image_confirmed'
        elif decision == 'both_sent':
            if photo.phase not in {'name_attempting', 'name_submitted'}:
                raise SenderError('当前阶段不能标记图片与名称均已完成。')
            phase = 'done'
        elif decision == 'not_sent':
            if photo.phase in {'image_attempting', 'image_submitted'}:
                phase = 'pending'
            elif photo.phase in {'name_attempting', 'name_submitted'}:
                phase = 'image_confirmed'
            else:
                raise SenderError('当前阶段无需重试。')
        else:
            raise SenderError('未知确认操作。')
        self.data['events'].append({'time': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                                    'index': index, 'from': photo.phase,
                                    'decision': decision, 'to': phase})
        self.checkpoint(photo, phase, token=None, proof='用户逐条核实')


class Backend(Protocol):
    def assert_target(self, binding: dict) -> None: ...
    def submit_image(self, path: Path, binding: dict, stop: threading.Event) -> dict: ...
    def submit_text(self, text: str, binding: dict, stop: threading.Event) -> dict: ...
    def observe(self, token: dict, binding: dict) -> Observation: ...


def wait_confirmed(backend: Backend, token: dict, binding: dict, stop: threading.Event,
                   timeout: float, emit: Callable, poll=0.15, stable=1.5):
    gate = CompletionGate(stable)
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        if stop.is_set():
            raise Stopped('已停止；已提交的消息仍可能由微信继续发送。')
        backend.assert_target(binding)
        observation = backend.observe(token, binding)
        if observation.state != previous:
            emit('status', observation.detail or observation.state.value)
            previous = observation.state
        if gate.feed(observation, time.monotonic()):
            return observation.detail or '微信客户端状态已确认'
        stop.wait(poll)
    raise NeedsReview('在设定时间内未取得可靠的发送完成状态。已暂停，需核实当前消息；不会自动重发。')


def run_queue(journal: Journal, backend: Backend, stop: threading.Event,
              emit: Callable, timeout=90.0, interval=1.0, stable=1.5):
    binding = journal.data['binding']
    for index, photo in enumerate(journal.photos):
        if stop.is_set():
            raise Stopped('已停止。')
        if photo.phase == 'done':
            continue
        if photo.phase not in {'pending', 'image_confirmed'}:
            raise NeedsReview('上次发送的结果尚未确认。请先处理待核实项，以免重复发送。')
        emit('item', (index, photo.phase))
        backend.assert_target(binding)
        if photo.phase == 'pending':
            path = Path(photo.path)
            if not path.is_file() or file_hash(path) != photo.sha256:
                raise SenderError(f'图片在预览后被修改或移走，请重新读取文件夹：{path.name}')
            if stop.is_set():
                raise Stopped('已停止。')
            journal.checkpoint(photo, 'image_attempting', token=None)
            emit('phase', (index, photo.phase))
            token = backend.submit_image(path, binding, stop)
            journal.checkpoint(photo, 'image_submitted', token=token)
            emit('phase', (index, photo.phase))
            proof = wait_confirmed(backend, token, binding, stop, timeout, emit, stable=stable)
            journal.checkpoint(photo, 'image_confirmed', proof=proof, token=None)
            emit('phase', (index, photo.phase))
        if stop.is_set():
            raise Stopped('图片已确认完成，尚未发送名称。')
        backend.assert_target(binding)
        journal.checkpoint(photo, 'name_attempting', token=None)
        emit('phase', (index, photo.phase))
        token = backend.submit_text(photo.name, binding, stop)
        journal.checkpoint(photo, 'name_submitted', token=token)
        emit('phase', (index, photo.phase))
        proof = wait_confirmed(backend, token, binding, stop, timeout, emit, stable=stable)
        journal.checkpoint(photo, 'done', proof=proof, token=None)
        emit('phase', (index, photo.phase))
        if stop.wait(interval):
            raise Stopped('已停止。')
    emit('complete', len(journal.photos))


class DemoBackend:
    def __init__(self):
        self.tokens = {}
        self.counter = 0

    def assert_target(self, binding):
        if not binding.get('demo'):
            raise SenderError('演示模式禁止连接真实任务。')

    def _submit(self, kind):
        self.counter += 1
        token = {'id': f'demo-{self.counter}', 'kind': kind}
        self.tokens[token['id']] = time.monotonic()
        return token

    def submit_image(self, path, binding, stop):
        return self._submit('image')

    def submit_text(self, text, binding, stop):
        return self._submit('text')

    def observe(self, token, binding):
        elapsed = time.monotonic() - self.tokens[token['id']]
        pending = elapsed < (2.5 if token['kind'] == 'image' else 0.5)
        return Observation(token['id'], State.SENDING if pending else State.SENT,
                           '演示：模拟发送中' if pending else '演示：模拟发送完成')
