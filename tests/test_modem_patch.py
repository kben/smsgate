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



class TestSplitUnsolicited(unittest.TestCase):
    TERM = re.compile(r'^OK|ERROR|(\+CM[ES] ERROR: \d+)|(COMMAND NOT SUPPORT)$')

    def test_creg_answer_is_kept_when_asked(self):
        own, unsolicited = modem.split_unsolicited(['+CREG: 0,1', 'OK'], 'AT+CREG?', self.TERM)
        self.assertEqual(own, ['+CREG: 0,1', 'OK'])
        self.assertEqual(unsolicited, [])

    def test_creg_is_unsolicited_during_other_commands(self):
        own, unsolicited = modem.split_unsolicited(['+CREG: 1', '+CSQ: 20,99', 'OK'], 'AT+CSQ', self.TERM)
        self.assertEqual(own, ['+CSQ: 20,99', 'OK'])
        self.assertEqual(unsolicited, ['+CREG: 1'])

    def test_cereg_answer_is_kept_when_asked(self):
        own, _ = modem.split_unsolicited(['+CEREG: 0,5', 'OK'], 'AT+CEREG?', self.TERM)
        self.assertEqual(own, ['+CEREG: 0,5', 'OK'])

    def test_incoming_sms_pdu_line_follows_cmt(self):
        own, unsolicited = modem.split_unsolicited(['+CMT: ,23', '0791…', 'OK'], 'AT', self.TERM)
        self.assertEqual(own, ['OK'])
        self.assertEqual(unsolicited, ['+CMT: ,23', '0791…'])

    def test_zte_notifications_are_unsolicited(self):
        own, unsolicited = modem.split_unsolicited(['+ZMTime: 5;', '+ZEND', 'OK'], 'AT', self.TERM)
        self.assertEqual(own, ['OK'])
        self.assertEqual(unsolicited, ['+ZMTime: 5;', '+ZEND'])


if __name__ == '__main__':
    unittest.main()
