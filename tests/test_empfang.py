"""
Receiving SMS: long SMS whose second part got lost (19.11.2026, ref 24 from a Mitarbeiterin), notification blocks
with several +CMTI, SMS left in the modem memory.
"""
import datetime
import logging
import os
import queue
import sys
import threading
import types
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../server")))

import modem  # noqa: E402
import sms  # noqa: E402
from gsmmodem.modem import ReceivedSms  # noqa: E402
from gsmmodem.pdu import Concatenation  # noqa: E402

SENDER = "+4915500000000"
SENT_AT = datetime.datetime(2026, 11, 19, 12, 38, 20, tzinfo=datetime.timezone.utc)


class FakeDiskQueue:
    def __init__(self):
        self.saved = []
        self.deleted = []

    def save_buffered_part(self, sms_obj):
        self.saved.append(sms_obj.get_id())

    def delete_buffered_part(self, sms_id):
        self.deleted.append(sms_id)


class FakeGsm:
    """Stands in for the library modem: stored SMS and deletions."""

    def __init__(self, stored=()):
        self.stored = list(stored)
        self.delete_flags = []
        self.listed_memory = []

    def listStoredSms(self, status, memory=None):
        self.listed_memory.append(memory)
        return list(self.stored)

    def deleteMultipleStoredSms(self, flag):
        self.delete_flags.append(flag)
        self.stored = []


def receiving_modem(gsm=None):
    """A Modem object without thread or serial port, as far as receiving needs it."""
    m = modem.Modem.__new__(modem.Modem)
    m.identifier = "01"
    m.l = logging.getLogger("test-empfang")
    m.modem_config = types.SimpleNamespace(phone_number="+4915154834745")
    m.sms_disk_queue = FakeDiskQueue()
    m.sms_receiver_queue = queue.Queue()
    m.event_available = threading.Event()
    m.received_concat_buffer = {}
    m.processed_cmt_signatures = {}
    m.recent_received = {}
    m._receive_lock = threading.RLock()
    m.stats_received_sms = 0
    m.last_received = None
    m.health_check_expected_token = None
    m.current_network = "Testnetz"
    m.modem = gsm
    m.receive_memory = "ME"
    return m


def part(num, total, text, ref=24):
    return sms.SMS(
        sms_id=None, recipient="+4915154834745", text=text, timestamp=SENT_AT, sender=SENDER,
        concat_ref=ref, concat_parts=total, concat_num=num,
    )


def received(m):
    out = []
    while not m.sms_receiver_queue.empty():
        out.append(m.sms_receiver_queue.get())
    return out


class TestConcat(unittest.TestCase):

    def test_both_parts_make_one_sms(self):
        m = receiving_modem()
        m._handle_incoming_sms(part(1, 2, "Hallo, ich kann am Freitag "))
        self.assertEqual(received(m), [])
        m._handle_incoming_sms(part(2, 2, "leider nicht."))
        [complete] = received(m)
        self.assertEqual(complete.get_text(), "Hallo, ich kann am Freitag leider nicht.")
        self.assertEqual(m.received_concat_buffer, {})
        self.assertEqual(len(m.sms_disk_queue.deleted), 2)

    def test_missing_part_goes_on_with_note_after_waiting_time(self):
        m = receiving_modem()
        m._handle_incoming_sms(part(1, 2, "Hallo, ich kann am Freitag"))
        self.assertEqual(m.deliver_overdue_concat_sms(), 0)  # too early

        entry = m.received_concat_buffer[(SENDER, 24)]
        entry['first_seen'] -= datetime.timedelta(seconds=modem.CONCAT_INCOMPLETE_SECONDS)
        self.assertEqual(m.deliver_overdue_concat_sms(), 1)
        [incomplete] = received(m)
        self.assertEqual(incomplete.get_text(), "Hallo, ich kann am Freitag [Teil 2 von 2 fehlt]")
        self.assertEqual(m.received_concat_buffer, {})

    def test_reused_reference_delivers_the_old_message_first(self):
        m = receiving_modem()
        m._handle_incoming_sms(part(1, 2, "alte Nachricht"))
        m._handle_incoming_sms(part(1, 2, "neue Nachricht "))
        [old] = received(m)
        self.assertEqual(old.get_text(), "alte Nachricht [Teil 2 von 2 fehlt]")
        m._handle_incoming_sms(part(2, 2, "Ende"))
        self.assertEqual([s.get_text() for s in received(m)], ["neue Nachricht Ende"])

    def test_same_part_twice_is_ignored(self):
        m = receiving_modem()
        m._handle_incoming_sms(part(1, 2, "Teil eins "))
        m._handle_incoming_sms(part(1, 2, "Teil eins "))
        m._handle_incoming_sms(part(2, 2, "Teil zwei"))
        self.assertEqual([s.get_text() for s in received(m)], ["Teil eins Teil zwei"])


class TestStoredSms(unittest.TestCase):

    def stored(self, gsm, text, num=None, index=1):
        udh = []
        if num:
            udh = [Concatenation(0x00, 3, [24, 2, num])]
        return ReceivedSms(gsm, 1, SENDER, SENT_AT, text, None, udh, index)

    def test_left_over_part_completes_the_buffered_sms(self):
        gsm = FakeGsm()
        m = receiving_modem(gsm)
        m._handle_incoming_sms(part(1, 2, "Hallo, ich kann am Freitag "))
        # the notification of part 2 got lost — the part is still in the modem memory
        gsm.stored = [self.stored(gsm, "leider nicht.", num=2)]
        self.assertEqual(m.process_stored_sms(), 1)
        self.assertEqual([s.get_text() for s in received(m)], ["Hallo, ich kann am Freitag leider nicht."])
        self.assertEqual(gsm.delete_flags, [1])  # only read messages

    def test_sms_handled_by_its_notification_is_not_delivered_twice(self):
        gsm = FakeGsm()
        m = receiving_modem(gsm)
        stored = self.stored(gsm, "Bin krank heute")
        m._handle_incoming_gsmmodem_sms(stored)  # via +CMTI
        gsm.stored = [stored]  # the sweep reads it before the notification thread deleted it
        m.process_stored_sms()
        self.assertEqual([s.get_text() for s in received(m)], ["Bin krank heute"])

    def test_lists_the_receive_memory_not_the_status_report_memory(self):
        # reading a delivery report left the library's read memory on "SR" (CMS 500 on listing, 19.11.2026)
        gsm = FakeGsm()
        m = receiving_modem(gsm)
        m.process_stored_sms()
        self.assertEqual(gsm.listed_memory, ["ME"])

    def test_nothing_stored_deletes_nothing(self):
        gsm = FakeGsm()
        m = receiving_modem(gsm)
        self.assertEqual(m.process_stored_sms(), 0)
        self.assertEqual(gsm.delete_flags, [])


class TestNotificationBlocks(unittest.TestCase):

    def test_two_indications_in_one_block_are_split(self):
        self.assertEqual(
            modem.split_sms_indications(['+CMTI: "SM",1', '+CMTI: "SM",2']),
            [['+CMTI: "SM",1'], ['+CMTI: "SM",2']],
        )
        self.assertEqual(
            modem.split_sms_indications(['+CMTI: "SM",3', '+CDSI: "SR",1', '+CREG: 1']),
            [['+CMTI: "SM",3'], ['+CDSI: "SR",1'], ['+CREG: 1']],
        )
        self.assertEqual(modem.split_sms_indications(['+CMTI: "SM",1']), [['+CMTI: "SM",1']])

    def test_cmti_block_reaches_the_library_line_by_line_without_decoding_errors(self):
        handed = []
        original = modem.GsmModem._handleModemNotification
        modem.GsmModem._handleModemNotification = lambda self, lines: handed.append(lines)
        try:
            gsm = modem.MyGsmModem.__new__(modem.MyGsmModem)
            gsm.l = logging.getLogger("test-gsm")
            gsm.gw_modem = receiving_modem()
            # before: '+CMTI' matched the '+CMT' branch and the next line was decoded as message (EncodingError)
            gsm._handleModemNotification(['+CMTI: "SM",1', '+CMTI: "SM",2'])
        finally:
            modem.GsmModem._handleModemNotification = original
        self.assertEqual(handed, [['+CMTI: "SM",1'], ['+CMTI: "SM",2']])


if __name__ == "__main__":
    unittest.main()
