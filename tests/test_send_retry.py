import datetime
import logging
import os
import queue
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../server")))

import modem
import modempool
import sms
from gsmmodem.exceptions import CmsError


class FakeDiskQueue:
    def __init__(self):
        self.deleted = []

    def delete_outgoing_sms(self, sms_id):
        self.deleted.append(sms_id)

    def save_delivery_state(self, identifier, state):
        self.delivery_state = state


class FakeSent:
    def __init__(self, reference):
        self.reference = reference


class FakeGsm:
    """Stands in for the serial GsmModem: raises the given errors in order, then accepts."""

    def __init__(self, errors=None, reference=42):
        self.errors = list(errors or [])
        self.reference = reference
        self.calls = 0

    def sendSms(self, recipient, text, waitForDeliveryReport=False, sendFlash=False):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return FakeSent(self.reference)


def bare_modem(gsm=None):
    """A Modem object without starting its thread or touching serial ports."""
    m = modem.Modem.__new__(modem.Modem)
    m.identifier = "00"
    m.l = logging.getLogger("test-modem")
    m.sms_sender_queue = queue.Queue()
    m.sent_sms = {}
    m.retry_sms = []
    m.sent_by_reference = {}
    m.status_callback = None
    m.zustellung = {}
    m.sms_disk_queue = FakeDiskQueue()
    m.modem = gsm
    m.status = None
    return m


def new_sms(age_seconds=0):
    ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=age_seconds)
    return sms.SMS(sms_id=None, recipient="+491774812002", text="Login", timestamp=ts, sender="")


class TestRetryDecision(unittest.TestCase):

    def test_backoff_starts_at_5_doubles_and_caps_at_60(self):
        err = CmsError("AT+CMGS", 38)
        self.assertEqual(modem.retry_decision(0, err, None), (False, 5))
        self.assertEqual(modem.retry_decision(0, err, 5), (False, 10))
        self.assertEqual(modem.retry_decision(0, err, 40), (False, 60))
        self.assertEqual(modem.retry_decision(0, err, 60), (False, 60))

    def test_permanent_error_gives_up_at_once(self):
        self.assertTrue(modem.retry_decision(0, CmsError("AT+CMGS", 1), None)[0])

    def test_too_old_gives_up(self):
        self.assertTrue(modem.retry_decision(modem.MAX_SEND_AGE_SECONDS, CmsError("AT+CMGS", 38), 60)[0])


class TestDoSendSms(unittest.TestCase):

    def test_success_logs_reference_and_clears_disk_queue(self):
        m = bare_modem(FakeGsm(reference=7))
        s = new_sms()
        with self.assertLogs("test-modem", level="INFO") as logs:
            self.assertTrue(m._do_send_sms(s))
        self.assertIn(s.get_id(), m.sms_disk_queue.deleted)
        self.assertEqual(m.sent_by_reference[7][0], s.get_id())
        self.assertTrue(any("accepted by the network" in line and "reference 7" in line for line in logs.output))

    def test_error_schedules_retry_without_blocking(self):
        m = bare_modem(FakeGsm(errors=[CmsError("AT+CMGS", 38)]))
        s = new_sms()
        started = time.time()
        self.assertFalse(m._do_send_sms(s))
        self.assertLess(time.time() - started, 1, "the sending thread must not sleep for a retry")
        self.assertEqual(len(m.retry_sms), 1)
        self.assertEqual(s.retry_delay, modem.RETRY_DELAY_START)
        self.assertEqual(m.sms_disk_queue.deleted, [], "a retried SMS stays in the disk queue")

    def test_permanent_error_is_given_up(self):
        m = bare_modem(FakeGsm(errors=[CmsError("AT+CMGS", 1)]))
        s = new_sms()
        self.assertFalse(m._do_send_sms(s))
        self.assertEqual(m.retry_sms, [])
        self.assertIn(s.get_id(), m.sms_disk_queue.deleted)

    def test_due_retries_move_to_the_queue(self):
        m = bare_modem()
        s1, s2 = new_sms(), new_sms()
        m.retry_sms = [(time.time() - 1, s1), (time.time() + 30, s2)]
        wait = m._requeue_due_retries()
        self.assertIs(m.sms_sender_queue.get_nowait(), s1)
        self.assertEqual(m.retry_sms[0][1], s2)
        self.assertTrue(25 < wait <= 30)

    def test_wait_is_60_without_retries(self):
        self.assertEqual(bare_modem()._requeue_due_retries(), 60)


class TestDeliveryReport(unittest.TestCase):

    def test_report_is_logged_with_sms_id(self):
        m = bare_modem()
        s = new_sms()
        m._remember_reference(9, s)

        class Report:
            reference = 9
            number = "+491774812002"
            deliveryStatus = 0
            timeSent = datetime.datetime.now(datetime.timezone.utc)
            timeFinalized = datetime.datetime.now(datetime.timezone.utc)

        with self.assertLogs("test-modem", level="INFO") as logs:
            m._handle_status_report(Report())
        self.assertTrue(any(s.get_id() in line and "delivered" in line for line in logs.output))

    def test_status_texts(self):
        self.assertEqual(modem.delivery_status_text(0), "delivered")
        self.assertIn("keeps trying", modem.delivery_status_text(48))
        self.assertIn("failed", modem.delivery_status_text(68))


class TestMultipart(unittest.TestCase):

    def test_long_login_link_has_two_parts(self):
        link = "https://app.example.de/mitarbeiter/?token=" + "x" * 170
        self.assertEqual(modem.part_count("+491774812002", link), 2)
        self.assertEqual(modem.part_count("+491774812002", "Hallo"), 1)

    def test_part_references_count_back_and_wrap(self):
        self.assertEqual(modem.part_references(5, 2), [4, 5])
        self.assertEqual(modem.part_references(0, 2), [255, 0])
        self.assertEqual(modem.part_references(9, 1), [9])

    def test_both_parts_are_logged_with_sms_id(self):
        link = "https://app.example.de/mitarbeiter/?token=" + "x" * 170
        m = bare_modem(FakeGsm(reference=5))
        s = sms.SMS(sms_id=None, recipient="+491774812002", text=link, sender="")
        self.assertTrue(m._do_send_sms(s))

        class Report:
            number = "+491774812002"
            deliveryStatus = 0
            timeSent = None
            timeFinalized = None

        for ref, teil in ((4, "part 1/2"), (5, "part 2/2")):
            r = Report()
            r.reference = ref
            with self.assertLogs("test-modem", level="INFO") as logs:
                m._handle_status_report(r)
            self.assertTrue(any(s.get_id() in line and teil in line for line in logs.output), logs.output)


class TestHealthState(unittest.TestCase):

    def test_dead_sending_thread_is_critical(self):
        m = bare_modem()
        m.health_state, m.health_logs = "OK", None
        threading.Thread.__init__(m)
        m.run = lambda: None  # a thread that ends at once, like a crashed sending loop
        threading.Thread.start(m)
        m.join()
        self.assertEqual(m.get_health_state()[0], "CRITICAL")

    def test_not_started_thread_reports_stored_state(self):
        m = bare_modem()
        m.health_state, m.health_logs = "OK", None
        threading.Thread.__init__(m)
        self.assertEqual(m.get_health_state(), ("OK", None))


class TestUnroutable(unittest.TestCase):

    def pool(self):
        p = modempool.ModemPool(600)
        p.set_event_thread(threading.Event())
        p.disk_queue = FakeDiskQueue()
        return p

    def test_sms_without_modem_is_kept_and_retried(self):
        p = self.pool()
        s = new_sms()
        p.sms_queue_out.put(s)
        p.process_outgoing_sms()
        self.assertTrue(p.has_unroutable_sms())
        self.assertEqual(p.disk_queue.deleted, [])
        p.process_outgoing_sms()
        self.assertEqual(p.sms_unroutable, [s], "kept again, not duplicated")

    def test_old_sms_without_modem_is_given_up(self):
        p = self.pool()
        s = new_sms(age_seconds=modem.MAX_SEND_AGE_SECONDS + 1)
        p.sms_queue_out.put(s)
        p.process_outgoing_sms()
        self.assertFalse(p.has_unroutable_sms())
        self.assertIn(s.get_id(), p.disk_queue.deleted)


if __name__ == "__main__":
    unittest.main()
