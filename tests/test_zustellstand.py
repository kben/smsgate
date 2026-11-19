"""
Delivery reports after a restart (19.11.2026: report for an SMS of 17.11. said "[unknown]", aidlink never learned
that it failed) and the validity period of sent SMS.
"""
import datetime
import logging
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../server")))

import modem  # noqa: E402
import sms  # noqa: E402
import sms_disk_queue  # noqa: E402
import gsmmodem.modem  # noqa: E402


def sending_modem(queue_dir):
    """A Modem object without thread or serial port, as far as delivery tracking needs it."""
    m = modem.Modem.__new__(modem.Modem)
    m.identifier = "00"
    m.l = logging.getLogger("test-zustellstand")
    m.sent_by_reference = {}
    m.zustellung = {}
    m.gemeldet = []
    m.status_callback = lambda *args: m.gemeldet.append(args)
    m.sms_disk_queue = sms_disk_queue.SMSDiskQueue(None)
    m.sms_disk_queue.queue_dir = queue_dir
    return m


def report(reference, status):
    return types.SimpleNamespace(
        reference=reference, number="+4915100000000", deliveryStatus=status,
        timeFinalized=datetime.datetime(2026, 11, 19, 15, 24, tzinfo=datetime.timezone.utc),
        timeSent=datetime.datetime(2026, 11, 17, 15, 24, tzinfo=datetime.timezone.utc),
    )


class TestZustellstandNachNeustart(unittest.TestCase):

    def test_failure_after_restart_reaches_the_right_sms(self):
        with tempfile.TemporaryDirectory() as d:
            vorher = sending_modem(d)
            gesendet = sms.SMS(sms_id="sms-17-11", recipient="+4915100000000", text="Hallo Nico")
            vorher._remember_reference(179, gesendet, 2)
            vorher.zustellung["sms-17-11"] = {"teile": 2, "zugestellt": set()}
            vorher._zustellung_auswerten("sms-17-11", 1, 0, None)  # part 1 delivered, saved
            self.assertEqual(vorher.gemeldet, [])

            # smsgate restarts: a new modem object loads the saved state
            nachher = sending_modem(d)
            nachher._zustellstand_laden()
            self.assertEqual(nachher.sent_by_reference[179][0], "sms-17-11")
            self.assertEqual(nachher.zustellung["sms-17-11"]["zugestellt"], {1})

            nachher._handle_status_report(report(179, 70))
            [(sms_id, status, _am, _teile, _zugestellt, text)] = nachher.gemeldet
            self.assertEqual((sms_id, status), ("sms-17-11", "fehlgeschlagen"))
            self.assertIn("Status 70", text)
            self.assertNotIn("sms-17-11", nachher.zustellung)

    def test_old_and_unreadable_state_is_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            m = sending_modem(d)
            alt = 0  # accepted in 1970
            m.sms_disk_queue.save_delivery_state("00", {"references": {"5": ["alt", "+49", alt, 1, 1]},
                                                        "zustellung": {"alt": {"teile": 1, "zugestellt": []}}})
            m._zustellstand_laden()
            self.assertEqual((m.sent_by_reference, m.zustellung), ({}, {}))

            with open(os.path.join(d, "delivery_00.json"), "w") as f:
                f.write("{kaputt")
            m._zustellstand_laden()
            self.assertEqual(m.sent_by_reference, {})


class TestGueltigkeit(unittest.TestCase):

    def tearDown(self):
        modem.set_sms_validity(0)

    def test_validity_period_goes_into_the_pdu(self):
        ohne = gsmmodem.modem.encodeSmsSubmitPdu("+4915100000000", "Test")[0].data
        modem.set_sms_validity(4)
        mit = gsmmodem.modem.encodeSmsSubmitPdu("+4915100000000", "Test")[0].data
        # first octet after the SMSC field: VPF bits 0x18 = 0x10 (relative); one octet longer; 4 days = 166 + 4
        self.assertEqual(ohne[1] & 0x18, 0)
        self.assertEqual(mit[1] & 0x18, 0x10)
        self.assertEqual(len(mit), len(ohne) + 1)
        self.assertIn(170, mit)

    def test_reports_are_kept_longer_than_the_validity_period(self):
        modem.set_sms_validity(4)
        self.assertGreater(modem.DELIVERY_REPORT_KEEP_SECONDS, 4 * 24 * 3600)


if __name__ == "__main__":
    unittest.main()
