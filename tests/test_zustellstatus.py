import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../server")))
sys.path.insert(0, os.path.dirname(__file__))

import modem
import modempool
import sms
import zustellstatus
from gsmmodem.exceptions import CmsError
from test_send_retry import FakeGsm, bare_modem, new_sms

LINK = "https://app.example.de/mitarbeiter/?token=" + "x" * 170  # two parts


class Report:
    number = "+491774812002"
    timeSent = None
    timeFinalized = None

    def __init__(self, reference, status):
        self.reference = reference
        self.deliveryStatus = status


def mit_meldungen(m):
    meldungen = []
    m.status_callback = lambda *a: meldungen.append(a)
    m.zustellung = {}
    return meldungen


class TestModemMeldungen(unittest.TestCase):

    def test_angenommen_dann_zugestellt_erst_nach_allen_teilen(self):
        m = bare_modem(FakeGsm(reference=5))
        meldungen = mit_meldungen(m)
        s = sms.SMS(sms_id=None, recipient="+491774812002", text=LINK, sender="")
        self.assertTrue(m._do_send_sms(s))
        self.assertEqual(meldungen[0][:2], (s.get_id(), "angenommen"))
        self.assertEqual(meldungen[0][3], 2)
        m._handle_status_report(Report(4, 0))
        self.assertEqual(len(meldungen), 1, "erst Teil 1 zugestellt")
        m._handle_status_report(Report(5, 0))
        self.assertEqual(meldungen[1][:2], (s.get_id(), "zugestellt"))
        self.assertEqual(meldungen[1][4], 2)

    def test_dauerhafter_fehler_vom_netz(self):
        m = bare_modem(FakeGsm(reference=9))
        meldungen = mit_meldungen(m)
        s = new_sms()
        m._do_send_sms(s)
        m._handle_status_report(Report(9, 68))
        self.assertEqual(meldungen[-1][:2], (s.get_id(), "fehlgeschlagen"))
        self.assertIn("68", meldungen[-1][5])

    def test_voruebergehender_fehler_bleibt_angenommen(self):
        m = bare_modem(FakeGsm(reference=9))
        meldungen = mit_meldungen(m)
        m._do_send_sms(new_sms())
        m._handle_status_report(Report(9, 48))
        self.assertEqual([x[1] for x in meldungen], ["angenommen"])

    def test_aufgegeben_wird_gemeldet(self):
        m = bare_modem(FakeGsm(errors=[CmsError("AT+CMGS", 1)]))
        meldungen = mit_meldungen(m)
        s = new_sms()
        m._do_send_sms(s)
        self.assertEqual(meldungen[-1][:2], (s.get_id(), "fehlgeschlagen"))

    def test_selbsttest_wird_nicht_gemeldet(self):
        m = bare_modem(FakeGsm(reference=3))
        meldungen = mit_meldungen(m)
        s = sms.SMS(sms_id=None, recipient="+4915100000", text="health-check-abc", sender="")
        m._do_send_sms(s)
        m._handle_status_report(Report(3, 0))
        self.assertEqual(meldungen, [])

    def test_pool_meldet_aufgegebene_sms_ohne_modem(self):
        p = modempool.ModemPool(600)
        p.set_event_thread(threading.Event())
        meldungen = []
        p.set_status_callback(lambda *a: meldungen.append(a))
        s = new_sms(age_seconds=modem.MAX_SEND_AGE_SECONDS + 1)
        p.sms_queue_out.put(s)
        p.process_outgoing_sms()
        self.assertEqual(meldungen[0][:2], (s.get_id(), "fehlgeschlagen"))


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params):
        if self.conn.fehler:
            self.conn.fehler -= 1
            raise RuntimeError("DB weg")
        self.conn.ausgefuehrt.append(params)


class FakeConn:
    def __init__(self, fehler=0):
        self.fehler = fehler
        self.ausgefuehrt = []
        self.commits = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def close(self):
        pass


class TestSchreiber(unittest.TestCase):

    def test_schreibt_upsert_mit_den_werten(self):
        conn = FakeConn()
        w = zustellstatus.ZustellStatusSchreiber(lambda: conn, wartezeiten=(0, 0), starten=False)
        w.melde("abc", "zugestellt", teile=2, teile_zugestellt=2)
        self.assertTrue(w.verarbeite(w.warteschlange.get_nowait()))
        self.assertEqual(conn.ausgefuehrt[0][0:2], ("abc", "zugestellt"))
        self.assertEqual(conn.ausgefuehrt[0][3:5], (2, 2))
        self.assertEqual(conn.commits, 1)

    def test_wiederholt_nach_fehler_und_gibt_dann_auf(self):
        conn = FakeConn(fehler=1)
        w = zustellstatus.ZustellStatusSchreiber(lambda: conn, wartezeiten=(0, 0), starten=False)
        w.melde("abc", "angenommen")
        self.assertTrue(w.verarbeite(w.warteschlange.get_nowait()))
        conn2 = FakeConn(fehler=5)
        w2 = zustellstatus.ZustellStatusSchreiber(lambda: conn2, wartezeiten=(0, 0), starten=False)
        w2.melde("abc", "angenommen")
        self.assertFalse(w2.verarbeite(w2.warteschlange.get_nowait()))

    def test_unbekannter_status_wird_ignoriert(self):
        w = zustellstatus.ZustellStatusSchreiber(lambda: FakeConn(), starten=False)
        w.melde("abc", "irgendwas")
        self.assertTrue(w.warteschlange.empty())


if __name__ == "__main__":
    unittest.main()
