# -----------------------------------------------------------------------------
# Copyright (c) 2022-2026 Martin Schobert, Pentagrid AG
#
# All rights reserved.
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
#  ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
#  WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
#  DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
#  ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
#  (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
#  LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
#  ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
#  (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
#  SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
#  The views and conclusions contained in the software and documentation are those
#  of the authors and should not be interpreted as representing official policies,
#  either expressed or implied, of the project.
#
#  NON-MILITARY-USAGE CLAUSE
#  Redistribution and use in source and binary form for military use and
#  military research is not permitted. Infringement of these clauses may
#  result in publishing the source code of the utilizing applications and
#  libraries to the public. As this software is developed, tested and
#  reviewed by *international* volunteers, this clause shall not be refused
#  due to the matter of *national* security concerns.
# -----------------------------------------------------------------------------
import binascii
import datetime
import glob
import helper
import logging
import queue
import random
import re
import sys
import threading
import time
import traceback
import uuid
from typing import Optional, List, Tuple

import serial
from gsmmodem.exceptions import (
    TimeoutException,
    PinRequiredError,
    IncorrectPinError,
    GsmModemException,
    CmeError,
    CmsError,
    CommandError,
)
from gsmmodem.modem import GsmModem, SerialComms, SentSms, ReceivedSms, Sms
from gsmmodem.pdu import decodeSmsPdu, Concatenation
import gsmmodem.modem
import gsmmodem.pdu

# Patch GsmModem class-level regexes to prevent AttributeError/NoneType match errors
GsmModem.CMGR_SM_DELIVER_REGEX_TEXT = re.compile(r'^\+CMGR: "([^"]+)","([^"]+)",[^,]*,"([^"]+)"$')
GsmModem.CMGR_SM_REPORT_REGEXT_TEXT = re.compile(r'^\+CMGR: ([^,]*),\d+,(\d+),"{0,1}([^"]*)"{0,1},\d*,"([^"]+)","([^"]+)",(\d+)$')
GsmModem.CMGR_REGEX_PDU = re.compile(r'^\+CMGR:\s*(\d*),\s*"{0,1}([^"]*)"{0,1},\s*(\d+)$')

# Patch gsmmodem's encodeUcs2 to natively support surrogate pairs / emojis
# and prevent ValueError: byte must be in range(0, 256) as well as UnicodeEncodeError
def _patched_encode_ucs2(text):
    return bytearray(text.encode("utf-16-be", "surrogatepass"))

gsmmodem.pdu.encodeUcs2 = _patched_encode_ucs2


# Patch gsmmodem's divideTextUcs2 to split based on UTF-16 code units (2 bytes per unit)
# instead of Python string length to avoid generating over-sized PDUs (>140 bytes) which
# are rejected by modems/SMSCs.
def _patched_divide_text_ucs2(plainText):
    chunks = []
    current_chunk = []
    current_units = 0
    max_units = 67  # MAX_MULTIPART_MESSAGE_LENGTH[0x08]
    for char in plainText:
        code_units = len(char.encode("utf-16-be", "surrogatepass")) // 2
        if current_units + code_units > max_units:
            chunks.append("".join(current_chunk))
            current_chunk = [char]
            current_units = code_units
        else:
            current_chunk.append(char)
            current_units += code_units
    if current_chunk:
        chunks.append("".join(current_chunk))
    return chunks

gsmmodem.pdu.divideTextUcs2 = _patched_divide_text_ucs2


# Patch GsmModem to serialize all operations and protect multi-step transactions (like SMS sending) from being interleaved by notifications or concurrent commands.
import functools

_original_gsm_modem_init = GsmModem.__init__

def _patched_gsm_modem_init(self, *args, **kwargs):
    _original_gsm_modem_init(self, *args, **kwargs)
    self._command_lock = threading.RLock()
    self._smsRef = random.randint(0, 255)

GsmModem.__init__ = _patched_gsm_modem_init

def locked_method(original_method):
    @functools.wraps(original_method)
    def wrapper(self, *args, **kwargs):
        lock = getattr(self, '_command_lock', None)
        if lock is None:
            lock = self._command_lock = threading.RLock()
        with lock:
            return original_method(self, *args, **kwargs)
    return wrapper

# Serialize all high-level methods of GsmModem
GsmModem.sendSms = locked_method(GsmModem.sendSms)
GsmModem.sendUssd = locked_method(GsmModem.sendUssd)
GsmModem.readStoredSms = locked_method(GsmModem.readStoredSms)
GsmModem.deleteStoredSms = locked_method(GsmModem.deleteStoredSms)
GsmModem.listStoredSms = locked_method(GsmModem.listStoredSms)
GsmModem.deleteMultipleStoredSms = locked_method(GsmModem.deleteMultipleStoredSms)

if hasattr(GsmModem, '_GsmModem__threadedHandleModemNotification'):
    GsmModem._GsmModem__threadedHandleModemNotification = locked_method(GsmModem._GsmModem__threadedHandleModemNotification)

_original_handleSmsReceived = GsmModem._handleSmsReceived

@functools.wraps(_original_handleSmsReceived)
def _patched_handleSmsReceived(self, notificationLine):
    for attempt in range(3):
        try:
            return _original_handleSmsReceived(self, notificationLine)
        except CmsError as e:
            if e.code == 321:
                if attempt < 2:
                    self.log.debug('SMS message at index invalid (CMS 321) on attempt %d; retrying in 200ms...', attempt + 1)
                    time.sleep(0.2)
                    continue
                self.log.warning('SMS message at index already cleared or invalid (CMS 321) for notification %r', notificationLine)
            else:
                self.log.error('Error handling received SMS notification %r: %s', notificationLine, e, exc_info=True)
                break
        except (TimeoutException, serial.SerialException, AttributeError) as e:
            self.log.warning('Modem connection lost or closed while handling received SMS notification %r: %s', notificationLine, e)
            break
        except Exception as e:
            self.log.error('Error handling received SMS notification %r: %s', notificationLine, e, exc_info=True)
            break

GsmModem._handleSmsReceived = _patched_handleSmsReceived


_original_handleSmsStatusReport = GsmModem._handleSmsStatusReport

@functools.wraps(_original_handleSmsStatusReport)
def _patched_handleSmsStatusReport(self, notificationLine):
    for attempt in range(3):
        try:
            return _original_handleSmsStatusReport(self, notificationLine)
        except CmsError as e:
            if e.code == 321:
                if attempt < 2:
                    self.log.debug('SMS status report at index invalid (CMS 321) on attempt %d; retrying in 200ms...', attempt + 1)
                    time.sleep(0.2)
                    continue
                self.log.warning('SMS status report at index already cleared or invalid (CMS 321) for notification %r', notificationLine)
            else:
                self.log.error('Error handling SMS status report notification %r: %s', notificationLine, e, exc_info=True)
                break
        except (TimeoutException, serial.SerialException, AttributeError) as e:
            self.log.warning('Modem connection lost or closed while handling SMS status report notification %r: %s', notificationLine, e)
            break
        except Exception as e:
            self.log.error('Error handling SMS status report notification %r: %s', notificationLine, e, exc_info=True)
            break

GsmModem._handleSmsStatusReport = _patched_handleSmsStatusReport


_original_handleSmsStatusReportTe = GsmModem._handleSmsStatusReportTe

@functools.wraps(_original_handleSmsStatusReportTe)
def _patched_handleSmsStatusReportTe(self, length, notificationLine):
    for attempt in range(3):
        try:
            return _original_handleSmsStatusReportTe(self, length, notificationLine)
        except CmsError as e:
            if e.code == 321:
                if attempt < 2:
                    self.log.debug('TE SMS status report at index invalid (CMS 321) on attempt %d; retrying in 200ms...', attempt + 1)
                    time.sleep(0.2)
                    continue
                self.log.warning('TE SMS status report at index already cleared or invalid (CMS 321) for notification %r', notificationLine)
            else:
                self.log.error('Error handling TE SMS status report notification %r (length %s): %s', notificationLine, length, e, exc_info=True)
                break
        except (TimeoutException, serial.SerialException, AttributeError) as e:
            self.log.warning('Modem connection lost or closed while handling TE SMS status report notification %r: %s', notificationLine, e)
            break
        except Exception as e:
            self.log.error('Error handling TE SMS status report notification %r (length %s): %s', notificationLine, length, e, exc_info=True)
            break

GsmModem._handleSmsStatusReportTe = _patched_handleSmsStatusReportTe


if hasattr(GsmModem, '_GsmModem__threadedHandleModemNotification'):
    _original_threadedHandleModemNotification = GsmModem._GsmModem__threadedHandleModemNotification

    @functools.wraps(_original_threadedHandleModemNotification)
    def _patched_threadedHandleModemNotification(self, *args, **kwargs):
        try:
            return _original_threadedHandleModemNotification(self, *args, **kwargs)
        except Exception as e:
            self.log.error('Exception in notification handling thread: %s', e, exc_info=True)

    GsmModem._GsmModem__threadedHandleModemNotification = _patched_threadedHandleModemNotification


_original_gsm_modem_write = GsmModem.write

# Network registration lines: unsolicited when the modem pushes them, but the answer when we asked for them
REGISTRATION_PREFIXES = ('+CREG:', '+CGREG:', '+CEREG:')
UNSOLICITED_PREFIXES = ('+CMTI', '+CDSI', '+DTMF', '+Z', '^', '+WIND', '+STIN', '+CRING') + REGISTRATION_PREFIXES


def split_unsolicited(response_lines, command=None, response_term=None, call_status_updates=()):
    """
    Split the response of a command into its own lines and unsolicited notifications that arrived in between.
    The answer to a registration query (AT+CREG? / AT+CGREG? / AT+CEREG?) is kept — before, it was filtered as
    unsolicited, so the library never saw it and disabled the registration check ("+CREG check disabled").
    @return: (own lines, unsolicited lines)
    """
    asked = ''
    if isinstance(command, str):
        cmd = command.strip().upper()
        for prefix in REGISTRATION_PREFIXES:
            if cmd.startswith('AT' + prefix[:-1]):  # e.g. AT+CREG? -> keep +CREG:
                asked = prefix
    own, unsolicited = [], []
    next_line_is_pdu = False
    for line in response_lines:
        is_unsolicited = False
        if response_term is not None and response_term.match(line):
            pass
        elif next_line_is_pdu:
            is_unsolicited = True
            next_line_is_pdu = False
        elif asked and line.startswith(asked):
            pass
        elif line.startswith(UNSOLICITED_PREFIXES) or 'RING' in line:
            is_unsolicited = True
        elif line.startswith('+CDS') or line.startswith('+CMT'):
            is_unsolicited = True
            next_line_is_pdu = True
        else:
            for updateRegex, _ in call_status_updates:
                if updateRegex.match(line):
                    is_unsolicited = True
                    break
        (unsolicited if is_unsolicited else own).append(line)
    return own, unsolicited


@locked_method
def _patched_gsm_modem_write(self, *args, **kwargs):
    waitForResponse = True
    if len(args) > 1:
        waitForResponse = args[1]
    elif 'waitForResponse' in kwargs:
        waitForResponse = kwargs['waitForResponse']
    command = args[0] if args else kwargs.get('data')

    responseLines = _original_gsm_modem_write(self, *args, **kwargs)
    if waitForResponse and responseLines:
        responseLines, unsolicitedLines = split_unsolicited(
            responseLines, command, self.RESPONSE_TERM, getattr(self, '_callStatusUpdates', [])
        )
        if unsolicitedLines:
            self.log.debug('Filtered unsolicited lines from command response: %s', unsolicitedLines)
            self._handleModemNotification(unsolicitedLines)
    return responseLines

GsmModem.write = _patched_gsm_modem_write



import modemconfig
import sms
import serialportmapper


# Outgoing SMS older than this are given up instead of being retried forever: a login link that arrives hours
# later is useless, and a never-ending retry delays every other SMS of the modem.
MAX_SEND_AGE_SECONDS = 6 * 3600

# CMS errors that retrying will not fix (unassigned number, invalid mandatory information, invalid PDU/text
# parameters) — such an SMS is given up at once.
PERMANENT_CMS_ERRORS = {1, 96, 304, 305}

# First retry delay and upper bound (exponential backoff), in seconds.
RETRY_DELAY_START = 5
RETRY_DELAY_MAX = 60

# While a modem is not OK, its full health check (incl. the USSD balance request, which can take ~30 s) is
# repeated at most this often — before, it ran in every loop of the sending thread.
UNHEALTHY_RECHECK_SECONDS = 300

# A concatenated SMS still missing parts after this time goes on with the parts received and a note for each
# missing part (before: discarded after an hour, only when the next concatenated SMS arrived — nothing reached aidlink)
CONCAT_INCOMPLETE_SECONDS = 1800

# The same received SMS (or part) seen again within this time is ignored: the stored-message sweep may read an SMS
# whose notification is handled at the same moment
RECEIVED_DEDUP_SECONDS = 3600

# Notifications of a new stored SMS or status report; the library only handles the first one of a block
SMS_INDICATION_PREFIXES = ('+CMTI', '+CDSI')


def concat_text(parts: dict, total_parts: int) -> str:
    """
    Text of a concatenated SMS from its parts; a missing part becomes a note in its place.
    @param parts: part number -> sms.SMS
    @param total_parts: number of parts announced by the sender
    """
    texts = []
    for idx in range(1, total_parts + 1):
        part = parts.get(idx)
        texts.append(part.get_text() if part else f" [Teil {idx} von {total_parts} fehlt] ")
    return "".join(texts).strip()


def split_sms_indications(lines: List[str]) -> List[List[str]]:
    """
    Split a notification block so that every new-SMS / status-report indication is handled on its own. The library
    handles the first +CMTI of a block and returns — two SMS (or both parts of a long SMS) notified together lost the
    second one.
    @return: blocks to hand to the library one after the other
    """
    indications = [line for line in lines if line.startswith(SMS_INDICATION_PREFIXES)]
    if len(indications) <= 1:
        return [lines]
    rest = [line for line in lines if not line.startswith(SMS_INDICATION_PREFIXES)]
    return [[line] for line in indications] + ([rest] if rest else [])

# How long an SMS reference is kept to match the delivery report (status report) of the network.
DELIVERY_REPORT_KEEP_SECONDS = 3 * 24 * 3600

# How long the network keeps trying to deliver an SMS to a phone that is off or unreachable (TP validity period).
# None = default of the SMS centre (seen 19.11.2026: about two days, then status 70). Set from smsgate.conf
# ([modempool] sms_validity_days, default 4) by set_sms_validity().
SMS_VALIDITY: Optional[datetime.timedelta] = None


def set_sms_validity(days: float) -> None:
    """
    Validity period of sent SMS in days; 0 or less = leave it to the SMS centre. Kept for the delivery reports:
    DELIVERY_REPORT_KEEP_SECONDS must be longer, a failure is only reported when the period is over.
    """
    global SMS_VALIDITY, DELIVERY_REPORT_KEEP_SECONDS
    SMS_VALIDITY = datetime.timedelta(days=days) if days > 0 else None
    if SMS_VALIDITY is not None:
        DELIVERY_REPORT_KEEP_SECONDS = max(3 * 24 * 3600, int(SMS_VALIDITY.total_seconds()) + 24 * 3600)


_original_encode_submit = gsmmodem.modem.encodeSmsSubmitPdu


def _encode_submit_with_validity(*args, **kwargs):
    """The library's sendSms() sets no validity period — add ours (relative, e.g. 4 days)."""
    if SMS_VALIDITY is not None and kwargs.get('validity') is None:
        kwargs['validity'] = SMS_VALIDITY
    return _original_encode_submit(*args, **kwargs)


gsmmodem.modem.encodeSmsSubmitPdu = _encode_submit_with_validity

# TP-Status of a status report (3GPP TS 23.040 9.2.3.15): 0-31 delivered, 32-63 temporary error (the SMSC keeps
# trying), 64 and above permanent error.
def delivery_status_text(status: Optional[int]) -> str:
    if status is None:
        return "unknown status"
    if status <= 31:
        return "delivered"
    if status <= 63:
        return f"not yet delivered, SMSC keeps trying (status {status})"
    return f"delivery failed (status {status})"


def part_count(recipient: str, text: str) -> int:
    """Number of PDUs (parts) the library sends for this text — same encoding as GsmModem.sendSms."""
    try:
        return max(1, len(gsmmodem.pdu.encodeSmsSubmitPdu(recipient, text or "")))
    except Exception:
        return 1


def part_references(last_reference: int, parts: int) -> List[int]:
    """
    Network references of all parts of a multipart SMS. sendSms only returns the reference of the last part; the
    modem numbers the parts consecutively (mod 256), so the earlier parts are the references before it.
    """
    return [(last_reference - parts + 1 + i) % 256 for i in range(parts)]


def retry_decision(age_seconds: float, error: Exception, last_delay: Optional[float]) -> Tuple[bool, float]:
    """
    Decide what happens with an SMS whose sending failed.
    @param last_delay: delay before the previous attempt, None on the first failure.
    @return: (give_up, delay) — give_up for permanent errors or SMS older than MAX_SEND_AGE_SECONDS, otherwise the
        delay before the next attempt: RETRY_DELAY_START, then doubled up to RETRY_DELAY_MAX.
    """
    if isinstance(error, CmsError) and getattr(error, "code", None) in PERMANENT_CMS_ERRORS:
        return True, 0
    if age_seconds >= MAX_SEND_AGE_SECONDS:
        return True, 0
    if last_delay is None:
        return False, RETRY_DELAY_START
    return False, min(last_delay * 2, RETRY_DELAY_MAX)



class Modem(threading.Thread):
    def __init__(self, identifier: str, modem_config: modemconfig.ModemConfig, serial_ports_hint_file: str, sms_disk_queue=None) -> None:
        assert identifier is not None
        self.event_available = None
        self.identifier = identifier
        self.modem_config = modem_config
        self.serial_ports_hint_file = serial_ports_hint_file
        self.sms_disk_queue = sms_disk_queue

        self.balance = None
        self.sms_receiver_queue = queue.Queue()
        self.sms_sender_queue = queue.Queue()
        self.sent_sms = {}  # a dict of sent SMS; value is an SMS object
        # SMS waiting for their next attempt after a failure: list of (due timestamp, SMS) — the sending thread
        # does not sleep for retries any more, other SMS keep going out
        self.retry_sms = []
        # network reference of an accepted SMS -> (sms_id, recipient, accepted at), to log delivery reports
        self.sent_by_reference = {}
        # delivery status for aidlink (zustellstatus.py): callback(sms_id, status, am, teile, teile_zugestellt, text)
        self.status_callback = None
        # sms_id -> {"teile": n, "zugestellt": set of delivered part numbers} until the SMS is delivered or failed
        self.zustellung = {}
        self.received_concat_buffer = {}  # key: (sender, concat_ref) -> dict of {num: SMS_object}
        self.processed_cmt_signatures = {}  # dict of (sender, text) -> timestamp for recently received CMT messages
        self.recent_received = {}  # signature of a received SMS or part -> when seen (RECEIVED_DEDUP_SECONDS)
        self._receive_lock = threading.RLock()  # concat buffer and recent_received (notification threads, sweep)

        self.last_health_check = None
        self.health_state = "OK"
        self.health_logs = None
        self.health_check_expected_token = None

        self.modem = None
        # memory incoming SMS are stored in (ME/SM), chosen by the library at connect — reading a delivery report
        # switches the library's read memory to "SR" and keeps it there (see process_stored_sms)
        self.receive_memory = None
        self.current_port = None
        self.status = None
        self.init_counter = 0
        self.last_init = datetime.datetime.now(tz=None)
        self.last_received = None
        self.last_sent = None

        self.stats_sent_sms = 0
        self.stats_received_sms = 0

        self.current_network = None
        self.current_signal = 0

        self.send_last_call_time = 0

        self.l = logging.getLogger(f"Modem [{identifier}]")
        # references and open deliveries survive a restart: reports arrive up to days later (validity period)
        self._zustellstand_laden()

        threading.Thread.__init__(self)
        self.start()

    def get_identifier(self) -> str:
        """
        Get the modem name.
        @return: Returns the modem identifier as string.
        """
        return self.identifier

    def get_prefixes(self) -> List[str]:
        """
        Get the phone prefixes that this modem is responsible for.
        For each modem, it is possible to define a list of phone number prefixes like "+49172", "+49", that this modem
        is responsible for. If you have multiple SIM cards, it allows you to do routing decisions.
        @return: Returns a list of phone number prefixes for a modem.
        """
        return self.modem_config.prefixes

    def get_costs(self) -> float:
        """
        Get costs for sending a single SMS.
        This information is used for routing decisions. It is just a number and not strictly related to a currency, but
        usually refers to the currency that is mentioned in the ModemConfig.
        @return: Returns the costs per SMS.
        """
        return self.modem_config.costs_per_sms

    def get_phone_number(self) -> str:
        """
        Get the phone numer associated with a modem.
        Per convention phone numbers are entered everywhere in E.123 international notation.
        @return: Returns the phone number as string.
        """
        return self.modem_config.phone_number

    def set_event_thread(self, event_available: threading.Event) -> None:
        """
        Pass a threading.Event object to Modem object to get notified about incoming SMS.
        @param event_available: a threading.Event object to signal between threads. For an incoming SMS, the event
            object's method set() is called.
        """
        self.event_available = event_available

    def get_balance(self) -> float:
        """
        Returns the modem's SIM card account balance.
        @return: Returns the account balance as float or None, if the balance is not known (yet).
        """
        return self.balance

    def get_currency(self) -> str:
        """
        Returns the modem's SIM card account balance currency.
        @return: A string indicating the currency, for example "EUR" or "CHF".
        """
        return self.modem_config.ussd_currency

    def get_current_network(self) -> Optional[str]:
        """
        Get the current network name that a modem is using.
        @return: Returns the network name as string, for example "Vodafone".
        """
        return self.current_network

    def get_current_signal_rssi(self) -> int:
        """
        Get the received signal strength indicator (RSSI) value.
        The value is requested via the 'AT+CSQ' command.
        @return: Returns the signal strength as a value between 0 and 00, or -1 if it is unknown.
        """
        return self.current_signal

    def get_modem_config(self) -> modemconfig.ModemConfig:
        """
        Get the modem configuration
        @return: Returns the config as ModemConfiguration object.
        """
        return self.modem_config

    def get_current_signal_dB(self) -> int:
        """
        Get the signal strength as dBm value (decibel referring to mW).
        The RSSI maps to dBm as below:
        0 113 dBm or less
        1 111 dBm
        2...30 109... 53 dBm
        31 51 dBm or greater
        99 not known or not detectable
        Here is a detailed table: https://m2msupport.net/m2msupport/atcsq-signal-quality/
        @return: An int value representing a dBm value.
        """
        i = self.current_signal
        if 2 <= i <= 30:
            return [
                -109,
                -107,
                -105,
                -103,
                -101,
                -99,
                -97,
                -95,
                -93,
                -91,
                -89,
                -87,
                -85,
                -83,
                -81,
                -79,
                -77,
                -75,
                -73,
                -71,
                -69,
                -67,
                -65,
                -63,
                -61,
                -59,
                -57,
                -55,
                -53,
            ][i - 2]
        elif i >= 31:
            return -51
        else:
            return -113

    def get_port(self) -> Optional[str]:
        """
        Get the device file name for the modem's serial port.
        @return: Returns a string with the serial port name or None, if there is no port known (yet).
        """
        return self.current_port

    def get_status(self) -> Optional[str]:
        """
        Get a textual string representing the current state of the modem in human-readable form.
        @return: Returns a message indicating the modem's current state. Value may be None.
        """
        return self.status

    def get_init_counter(self) -> int:
        """
        Get the counter for modem initialisations.
        Each time a modem is initialized, the counter gets increased. An initialisation happens or is necessary after
        internal errors or at server startup. Therefore, the counter indicates there is an issue that results in
        reinitialisations and it is observable in comparison with other modem objects.
        @return: Returns a counter how many modem initialisations happened during the server uptime.
        """
        return self.init_counter

    def get_last_init(self) -> datetime.datetime:
        """
        Get the timestamp of the last initialisation.
        @return: Returns the last initialisation as datetime object.
        """
        return self.last_init

    def get_last_received(self) -> Optional[datetime.datetime]:
        """
        Get the timestamp of the last received SMS.
        @return: Returns the timestamp of the last received SMS as datetime object or None.
        """
        return self.last_received

    def get_last_sent(self) -> Optional[datetime.datetime]:
        """
        Get the timestamp of the last sent SMS.
        @return: Returns the timestamp of the last sent SMS as datetime object or None.
        """
        return self.last_sent

    def get_stats_sent_sms(self) -> int:
        """
        Get the counter for sent SMS.
        @return: Returns number of sent SMS.
        """
        return self.stats_sent_sms

    def get_stats_received_sms(self) -> int:
        """
        Get the counter for received SMS.
        @return: Returns number of received SMS.
        """
        return self.stats_received_sms

    def _handle_incoming_gsmmodem_sms(self, _sms: ReceivedSms) -> None:
        """
        Handle incoming SMS from the python-gsmmodem-new layer.
        The _sms parameter is a gsmmodem.modem.ReceivedSms.
        """
        # If this message was already processed in _handleModemNotification, skip it
        now = helper.ensure_aware(datetime.datetime.now())

        # Clean up entries older than 30 seconds to prevent memory leaks
        self.processed_cmt_signatures = {
            k: ts for k, ts in self.processed_cmt_signatures.items()
            if (now - helper.ensure_aware(ts)).total_seconds() < 30
        }

        sig = (_sms.number, _sms.text)
        if sig in self.processed_cmt_signatures:
            self.l.debug("SMS already processed via _handleModemNotification, ignoring duplicate callback.")
            del self.processed_cmt_signatures[sig]
            return

        # Extract UDH/concatenation info if present
        udh = getattr(_sms, "udh", []) or []
        concat_ref = None
        concat_parts = None
        concat_num = None
        for ie in udh:
            if isinstance(ie, Concatenation):
                concat_ref = ie.reference
                concat_parts = ie.parts
                concat_num = ie.number
                break

        new_sms = sms.SMS(
            sms_id=None,
            recipient=self.modem_config.phone_number,
            text=_sms.text,
            sender=_sms.number,
            timestamp=_sms.time,
            receiving_modem=self,
            concat_ref=concat_ref,
            concat_parts=concat_parts,
            concat_num=concat_num,
        )

        self._handle_incoming_sms(new_sms)

    def _handle_incoming_sms(self, _sms: sms.SMS) -> None:
        """
        Handle incoming SMS.
        The _sms parameter is a sms.SMS object. Parts of a concatenated SMS are buffered until all parts are there
        (or CONCAT_INCOMPLETE_SECONDS passed, see deliver_overdue_concat_sms).
        """
        if self._seen_before(_sms):
            self.l.info(f"Ignoring SMS from {_sms.get_sender()} handled before (duplicate notification or stored-message sweep).")
            return

        if _sms.is_concatenated():
            _sms = self._add_concat_part(_sms)
            if _sms is None:
                return  # Do not deliver the partial segment yet

        self._deliver_received_sms(_sms)

    def _seen_before(self, _sms: sms.SMS) -> bool:
        """
        Remember a received SMS (or part) and tell whether it was seen within RECEIVED_DEDUP_SECONDS.
        """
        now = helper.ensure_aware(datetime.datetime.now())
        ts = _sms.get_timestamp()
        signature = (
            _sms.get_sender(),
            _sms.get_concat_ref(),
            _sms.get_concat_num(),
            _sms.get_text(),
            ts.isoformat() if ts else None,
        )
        with self._receive_lock:
            self.recent_received = {
                k: seen for k, seen in self.recent_received.items()
                if (now - seen).total_seconds() < RECEIVED_DEDUP_SECONDS
            }
            if signature in self.recent_received:
                return True
            self.recent_received[signature] = now
            return False

    def _add_concat_part(self, _sms: sms.SMS) -> Optional[sms.SMS]:
        """
        Buffer one part of a concatenated SMS.
        @return: The reassembled SMS once all parts are there, otherwise None.
        """
        now = helper.ensure_aware(datetime.datetime.now())
        sender = _sms.get_sender() or "unknown"
        ref = _sms.get_concat_ref()
        num = _sms.get_concat_num()
        total_parts = _sms.get_concat_parts()

        self.l.info(f"Received concatenated SMS part {num}/{total_parts} with ref {ref} from {sender}")
        key = (sender, ref)

        with self._receive_lock:
            entry = self.received_concat_buffer.get(key)
            if entry is not None:
                age = (now - helper.ensure_aware(entry['first_seen'])).total_seconds()
                other_text = num in entry['parts'] and entry['parts'][num].get_text() != _sms.get_text()
                if age > CONCAT_INCOMPLETE_SECONDS or other_text:
                    # the reference is reused by a new message: the old one goes on as far as it came
                    self.l.warning(f"New message with ref {ref} from {sender} while an older one is incomplete — delivering the older one as received.")
                    self._deliver_incomplete_concat(key)
                    entry = None

            if entry is None:
                entry = self.received_concat_buffer[key] = {
                    'parts': {},
                    'total_parts': total_parts,
                    'first_seen': now,
                    'recipient': _sms.get_recipient(),
                    'receiving_modem': _sms.get_receiving_modem(),
                    'flash': _sms.is_flash(),
                    'timestamp': _sms.get_timestamp(),
                }

            # Store/overwrite the part (handles duplicate delivery gracefully)
            entry['parts'][num] = _sms
            if self.sms_disk_queue:
                self.sms_disk_queue.save_buffered_part(_sms)

            if len(entry['parts']) < entry['total_parts']:
                self.l.info(f"Buffered part {num}/{total_parts} of ref {ref} from {sender}. Currently have {len(entry['parts'])}/{entry['total_parts']} parts.")
                return None

            self.l.info(f"All {entry['total_parts']} parts of concatenated SMS with ref {ref} from {sender} received. Reassembling...")
            return self._take_concat(key)

    def _take_concat(self, key) -> sms.SMS:
        """
        Remove a concatenated SMS from the buffer (memory and disk) and build one SMS of its parts.
        """
        entry = self.received_concat_buffer.pop(key)
        if self.sms_disk_queue:
            for part_sms in entry['parts'].values():
                self.sms_disk_queue.delete_buffered_part(part_sms.get_id())
        return sms.SMS(
            sms_id=None,
            recipient=entry['recipient'],
            text=concat_text(entry['parts'], entry['total_parts']),
            timestamp=entry['timestamp'],
            sender=key[0],
            receiving_modem=entry['receiving_modem'],
            flash=entry['flash']
        )

    def _deliver_incomplete_concat(self, key) -> None:
        """
        Deliver a concatenated SMS with the parts received; missing parts are marked in the text.
        """
        entry = self.received_concat_buffer[key]
        missing = [i for i in range(1, entry['total_parts'] + 1) if i not in entry['parts']]
        self.l.warning(f"Concatenated SMS with ref {key[1]} from {key[0]} incomplete (missing part(s) {missing} of {entry['total_parts']}) — delivering the parts received.")
        self._deliver_received_sms(self._take_concat(key))

    def deliver_overdue_concat_sms(self) -> int:
        """
        Deliver concatenated SMS still missing parts after CONCAT_INCOMPLETE_SECONDS (called by the health check).
        @return: Number of SMS delivered incomplete.
        """
        now = helper.ensure_aware(datetime.datetime.now())
        delivered = 0
        with self._receive_lock:
            for key, entry in list(self.received_concat_buffer.items()):
                if (now - helper.ensure_aware(entry['first_seen'])).total_seconds() >= CONCAT_INCOMPLETE_SECONDS:
                    self._deliver_incomplete_concat(key)
                    delivered += 1
        return delivered

    def process_stored_sms(self) -> int:
        """
        Handle SMS left in the modem memory — received while smsgate was not running, or whose notification got lost —
        and delete them. Called at start (before: all stored SMS were deleted unread) and by every health check.
        @return: Number of SMS found.
        """
        if self.modem is None:
            return 0
        # the same lock as all modem commands (locked_method): no notification is handled in between
        lock = getattr(self.modem, '_command_lock', None)
        if lock is None:
            lock = self.modem._command_lock = threading.RLock()
        with lock:
            try:
                # always the receive memory: after a delivery report the library's read memory is "SR", where
                # listing fails (CMS 500) and received SMS would never be found
                stored = self.modem.listStoredSms(status=Sms.STATUS_ALL, memory=self.receive_memory)
            except (CmsError, CommandError, TimeoutException) as e:
                self.l.warning(f"Could not read SMS stored in the modem: {e}")
                return 0
            received = [m for m in stored if isinstance(m, ReceivedSms)]
            if received:
                self.l.warning(f"{len(received)} SMS found in the modem memory that were not handled on arrival — handling them now.")
            for m in received:
                self._handle_incoming_gsmmodem_sms(m)
            if stored:
                # listing marked them read; new SMS arriving meanwhile stay unread and are kept
                try:
                    self.modem.deleteMultipleStoredSms(1)
                except CmsError:
                    self.l.warning("Exception: Failed to delete read SMS.")
        return len(received)

    def _deliver_received_sms(self, _sms: sms.SMS) -> None:
        """
        Hand a received (complete) SMS on to the router.
        """
        self.l.info("== SMS message received ==")

        self.last_received = datetime.datetime.now(tz=None)
        self.stats_received_sms += 1

        # first check if we sent the SMS to ourself
        if (
                self.health_check_expected_token
                and self.health_check_expected_token in _sms.text
        ):
            self.l.info("Modem received expected health SMS")
            # clear value
            self.health_check_expected_token = None

        # Only log SMS in debug mode and then without content
        self.l.debug(_sms.to_string(content=False))

        self.l.info(f"Put SMS in queue.")
        self.sms_receiver_queue.put(_sms)
        self.event_available.set()

    def get_sms(self) -> sms.SMS:
        """
        Get incoming SMS.
        @return: Returns an SMS object that is in the local SMS Queue. If there is no element in the queue, a
            queue.Empty exception is thrown, because the Python Queue class is used as underlying object.
        """
        return self.sms_receiver_queue.get(block=False)

    def has_sms(self) -> bool:
        """
        Check if there is an SMS available in the incoming queue.
        @return: Returns True or False.
        """
        return not self.sms_receiver_queue.empty()

    def _check_balance_thresholds(self) -> Tuple[str, Optional[str]]:
        """
        Check if account balance does not fall below warning or critical thresholds.
        When using prepaid SIM cards, there must be enough balance to be able to send SMS. Even if one "only" like to
        receive SMS, operators want to see financial events for SIM card to see if a SIM card is still active, which
        means it is necessary to trigger billable events from time to time. It is therefore necessary to check if there
        is enough budget available.
        @return: Returns the check result as tuple State, Message. State is either "OK", "WARNING", or "CRITICAL".
            Message is a human-readable string giving details about the issue. Message may be a None.
        """
        assert (
                self.modem_config.account_balance_critical <= self.modem_config.account_balance_warning
        )

        if self.balance < self.modem_config.account_balance_critical:
            s = f"Modem[{self.identifier}]: Critical: Account balance of {self.balance} {self.modem_config.ussd_currency} " \
                f"is lower than threshold of {self.modem_config.account_balance_critical} {self.modem_config.ussd_currency}."
            self.l.warning(s)
            return "CRITICAL", s

        elif self.balance < self.modem_config.account_balance_warning:
            s = f"Modem[{self.identifier}]: Warning: Account balance of {self.balance} {self.modem_config.ussd_currency} " \
                f"is lower than threshold of {self.modem_config.account_balance_warning} {self.modem_config.ussd_currency}."
            self.l.warning(s)
            return "WARNING", s

        else:
            return "OK", None

    def send_sms(self, _sms: sms.SMS) -> None:
        """
        Use the modem to send an SMS.
        @param _sms: An SMS object of type sms.SMS, which is sent by the modem. Use get_delivery_status() to check the
            delivery status.
        """
        self.sms_sender_queue.put(_sms)
        self.last_sent = datetime.datetime.now(tz=None)
        self.stats_sent_sms += 1

    def get_delivery_status(self, sms_id: str) -> bool:
        """
        Returns the delivery status for an SMS referenced by a UUID.
        @param sms_id: The SMS' UUID. Each sms.SMS object has a UUID and the modem object keeps track of sent SMS as
            long as the server is up and running. You can check the delivery status for a UUID.
        @return: Returns True or False. If the UUID is unknown, False is returned.
        """

        if (
                sms_id in self.sent_sms
                and self.sent_sms[sms_id].status == SentSms.DELIVERED
        ):
            return True
        return False

    def cleanup(self, sms_id: str) -> bool:
        """
        Clean up sent datastructure and remove information regarding 'sms_id'.
        This function is called by the modempool during the modem pool clean up to get rid of old data.
        @param sms_id: The SMS' UUID. The modempool also knows which SMS were sent.
        @return: Returns True or False. If the UUID is unknown, False is returned.
        """

        if sms_id in self.sent_sms:
            del self.sent_sms[sms_id]
            return True

        return False

    def get_health_state(self) -> Tuple[str, Optional[str]]:
        """
        Get the health state. A modem whose sending thread has stopped is CRITICAL, so the router uses
        another modem instead of queueing SMS that are never sent.
        @return: Returns the modem's internal health state as tuple of State, Message. State is either "OK", "WARNING",
            or "CRITICAL". Message is a human-readable string giving details about the issue. Message may be a None.
        """
        if self.ident is not None and not self.is_alive():
            return "CRITICAL", f"{self.identifier} Sending thread is not running."
        return self.health_state, self.health_logs

    def _do_health_check(self, do_now: bool = False) -> None:
        """
        Internal method that potentially triggers a health check.
        This method checks if performing a health check is necessary and will act accordingly.
        @param do_now: Boolean flag to force a health check.
        """
        now = helper.ensure_aware(datetime.datetime.now())
        last_hc = helper.ensure_aware(self.last_health_check) if self.last_health_check else None
        since_last = (now - last_hc).total_seconds() if last_hc else None
        if ((last_hc is None)
            or do_now
            or (self.health_state != "OK" and since_last >= UNHEALTHY_RECHECK_SECONDS)
            or (since_last >= self.modem_config.health_check_interval)):
            self.health_state, self.health_logs = self._really_do_health_check()

    def _repeat_initialization(self) -> None:
        """
        Repeat initialisation until it is successful.
        """
        while not self._init_modem():
            self.l.error(
                f"Initialization failed. Timeout occurred. This may mean, the modem was lost. Reinitializing modem."
            )
            self._do_health_check(do_now=True)
            time.sleep(30)

    def _find_port(self, device_name: str, expected_imei: str) -> Optional[str]:
        """
        Looks up a serial port identified by a full name or a glob and returns the serial port name.

        When using USB devices of the same USB vendor ID and product ID, the
        order of devices is not fixed and the names are not always the same.
        For example modem slot 00 could be /dev/ttyACM0, but also /dev/ttyACM3
        or any other. On the other side, we may have a real RS232 serial modem,
        which always has the same name. Therefore, we implement a lookup and
        find modems corresponding to SIM cards identified via the modem's IMEI.

        Checking the modem device here during the initialization allows us to
        reinitialize a modem also later during runtime. When the modem is
        initialized, the code checks which device should be opened. Since each thread
        does this check, there is a bit of overhead.
        @param device_name: The serial port's device file name as string. The value may contain a wildcard. If a
            wildcard is used, the modem probes for a device file names and used the IMEI to find a port. If the port
            name is fixed, the function itself does no IMEI check.
        @param expected_imei: There is a configuration entry for each modem that references the IMEI as string. The
            IMEI is the International Mobile Equipment Identity a unique ID for the modem hardware. This parameter is
            necessary to find the correct serial device.
        @return: Returns the device file name as string or None, if it was not found.
        """

        # Case 1: Device name is fixed
        if not device_name.endswith("*"):
            return device_name

        # Wait a random time, because otherwise all threads will look up their devices at the same time.
        time.sleep(random.randint(0, 15))

        # Case 2: Port is known
        spm = serialportmapper.SerialPortMapper(self.serial_ports_hint_file)
        port = spm.get_mapping(expected_imei)
        if port:
            if not self._port_was_renumbered(port):
                return port

        # Case 3: Find port
        device_list = glob.glob(device_name)
        random.shuffle(device_list)

        for f in device_list:
            self.l.info(f"Try to find correct port. Will open {f}.")
            self.status = f"Try port {f}."

            if self._check_imei(f, self.modem_config.baud, expected_imei):
                self.status = f"Port {f} found."
                return f

        self.l.error(f"Can't find modem with IMEI {expected_imei}")
        return None

    def _check_imei(self, port: str, baud: int, expected_imei: str) -> bool:
        """
        Check if the modem behind a certain port is the one with an expected IMEI.
        The function opens the serial port and asks the modem for the IMEI via AT+CGSN, which is than compared to the
        expected_imei.
        In addition, every information about IMEI to serial port mapping is stored in the SerialPortMapper instance to
        speed up lookups.
        @param port: The serial port of the modem given as device file name.
        @param baud: The symbol rate for the device file name as int, such as 9600 or 115200.
        @param expected_imei: A string with the expected IMEI.
        @return: Returns True if the modem behind a serial port has the given IMEI. Otherwise False is returned. In
            case of communication errors, None is returned.
        """
        modem = None
        try:
            modem = SerialComms(port, baud, exclusive=True)
            assert modem
            modem.connect()

            self.l.debug(f"Expected IMEI of modem: {expected_imei}")

            for i in range(0, 5):
                try:
                    modem.write("AT&F\r\n")
                    modem.write("AT&F\r\n")
                    modem.write("AT&F\r\n")
                    modem.write("ATZ\r\n")
                    modem.write("ATZ\r\n")
                    modem.write("ATZ\r\n")
                    modem.write("ATE0\r\n")  # disable echo
                    modem.write("AT&W\r\n")  # save

                    found_imei = modem.write("AT+CGSN\r\n")[0]
                    self.l.info(f"Read IMEI of modem    : {found_imei}")

                    if expected_imei is not None:
                        spm = serialportmapper.SerialPortMapper(self.serial_ports_hint_file)
                        if expected_imei == found_imei:
                            spm.set_mapping(found_imei, port)
                            self.l.info(f"Modem found on serial port {port}.")
                            modem.close()
                            return True
                        elif re.match(r"^\d+$", found_imei):
                            spm.set_mapping(found_imei, port)
                            self.l.debug(f"Unexpected modem at port {port}.")
                            modem.close()
                            return False
                        else:
                            self.l.debug(
                                f"Unexpected answer from modem at port {port}."
                            )

                except TimeoutException:
                    pass

            modem.close()

        except serial.serialutil.SerialException as e:
            self.l.error("_check_imei: Got exception: " + str(e))
            # We do not have a modem object here. Therefore, there is nothing to close.
        except GsmModemException as e:
            self.l.error("_check_imei: Got exception: " + str(e))
            modem.close()
        return False

    def _port_was_renumbered(self, use_port: str = None) -> bool:
        """
        Check if serial devices were renumbered.
        @return: Returns False if the known serial port still matches the expected IMEI and True else.
        """

        if use_port is not None:
            p = use_port
        else:
            p = self.current_port

        if p:
            # We have a port, which means it is not the first run. However, when there is
            # a re-initialisation after a modem power-loss, we just check the IMEI again,
            # because devices might be renumbered.
            self.status = "Check port renumbering."
            imei_status = self._check_imei(p, self.modem_config.baud, self.modem_config.imei)
            if imei_status:
                return False
            else:
                # There was a renumbering of ports, better we search again
                self.status = "Port was renumbered. Reinitializing."
                self.current_port = None

        return True

    def _init_modem(self) -> bool:
        """
        Initialize the modem object by finding the correct port, opening the serial device, passing the SIM card PIN
        and so on.
        @return: Returns the initialisation status as True or False.
        """

        # reset data
        self.current_network = None
        self.current_signal = 0

        self.status = "Try to initialize modem."
        self.l.info(f"Initializing modem {self.identifier}.")
        try:

            self._port_was_renumbered()

            if self.current_port is None:
                self.status = "Try finding port."
                self.current_port = self._find_port(self.modem_config.port, self.modem_config.imei)

            if self.current_port is None:
                self.status = "Failed finding port."
                self.l.error(
                    f"Problem: Can't find a port {self.modem_config.port} that matches IMEI {self.modem_config.imei}."
                )
                return False

            self.status = f"Finally initializing port {self.current_port}."
            time.sleep(10)
            self.modem = MyGsmModem(
                self,
                self.current_port,
                self.modem_config.baud,
                smsReceivedCallbackFunc=self._handle_incoming_gsmmodem_sms,
                smsStatusReportCallback=self._handle_status_report,
                exclusive=True,
            )  # was True
            self.modem.log = logging.getLogger(f"Modem [{self.identifier}]")
            self.status = f"Port {self.current_port} sucessfully opened."

        except GsmModemException as e:
            self.l.error("_check_imei: Got exception: " + str(e))
            self.status = "Check port renumbering - Exception."
            return False

        self.modem.smsTextMode = False

        self.l.debug(f"Connecting to GSM modem on {self.current_port}.")
        self.status = "Connecting to modem."

        try:
            assert self.modem_config.pin is None or int(self.modem_config.pin) >= 0
            self.modem.connect(
                self.modem_config.pin,
                waitingForModemToStartInSeconds=self.modem_config.wait_for_start,
            )
            #self.modem.processStoredSms()

        except PinRequiredError:
            self.l.error(f"Error: SIM card PIN required. Please specify a PIN.")
            self.status = "Error: SIM PIN required."
            self.modem.close()
            return False

        except IncorrectPinError:
            self.l.error(
                f"Error: Incorrect SIM card PIN entered. Stopping program to not accidentally enter it twice."
            )
            self.status = "Error: Incorrect SIM PIN."
            self.modem.close()
            sys.exit(1)

        except serial.serialutil.PortNotOpenError as e:
            self.l.error(f"Error: Failed to open serial connection.")
            self.status = "Error finally opening port."
            self.l.error(e)
            return False

        self.l.debug(f"Checking for network coverage...")

        # one check is enough (before: ten checks in a row, ~10 s extra per start and re-initialisation)
        self.status = "Waiting for network."
        try:
            self.modem.waitForNetworkCoverage(120)
            self.status = "Network found."
        except TimeoutException:
            self.l.error(f"Error: Failed to connect to network. Bad signal?")
            self.status = "Error: Failed to connect to network."
            self.modem.close()
            return False

        # the library stores the memory quoted ('"ME"'); it compares and sets it unquoted later
        self.receive_memory = (getattr(self.modem, '_smsMemReadDelete', None) or '').strip('"') or None

        # SMS stored while smsgate was not running are handed on, not deleted unread
        self.process_stored_sms()

        # We do not check the balance immediately, but wait a bit.
        # self.check_balance()

        self.status = "Ready."
        self.init_counter += 1
        self.last_init = datetime.datetime.now(tz=None)

        self.health_state = "OK"
        self.health_logs = None

        return True

    def set_ready(self) -> None:
        """
        Set human-readable status message to "Ready."
        """
        self.status = "Ready."

    def _delete_sms(self, all:bool=False) -> None:
        # Delete all unread/unsent stored SMS
        try:
            if all:
                self.modem.deleteMultipleStoredSms(4)
                #self.modem.write("AT+CMGD=,4\r\n")
            else:
                self.modem.deleteMultipleStoredSms(2)
                #self.modem.write("AT+CMGD=,2\r\n")
        except CmsError:
            self.l.warning("Exception: Failed to delete SMS.")


    def _send_ussd_ucs2(self, code: str) -> str:
        """
        Send USSD code in UCS2 format.
        @param code: The clear-text code to send.
        @return: Returns the decoded response as string.
        """
        self.l.debug("Set encoding UCS2.")
        self.modem.smsEncoding = "UCS2"

        # transform input to UCS2-encoding
        code_ucs2 = (
            binascii.hexlify(bytearray(code.encode("utf-16-be")))
            .decode("utf-8")
            .upper()
        )
        self.l.debug(f"Send USSD Code [UCS2:{code_ucs2},plain:{code}].")

        response = self.modem.sendUssd(code_ucs2, responseTimeout=30)
        decoded_response = binascii.unhexlify(response.message).decode("utf-16-be")

        # There was one case, where the Euro currency sign was encoded as
        # GSM 7-bit Basic Character Set Extension, which triggers a problem
        # when passed via XML and then parsed. Therefore, we use a work-around
        # here.
        decoded_response = decoded_response.replace("\x1b\x65", "€")

        self.l.debug(f"Decoded USSD response is [{decoded_response}]")

        self.set_ready()
        return decoded_response

    def _send_ussd_enc(self, code: str, encoding: str) -> str:
        """
        Send USSD code in a certain encoding, but passing it as ASCII without transformation.
        The function is used for testing and debugging.
        @param code: The clear-text code to send in ASCII.
        @param encoding: The encoding to use, without processing the input.
        @return: Returns the decoded response as string.
        """
        self.l.debug(f"Set encoding {encoding}.")
        self.modem.smsEncoding = encoding

        self.l.debug(f"Send USSD Code [{encoding}:{code}].")
        response = self.modem.sendUssd(code, responseTimeout=30).message
        self.l.debug(f"USSD response is [{response}]")
        self.set_ready()
        return response

    def send_ussd(self, code: str) -> Optional[str]:
        """
        Send an USSD code and wait for response.
        @param code: The clear-text code to send.
        @return: Returns the decoded response as string or None in case of an error.
        """
        self.status = "Send USSD."
        try:

            if self.modem_config.encoding == "UCS2" or self.modem_config.encoding is None:
                return self._send_ussd_ucs2(code)
            else:
                return self._send_ussd_enc(code, self.modem_config.encoding)

        except TimeoutException:
            self.l.error(
                f"Error: Failed to send USSD message. Got TimeoutException. Retry."
            )
            # self.send_ussd(code, 'GSM', counter + 1)
            return None
        except CmeError:
            self.l.error(f"Error: Failed to send USSD message. Got CmeError. Retry.")
            # self.send_ussd(code, 'GSM', counter + 1)
            return None

    def request_online_balance(self) -> Optional[float]:
        """
        Request account balance and get the balance as value, if possible.
        Currently, only balance checks via USSD codes are supported. Some providers do not support this, for example
        O2 in Germany.
        @return: Returns the balance as float. None is returned on error or if the balance information is not available.
        """

        if not self.modem_config.ussd_account_balance:
            return None

        try:

            response = self.send_ussd(self.modem_config.ussd_account_balance)
            if response is None:
                self.l.debug(f"USSD response is None. Stop processing.")
                return None

            self.l.debug(f"Applying regexp [{self.modem_config.ussd_account_balance_regexp}]")

            if self.modem_config.ussd_account_balance_regexp is None:
                self.l.warning("Regexp for extracting the balance from the USSD response is not set. Do not process "
                               "USSD response any further.")
                return None

            result = re.search(self.modem_config.ussd_account_balance_regexp, response)
            if result:
                balance = result.group(1)
                self.l.debug(f"Balance as string: {balance} {self.get_currency()}")
                balance = balance.replace(",", ".")
                self.balance = float(balance)
                self.l.debug(f"Balance as float: {self.balance} {self.get_currency()}")

                return self.balance
            else:
                self.l.error(f"Error: Regular expression [{self.modem_config.ussd_account_balance_regexp}] " +
                             f"failed for string [{response}]")
        except TimeoutException:
            self.l.error("Error: Failed to send USSD message.")

        return None

    def _do_send_sms(self, _sms: sms.SMS) -> bool:
        """
        Internal function to send SMS.
        @param _sms: An SMS object of type sms.SMS.
        @return: Returns either True or False. True means the SMS was accepted for delivery by the network.
            On a modem/network error the SMS is scheduled for a later attempt (without blocking this thread)
            or given up (permanent error, too old). Timeouts and lost modems propagate to run().
        """
        started = time.time()
        parts = part_count(_sms.get_recipient(), _sms.get_text())
        try:
            # sendSms returns a SentSms object
            self.status = "Send SMS."
            self.l.info(
                f"[{_sms.get_id()}] Sending SMS to {_sms.get_recipient()} ({parts} part{'s' if parts > 1 else ''}, "
                f"waited {_sms.get_age().total_seconds():.0f} s in the queue)."
            )
            sent = self.modem.sendSms(
                _sms.get_recipient(), _sms.get_text(),
                waitForDeliveryReport=False,
                sendFlash=_sms.is_flash()
            )
            self.sent_sms[_sms.get_id()] = sent
            reference = getattr(sent, "reference", None)
            self.l.info(
                f"[{_sms.get_id()}] SMS to {_sms.get_recipient()} accepted by the network after "
                f"{time.time() - started:.1f} s (reference {reference}{f', {parts} parts' if parts > 1 else ''})."
            )
            if reference is not None:
                self._remember_reference(reference, _sms, parts)
            if not self._ist_selbsttest(_sms):
                self.zustellung[_sms.get_id()] = {"teile": parts, "zugestellt": set()}
                self._melde_status(_sms.get_id(), "angenommen", teile=parts, teile_zugestellt=0)
            self._zustellstand_speichern()
            if self.sms_disk_queue:
                self.sms_disk_queue.delete_outgoing_sms(_sms.get_id())
            return True
        except (CmsError, CmeError, CommandError) as e:
            age = _sms.get_age().total_seconds()
            give_up, delay = retry_decision(age, e, getattr(_sms, "retry_delay", None))
            self.l.error(
                f"[{_sms.get_id()}] Failed to send SMS to {_sms.get_recipient()} after {time.time() - started:.1f} s "
                f"due to modem/network error: {e}"
            )
            if give_up:
                self.l.error(
                    f"[{_sms.get_id()}] Giving up SMS to {_sms.get_recipient()} (age {age:.0f} s, error {e}). "
                    "It will not be sent."
                )
                if self.sms_disk_queue:
                    self.sms_disk_queue.delete_outgoing_sms(_sms.get_id())
                if not self._ist_selbsttest(_sms):
                    self._melde_status(_sms.get_id(), "fehlgeschlagen", text=f"Gateway hat aufgegeben: {e}")
            else:
                _sms.retry_delay = delay
                self._schedule_retry(_sms, delay)

        return False

    def _melde_status(self, sms_id: str, status: str, am=None, teile=None, teile_zugestellt=None, text=None) -> None:
        """Report a delivery status to aidlink (never raises; the self-test SMS are not reported)."""
        if not self.status_callback:
            return
        try:
            self.status_callback(sms_id, status, am, teile, teile_zugestellt, text)
        except Exception as e:
            self.l.error(f"[{sms_id}] Zustellstatus nicht übergeben: {e}")

    @staticmethod
    def _ist_selbsttest(_sms: sms.SMS) -> bool:
        return (_sms.get_text() or "").startswith("health-check-")

    def _schedule_retry(self, _sms: sms.SMS, delay: float) -> None:
        """Schedule another attempt for an SMS after `delay` seconds without blocking the sending thread."""
        self.l.info(f"[{_sms.get_id()}] Retrying SMS to {_sms.get_recipient()} in {delay:.0f} seconds.")
        self.retry_sms.append((time.time() + delay, _sms))

    def _requeue_due_retries(self) -> float:
        """
        Move retries that are due into the sending queue.
        @return: Seconds until the next pending retry (at most 60), used as wait timeout of the sending loop.
        """
        now = time.time()
        due = [entry for entry in self.retry_sms if entry[0] <= now]
        self.retry_sms = [entry for entry in self.retry_sms if entry[0] > now]
        for _, _sms in due:
            self.sms_sender_queue.put(_sms)
        if not self.retry_sms:
            return 60
        return max(0.5, min(60, min(entry[0] for entry in self.retry_sms) - now))

    def _remember_reference(self, reference: int, _sms: sms.SMS, parts: int = 1) -> None:
        """
        Keep the network references of an accepted SMS (all parts) to log their delivery reports, drop old entries.
        Entry: (sms_id, recipient, accepted at, part number, parts).
        """
        now = time.time()
        self.sent_by_reference = {
            ref: entry for ref, entry in self.sent_by_reference.items()
            if now - entry[2] < DELIVERY_REPORT_KEEP_SECONDS
        }
        lebend = {entry[0] for entry in self.sent_by_reference.values()}
        self.zustellung = {k: v for k, v in self.zustellung.items() if k in lebend or k == _sms.get_id()}
        for nr, ref in enumerate(part_references(reference, parts), start=1):
            self.sent_by_reference[ref] = (_sms.get_id(), _sms.get_recipient(), now, nr, parts)

    def _zustellung_auswerten(self, sms_id: str, nr: int, status, finalized) -> None:
        """Delivered when all parts are delivered; failed on a permanent error (TP-Status >= 64)."""
        stand = self.zustellung.get(sms_id)
        if stand is None or status is None:
            return
        if status <= 31:
            stand["zugestellt"].add(nr)
            if len(stand["zugestellt"]) >= stand["teile"]:
                del self.zustellung[sms_id]
                self._melde_status(sms_id, "zugestellt", am=finalized, teile=stand["teile"],
                                   teile_zugestellt=len(stand["zugestellt"]))
        elif status >= 64:
            del self.zustellung[sms_id]
            self._melde_status(sms_id, "fehlgeschlagen", am=finalized,
                               text=f"vom Netz nicht zugestellt (Status {status})")
        self._zustellstand_speichern()

    def _zustellstand_speichern(self) -> None:
        """
        Save references and open deliveries to disk (sms_disk_queue). Before, they only lived in memory: after a
        restart a report said "[unknown]" and aidlink never learned that the SMS failed (Nico 19.11.: SMS of 17.11.
        expired after two days, smsgate restarted in between).
        """
        if not getattr(self, "sms_disk_queue", None):
            return
        self.sms_disk_queue.save_delivery_state(self.identifier, {
            "references": {str(ref): list(entry) for ref, entry in self.sent_by_reference.items()},
            "zustellung": {
                sms_id: {"teile": stand["teile"], "zugestellt": sorted(stand["zugestellt"])}
                for sms_id, stand in self.zustellung.items()
            },
        })

    def _zustellstand_laden(self) -> None:
        """Load what _zustellstand_speichern saved; entries older than DELIVERY_REPORT_KEEP_SECONDS are dropped."""
        if not getattr(self, "sms_disk_queue", None):
            return
        state = self.sms_disk_queue.load_delivery_state(self.identifier)
        now = time.time()
        try:
            self.sent_by_reference = {
                int(ref): tuple(entry) for ref, entry in state.get("references", {}).items()
                if now - entry[2] < DELIVERY_REPORT_KEEP_SECONDS
            }
            lebend = {entry[0] for entry in self.sent_by_reference.values()}
            self.zustellung = {
                sms_id: {"teile": stand["teile"], "zugestellt": set(stand["zugestellt"])}
                for sms_id, stand in state.get("zustellung", {}).items() if sms_id in lebend
            }
        except (TypeError, ValueError, KeyError, IndexError) as e:
            self.l.error(f"Ignoring unreadable delivery state: {e}")
            self.sent_by_reference, self.zustellung = {}, {}
        if self.sent_by_reference:
            self.l.info(f"Restored {len(self.sent_by_reference)} references of sent SMS for delivery reports.")

    def _handle_status_report(self, report) -> None:
        """
        Callback for delivery reports (status reports) of the network: logs when an SMS reached the phone or
        why not. timeSent = SMSC received the SMS, timeFinalized = delivered / finally failed.
        """
        entry = self.sent_by_reference.get(report.reference)
        sms_id, recipient, accepted, nr, parts = entry if entry else ("unknown", report.number, None, 1, 1)
        part_info = f"part {nr}/{parts}, " if parts > 1 else ""
        status = getattr(report, "deliveryStatus", None)
        finalized = getattr(report, "timeFinalized", None)
        delay = ""
        if accepted is not None and finalized is not None:
            try:
                delay = f", {finalized.timestamp() - accepted:.0f} s after acceptance"
            except Exception:
                delay = ""
        self._zustellung_auswerten(sms_id, nr, status, finalized)
        self.l.info(
            f"[{sms_id}] Delivery report for SMS to {recipient} ({part_info}reference {report.reference}): "
            f"{delivery_status_text(status)} at {finalized}{delay} (SMSC received {getattr(report, 'timeSent', None)})."
        )

    def print_info(self) -> None:
        """
        Print information about a modem such as used serial port, IMEI, IMSI, SMSC, network operator, signal quality.
        """
        assert self.modem

        self.current_network = self.modem.networkName
        self.current_signal = self.modem.signalStrength

        if self.current_network:
            self.current_network = self.current_network.strip()

        self.l.info(
            f"--------------------------------------------------------------------"
        )
        self.l.info(f"Modem port        : " + (self.current_port or "N/A"))
        self.l.info(f"Modem manufacturer: " + (self.modem.manufacturer or "N/A"))
        self.l.info(f"Modem model       : " + (self.modem.model or "N/A"))
        self.l.info(f"Modem revision    : " + (self.modem.revision or "N/A"))
        self.l.info(f"IMEI              : " + (self.modem.imei or "N/A"))
        self.l.info(f"IMSI              : " + (self.modem.imsi or "N/A"))
        self.l.info(f"SMSC              : " + (self.modem.smsc or "N/A"))
        self.l.info(f"Phone number      : " + (self.modem.ownNumber or "N/A"))
        self.l.info(f"Network           : " + (self.current_network or "N/A"))
        self.l.info(f"Signal strength   : " + str(self.current_signal))
        self.l.info(f"SMS Encoding      : " + (self.modem.smsEncoding or "N/A"))
        self.l.info(f"SMS Text Mode     : " + str(self.modem.smsTextMode))

    def _really_do_health_check(self) -> Tuple[str, Optional[str]]:
        """
        Internal health check implementation. It checks if the modem is connected to a network, parameters are in
        range, balance is in acceptable range. At certain intervals defined in the ModemConfig, a test SMS is sent the
        own number to check if that works and also to generate a financial event to keep the SIM card alive.
        @return: Returns the modem's internal health state as tuple of State, Message. State is either "OK", "WARNING",
            or "CRITICAL". Message is a human-readable string giving details about the issue. Message may be a None.
        """
        self.l.info(f"Run health check for modem.")
        self.last_health_check = datetime.datetime.now()

        if self.modem is None:
            if self.modem_config.enabled:
                return "CRITICAL", f"{self.identifier} No modem object."
            else:
                return "WARNING", f"{self.identifier} No modem object."

        if self.modem.manufacturer is None:
            return (
                "CRITICAL",
                f"{self.identifier} Failed to communicate with modem to detect manufacturer.",
            )

        if not self.modem.imsi:
            return "CRITICAL", f"{self.identifier} There is no IMSI."

        if not self.modem.smsc:
            return "CRITICAL", f"{self.identifier} No SMSC set."

        # SMS whose notification got lost, and long SMS still missing parts, go on now
        try:
            self.process_stored_sms()
            self.deliver_overdue_concat_sms()
        except Exception as e:
            self.l.error(f"Error handling stored or incomplete SMS: {e}", exc_info=True)

        s = self.modem.signalStrength
        if s == -1:
            return "WARNING", f"{self.identifier} Unknown signal strength."

        if s <= 1:
            return "CRITICAL", f"{self.identifier} Weak signal strength."

        if s <= 5:
            return "WARNING", f"{self.identifier} Weak signal strength."

        # Account balance checks fail frequently, therefore this check only results in warnings
        # with valid balances.

        if self.modem_config.ussd_account_balance and self.modem_config.ussd_account_balance_regexp:

            #    if self.check_balance() is None:
            #        return "WARNING", f"{self.identifier} Failed to check balance."

            if self.request_online_balance() is not None:
                if self.balance is not None:
                    level, log = self._check_balance_thresholds()
                    if level != "OK":
                        return level, log

        # At certain intervals send an SMS to ourself
        now = datetime.datetime.now()
        day_matches = False

        if self.modem_config.sms_self_test_interval == "monthly":
            if now.day == 1:
                day_matches = True

        elif self.modem_config.sms_self_test_interval == "weekly":
            if now.weekday() == 1:
                day_matches = True
        else: # Third option is daily
            day_matches = True

        if day_matches:

            seconds_since_midnight = (
                    now - now.replace(hour=0, minute=0, second=0, microsecond=0)
            ).total_seconds()
            self.l.info(f"Day for the SMS self test matches. There are {seconds_since_midnight} seconds since midnight.")

            if seconds_since_midnight <= self.modem_config.health_check_interval:
                # Have checked balance thresholds before, not necessarily the online-balance,
                # but the last balance value we have seen and stored.
                self.l.info("Send test SMS to ourself.")
                self._send_test_sms()

            elif (
                    self.health_check_expected_token
                    and seconds_since_midnight <= 2 * self.modem_config.health_check_interval
            ):
                # The expected token was not cleared, which means the expected SMS was not received.
                # Therefore, we send another SMS
                self.l.info(
                    "Send second test SMS to ourself, because last one was not received."
                )
                self._send_test_sms()

            elif self.health_check_expected_token:
                self.l.info("Failed to receive the test SMS. There is a problem.")
                return (
                    "WARNING",
                    f"{self.identifier} Failed to send test SMS to oneself.",
                )

        self.status = "Ready."
        return "OK", None

    def _send_test_sms(self) -> None:
        """
        Generate a UUID and send it as test SMS to the own number.
        """
        new_uuid = str(uuid.uuid4())
        self.health_check_expected_token = "health-check-" + new_uuid
        return self.send_sms(
            sms.SMS(
                sms_id=new_uuid,
                recipient=self.modem_config.phone_number,
                text=self.health_check_expected_token,
                sender=self.modem_config.phone_number,
            )
        )

    def close(self) -> None:
        """
        Closes the serial connection to the modem.
        """
        if self.modem:
            # if self.modem.alive:
            self.modem.close()
        self.modem = None

    def run(self) -> None:
        """
        Modem run loop
        """
        seconds_between_calls = 1

        while True:
            # the SMS currently being handled; None once it was sent, scheduled for retry or given up —
            # only an SMS still in hand is put back after a timeout (before, a stale SMS was re-sent or the
            # thread died with UnboundLocalError when no SMS had been handled yet)
            _sms = None

            try:
                # initialize modem if necessary
                if self.modem is None:
                    self._repeat_initialization()
                    self.print_info()

                self.l.debug("Wake up.")
                wait = self._requeue_due_retries()
                try:
                    # Wait for SMS, a due retry or for a timeout
                    _sms = self.sms_sender_queue.get(timeout=wait)
                    current_time = time.time()
                    elapsed_time = current_time - self.send_last_call_time

                    if elapsed_time < seconds_between_calls:
                        time_to_wait = seconds_between_calls - elapsed_time
                        self.l.info(f"Throttled. Waiting for {time_to_wait:.2f} seconds.")
                        time.sleep(time_to_wait)
                    self.send_last_call_time = time.time()
                    self._do_send_sms(_sms)
                    _sms = None
                except queue.Empty:
                    pass

                # start a health check?
                self._do_health_check()

            except (TimeoutException, serial.serialutil.PortNotOpenError, serial.SerialException) as e:
                self.l.error(
                    "Timeout occurred or modem lost. Reinitializing modem. Exception was: %s", e
                )
                self.status = "Timeout."
                self.close()

                if _sms is not None:
                    self.sms_sender_queue.put(_sms)
                self._do_health_check(do_now=True)

            except Exception as e:
                self.l.error(
                    "Unexpected error in modem runner. Exception was: %s", e
                )
                traceback.print_exc()
                self.status = "Error."
                self.close()

                if _sms is not None:
                    self.sms_sender_queue.put(_sms)
                self._do_health_check(do_now=True)


class MyGsmModem(GsmModem):

    def __init__(self, gw_modem: Modem, port, baudrate=115200, incomingCallCallbackFunc=None,
                 smsReceivedCallbackFunc=None, smsStatusReportCallback=None, requestDelivery=True, AT_CNMI="", *a,
                 **kw):
        super().__init__(port, baudrate, incomingCallCallbackFunc, smsReceivedCallbackFunc, smsStatusReportCallback,
                         requestDelivery, AT_CNMI, *a, **kw)
        self.gw_modem = gw_modem
        self.l = logging.getLogger("MyGsmModem")

    def _handleModemNotification(self, lines):

        # SMS notifications are logged (without content) so that a lost SMS can be traced
        indications = [line for line in lines if line.startswith(SMS_INDICATION_PREFIXES + ('+CMT:', '+CDS:'))]
        if indications:
            self.gw_modem.l.info(f"Modem notification: {indications}")

        for i in range(0, len(lines)):

            # '+CMT:' with colon — '+CMT' alone also matched '+CMTI' and read the next line as message
            if lines[i].startswith('+CMT:') and i + 1 < len(lines):
                try:
                    _sms = decodeSmsPdu(lines[i + 1])
                except Exception as e:
                    self.l.warning(f"Could not decode SMS after {lines[i]!r}: {e}")
                    continue

                # Extract UDH/concatenation info if present
                udh = _sms.get('udh', [])
                concat_ref = None
                concat_parts = None
                concat_num = None
                for ie in udh:
                    if isinstance(ie, Concatenation):
                        concat_ref = ie.reference
                        concat_parts = ie.parts
                        concat_num = ie.number
                        break

                new_sms = sms.SMS(
                    sms_id=None,
                    recipient=self.gw_modem.get_phone_number(),
                    text=_sms['text'] if 'text' in _sms else None,
                    sender=_sms['number'] if 'number' in _sms else None,
                    timestamp=_sms['time'] if 'time' in _sms else None,
                    receiving_modem=self.gw_modem,
                    flash=_sms['protocol_id']==0 if 'protocol_id' in _sms else False,
                    concat_ref=concat_ref,
                    concat_parts=concat_parts,
                    concat_num=concat_num,
                )

                sender_num = new_sms.get_sender()
                msg_text = new_sms.get_text()
                if sender_num and msg_text is not None:
                    self.gw_modem.processed_cmt_signatures[(sender_num, msg_text)] = helper.ensure_aware(datetime.datetime.now())

                self.gw_modem._handle_incoming_sms(new_sms)

        for block in split_sms_indications(lines):
            super()._handleModemNotification(block)
