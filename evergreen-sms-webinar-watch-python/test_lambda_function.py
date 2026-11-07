"""
Tests for evergreen-sms-webinar-watch. AWS and Kit are faked; the DynamoDB
tests use moto's in-memory DynamoDB if it's installed
(pip install "moto[dynamodb]"), and are skipped otherwise.

Run from this folder:   python3 -m unittest -v

All test values are made up and set in one place (TEST_SETTINGS below), so no
business details live in this file. To run the tests with other values, set any
of these environment variables first, e.g.
    TEST_ALLOWED_ORIGIN=https://www.mysite.com python3 -m unittest -v
"""

import base64
import inspect
import json
import os
import re
import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlsplit

import kit_token
import lambda_function as fn

# ---------------------------------------------------------------------------
# Test settings - every business-specific value used by the tests
# ---------------------------------------------------------------------------

TEST_SETTINGS = {
    "allowed_origin": os.environ.get("TEST_ALLOWED_ORIGIN", "https://www.example.com"),
    "started_tag_id": os.environ.get("TEST_STARTED_TAG_ID", "1000001"),
    "watched_tag_id": os.environ.get("TEST_WATCHED_TAG_ID", "1000002"),
    "watch_count_field": os.environ.get("TEST_WATCH_COUNT_FIELD", "test_watch_count"),
}
SUB = os.environ.get("TEST_SUBSCRIBER_ID", "1234567890")

KEY = base64.urlsafe_b64encode(bytes(range(64))).decode()
OTHER_KEY = base64.urlsafe_b64encode(bytes(range(1, 65))).decode()
SECRETS = {"kit_api_key": "kit-key", "token_secret": KEY, **TEST_SETTINGS}
TOKEN = kit_token.make_token(SUB, KEY)
ORIGIN = TEST_SETTINGS["allowed_origin"]
STARTED_TAG = TEST_SETTINGS["started_tag_id"]
WATCHED_TAG = TEST_SETTINGS["watched_tag_id"]
COUNT_FIELD = TEST_SETTINGS["watch_count_field"]
CONTEXT = SimpleNamespace(invoked_function_arn="arn:aws:lambda:eu-north-1:111:function:test-function")


def near_miss_origins(origin):
    """Websites that look like the allowed one but aren't, worked out from it."""
    parts = urlsplit(origin)
    host = parts.hostname
    other_host = host[4:] if host.startswith("www.") else "www." + host
    return [
        "https://evil.example",
        f"https://{other_host}",          # with/without www
        f"http://{host}",                 # not https
        f"https://{host}.evil.example",   # allowed name as a prefix
        f"https://evil-{host}",           # allowed name as a suffix
    ]


def page_call(body, path="/milestone", method="POST", origin=ORIGIN):
    headers = {"content-type": "application/json"}
    if origin:
        headers["origin"] = origin
    return {
        "rawPath": path,
        "headers": headers,
        "requestContext": {"http": {"method": method, "sourceIp": "1.2.3.4"}},
        "body": json.dumps(body) if not isinstance(body, str) else body,
        "isBase64Encoded": False,
    }


class PageTests(unittest.TestCase):
    def setUp(self):
        patchers = [
            mock.patch.object(fn, "settings", return_value=SECRETS),
            mock.patch.object(fn, "lambda_client"),
            mock.patch.object(fn, "events_table"),
            mock.patch.object(fn, "notify_failure"),
        ]
        self.settings, self.lambda_client, self.table, self.notify = [p.start() for p in patchers]
        for p in patchers:
            self.addCleanup(p.stop)

    def call(self, body, **kwargs):
        return fn.lambda_handler(page_call(body, **kwargs), CONTEXT)

    def logged(self):
        return [c.kwargs["Item"] for c in self.table.return_value.put_item.call_args_list]

    def jobs(self):
        return [json.loads(c.kwargs["Payload"]) for c in self.lambda_client.return_value.invoke.call_args_list]

    def test_started_logs_and_starts_kit_job(self):
        result = self.call({"token": TOKEN, "milestone": "started"})
        self.assertEqual(result["statusCode"], 200)
        [row] = self.logged()
        self.assertEqual((row["subscriber_id"], row["event"], row["source"]), (SUB, "started", "watch_page"))
        self.assertNotIn("minute", row)
        self.assertRegex(row["event_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z#[0-9a-f]{6}$")
        self.assertEqual(self.jobs(), [{"job": "kit_actions", "subscriber_id": SUB, "milestone": "started"}])
        self.assertEqual(self.lambda_client.return_value.invoke.call_args.kwargs["InvocationType"], "Event")

    def test_progress_logs_minute_and_never_touches_kit(self):
        result = self.call({"token": TOKEN, "milestone": "progress", "minute": 30})
        self.assertEqual(result["statusCode"], 200)
        [row] = self.logged()
        self.assertEqual((row["event"], row["minute"]), ("progress", 30))
        self.assertEqual(self.jobs(), [])

    def test_pitch_starts_kit_job(self):
        self.call({"token": TOKEN, "milestone": "reached_pitch"})
        self.assertEqual(self.jobs()[0]["milestone"], "reached_pitch")

    def test_bad_progress_minutes_refused(self):
        for minute in [0, -10, 15, 250, "20", 20.0, True, None]:
            result = self.call({"token": TOKEN, "milestone": "progress", "minute": minute})
            self.assertEqual(result["statusCode"], 400, minute)
        self.assertEqual(self.logged(), [])

    def test_unknown_milestone_refused(self):
        for milestone in ["sms-member", "STARTED", "", None, 12345]:
            result = self.call({"token": TOKEN, "milestone": milestone})
            self.assertEqual(result["statusCode"], 400, milestone)
        self.assertEqual(self.logged(), [])

    def test_page_cannot_choose_tags_or_fields(self):
        self.call({"token": TOKEN, "milestone": "started", "tag": 999, "tags": [999],
                   "field": "payment_plan", "increment": "payment_plan"})
        self.assertEqual(self.jobs(), [{"job": "kit_actions", "subscriber_id": SUB, "milestone": "started"}])

    def test_bad_tokens_refused(self):
        tampered = TOKEN[:-1] + ("A" if TOKEN[-1] != "A" else "B")
        for token in [None, "", "abc", tampered, kit_token.make_token(SUB, OTHER_KEY), SUB]:
            result = self.call({"token": token, "milestone": "started"})
            self.assertEqual(result["statusCode"], 403, token)
        self.assertEqual(self.logged(), [])
        self.assertEqual(self.jobs(), [])

    def test_allowed_origin_accepted(self):
        for origin in [ORIGIN, ORIGIN + "/"]:
            self.assertEqual(self.call({"token": TOKEN, "milestone": "started"}, origin=origin)["statusCode"], 200)

    def test_other_websites_refused(self):
        for origin in near_miss_origins(ORIGIN):
            result = self.call({"token": TOKEN, "milestone": "started"}, origin=origin)
            self.assertEqual(result["statusCode"], 403, origin)
        self.assertEqual(self.logged(), [])

    def test_no_origin_allowed_for_testing_tools(self):
        # Postman/curl send no Origin; the token still has to be valid
        self.assertEqual(self.call({"token": TOKEN, "milestone": "started"}, origin=None)["statusCode"], 200)

    def test_wrong_method_path_and_body(self):
        self.assertEqual(self.call({}, method="GET")["statusCode"], 405)
        self.assertEqual(self.call({"token": TOKEN, "milestone": "started"}, path="/kittag")["statusCode"], 404)
        self.assertEqual(self.call({"token": TOKEN, "milestone": "started"}, path="/milestone/")["statusCode"], 200)
        for body in ["not json", "[1,2]", "x" * 3000]:
            self.assertEqual(self.call(body)["statusCode"], 400, body[:10])

    def test_log_failure_reported(self):
        self.table.return_value.put_item.side_effect = Exception("dynamo down")
        result = self.call({"token": TOKEN, "milestone": "started"})
        self.assertEqual(result["statusCode"], 503)
        self.notify.assert_called_once()
        self.assertEqual(self.jobs(), [])

    def test_job_start_failure_reported(self):
        self.lambda_client.return_value.invoke.side_effect = Exception("boom")
        result = self.call({"token": TOKEN, "milestone": "reached_pitch"})
        self.assertEqual(result["statusCode"], 503)
        self.notify.assert_called_once()
        self.assertEqual(len(self.logged()), 1)

    def test_settings_failure(self):
        self.settings.side_effect = Exception("no ssm")
        self.assertEqual(self.call({"token": TOKEN, "milestone": "started"})["statusCode"], 503)

    def test_no_cors_headers_added_by_function(self):
        result = self.call({"token": TOKEN, "milestone": "started"})
        self.assertFalse(any(h.lower().startswith("access-control") for h in result["headers"]))


class FakeKit:
    """Pretends to be Kit: records calls and keeps one subscriber's fields."""

    def __init__(self, count=None, fail=None):
        self.calls = []
        self.fields = {COUNT_FIELD: count}
        self.fail = fail or {}

    def __call__(self, method, endpoint, body, key):
        self.calls.append((method, endpoint))
        if (method, endpoint.split("/")[0]) in self.fail:
            return self.fail[(method, endpoint.split("/")[0])]
        if method == "POST":
            return 201, "{}"
        if method == "PUT":
            self.fields.update(body["fields"])
        return 200, json.dumps({"subscriber": {"id": int(SUB), "fields": self.fields}})


REGISTRATION = "2026-01-01T12:00:00.000Z#abc123"


class JobTests(unittest.TestCase):
    """The event log is faked: self.registration is the latest 'registered' row, self.marks the counted ones."""

    def setUp(self):
        self.registration = REGISTRATION
        self.marks = set()
        patchers = [mock.patch.object(fn, "settings", return_value=SECRETS),
                    mock.patch.object(fn, "notify_failure"),
                    mock.patch.object(fn, "latest_registration", side_effect=lambda s: self.registration),
                    mock.patch.object(fn, "mark_pitch_counted", side_effect=self.fake_mark),
                    mock.patch.object(fn, "unmark_pitch_counted", side_effect=lambda s, r: self.marks.discard((s, r)))]
        self.settings, self.notify, self.latest, self.mark, self.unmark = [p.start() for p in patchers]
        for p in patchers:
            self.addCleanup(p.stop)

    def fake_mark(self, subscriber_id, registration):
        if (subscriber_id, registration) in self.marks:
            return False
        self.marks.add((subscriber_id, registration))
        return True

    def run_job(self, kit, milestone="reached_pitch", subscriber_id=SUB):
        with mock.patch.object(fn, "kit_request", side_effect=kit):
            fn.lambda_handler({"job": "kit_actions", "subscriber_id": subscriber_id, "milestone": milestone}, CONTEXT)

    def test_started_tags_only(self):
        kit = FakeKit()
        self.run_job(kit, "started")
        self.assertEqual(kit.calls, [("POST", f"tags/{STARTED_TAG}/subscribers/{SUB}")])

    def test_pitch_tags_then_increments(self):
        kit = FakeKit(count="4")
        self.run_job(kit)
        self.assertEqual(kit.calls, [("POST", f"tags/{WATCHED_TAG}/subscribers/{SUB}"),
                                     ("GET", f"subscribers/{SUB}"), ("PUT", f"subscribers/{SUB}")])
        self.assertEqual(kit.fields[COUNT_FIELD], "5")
        self.notify.assert_not_called()

    def test_blank_or_junk_counts_as_zero(self):
        for start in [None, "", "  ", "abc", "-3", "2.5"]:
            kit = FakeKit(count=start)
            self.marks.clear()
            self.run_job(kit)
            self.assertEqual(kit.fields[COUNT_FIELD], "1", start)

    def test_padded_number_read_correctly(self):
        kit = FakeKit(count=" 7 ")
        self.run_job(kit)
        self.assertEqual(kit.fields[COUNT_FIELD], "8")

    def test_rewatch_in_same_registration_not_counted_again(self):
        kit = FakeKit(count="4")
        self.run_job(kit)
        self.run_job(kit)  # re-watch, second tab or Lambda retry
        self.assertEqual(kit.fields[COUNT_FIELD], "5")
        self.assertEqual([c[0] for c in kit.calls].count("POST"), 2)  # tag still added each time
        self.notify.assert_not_called()

    def test_new_registration_counted_again(self):
        kit = FakeKit(count="4")
        self.run_job(kit)
        self.registration = "2026-01-05T09:00:00.000Z#def456"  # they registered again
        self.run_job(kit)
        self.assertEqual(kit.fields[COUNT_FIELD], "6")

    def test_no_registration_logged_tagged_not_counted_and_reported(self):
        self.registration = None
        kit = FakeKit(count="4")
        self.run_job(kit)
        self.assertEqual(kit.calls, [("POST", f"tags/{WATCHED_TAG}/subscribers/{SUB}")])
        self.assertEqual(kit.fields[COUNT_FIELD], "4")
        self.notify.assert_called_once()
        self.assertIn("no registration", self.notify.call_args.args[0])

    def test_kit_busy_on_count_unmarks_so_retry_counts(self):
        kit = FakeKit(count="4", fail={("PUT", "subscribers"): (429, "busy")})
        with self.assertRaises(fn.RetryableError):
            self.run_job(kit)
        self.assertEqual(self.marks, set())
        del kit.fail[("PUT", "subscribers")]
        self.run_job(kit)  # Lambda's retry
        self.assertEqual(kit.fields[COUNT_FIELD], "5")

    def test_event_log_error_retries(self):
        self.latest.side_effect = Exception("dynamo down")
        with self.assertRaises(Exception):
            self.run_job(FakeKit(count="4"))

    def test_started_never_touches_the_count(self):
        self.run_job(FakeKit(), "started")
        self.latest.assert_not_called()
        self.mark.assert_not_called()

    def test_kit_busy_retries_before_counting(self):
        for status in [429, 500, 503]:
            kit = FakeKit(count="1", fail={("POST", "tags"): (status, "busy")})
            with self.assertRaises(fn.RetryableError):
                self.run_job(kit)
            self.assertNotIn("PUT", [c[0] for c in kit.calls])  # never counted
            self.assertEqual(self.marks, set())  # and not marked, so the retry counts

    def test_kit_client_error_reported_not_retried(self):
        kit = FakeKit(count="1", fail={("POST", "tags"): (404, "no such tag")})
        self.run_job(kit)
        self.notify.assert_called()

    def test_count_not_saved_reported(self):
        def kit(method, endpoint, body, key):
            if method == "POST":
                return 201, "{}"
            return 200, json.dumps({"subscriber": {"fields": {COUNT_FIELD: "1"}}})
        self.run_job(kit)
        self.notify.assert_called_once()

    def test_bad_job_input_reported(self):
        kit = FakeKit()
        self.run_job(kit, milestone="sms-member")
        self.run_job(kit, subscriber_id="abc")
        self.assertEqual(kit.calls, [])
        self.assertEqual(self.notify.call_count, 2)


# ---------------------------------------------------------------------------
# Finding and marking registrations, against an in-memory DynamoDB
# ---------------------------------------------------------------------------

try:
    import boto3
    from moto import mock_aws
except ImportError:
    mock_aws = None


@unittest.skipIf(mock_aws is None, "moto not installed")
class EventLogTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-2")
        os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
        os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
        self.aws = mock_aws()
        self.aws.start()
        self.addCleanup(self.aws.stop)
        boto3.client("dynamodb").create_table(
            TableName=fn.EVENTS_TABLE, BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("subscriber_id", "event_at")],
            KeySchema=[{"AttributeName": "subscriber_id", "KeyType": "HASH"},
                       {"AttributeName": "event_at", "KeyType": "RANGE"}])
        fn._clients.pop("table", None)
        self.addCleanup(fn._clients.pop, "table", None)

    def put(self, subscriber_id, event_at, event):
        fn.events_table().put_item(Item={"subscriber_id": subscriber_id, "event_at": event_at, "event": event})

    def test_finds_latest_registration_among_other_events(self):
        self.put(SUB, "2026-01-01T10:00:00.000Z#a", "registered")
        self.put(SUB, "2026-01-01T10:05:00.000Z#b", "started")
        self.put(SUB, "2026-01-03T09:00:00.000Z#c", "registered")
        self.put(SUB, "2026-01-03T09:10:00.000Z#d", "progress")
        self.put(SUB, "2026-01-03T10:15:00.000Z#e", "reached_pitch")
        self.put("999", "2026-01-09T00:00:00.000Z#f", "registered")  # someone else
        self.assertEqual(fn.latest_registration(SUB), "2026-01-03T09:00:00.000Z#c")

    def test_finds_registration_behind_many_later_events(self):
        # Many progress rows after the registration: the search must page through them
        self.put(SUB, "2026-01-01T10:00:00.000Z#reg", "registered")
        for i in range(300):
            self.put(SUB, f"2026-01-01T11:{i // 60:02d}:{i % 60:02d}.000Z#{i:03d}", "progress")
        self.assertEqual(fn.latest_registration(SUB), "2026-01-01T10:00:00.000Z#reg")

    def test_no_registration(self):
        self.put(SUB, "2026-01-01T10:05:00.000Z#b", "started")
        self.assertIsNone(fn.latest_registration(SUB))
        self.assertIsNone(fn.latest_registration("555"))

    def test_first_mark_wins_and_unmark_reopens(self):
        self.put(SUB, REGISTRATION, "registered")
        self.assertTrue(fn.mark_pitch_counted(SUB, REGISTRATION))
        self.assertFalse(fn.mark_pitch_counted(SUB, REGISTRATION))
        fn.unmark_pitch_counted(SUB, REGISTRATION)
        self.assertTrue(fn.mark_pitch_counted(SUB, REGISTRATION))

    def test_pitch_mark_independent_of_registration_count_mark(self):
        # The registration function's counted_at mark must not stop the watch count
        fn.events_table().put_item(Item={"subscriber_id": SUB, "event_at": REGISTRATION, "event": "registered",
                                         "counted_at": "2026-01-01T12:00:01+00:00"})
        self.assertTrue(fn.mark_pitch_counted(SUB, REGISTRATION))

    def test_cannot_mark_missing_registration(self):
        self.assertFalse(fn.mark_pitch_counted(SUB, REGISTRATION))
        self.assertNotIn("Item", fn.events_table().get_item(Key={"subscriber_id": SUB, "event_at": REGISTRATION}))


class SettingsTests(unittest.TestCase):
    PREFIX = "/zki/"
    OWN = PREFIX + "evergreen-watch/"

    def setUp(self):
        fn._settings = None
        self.addCleanup(setattr, fn, "_settings", None)
        # Values as they might be pasted into Parameter Store, stray spaces included
        self.params = {
            self.PREFIX + "kit-api-key": "kit-key \n",
            self.PREFIX + "token-secret": KEY + "\n",
            self.OWN + "allowed-origin": ORIGIN,
            self.OWN + "started-tag-id": " " + STARTED_TAG,
            self.OWN + "watched-tag-id": WATCHED_TAG,
            self.OWN + "watch-count-field": COUNT_FIELD,
        }

    def load(self, overrides=None):
        fn._settings = None
        values = dict(self.params, **(overrides or {}))
        params = [{"Name": n, "Value": v} for n, v in values.items() if v is not None]
        with mock.patch.object(fn, "ssm_client") as ssm:
            ssm.return_value.get_parameters.return_value = {"Parameters": params}
            result = fn.settings()
            fn.settings()
        return result, ssm

    def test_loads_once_and_strips(self):
        cfg, ssm = self.load()
        self.assertEqual(cfg, SECRETS)
        ssm.return_value.get_parameters.assert_called_once()

    def test_each_setting_required(self):
        for name in self.params:
            with self.assertRaises(RuntimeError, msg=name):
                self.load({name: None})

    def test_bad_values_refused(self):
        host = urlsplit(ORIGIN).hostname
        for name, bad in [(self.OWN + "started-tag-id", "started_watching"),
                          (self.OWN + "watched-tag-id", WATCHED_TAG + "x"),
                          (self.OWN + "allowed-origin", host),
                          (self.OWN + "allowed-origin", "http://" + host)]:
            with self.assertRaises(RuntimeError, msg=f"{name} = {bad}"):
                self.load({name: bad})


class NoBusinessDetailsTests(unittest.TestCase):
    """The function's code must work for any business: its settings come from Parameter Store."""

    def test_no_websites_in_code(self):
        hosts = re.findall(r"https?://([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)", inspect.getsource(fn))
        # Kit's API is fine, and example.com is reserved for documentation examples
        unexpected = [h for h in hosts if h != "api.kit.com" and not h.endswith("example.com")]
        self.assertEqual(unexpected, [], "Websites belong in Parameter Store, not the code")

    def test_no_kit_ids_in_code(self):
        # Kit tag and subscriber IDs are long numbers; the code shouldn't contain any
        self.assertEqual(re.findall(r"\b\d{6,}\b", inspect.getsource(fn)), [])


if __name__ == "__main__":
    unittest.main()
