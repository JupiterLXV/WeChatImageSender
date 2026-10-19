"""Second separately authorized image; observe menus during and after upload.

Never automatically send its caption. Use only with explicit human approval.
The exclusive marker prevents a repeat submission after any partial failure.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import threading
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from PIL import Image, ImageDraw, ImageGrab
from sender.accessibility import restore_all
from sender.qt_menu import read_image_menu
from sender.qt_visual import prepare_source, image_state
from sender.wechat import WeChatBackend, rid
from sender.winutil import dpi_aware, user32
from open_filehelper import open_filehelper


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--authorized-additional-test', action='store_true')
    args = parser.parse_args()
    if not args.authorized_additional_test:
        parser.error('Separate human authorization is required for this additional test')
    sys.stdout.reconfigure(encoding='utf-8')
    dpi_aware()
    folder = PROJECT / 'data' / 'wechat4-test-2'
    folder.mkdir(exist_ok=True)
    marker = folder / 'image-attempt.json'
    if marker.exists():
        raise RuntimeError('Additional test was already attempted. Do not resend.')
    fixture = folder / 'wechat4_menu_test.png'
    source = Image.frombytes('RGB', (3000, 2000), os.urandom(3000 * 2000 * 3))
    draw = ImageDraw.Draw(source)
    draw.rectangle((0, 0, 3000, 250), fill='#e0f0ff')
    draw.text((60, 60), 'WeChat 4.1.15 / additional authorized menu-state test', fill='black', font_size=65)
    source.save(fixture)
    backend = WeChatBackend(allow_activation=True)
    try:
        binding = open_filehelper(backend)
        if backend._input_value():
            raise RuntimeError('The authorized chat contains a draft')
        record = {'binding': binding, 'path': str(fixture), 'phase': 'attempting'}
        with marker.open('x', encoding='utf-8') as handle:
            json.dump(record, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        start = time.monotonic()
        token = backend.submit_image(fixture, binding, threading.Event())
        record.update(token=token, phase='submitted')
        marker.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
        history = []
        for index in range(12):
            obs = backend.observe(token, binding)
            entry = {'elapsed': round(time.monotonic() - start, 3), 'state': obs.state.value,
                     'id': token['id']}
            if token['id']:
                row = next(r for r in backend._rows() if rid(r) == token['id'])
                r = row.BoundingRectangle
                frame = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom))
                frame.save(folder / f'row-{index:02}.png')
                dpi = user32.GetDpiForWindow(binding['hwnd'])
                result = image_state(frame, source, dpi, binding['version'])
                entry['visual_before'] = result.state.value
                if result.matched:
                    entry['menu'] = read_image_menu(backend, binding, row, source, dpi)
                    after = backend.observe(token, binding)
                    entry['state_after'] = after.state.value
            history.append(entry)
            (folder / 'observations.json').write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding='utf-8')
            marker.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
            print(json.dumps(entry, ensure_ascii=False), flush=True)
            time.sleep(.25)
        record['phase'] = 'image_observed_caption_not_sent'
        marker.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
    finally:
        restore_all()


if __name__ == '__main__':
    main()
