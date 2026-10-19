import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from sender.core import (CompletionGate, Journal, NeedsReview, Observation, Photo, SenderError,
                         State, Stopped, file_hash, run_queue, scan_folder)


class GateTests(unittest.TestCase):
    def test_thumbnail_or_elapsed_time_is_never_confirmation(self):
        gate = CompletionGate(1)
        for t in [0, 1, 30, 900]:
            self.assertFalse(gate.feed(Observation('x', State.UNKNOWN), t))
            self.assertFalse(gate.feed(Observation('x', State.CLEAR), t))

    def test_upload_must_stop_and_remain_clear(self):
        gate = CompletionGate(1)
        self.assertFalse(gate.feed(Observation('x', State.SENDING), 0))
        self.assertFalse(gate.feed(Observation('x', State.SENDING), 100))
        self.assertFalse(gate.feed(Observation('x', State.CLEAR), 101))
        self.assertFalse(gate.feed(Observation('x', State.CLEAR), 101.9))
        self.assertTrue(gate.feed(Observation('x', State.CLEAR), 102))

    def test_upload_restart_resets_stability(self):
        gate = CompletionGate(1)
        for state, t in [(State.SENDING, 0), (State.CLEAR, 1), (State.SENDING, 1.9), (State.CLEAR, 2)]:
            self.assertFalse(gate.feed(Observation('x', state), t))
        self.assertFalse(gate.feed(Observation('x', State.CLEAR), 2.9))
        self.assertTrue(gate.feed(Observation('x', State.CLEAR), 3))

    def test_missing_initial_bubble_does_not_pin_id(self):
        gate = CompletionGate(0)
        self.assertFalse(gate.feed(Observation('', State.UNKNOWN), 0))
        self.assertTrue(gate.feed(Observation('new', State.SENT), 1))

    def test_row_replacement_and_failure_block(self):
        for state, row_id in [(State.CLEAR, 'other'), (State.FAILED, 'x')]:
            gate = CompletionGate(0)
            gate.feed(Observation('x', State.SENDING), 0)
            with self.assertRaises(NeedsReview):
                gate.feed(Observation(row_id, state), 1)


class FakeBackend:
    def __init__(self, image_state=State.SENT, fail_caption=False, fail_target_at=None):
        self.actions = []
        self.image_state = image_state
        self.fail_caption = fail_caption
        self.fail_target_at = fail_target_at
        self.checks = 0

    def assert_target(self, binding):
        self.checks += 1
        if self.fail_target_at == self.checks:
            raise NeedsReview('chat changed')

    def submit_image(self, path, binding, stop):
        self.actions.append(('image', path.name))
        return {'kind': 'image', 'id': path.name}

    def submit_text(self, text, binding, stop):
        self.actions.append(('name', text))
        if self.fail_caption:
            raise OSError('uncertain click')
        return {'kind': 'name', 'id': text}

    def observe(self, token, binding):
        return Observation(token['id'], self.image_state if token['kind'] == 'image' else State.SENT)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        for name in ['10.png', '2.png', '1.png']:
            Image.new('RGB', (4, 4), 'red').save(self.root / name)
        self.photos = scan_folder(self.root, False, True)
        self.journal = Journal.create(self.root / 'jobs', {'key': 'test'}, self.photos)
        self.stop = threading.Event()

    def tearDown(self):
        self.temp.cleanup()

    def run_it(self, backend, emit=lambda *args: None, timeout=0.02):
        run_queue(self.journal, backend, self.stop, emit, timeout=timeout, interval=0, stable=0)

    def test_natural_order_and_exact_pairs(self):
        backend = FakeBackend()
        self.run_it(backend)
        self.assertEqual(backend.actions, [('image', '1.png'), ('name', '1.png'),
                                          ('image', '2.png'), ('name', '2.png'),
                                          ('image', '10.png'), ('name', '10.png')])
        self.assertTrue(all(p.phase == 'done' for p in Journal.load(self.journal.path).photos))

    def test_failure_and_unknown_never_send_name_or_next_photo(self):
        for state in [State.FAILED, State.UNKNOWN, State.SENDING]:
            with self.subTest(state=state):
                journal = Journal.create(self.root / 'jobs', {'key': 'test'}, self.photos)
                backend = FakeBackend(state)
                with self.assertRaises(NeedsReview):
                    run_queue(journal, backend, self.stop, lambda *a: None, timeout=0.001,
                              interval=0, stable=0)
                self.assertEqual(backend.actions, [('image', '1.png')])
                self.assertEqual(journal.photos[0].phase, 'image_submitted')

    def test_crash_after_name_click_requires_review(self):
        backend = FakeBackend(fail_caption=True)
        with self.assertRaises(OSError):
            self.run_it(backend)
        self.assertEqual(Journal.load(self.journal.path).photos[0].phase, 'name_attempting')
        backend2 = FakeBackend()
        with self.assertRaises(NeedsReview):
            self.run_it(backend2)
        self.assertEqual(backend2.actions, [])

    def test_stop_after_image_ack_preserves_image_and_omits_name(self):
        def emit(event, value):
            if event == 'phase' and value[1] == 'image_confirmed':
                self.stop.set()
        backend = FakeBackend()
        with self.assertRaises(Stopped):
            self.run_it(backend, emit)
        self.assertEqual(backend.actions, [('image', '1.png')])
        self.assertEqual(self.journal.photos[0].phase, 'image_confirmed')

    def test_file_change_blocks_before_send(self):
        (self.root / '1.png').write_bytes(b'changed')
        backend = FakeBackend()
        with self.assertRaises(SenderError):
            self.run_it(backend)
        self.assertEqual(backend.actions, [])

    def test_user_review_resumes_with_name_without_resending_image(self):
        self.journal.checkpoint(self.journal.photos[0], 'image_submitted')
        self.journal.resolve(0, 'image_sent')
        backend = FakeBackend()
        self.run_it(backend)
        self.assertEqual(backend.actions[0], ('name', '1.png'))
        self.assertNotIn(('image', '1.png'), backend.actions)

    def test_switched_chat_prevents_name(self):
        backend = FakeBackend(fail_target_at=3)
        with self.assertRaises(NeedsReview):
            self.run_it(backend)
        self.assertEqual(backend.actions, [('image', '1.png')])

    def test_bad_image_stops_scan(self):
        (self.root / 'bad.png').write_bytes(b'not-an-image')
        with self.assertRaises(SenderError):
            scan_folder(self.root, False, True)

    def test_name_setting_and_recursive_scan(self):
        sub = self.root / 'sub'
        sub.mkdir()
        Image.new('RGB', (2, 2)).save(sub / '中文.PNG')
        self.assertEqual(len(scan_folder(self.root, False, False)), 3)
        self.assertEqual([p.name for p in scan_folder(self.root, True, False)], ['1', '2', '10', '中文'])

    def test_failed_checkpoint_does_not_unblock_caption(self):
        photo = self.journal.photos[0]
        self.journal.checkpoint(photo, 'image_submitted')
        with patch.object(self.journal, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.journal.checkpoint(photo, 'image_confirmed')
        self.assertEqual(photo.phase, 'image_submitted')
        backend = FakeBackend()
        with self.assertRaises(NeedsReview):
            self.run_it(backend)
        self.assertEqual(backend.actions, [])


if __name__ == '__main__':
    unittest.main()
