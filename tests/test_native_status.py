import unittest
from dataclasses import replace

from sender.core import State
from sender.native_status import Node, image_state


def pane(*children, visible=True):
    return Node('PaneControl', '', visible, children)


def tree(*indicators, account='my-account', content=None):
    content = content or pane(pane(pane(visible=False), pane(pane(Node('ButtonControl', '', True)))))
    return Node('ListItemControl', '[图片]', True, (
        pane(pane(), pane(pane(*indicators, content)), Node('ButtonControl', account, True)),
    ))


class NativeStateTests(unittest.TestCase):
    def state(self, root, version='3.9.12.55'):
        return image_state(root, version, 'my-account')

    def test_observed_upload_indicator_blocks(self):
        self.assertEqual(self.state(tree(Node('ButtonControl', '', True))), State.SENDING)

    def test_retry_indicator_blocks_even_if_not_named(self):
        for label in ['', '重新发送', 'unknown icon']:
            self.assertEqual(self.state(tree(Node('ButtonControl', label, True))), State.SENDING)

    def test_observed_completed_layout_is_accepted(self):
        self.assertEqual(self.state(tree()), State.SENT)

    def test_other_versions_and_accounts_are_unknown(self):
        self.assertEqual(self.state(tree(), '3.9.13.1'), State.UNKNOWN)
        self.assertEqual(self.state(tree(account='someone-else')), State.UNKNOWN)

    def test_missing_or_hidden_image_is_never_complete(self):
        self.assertEqual(self.state(tree(content=pane())), State.UNKNOWN)
        self.assertEqual(self.state(tree(content=pane(Node('ButtonControl', '', False)))), State.UNKNOWN)
        self.assertEqual(self.state(replace(tree(), visible=False)), State.UNKNOWN)

    def test_unexpected_error_or_progress_nodes_block(self):
        for kind in ['ProgressBarControl', 'TextControl', 'StatusBarControl']:
            self.assertEqual(self.state(tree(content=pane(Node('ButtonControl', '', True),
                                                           Node(kind, 'something changed', True)))), State.UNKNOWN)

    def test_extra_buttons_are_not_treated_as_completed_image(self):
        self.assertEqual(self.state(tree(content=pane(Node('ButtonControl', '', True),
                                                      Node('ButtonControl', '', True)))), State.UNKNOWN)


if __name__ == '__main__':
    unittest.main()
