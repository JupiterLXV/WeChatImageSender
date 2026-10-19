"""Confirm the existing authorized second image, then submit its filename once.

No image is submitted by this tool. Do not run without the corresponding human
authorization; an exclusive caption marker prevents repeating a partial send.
"""
import json
import argparse
import os
from pathlib import Path
import sys
import threading

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from sender.core import Journal, scan_folder, wait_confirmed, run_queue
from sender.accessibility import restore_all
from sender.wechat import WeChatBackend
from sender.winutil import dpi_aware


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--authorized-existing-caption', action='store_true')
    if not parser.parse_args().authorized_existing_caption:
        parser.error('Explicit authorization for the existing test image caption is required')
    sys.stdout.reconfigure(encoding='utf-8')
    dpi_aware()
    folder = PROJECT / 'data' / 'wechat4-test-2'
    record = json.loads((folder / 'image-attempt.json').read_text(encoding='utf-8'))
    caption = 'wechat4_menu_test.png'
    marker = folder / 'caption-attempt.json'
    if marker.exists():
        raise RuntimeError('Caption was already attempted. Do not resend.')
    if record['binding']['title'] != '文件传输助手' or Path(record['path']).name != caption:
        raise RuntimeError('Not the separately authorized test')
    backend = WeChatBackend(allow_activation=True)
    stop = threading.Event()
    try:
        backend.attach(record['binding'])
        if backend._input_value() or any(r.Name == caption for r in backend._rows()):
            raise RuntimeError('Draft present or caption already visible; no submission')
        # Exercise the fast-send path: do not depend on a remembered busy bit.
        record['token']['seen_busy'] = False
        proof = wait_confirmed(backend, record['token'], record['binding'], stop, 20,
                               lambda k, v: print(k, v, flush=True))
        photo = next(p for p in scan_folder(folder, False, True) if Path(p.path).name == caption)
        journal = Journal.create(folder / 'jobs', record['binding'], [photo])
        journal.checkpoint(journal.photos[0], 'image_confirmed', proof=proof)
        with marker.open('x', encoding='utf-8') as handle:
            json.dump({'phase': 'attempting', 'image_proof': proof, 'journal': str(journal.path)},
                      handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        run_queue(journal, backend, stop, lambda k, v: print(k, v, flush=True), timeout=30, interval=0)
        result = {'phase': journal.photos[0].phase, 'caption': caption,
                  'image_proof': proof, 'caption_proof': journal.photos[0].proof}
        (folder / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        restore_all()


if __name__ == '__main__':
    main()
