import unittest
import datetime
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../server")))

import helper
import sms


class TestTimezoneHandling(unittest.TestCase):

    def test_ensure_aware_naive(self):
        dt_naive = datetime.datetime.now()
        dt_aware = helper.ensure_aware(dt_naive)
        self.assertIsNotNone(dt_aware.tzinfo)
        self.assertIsNotNone(dt_aware.tzinfo.utcoffset(dt_aware))

    def test_ensure_aware_already_aware(self):
        dt_utc = datetime.datetime.now(datetime.timezone.utc)
        dt_aware = helper.ensure_aware(dt_utc)
        self.assertEqual(dt_aware.tzinfo, datetime.timezone.utc)

    def test_ensure_aware_none(self):
        self.assertIsNone(helper.ensure_aware(None))

    def test_sms_get_age_naive_timestamp(self):
        s = sms.SMS(
            sms_id="test-1",
            recipient="+1234567890",
            text="hello",
            timestamp=datetime.datetime.now() # naive
        )
        age = s.get_age()
        self.assertIsInstance(age, datetime.timedelta)

    def test_sms_get_age_aware_timestamp(self):
        s = sms.SMS(
            sms_id="test-2",
            recipient="+1234567890",
            text="hello",
            timestamp=datetime.datetime.now(datetime.timezone.utc) # aware UTC
        )
        age = s.get_age()
        self.assertIsInstance(age, datetime.timedelta)

    def test_sms_get_age_offset_aware_timestamp(self):
        tz = datetime.timezone(datetime.timedelta(hours=2))
        s = sms.SMS(
            sms_id="test-3",
            recipient="+1234567890",
            text="hello",
            timestamp=datetime.datetime.now(tz) # aware offset
        )
        age = s.get_age()
        self.assertIsInstance(age, datetime.timedelta)

    def test_subtraction_naive_and_aware(self):
        now = helper.ensure_aware(datetime.datetime.now())
        tz_offset = datetime.timezone(datetime.timedelta(hours=2))
        first_seen_aware = datetime.datetime.now(tz_offset) - datetime.timedelta(seconds=100)
        first_seen_naive = datetime.datetime.now() - datetime.timedelta(seconds=100)

        # Both subtractions should work without TypeError
        diff1 = (now - helper.ensure_aware(first_seen_aware)).total_seconds()
        diff2 = (now - helper.ensure_aware(first_seen_naive)).total_seconds()

        self.assertGreater(diff1, 0)
        self.assertGreater(diff2, 0)


if __name__ == "__main__":
    unittest.main()
