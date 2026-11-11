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
from urllib.parse import parse_qs, quote, urlsplit

from botocore.exceptions import ClientError

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
    "clicked_tag_id": os.environ.get("TEST_CLICKED_TAG_ID", "1000003"),
    "pitch_second": os.environ.get("TEST_PITCH_SECOND", "4329"),          # 72:09 -> marks 10..70
    "allowed_missing_marks": os.environ.get("TEST_ALLOWED_MISSING_MARKS", "1"),
    "buy_link": os.environ.get("TEST_BUY_LINK", "https://timer.example.com/t/abc123/{email}"),
    "buy_fallback_url": os.environ.get("TEST_BUY_FALLBACK_URL", "https://www.example.com/try-again"),
    "in_cart_tag_id": os.environ.get("TEST_IN_CART_TAG_ID", "1000004"),
    "cart_watch_link": os.environ.get("TEST_CART_WATCH_LINK", "https://timer.example.com/p/def456/{email}"),
    "expired_url": os.environ.get("TEST_EXPIRED_URL", "https://www.example.com/expired"),
    "watch_expired_error_url": os.environ.get("TEST_WATCH_EXPIRED_ERROR_URL",
                                              "https://www.example.com/replay-try-again"),
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
CLICKED_TAG = TEST_SETTINGS["clicked_tag_id"]
PITCH_SECOND = int(TEST_SETTINGS["pitch_second"])
ALLOWED_MISSING = int(TEST_SETTINGS["allowed_missing_marks"])
ALL_MARKS = set(range(10, PITCH_SECOND // 60 + 1, 10))
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


class PageFakes:
    """Fakes AWS for calls from the web pages. Mixed into the page test classes."""

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


class PageTests(PageFakes, unittest.TestCase):
    def test_started_logs_and_starts_kit_job(self):
        result = self.call({"token": TOKEN, "milestone": "started"})
        self.assertEqual(result["statusCode"], 200)
        [row] = self.logged()
        self.assertEqual((row["subscriber_id"], row["event"], row["source"]), (SUB, "started", "watch_page"))
        self.assertNotIn("minute", row)
        self.assertRegex(row["event_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z#[0-9a-f]{6}$")
        self.assertEqual(self.jobs(), [{"job": "kit_actions", "subscriber_id": SUB, "milestone": "started",
                                        "event_at": row["event_at"]}])
        self.assertEqual(self.lambda_client.return_value.invoke.call_args.kwargs["InvocationType"], "Event")

    def test_progress_logs_minute_and_never_touches_kit(self):
        result = self.call({"token": TOKEN, "milestone": "progress", "minute": 30})
        self.assertEqual(result["statusCode"], 200)
        [row] = self.logged()
        self.assertEqual((row["event"], row["minute"]), ("progress", 30))
        self.assertEqual(self.jobs(), [])

    def test_pitch_starts_kit_job_with_its_row(self):
        self.call({"token": TOKEN, "milestone": "reached_pitch"})
        [row] = self.logged()
        self.assertEqual((self.jobs()[0]["milestone"], self.jobs()[0]["event_at"]), ("reached_pitch", row["event_at"]))

    def test_cta_click_logged_and_starts_kit_job(self):
        self.assertEqual(self.call({"token": TOKEN, "milestone": "cta_click"})["statusCode"], 200)
        [row] = self.logged()
        self.assertEqual(row["event"], "cta_click")
        self.assertEqual(self.jobs()[0]["milestone"], "cta_click")

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
        [job] = self.jobs()
        self.assertEqual(set(job), {"job", "subscriber_id", "milestone", "event_at"})
        self.assertEqual(job["milestone"], "started")

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


UTMS = {"utm_source": "test_source", "utm_medium": "test_medium",
        "utm_campaign": "test_campaign", "utm_content": "test_content"}


class PageViewTests(PageFakes, unittest.TestCase):
    """POST /page-view from the registration and sales pages."""

    def call(self, body, path="/page-view", **kwargs):
        return fn.lambda_handler(page_call(body, path=path, **kwargs), CONTEXT)

    def test_registration_view_logged_anonymously_with_utms(self):
        result = self.call({"page": "registration", **UTMS})
        self.assertEqual(result["statusCode"], 200)
        [row] = self.logged()
        self.assertEqual((row["subscriber_id"], row["event"], row["source"]),
                         ("registration_page", "registration_page_view", "registration_page"))
        self.assertEqual({k: row[k] for k in UTMS}, UTMS)
        self.assertNotIn("token_status", row)
        self.assertRegex(row["event_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z#[0-9a-f]{6}$")
        self.assertEqual(self.jobs(), [])

    def test_registration_view_without_utms(self):
        self.assertEqual(self.call({"page": "registration"})["statusCode"], 200)
        [row] = self.logged()
        self.assertFalse(any(k.startswith("utm_") for k in row))

    def test_registration_view_ignores_a_token(self):
        self.call({"page": "registration", "token": TOKEN})
        [row] = self.logged()
        self.assertEqual(row["subscriber_id"], "registration_page")

    def test_sales_visit_with_good_token_logged_under_subscriber(self):
        self.assertEqual(self.call({"page": "sales", "token": TOKEN, "utm_source": "email"})["statusCode"], 200)
        [row] = self.logged()
        self.assertEqual((row["subscriber_id"], row["event"], row["source"], row["token_status"]),
                         (SUB, "sales_page_visit", "sales_page", "valid"))
        self.assertEqual(row["utm_source"], "email")
        self.assertEqual(self.jobs(), [])

    def test_sales_visit_without_token_still_counted(self):
        for body in [{"page": "sales"}, {"page": "sales", "token": ""}, {"page": "sales", "token": None}]:
            self.assertEqual(self.call(body)["statusCode"], 200, body)
        rows = self.logged()
        self.assertEqual({(r["subscriber_id"], r["token_status"]) for r in rows}, {("sales_page", "missing")})
        self.assertEqual(len(rows), 3)

    def test_sales_visit_with_bad_token_still_counted(self):
        tampered = TOKEN[:-1] + ("A" if TOKEN[-1] != "A" else "B")
        for token in ["abc", tampered, kit_token.make_token(SUB, OTHER_KEY), SUB, 12345, ["x"]]:
            self.assertEqual(self.call({"page": "sales", "token": token})["statusCode"], 200, token)
        rows = self.logged()
        self.assertEqual({(r["subscriber_id"], r["token_status"]) for r in rows}, {("sales_page", "invalid")})
        self.assertEqual(len(rows), 6)
        self.notify.assert_not_called()  # a stale link isn't an error worth an email

    def test_bad_utms_left_out(self):
        self.call({"page": "registration", "utm_source": "x" * 101, "utm_medium": "  ", "utm_campaign": 5,
                   "utm_content": "line\nbreak", "utm_term": "not stored"})
        [row] = self.logged()
        self.assertFalse(any(k.startswith("utm_") for k in row))

    def test_utms_trimmed_and_longest_kept(self):
        self.call({"page": "registration", "utm_source": "  meta  ", "utm_campaign": "c" * 100})
        [row] = self.logged()
        self.assertEqual((row["utm_source"], row["utm_campaign"]), ("meta", "c" * 100))

    def test_page_cannot_overwrite_row_fields(self):
        self.call({"page": "sales", "token": TOKEN, "subscriber_id": "999", "event": "purchase",
                   "event_at": "x", "source": "kajabi", "token_status": "valid", "minute": 70})
        [row] = self.logged()
        self.assertEqual((row["subscriber_id"], row["event"], row["source"]), (SUB, "sales_page_visit", "sales_page"))
        self.assertNotEqual(row["event_at"], "x")
        self.assertNotIn("minute", row)

    def test_unknown_page_refused(self):
        for page in ["watch", "Sales", "", None, 1, ["sales"]]:
            self.assertEqual(self.call({"page": page})["statusCode"], 400, page)
        self.assertEqual(self.logged(), [])

    def test_page_view_is_not_a_milestone(self):
        # Each path only accepts its own kind of call
        self.assertEqual(self.call({"token": TOKEN, "milestone": "started"})["statusCode"], 400)
        self.assertEqual(self.call({"page": "sales", "token": TOKEN}, path="/milestone")["statusCode"], 400)
        self.assertEqual(self.logged(), [])

    def test_other_websites_refused(self):
        for origin in near_miss_origins(ORIGIN):
            self.assertEqual(self.call({"page": "registration"}, origin=origin)["statusCode"], 403, origin)
        self.assertEqual(self.logged(), [])

    def test_allowed_origin_and_trailing_slash(self):
        self.assertEqual(self.call({"page": "registration"}, origin=ORIGIN + "/")["statusCode"], 200)
        self.assertEqual(self.call({"page": "registration"}, path="/page-view/")["statusCode"], 200)

    def test_wrong_method_and_body(self):
        self.assertEqual(self.call({"page": "registration"}, method="GET")["statusCode"], 405)
        for body in ["not json", "[1,2]", "x" * 3000]:
            self.assertEqual(self.call(body)["statusCode"], 400, body[:10])

    def test_log_failure_reported(self):
        self.table.return_value.put_item.side_effect = Exception("dynamo down")
        self.assertEqual(self.call({"page": "registration"})["statusCode"], 503)
        self.notify.assert_called_once()

    def test_settings_failure(self):
        self.settings.side_effect = Exception("no ssm")
        self.assertEqual(self.call({"page": "registration"})["statusCode"], 503)


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
PITCH_ROW = "2026-01-01T13:15:00.000Z#fff000"


class JobTests(unittest.TestCase):
    """The event log is faked: self.registration is the latest 'registered' row, self.marks the counted ones."""

    def setUp(self):
        self.registration = REGISTRATION
        self.marks = set()
        self.played = set(ALL_MARKS)      # progress marks played through in the current run
        self.recorded = []                # (event_at, qualified, missing) written to reached_pitch rows
        patchers = [mock.patch.object(fn, "settings", return_value=SECRETS),
                    mock.patch.object(fn, "notify_failure"),
                    mock.patch.object(fn, "latest_run_start", side_effect=lambda s: self.registration),
                    mock.patch.object(fn, "mark_pitch_counted", side_effect=self.fake_mark),
                    mock.patch.object(fn, "unmark_pitch_counted", side_effect=lambda s, r: self.marks.discard((s, r))),
                    mock.patch.object(fn, "progress_minutes_since", side_effect=lambda s, since: set(self.played)),
                    mock.patch.object(fn, "record_qualified",
                                      side_effect=lambda s, e, q, m: self.recorded.append((e, q, m)))]
        self.settings, self.notify, self.latest, self.mark, self.unmark, self.progress, self.record = \
            [p.start() for p in patchers]
        for p in patchers:
            self.addCleanup(p.stop)

    def fake_mark(self, subscriber_id, registration):
        if (subscriber_id, registration) in self.marks:
            return False
        self.marks.add((subscriber_id, registration))
        return True

    def run_job(self, kit, milestone="reached_pitch", subscriber_id=SUB, event_at=PITCH_ROW):
        with mock.patch.object(fn, "kit_request", side_effect=kit):
            fn.lambda_handler({"job": "kit_actions", "subscriber_id": subscriber_id, "milestone": milestone,
                               "event_at": event_at}, CONTEXT)

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

    def test_rewatch_in_same_run_not_counted_again(self):
        kit = FakeKit(count="4")
        self.run_job(kit)
        self.run_job(kit)  # re-watch, second tab or Lambda retry
        self.assertEqual(kit.fields[COUNT_FIELD], "5")
        self.assertEqual([c[0] for c in kit.calls].count("POST"), 2)  # tag still added each time
        self.notify.assert_not_called()

    def test_new_run_counted_again(self):
        kit = FakeKit(count="4")
        self.run_job(kit)
        self.registration = "2026-01-05T09:00:00.000Z#def456"  # a new run started
        self.run_job(kit)
        self.assertEqual(kit.fields[COUNT_FIELD], "6")

    def test_no_run_not_qualified_not_tagged_and_reported(self):
        self.registration = None
        kit = FakeKit(count="4")
        self.run_job(kit)
        self.assertEqual(kit.calls, [])
        self.assertEqual(self.recorded, [(PITCH_ROW, False, None)])
        self.notify.assert_called_once()
        self.assertIn("no run on record", self.notify.call_args.args[0])

    def test_watched_properly_qualifies(self):
        kit = FakeKit(count="0")
        self.run_job(kit)
        self.assertEqual(self.recorded, [(PITCH_ROW, True, [])])
        self.assertEqual(kit.fields[COUNT_FIELD], "1")

    def test_allowed_number_of_missing_marks_still_qualifies(self):
        self.played = set(sorted(ALL_MARKS)[ALLOWED_MISSING:])  # skipped the first stretch(es)
        kit = FakeKit(count="0")
        self.run_job(kit)
        self.assertTrue(self.recorded[0][1])
        self.assertEqual(self.recorded[0][2], sorted(ALL_MARKS)[:ALLOWED_MISSING])
        self.assertEqual(kit.fields[COUNT_FIELD], "1")

    def test_too_many_missing_marks_not_qualified(self):
        self.played = set(sorted(ALL_MARKS)[ALLOWED_MISSING + 1:])
        kit = FakeKit(count="0")
        self.run_job(kit)
        self.assertEqual(self.recorded[0][:2], (PITCH_ROW, False))
        self.assertEqual(kit.calls, [])  # no tag, no count
        self.notify.assert_not_called()  # skipping is allowed, not an error

    def test_dragged_straight_to_pitch_not_qualified(self):
        self.played = {10}
        kit = FakeKit(count="0")
        self.run_job(kit)
        self.assertFalse(self.recorded[0][1])
        self.assertEqual(kit.calls, [])

    def test_marks_beyond_the_pitch_do_not_help(self):
        self.played = {80, 90, 100, 110, 120, 130, 140}
        self.run_job(FakeKit(count="0"))
        self.assertFalse(self.recorded[0][1])

    def test_watching_the_rest_later_then_qualifies(self):
        self.played = {10, 20}
        kit = FakeKit(count="0")
        self.run_job(kit)               # dragged to the pitch early: not qualified
        self.played = set(ALL_MARKS)    # came back and watched properly
        self.run_job(kit, event_at="2026-01-02T10:00:00.000Z#aaa111")
        self.assertEqual([r[1] for r in self.recorded], [False, True])
        self.assertEqual(kit.fields[COUNT_FIELD], "1")

    def test_pitch_without_event_at_reported(self):
        kit = FakeKit()
        self.run_job(kit, event_at=None)
        self.assertEqual(kit.calls, [])
        self.notify.assert_called_once()

    def test_cta_click_tags_clicked_only(self):
        kit = FakeKit(count="4")
        self.run_job(kit, "cta_click")
        self.assertEqual(kit.calls, [("POST", f"tags/{CLICKED_TAG}/subscribers/{SUB}")])
        self.assertEqual(kit.fields[COUNT_FIELD], "4")
        self.progress.assert_not_called()

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
        self.record.assert_not_called()

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

    def put(self, subscriber_id, event_at, event, new_run=None):
        item = {"subscriber_id": subscriber_id, "event_at": event_at, "event": event}
        if new_run is not None:
            item["new_run"] = new_run
        fn.events_table().put_item(Item=item)

    def test_page_view_rows_written_and_kept_apart_from_subscribers(self):
        self.put(SUB, "2026-01-01T10:00:00.000Z#a", "registered", True)
        fn.log_event("registration_page", "registration_page_view", source="registration_page",
                     details={"utm_source": "test_source"})
        fn.log_event(SUB, "sales_page_visit", source="sales_page", details={"token_status": "valid"})
        [view] = fn.events_table().query(KeyConditionExpression=fn.Key("subscriber_id").eq("registration_page"))["Items"]
        self.assertEqual((view["event"], view["source"], view["utm_source"]),
                         ("registration_page_view", "registration_page", "test_source"))
        rows = fn.events_table().query(KeyConditionExpression=fn.Key("subscriber_id").eq(SUB))["Items"]
        self.assertEqual([r["event"] for r in rows], ["registered", "sales_page_visit"])
        self.assertEqual(fn.latest_run_start(SUB), "2026-01-01T10:00:00.000Z#a")  # visits don't disturb runs

    def test_finds_start_of_latest_run_among_other_events(self):
        self.put(SUB, "2026-01-01T10:00:00.000Z#a", "registered", True)
        self.put(SUB, "2026-01-01T10:05:00.000Z#b", "started")
        self.put(SUB, "2026-01-03T09:00:00.000Z#c", "registered", True)    # run 2 starts
        self.put(SUB, "2026-01-03T09:10:00.000Z#d", "progress")
        self.put(SUB, "2026-01-04T08:00:00.000Z#e", "registered", False)   # re-registered within run 2
        self.put(SUB, "2026-01-04T09:00:00.000Z#g", "registered")          # older row with no new_run
        self.put(SUB, "2026-01-04T10:15:00.000Z#h", "reached_pitch")
        self.put("999", "2026-01-09T00:00:00.000Z#f", "registered", True)  # someone else
        self.assertEqual(fn.latest_run_start(SUB), "2026-01-03T09:00:00.000Z#c")

    def test_finds_registration_behind_many_later_events(self):
        # Many progress rows after the registration: the search must page through them
        self.put(SUB, "2026-01-01T10:00:00.000Z#reg", "registered", True)
        for i in range(300):
            self.put(SUB, f"2026-01-01T11:{i // 60:02d}:{i % 60:02d}.000Z#{i:03d}", "progress")
        self.assertEqual(fn.latest_run_start(SUB), "2026-01-01T10:00:00.000Z#reg")

    def test_no_run(self):
        self.put(SUB, "2026-01-01T10:05:00.000Z#b", "started")
        self.put(SUB, "2026-01-01T10:00:00.000Z#a", "registered", False)  # only an in-window re-registration
        self.assertIsNone(fn.latest_run_start(SUB))
        self.assertIsNone(fn.latest_run_start("555"))

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

    def test_progress_minutes_since_run_start(self):
        self.put(SUB, "2026-01-01T10:10:00.000Z#a", "progress")           # previous run: ignored
        self.put(SUB, "2026-01-03T09:00:00.000Z#run", "registered", True)
        self.put(SUB, "2026-01-03T09:10:00.000Z#b", "progress")
        self.put(SUB, "2026-01-03T09:20:00.000Z#c", "progress")
        self.put(SUB, "2026-01-04T08:10:00.000Z#d", "progress")           # watched 10 again next day
        self.put(SUB, "2026-01-04T08:12:00.000Z#e", "started")
        self.put("999", "2026-01-03T09:30:00.000Z#f", "progress")          # someone else
        table = fn.events_table()
        for key, minute in [("2026-01-01T10:10:00.000Z#a", 10), ("2026-01-03T09:10:00.000Z#b", 10),
                            ("2026-01-03T09:20:00.000Z#c", 20), ("2026-01-04T08:10:00.000Z#d", 10),
                            ("2026-01-03T09:30:00.000Z#f", 30)]:
            subscriber = "999" if key.endswith("#f") else SUB
            table.update_item(Key={"subscriber_id": subscriber, "event_at": key},
                              UpdateExpression="SET #m = :m", ExpressionAttributeNames={"#m": "minute"},
                              ExpressionAttributeValues={":m": minute})
        self.assertEqual(fn.progress_minutes_since(SUB, "2026-01-03T09:00:00.000Z#run"), {10, 20})

    def test_progress_minutes_paged(self):
        self.put(SUB, "2026-01-01T09:00:00.000Z#run", "registered", True)
        for i in range(300):
            fn.events_table().put_item(Item={"subscriber_id": SUB, "event_at": f"2026-01-01T10:{i // 60:02d}:{i % 60:02d}.000Z#{i:03d}",
                                             "event": "progress", "minute": 10 * (1 + i % 7)})
        self.assertEqual(fn.progress_minutes_since(SUB, "2026-01-01T09:00:00.000Z#run"), {10, 20, 30, 40, 50, 60, 70})

    def test_record_qualified(self):
        self.put(SUB, "2026-01-01T13:15:00.000Z#p", "reached_pitch")
        fn.record_qualified(SUB, "2026-01-01T13:15:00.000Z#p", False, [10, 20])
        row = fn.events_table().get_item(Key={"subscriber_id": SUB, "event_at": "2026-01-01T13:15:00.000Z#p"})["Item"]
        self.assertIs(row["qualified"], False)
        self.assertEqual([int(m) for m in row["missing_marks"]], [10, 20])

    def test_record_qualified_needs_the_row(self):
        with self.assertRaises(ClientError):
            fn.record_qualified(SUB, "2026-01-01T13:15:00.000Z#nope", True, [])

    def test_cannot_mark_missing_registration(self):
        self.assertFalse(fn.mark_pitch_counted(SUB, REGISTRATION))
        self.assertNotIn("Item", fn.events_table().get_item(Key={"subscriber_id": SUB, "event_at": REGISTRATION}))


EMAIL = "rider+test@example.com"


def buy_call(query, method="GET"):
    return {
        "rawPath": "/buy",
        "rawQueryString": "&".join(f"{k}={v}" for k, v in (query or {}).items()),
        "queryStringParameters": query,
        "headers": {},
        "requestContext": {"http": {"method": method, "sourceIp": "1.2.3.4"}},
        "isBase64Encoded": False,
    }


def kit_reply(email=EMAIL, status=200):
    return status, json.dumps({"subscriber": {"id": int(SUB), "email_address": email, "fields": {}}})


class BuyTests(PageFakes, unittest.TestCase):
    """GET /buy: the watch page's buy button."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(fn, "kit_request", return_value=kit_reply())
        self.kit = patcher.start()
        self.addCleanup(patcher.stop)

    def buy(self, query={"indiv_token": TOKEN}, **kwargs):
        with self.assertLogs(fn.logger, level="INFO") as logs:
            fn.logger.info("start")  # assertLogs needs at least one line
            result = fn.lambda_handler(buy_call(query, **kwargs), CONTEXT)
        self.log_lines = "\n".join(logs.output)
        return result

    def assertRedirect(self, result, location):
        self.assertEqual(result["statusCode"], 302)
        self.assertEqual(result["headers"]["Location"], location)
        self.assertEqual(result["headers"]["Cache-Control"], "no-store")

    def expected_cart_link(self, email=EMAIL):
        return (TEST_SETTINGS["buy_link"].replace("{email}", quote(email, safe="@+"))
                + "?indiv_token=" + quote(TOKEN, safe=""))

    def test_sends_to_cart_with_email_and_token(self):
        result = self.buy()
        self.assertRedirect(result, self.expected_cart_link())
        location = result["headers"]["Location"]
        self.assertIn("/" + EMAIL + "?", location)                       # email at the end of the path
        self.assertEqual(parse_qs(urlsplit(location).query)["indiv_token"], [TOKEN])
        self.kit.assert_called_once()
        self.assertEqual(self.kit.call_args.args[:2], ("GET", f"subscribers/{SUB}"))
        self.assertEqual(self.kit.call_args.kwargs["timeout"], fn.BUY_KIT_TIMEOUT_SECONDS)

    def test_logs_and_tags_the_click(self):
        self.buy()
        [row] = self.logged()
        self.assertEqual((row["subscriber_id"], row["event"], row["source"]), (SUB, "cta_click", "watch_page"))
        self.assertEqual(self.jobs(), [{"job": "kit_actions", "subscriber_id": SUB, "milestone": "cta_click",
                                        "event_at": row["event_at"]}])
        self.notify.assert_not_called()

    def test_email_never_logged_or_stored(self):
        self.buy()
        self.assertNotIn(EMAIL, self.log_lines)
        self.assertNotIn(EMAIL, json.dumps(self.logged()))
        self.assertNotIn(EMAIL, json.dumps(self.jobs()))

    def test_link_with_existing_query(self):
        with mock.patch.dict(SECRETS, {"buy_link": "https://timer.example.com/t/{email}?x=1"}):
            location = self.buy()["headers"]["Location"]
        self.assertTrue(location.startswith("https://timer.example.com/t/" + EMAIL + "?x=1&indiv_token="))

    def test_odd_characters_in_email_are_encoded(self):
        self.kit.return_value = kit_reply("o'brien/x?y#z@example.com")
        location = self.buy()["headers"]["Location"]
        self.assertIn("/o%27brien%2Fx%3Fy%23z@example.com?indiv_token=", location)

    def test_missing_and_invalid_tokens_go_to_fallback(self):
        bad = kit_token.make_token(SUB, OTHER_KEY)
        for query, status in [(None, "missing"), ({}, "missing"), ({"indiv_token": ""}, "missing"),
                              ({"indiv_token": bad}, "invalid"), ({"indiv_token": "junk"}, "invalid")]:
            self.table.reset_mock()
            self.assertRedirect(self.buy(query), TEST_SETTINGS["buy_fallback_url"])
            [row] = self.logged()
            self.assertEqual((row["subscriber_id"], row["event"], row["token_status"]),
                             ("buy_button", "cta_click", status), query)
        self.kit.assert_not_called()
        self.assertEqual(self.jobs(), [])

    def test_kit_busy_then_ok(self):
        self.kit.side_effect = [(503, "busy"), kit_reply()]
        self.assertRedirect(self.buy(), self.expected_cart_link())
        self.assertEqual(self.kit.call_count, 2)
        self.notify.assert_not_called()

    def test_kit_network_error_then_ok(self):
        self.kit.side_effect = [fn.RetryableError("timeout"), kit_reply()]
        self.assertRedirect(self.buy(), self.expected_cart_link())

    def test_kit_failing_goes_to_fallback_and_emails_admin(self):
        for side_effect, calls in [([(429, "slow down"), (500, "err")], 2),
                                   ([fn.RetryableError("x"), fn.RetryableError("y")], 2),
                                   ([(404, "not found")], 1),
                                   ([(200, "not json")], 1),
                                   ([kit_reply(email="")], 1),
                                   ([kit_reply(email="no at sign")], 1),
                                   ([kit_reply(email=None)], 1)]:
            self.kit.reset_mock(side_effect=True)
            self.notify.reset_mock()
            self.kit.side_effect = side_effect
            self.assertRedirect(self.buy(), TEST_SETTINGS["buy_fallback_url"])
            self.assertEqual(self.kit.call_count, calls, side_effect)
            self.notify.assert_called_once()
            self.assertIn(SUB, self.notify.call_args.args[0])
        self.assertTrue(all(row["event"] == "cta_click" for row in self.logged()))  # still logged

    def test_failure_email_has_no_email_address(self):
        self.kit.return_value = (200, json.dumps({"subscriber": {"email_address": EMAIL + " "}}))
        self.buy()
        self.notify.assert_called_once()
        self.assertNotIn(EMAIL, self.notify.call_args.args[0])

    def test_log_and_job_failures_dont_stop_the_sale(self):
        self.table.return_value.put_item.side_effect = Exception("dynamo down")
        self.lambda_client.return_value.invoke.side_effect = Exception("boom")
        self.assertRedirect(self.buy(), self.expected_cart_link())
        self.assertEqual(self.notify.call_count, 2)

    def test_settings_failure(self):
        self.settings.side_effect = Exception("no ssm")
        self.assertEqual(self.buy()["statusCode"], 503)

    def test_get_only(self):
        self.assertEqual(self.buy(method="POST")["statusCode"], 405)
        self.assertEqual(self.call({"token": TOKEN, "milestone": "started"}, path="/buy")["statusCode"], 405)
        self.kit.assert_not_called()

    def test_milestone_cta_click_still_accepted(self):
        # An old copy of the watch page may still send it
        self.assertEqual(self.call({"token": TOKEN, "milestone": "cta_click"})["statusCode"], 200)


def watch_expired_call(query, method="GET"):
    return {
        "rawPath": "/watch-expired",
        "rawQueryString": "&".join(f"{k}={v}" for k, v in (query or {}).items()),
        "queryStringParameters": query,
        "headers": {},
        "requestContext": {"http": {"method": method, "sourceIp": "1.2.3.4"}},
        "isBase64Encoded": False,
    }


IN_CART_TAG = TEST_SETTINGS["in_cart_tag_id"]


def tags_reply(tag_ids, has_next=False, end_cursor="cursor1", status=200):
    return status, json.dumps({"tags": [{"id": int(t), "name": f"tag {t}", "tagged_at": "2026-11-10T10:00:00Z"}
                                        for t in tag_ids],
                               "pagination": {"has_previous_page": False, "has_next_page": has_next,
                                              "start_cursor": "c0", "end_cursor": end_cursor, "per_page": 1000}})


class WatchExpiredTests(PageFakes, unittest.TestCase):
    """GET /watch-expired: where the watch timer's expired redirect goes."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(fn, "kit_request")
        self.kit = patcher.start()
        self.addCleanup(patcher.stop)

    def expire(self, query={"indiv_token": TOKEN, "email": EMAIL, "utm_source": "me"}, **kwargs):
        with self.assertLogs(fn.logger, level="INFO") as logs:
            fn.logger.info("start")
            result = fn.lambda_handler(watch_expired_call(query, **kwargs), CONTEXT)
        self.log_lines = "\n".join(logs.output)
        return result

    def assertRedirect(self, result, location):
        self.assertEqual(result["statusCode"], 302)
        self.assertEqual(result["headers"]["Location"], location)
        self.assertEqual(result["headers"]["Cache-Control"], "no-store")

    def replay_link(self):
        return (TEST_SETTINGS["cart_watch_link"].replace("{email}", quote(EMAIL, safe="@+"))
                + "?indiv_token=" + quote(TOKEN, safe=""))

    def error_link(self):
        return TEST_SETTINGS["watch_expired_error_url"] + "?indiv_token=" + quote(TOKEN, safe="")

    def outcome(self, subscriber_id=SUB):
        [row] = self.logged()
        self.assertEqual((row["subscriber_id"], row["event"], row["source"]),
                         (subscriber_id, "watch_expired_redirect", "watch_expired"))
        return row["outcome"]

    def test_cart_open_goes_back_to_replay(self):
        self.kit.side_effect = [tags_reply(["111", IN_CART_TAG]), kit_reply()]
        self.assertRedirect(self.expire(), self.replay_link())
        self.assertEqual(self.kit.call_args_list[0].args[:2],
                         ("GET", f"subscribers/{SUB}/tags?per_page={fn.KIT_TAGS_PER_PAGE}"))
        self.assertEqual(self.kit.call_args_list[1].args[:2], ("GET", f"subscribers/{SUB}"))
        self.assertTrue(all(c.kwargs["timeout"] == fn.BUY_KIT_TIMEOUT_SECONDS for c in self.kit.call_args_list))
        self.assertEqual(self.outcome(), "cart")
        self.notify.assert_not_called()

    def test_no_cart_goes_to_expired_without_asking_for_email(self):
        self.kit.side_effect = [tags_reply(["111", "222"])]
        self.assertRedirect(self.expire(), TEST_SETTINGS["expired_url"])
        self.assertEqual(self.kit.call_count, 1)
        self.assertEqual(self.outcome(), "expired")
        self.notify.assert_not_called()

    def test_no_tags_at_all(self):
        self.kit.side_effect = [tags_reply([])]
        self.assertRedirect(self.expire(), TEST_SETTINGS["expired_url"])

    def test_tag_found_on_a_later_page(self):
        self.kit.side_effect = [tags_reply(["111"], has_next=True, end_cursor="abc"),
                                tags_reply([IN_CART_TAG]), kit_reply()]
        self.assertRedirect(self.expire(), self.replay_link())
        self.assertIn("after=abc", self.kit.call_args_list[1].args[1])

    def test_ignores_the_email_in_the_address(self):
        # Only the token says who it is: a typed-in email can't open someone else's cart
        self.kit.side_effect = [tags_reply([IN_CART_TAG]), kit_reply()]
        result = self.expire({"indiv_token": TOKEN, "email": "someone.else@example.com"})
        self.assertRedirect(result, self.replay_link())
        self.assertNotIn("someone.else", result["headers"]["Location"])

    def test_email_never_logged_or_stored(self):
        self.kit.side_effect = [tags_reply([IN_CART_TAG]), kit_reply()]
        self.expire()
        self.assertNotIn(EMAIL, self.log_lines)
        self.assertNotIn(EMAIL, json.dumps(self.logged()))

    def test_missing_and_invalid_tokens_go_to_expired(self):
        bad = kit_token.make_token(SUB, OTHER_KEY)
        for query, status in [(None, "missing"), ({}, "missing"), ({"indiv_token": ""}, "missing"),
                              ({"email": EMAIL}, "missing"),
                              ({"indiv_token": bad}, "invalid"), ({"indiv_token": "junk"}, "invalid")]:
            self.table.reset_mock()
            self.assertRedirect(self.expire(query), TEST_SETTINGS["expired_url"])
            self.assertEqual(self.outcome("watch_expired"), "expired")
            self.assertEqual(self.logged()[0]["token_status"], status, query)
        self.kit.assert_not_called()
        self.notify.assert_not_called()

    def test_kit_busy_then_ok(self):
        self.kit.side_effect = [(503, "busy"), tags_reply([IN_CART_TAG]), fn.RetryableError("x"), kit_reply()]
        self.assertRedirect(self.expire(), self.replay_link())
        self.notify.assert_not_called()

    def test_tags_unreadable_goes_to_try_again_page(self):
        for side_effect in [[(429, "slow"), (500, "err")],
                            [fn.RetryableError("x"), fn.RetryableError("y")],
                            [(404, "not found")],
                            [(200, "not json")],
                            [(200, json.dumps({"no": "tags"}))]]:
            self.kit.reset_mock(side_effect=True)
            self.notify.reset_mock()
            self.table.reset_mock()
            self.kit.side_effect = side_effect
            self.assertRedirect(self.expire(), self.error_link())
            self.assertEqual(self.outcome(), "error")
            self.notify.assert_called_once()
            self.assertIn(SUB, self.notify.call_args.args[0])

    def test_email_unavailable_goes_to_try_again_page(self):
        for second in [[(500, "err"), (500, "err")], [kit_reply(email="")], [kit_reply(email=None)]]:
            self.kit.reset_mock(side_effect=True)
            self.notify.reset_mock()
            self.table.reset_mock()
            self.kit.side_effect = [tags_reply([IN_CART_TAG])] + second
            self.assertRedirect(self.expire(), self.error_link())
            self.assertEqual(self.outcome(), "error")
            self.notify.assert_called_once()

    def test_too_many_tag_pages_goes_to_try_again_page(self):
        self.kit.side_effect = [tags_reply(["111"], has_next=True)] * fn.KIT_TAG_PAGES
        self.assertRedirect(self.expire(), self.error_link())
        self.notify.assert_called_once()

    def test_error_page_with_existing_query(self):
        self.kit.side_effect = [(404, "x")]
        with mock.patch.dict(SECRETS, {"watch_expired_error_url": "https://www.example.com/again?x=1"}):
            location = self.expire()["headers"]["Location"]
        self.assertEqual(location, "https://www.example.com/again?x=1&indiv_token=" + quote(TOKEN, safe=""))

    def test_log_failure_doesnt_stop_the_redirect(self):
        self.table.return_value.put_item.side_effect = Exception("dynamo down")
        self.kit.side_effect = [tags_reply([IN_CART_TAG]), kit_reply()]
        self.assertRedirect(self.expire(), self.replay_link())
        self.notify.assert_called_once()

    def test_never_tags_or_starts_jobs(self):
        self.kit.side_effect = [tags_reply([IN_CART_TAG]), kit_reply()]
        self.expire()
        self.assertTrue(all(c.args[0] == "GET" for c in self.kit.call_args_list))
        self.assertEqual(self.jobs(), [])

    def test_settings_failure(self):
        self.settings.side_effect = Exception("no ssm")
        self.assertEqual(self.expire()["statusCode"], 503)

    def test_get_only(self):
        self.assertEqual(self.expire(method="POST")["statusCode"], 405)
        self.kit.assert_not_called()


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
            self.OWN + "clicked-tag-id": CLICKED_TAG,
            self.OWN + "pitch-second": str(PITCH_SECOND) + "\n",
            self.OWN + "allowed-missing-marks": str(ALLOWED_MISSING),
            self.OWN + "buy-link": TEST_SETTINGS["buy_link"] + " ",
            self.OWN + "buy-fallback-url": TEST_SETTINGS["buy_fallback_url"],
            self.OWN + "in-cart-tag-id": TEST_SETTINGS["in_cart_tag_id"] + "\n",
            self.OWN + "cart-watch-link": TEST_SETTINGS["cart_watch_link"],
            self.OWN + "expired-url": " " + TEST_SETTINGS["expired_url"],
            self.OWN + "watch-expired-error-url": TEST_SETTINGS["watch_expired_error_url"],
        }

    def load(self, overrides=None):
        fn._settings = None
        values = dict(self.params, **(overrides or {}))
        params = {n: v for n, v in values.items() if v is not None}

        def get_parameters(Names, WithDecryption):
            # Like AWS: at most 10 names per call, and only the ones asked for come back
            if len(Names) > 10:
                raise ClientError({"Error": {"Code": "ValidationException",
                                             "Message": "Member must have length less than or equal to 10"}},
                                  "GetParameters")
            self.assertTrue(WithDecryption)
            return {"Parameters": [{"Name": n, "Value": params[n]} for n in Names if n in params]}

        with mock.patch.object(fn, "ssm_client") as ssm:
            ssm.return_value.get_parameters.side_effect = get_parameters
            result = fn.settings()
            fn.settings()
        return result, ssm

    def test_loads_once_and_strips(self):
        cfg, ssm = self.load()
        self.assertEqual(cfg, SECRETS)
        # 15 settings: two calls the first time, none the second (loaded once per container)
        self.assertEqual(ssm.return_value.get_parameters.call_count, 2)

    def test_each_setting_required(self):
        for name in self.params:
            with self.assertRaises(RuntimeError, msg=name):
                self.load({name: None})

    def test_bad_values_refused(self):
        host = urlsplit(ORIGIN).hostname
        for name, bad in [(self.OWN + "started-tag-id", "started_watching"),
                          (self.OWN + "watched-tag-id", WATCHED_TAG + "x"),
                          (self.OWN + "allowed-origin", host),
                          (self.OWN + "allowed-origin", "http://" + host),
                          (self.OWN + "clicked-tag-id", "clicked_buy"),
                          (self.OWN + "pitch-second", "72:09"),
                          (self.OWN + "pitch-second", "0"),
                          (self.OWN + "pitch-second", "999999"),
                          (self.OWN + "allowed-missing-marks", "-1"),
                          (self.OWN + "allowed-missing-marks", "one"),
                          (self.OWN + "buy-link", "http://timer.example.com/t/{email}"),
                          (self.OWN + "buy-link", "https://timer.example.com/t/abc123"),
                          (self.OWN + "buy-link", "https://timer.example.com/{email}/{email}"),
                          (self.OWN + "buy-link", "https://timer.example.com/t/{email}#x"),
                          (self.OWN + "buy-fallback-url", "www.example.com/try-again"),
                          (self.OWN + "in-cart-tag-id", "in_cart_window"),
                          (self.OWN + "cart-watch-link", "https://timer.example.com/p/def456"),
                          (self.OWN + "cart-watch-link", "http://timer.example.com/p/{email}"),
                          (self.OWN + "cart-watch-link", "https://timer.example.com/p/{email}#x"),
                          (self.OWN + "expired-url", "www.example.com/expired"),
                          (self.OWN + "watch-expired-error-url", "http://www.example.com/replay-try-again"),
                          (self.OWN + "watch-expired-error-url", "https://www.example.com/x#y")]:
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


class PageBlockTests(unittest.TestCase):
    """The page-views/*.js blocks are pasted into Kajabi's Custom Javascript, which wraps them in script tags."""

    FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "page-views")

    def blocks(self):
        names = sorted(n for n in os.listdir(self.FOLDER) if n.endswith(".js"))
        self.assertEqual(names, ["registration-page.js", "replay-try-again-page.js", "sales-page.js"])
        for name in names:
            with open(os.path.join(self.FOLDER, name), encoding="utf-8") as f:
                yield name, f.read()

    def test_no_script_tags(self):
        # Even in a comment, a closing script tag ends Kajabi's wrapper early and the block never runs
        for name, text in self.blocks():
            self.assertNotRegex(text.lower(), r"<\s*/?\s*script", name)

    def test_placeholders_only(self):
        # Real addresses and Kit field keys are filled in on the page, never committed
        for name, text in self.blocks():
            hosts = re.findall(r"https?://([A-Za-z0-9.-]+)", text)
            self.assertEqual([h for h in hosts if not h.startswith("YOUR-")], [], name)
        self.assertEqual(re.findall(r'utm_\w+: "(?!KIT_FIELD_KEY_FOR_)', dict(self.blocks())["registration-page.js"]), [])


if __name__ == "__main__":
    unittest.main()
