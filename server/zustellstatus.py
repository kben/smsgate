# -----------------------------------------------------------------------------
# Delivery status of outgoing SMS for aidlink (19.11.2026)
# -----------------------------------------------------------------------------
"""
Reports the delivery status of outgoing SMS to the aidlink database (table sms_zustellung, keyed by the SMS id
the gateway returned to aidlink): 'angenommen' (accepted by the network), 'zugestellt' (all parts delivered),
'fehlgeschlagen' (permanent error, given up). A trigger in aidlink copies the status onto the message.

Own thread and own connection: the modem threads only put events into a queue and never wait for the database;
the existing path for incoming SMS stays untouched. A failed write is retried with growing pauses.
"""
import datetime
import logging
import queue
import threading
import time
from typing import Callable, Optional

STATUS = ("angenommen", "zugestellt", "fehlgeschlagen")

# Forward only (the aidlink function sms_status_rang decides): a late 'angenommen' never overwrites 'zugestellt'.
UPSERT = """
INSERT INTO public.sms_zustellung (sms_id, status, status_am, teile, teile_zugestellt, text, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, now())
ON CONFLICT (sms_id) DO UPDATE SET
  status = EXCLUDED.status,
  status_am = EXCLUDED.status_am,
  teile = COALESCE(EXCLUDED.teile, public.sms_zustellung.teile),
  teile_zugestellt = COALESCE(EXCLUDED.teile_zugestellt, public.sms_zustellung.teile_zugestellt),
  text = EXCLUDED.text,
  updated_at = now()
WHERE public.sms_status_rang(public.sms_zustellung.status) < public.sms_status_rang(EXCLUDED.status)
"""

# pauses between write attempts of one event (seconds); afterwards the event is dropped and logged
WARTEZEITEN = (5, 15, 30, 60, 120, 300)


class ZustellStatusSchreiber:
    def __init__(self, verbinde: Callable[[], object], wartezeiten=WARTEZEITEN, starten: bool = True) -> None:
        """
        @param verbinde: returns a new DB-API connection (psycopg2.connect with the dbstore settings)
        """
        self.l = logging.getLogger("ZustellStatus")
        self.verbinde = verbinde
        self.wartezeiten = wartezeiten
        self.warteschlange: "queue.Queue[dict]" = queue.Queue()
        self.verbindung = None
        if starten:
            threading.Thread(target=self._laufen, daemon=True, name="zustellstatus").start()

    def melde(self, sms_id: str, status: str, am: Optional[datetime.datetime] = None, teile: Optional[int] = None,
              teile_zugestellt: Optional[int] = None, text: Optional[str] = None) -> None:
        """Queue a status (never blocks, never raises)."""
        if status not in STATUS or not sms_id:
            return
        am = am or datetime.datetime.now(datetime.timezone.utc)
        self.warteschlange.put({"sms_id": sms_id, "status": status, "am": am, "teile": teile,
                                "teile_zugestellt": teile_zugestellt, "text": text, "versuch": 0})

    def schreibe(self, e: dict) -> None:
        """One write; raises on error (connection is reset then)."""
        try:
            if self.verbindung is None:
                self.verbindung = self.verbinde()
            cur = self.verbindung.cursor()
            cur.execute(UPSERT, (e["sms_id"], e["status"], e["am"], e["teile"], e["teile_zugestellt"], e["text"]))
            self.verbindung.commit()
        except Exception:
            try:
                if self.verbindung is not None:
                    self.verbindung.close()
            except Exception:
                pass
            self.verbindung = None
            raise

    def verarbeite(self, e: dict) -> bool:
        """Write one event; on error wait and retry up to len(wartezeiten) times. Returns True when written."""
        while True:
            try:
                self.schreibe(e)
                self.l.info(f"[{e['sms_id']}] Zustellstatus '{e['status']}' an aidlink gemeldet.")
                return True
            except Exception as ex:
                if e["versuch"] >= len(self.wartezeiten):
                    self.l.error(f"[{e['sms_id']}] Zustellstatus '{e['status']}' nicht gemeldet, aufgegeben: {ex}")
                    return False
                pause = self.wartezeiten[e["versuch"]]
                e["versuch"] += 1
                self.l.warning(f"[{e['sms_id']}] Zustellstatus '{e['status']}' nicht gemeldet ({ex}) — "
                               f"neuer Versuch in {pause} s.")
                time.sleep(pause)

    def _laufen(self) -> None:
        while True:
            e = self.warteschlange.get()
            try:
                self.verarbeite(e)
            except Exception as ex:  # never let the thread die
                self.l.error(f"Zustellstatus: unerwarteter Fehler: {ex}")
