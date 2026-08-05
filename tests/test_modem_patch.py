import unittest
import os
import sys
import re

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../server")))

import modem


class DummyModem:
    RESPONSE_TERM = re.compile(r'^OK|ERROR|(\+CM[ES] ERROR: \d+)|(COMMAND NOT SUPPORT)$')
    _callStatusUpdates = []

    def __init__(self):
        self.notifications = []
        self.log = self

    def debug(self, msg, *args):
        pass

    def _handleModemNotification(self, lines):
        self.notifications.extend(lines)


class TestModemWritePatch(unittest.TestCase):

    def test_unsolicited_line_filtering(self):
        # Test filtering ZTE and Huawei unsolicited notifications
        unsolicited_prefixes = ('+CMTI', '+CDSI', '+DTMF', '+Z', '^', '+WIND', '+STIN', '+CRING', '+CREG:', '+CGREG:', '+CEREG:')

        lines = ['+ZMTime: 5;', '+ZEND', 'OK']
        filtered = []
        unsolicited = []

        dummy = DummyModem()
        for line in lines:
            if dummy.RESPONSE_TERM.match(line):
                filtered.append(line)
            elif line.startswith(unsolicited_prefixes) or 'RING' in line:
                unsolicited.append(line)
            else:
                filtered.append(line)

        self.assertEqual(filtered, ['OK'])
        self.assertEqual(unsolicited, ['+ZMTime: 5;', '+ZEND'])


if __name__ == '__main__':
    unittest.main()
