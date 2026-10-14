"""Hardware identification must never silently choose the wrong weight."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import DEVICE1_NODES, detect_device_count


class HardwareSelectionTests(unittest.TestCase):
    def detect_with(self, second):
        def is_device(path):
            name = '/' + str(path).replace('\\', '/').lstrip('/')
            return name not in DEVICE1_NODES or name in second
        with mock.patch.object(Path, 'is_char_device', new=is_device):
            return detect_device_count()

    def test_single_and_dual(self):
        self.assertEqual(self.detect_with(set()), 1)
        self.assertEqual(self.detect_with(set(DEVICE1_NODES)), 2)

    def test_partial_second_card_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, '加速卡 1 设备节点不完整'):
            self.detect_with({DEVICE1_NODES[0]})


if __name__ == '__main__':
    unittest.main()
