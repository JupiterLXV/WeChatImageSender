"""One authorized 4.x test image. Never automatically resend or send its caption.

Requires --authorized-filehelper-test; an exclusive marker prevents a second
submission. All diagnostics stay under ignored data/.
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
from PIL import Image, ImageDraw
import uiautomation as uia
from sender.accessibility import restore_all
from sender.wechat import WeChatBackend, rid, walk
from sender.qt_input import click_point
from sender.winutil import dpi_aware, user32


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--authorized-filehelper-test', action='store_true')
    args = parser.parse_args()
    if not args.authorized_filehelper_test:
        parser.error('Explicit human authorization for one File Transfer Assistant image is required')
    sys.stdout.reconfigure(encoding='utf-8')
    dpi_aware()
    folder = PROJECT / 'data' / 'wechat4-test'
    folder.mkdir(exist_ok=True)
    marker = folder / 'image-attempt.json'
    if marker.exists():
        raise RuntimeError('Already attempted; do not resend')
    fixture = folder / 'wechat4_sender_test.png'
    image = Image.frombytes('RGB', (3000, 2000), os.urandom(3000 * 2000 * 3))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 3000, 220), fill='white')
    draw.text((60, 60), 'WeChat 4.1.15 send-state validation / ONE AUTHORIZED IMAGE', fill='black', font_size=60)
    image.save(fixture)
    backend = WeChatBackend(allow_activation=True)
    try:
        binding = backend.bind_current()
        if binding['title'] != '文件传输助手' or backend._input_value():
            raise RuntimeError('Expected File Transfer Assistant and an empty input')
        with marker.open('x', encoding='utf-8') as handle:
            json.dump({'binding': binding, 'path': str(fixture), 'phase': 'attempting'}, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        start = time.monotonic()
        token = backend.submit_image(fixture, binding, threading.Event())
        marker.write_text(json.dumps({'binding': binding, 'path': str(fixture), 'token': token,
                                     'phase': 'submitted'}, ensure_ascii=False), encoding='utf-8')
        records = []
        for index in range(12):
            backend.assert_target(binding)
            observation = backend.observe(token, binding)
            item = {'elapsed': round(time.monotonic() - start, 3),
                    'state': observation.state.value, 'row_id': token['id']}
            if token['id']:
                row = next(r for r in backend._rows() if rid(r) == token['id'])
                row.CaptureToImage(str(folder / f'row-{index:02}.png'))
                r = row.BoundingRectangle
                click_point(binding['hwnd'], binding['pid'], r.right - 125, r.top + 45, right=True)
                time.sleep(0.12)
                nodes = list(walk(backend.window, depth=45, budget=2500, skip_lists=True))
                menus = [c for c in nodes if 'Menu' in c.ControlTypeName or 'Menu' in c.ClassName]
                # Some Qt popup providers expose a separate native root.
                if not menus:
                    for root in uia.GetRootControl().GetChildren():
                        if root.ProcessId == binding['pid']:
                            menus.extend(c for c in walk(root, depth=45, budget=2500, skip_lists=True)
                                         if 'Menu' in c.ControlTypeName or 'Menu' in c.ClassName)
                item['menu'] = [{'type': c.ControlTypeName, 'class': c.ClassName, 'name': c.Name,
                                 'enabled': c.IsEnabled, 'offscreen': c.IsOffscreen,
                                 'id': rid(c), 'rect': str(c.BoundingRectangle)} for c in menus]
                if menus:
                    uia.SendKeys('{Esc}', waitTime=0)
                time.sleep(0.15)
            records.append(item)
            (folder / 'observations.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(item, ensure_ascii=False), flush=True)
            time.sleep(0.4)
        marker.write_text(json.dumps({'binding': binding, 'path': str(fixture), 'token': token,
                                     'phase': 'image_observed_caption_not_sent'}, ensure_ascii=False), encoding='utf-8')
    finally:
        restore_all()


if __name__ == '__main__':
    main()
