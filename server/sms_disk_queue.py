import datetime
import json
import logging
import os
import sms

class StubModem:
    def __init__(self, identifier):
        self.identifier = identifier

    def get_identifier(self) -> str:
        return self.identifier

    def get_current_network(self) -> str:
        return "Unknown"

class SMSDiskQueue:
    def __init__(self, config=None):
        self.l = logging.getLogger("SMSDiskQueue")
        
        # Try to retrieve custom temporary queue directory from config
        self.queue_dir = "/tmp/smsgate_queue"
        if config:
            if config.has_section("dbstore") and config.has_option("dbstore", "queue_dir"):
                self.queue_dir = config.get("dbstore", "queue_dir")
            elif config.has_section("server") and config.has_option("server", "temp_dir"):
                self.queue_dir = config.get("server", "temp_dir")
                
        self.l.info(f"Initializing SMSDiskQueue with directory: {self.queue_dir}")
        try:
            os.makedirs(self.queue_dir, exist_ok=True)
        except Exception as e:
            self.l.error(f"Failed to create queue directory {self.queue_dir}: {e}")

    def _sms_to_dict(self, sms_obj: sms.SMS) -> dict:
        modem_id = None
        if sms_obj.receiving_modem:
            if hasattr(sms_obj.receiving_modem, "get_identifier"):
                modem_id = sms_obj.receiving_modem.get_identifier()
            elif isinstance(sms_obj.receiving_modem, str):
                modem_id = sms_obj.receiving_modem

        return {
            "sms_id": sms_obj.sms_id,
            "recipient": sms_obj.recipient,
            "text": sms_obj.text,
            "timestamp": sms_obj.timestamp.isoformat() if sms_obj.timestamp else None,
            "created_timestamp": sms_obj.created_timestamp.isoformat() if sms_obj.created_timestamp else None,
            "sender": sms_obj.sender,
            "receiving_modem_id": modem_id,
            "flash": sms_obj.flash,
            "concat_ref": sms_obj.concat_ref,
            "concat_parts": sms_obj.concat_parts,
            "concat_num": sms_obj.concat_num,
        }

    def _dict_to_sms(self, d: dict, modems_pool=None) -> sms.SMS:
        modem_id = d.get("receiving_modem_id")
        modem_obj = None
        if modem_id:
            if modems_pool and hasattr(modems_pool, "modems") and modem_id in modems_pool.modems:
                modem_obj = modems_pool.modems[modem_id]
            else:
                modem_obj = StubModem(modem_id)

        timestamp = None
        if d.get("timestamp"):
            try:
                timestamp = datetime.datetime.fromisoformat(d["timestamp"])
            except Exception:
                pass

        _sms = sms.SMS(
            sms_id=d.get("sms_id"),
            recipient=d.get("recipient"),
            text=d.get("text"),
            timestamp=timestamp,
            sender=d.get("sender"),
            receiving_modem=modem_obj,
            flash=d.get("flash", False),
            concat_ref=d.get("concat_ref"),
            concat_parts=d.get("concat_parts"),
            concat_num=d.get("concat_num"),
        )
        if d.get("created_timestamp"):
            try:
                _sms.created_timestamp = datetime.datetime.fromisoformat(d["created_timestamp"])
            except Exception:
                pass
        return _sms

    def save_buffered_part(self, sms_obj: sms.SMS) -> None:
        """Save a partial (buffered) SMS segment to disk."""
        filepath = os.path.join(self.queue_dir, f"part_{sms_obj.get_id()}.json")
        try:
            data = self._sms_to_dict(sms_obj)
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.l.debug(f"Successfully saved buffered part {sms_obj.get_id()} to {filepath}")
        except Exception as e:
            self.l.error(f"Failed to save buffered part {sms_obj.get_id()} to disk: {e}")

    def delete_buffered_part(self, sms_id: str) -> None:
        """Delete a saved partial segment from disk."""
        filepath = os.path.join(self.queue_dir, f"part_{sms_id}.json")
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
                self.l.debug(f"Deleted buffered part file: {filepath}")
            except Exception as e:
                self.l.error(f"Failed to delete buffered part file {filepath}: {e}")

    def save_queued_sms(self, sms_obj: sms.SMS) -> None:
        """Save a fully completed / regular SMS waiting for database delivery to disk."""
        filepath = os.path.join(self.queue_dir, f"queued_{sms_obj.get_id()}.json")
        try:
            data = self._sms_to_dict(sms_obj)
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.l.debug(f"Successfully saved queued SMS {sms_obj.get_id()} to {filepath}")
        except Exception as e:
            self.l.error(f"Failed to save queued SMS {sms_obj.get_id()} to disk: {e}")

    def delete_queued_sms(self, sms_id: str) -> None:
        """Delete a saved queued SMS from disk after successful delivery."""
        filepath = os.path.join(self.queue_dir, f"queued_{sms_id}.json")
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
                self.l.debug(f"Deleted queued SMS file: {filepath}")
            except Exception as e:
                self.l.error(f"Failed to delete queued SMS file {filepath}: {e}")

    def load_all_buffered_parts(self, modems_pool=None) -> list:
        """Load and return all buffered parts saved on disk."""
        parts = []
        if not os.path.exists(self.queue_dir):
            return parts

        for filename in os.listdir(self.queue_dir):
            if filename.startswith("part_") and filename.endswith(".json"):
                filepath = os.path.join(self.queue_dir, filename)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    _sms = self._dict_to_sms(data, modems_pool)
                    parts.append(_sms)
                except Exception as e:
                    self.l.error(f"Failed to load buffered part from file {filepath}: {e}")
        return parts

    def load_all_queued_sms(self, modems_pool=None) -> list:
        """Load and return all queued SMS saved on disk."""
        queued = []
        if not os.path.exists(self.queue_dir):
            return queued

        for filename in os.listdir(self.queue_dir):
            if filename.startswith("queued_") and filename.endswith(".json"):
                filepath = os.path.join(self.queue_dir, filename)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    _sms = self._dict_to_sms(data, modems_pool)
                    queued.append(_sms)
                except Exception as e:
                    self.l.error(f"Failed to load queued SMS from file {filepath}: {e}")
        return queued
