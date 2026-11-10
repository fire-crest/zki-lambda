"""
Tests for evergreen-sms-webinar-registration. AWS and Kit are faked; the
DynamoDB tests use moto's in-memory DynamoDB if it's installed
(pip install "moto[dynamodb]"), and are skipped otherwise.

Run from this folder:   python3 -m unittest -v

All test values are made up and set in one place (TEST_SETTINGS below), so no
business details live in this file. Any of them can be overridden with an
environment variable, e.g.  TEST_TOKEN_READY_TAG_ID=42 python3 -m unittest -v
"""

import base64
import inspect
import json
import os
import re
import unittest
from types import SimpleNamespace
from unittest import mock

from botocore.exceptions import ClientError

import kit_token
import lambda_function as fn

# ---------------------------------------------------------------------------
# Test settings - every business-specific value used by the tests
# ---------------------------------------------------------------------------

TEST_SETTINGS = {
    "webhook_key": os.environ.get("TEST_WEBHOOK_KEY", "test-webhook-key"),
    "token_field": os.environ.get("TEST_TOKEN_FIELD", "test_token"),
    "registration_count_field": os.environ.get("TEST_REGISTRATION_COUNT_FIELD", "test_registration_count"),
    "token_ready_tag_id": os.environ.get("TEST_TOKEN_READY_TAG_ID", "1000003"),
    "watch_window_tag_id": os.environ.get("TEST_WATCH_WINDOW_TAG_ID", "1000004"),
    "cart_window_tag_id": os.environ.get("TEST_CART_WINDOW_TAG_ID", "1000005"),
}
UTM_FIELDS = ["test_utm_source", "test_utm_medium", "test_utm_campaign", "test_utm_content"]
SUB = os.environ.get("TEST_SUBSCRIBER_ID", "1234567890")
SUB2 = "1234567891"

KEY = base64.urlsafe_b64encode(bytes(range(64))).decode()
OTHER_KEY = base64.urlsafe_b64encode(bytes(range(1, 65))).decode()
SECRETS = {"kit_api_key": "kit-key", "token_secret": KEY, **TEST_SETTINGS, "utm_fields": UTM_FIELDS}
HOOK = TEST_SETTINGS["webhook_key"]
TOKEN_FIELD = TEST_SETTINGS["token_field"]
COUNT_FIELD = TEST_SETTINGS["registration_count_field"]
READY_TAG = TEST_SETTINGS["token_ready_tag_id"]
WATCH_WINDOW_TAG = TEST_SETTINGS["watch_window_tag_id"]
CART_WINDOW_TAG = TEST_SETTINGS["cart_window_tag_id"]
OTHER_TAG = "1000099"
EVENT_AT = "2026-01-01T12:00:00.000Z#abc123"
CONTEXT = SimpleNamespace(invoked_function_arn="arn:aws:lambda:eu-west-2:111:function:test-function")


def webhook_call(body, path=None, method="POST"):
    return {
        "rawPath": "/" + HOOK if path is None else path,
        "headers": {"content-type": "application/json"},
        "requestContext": {"http": {"method": method, "sourceIp": "1.2.3.4"}},
        "body": json.dumps(body) if not isinstance(body, str) else body,
        "isBase64Encoded": False,
    }


def conditional_failure():
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "no"}}, "UpdateItem")


# ---------------------------------------------------------------------------
# Webhook mode
# ---------------------------------------------------------------------------

class WebhookTests(unittest.TestCase):
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
        return fn.lambda_handler(webhook_call(body, **kwargs), CONTEXT)

    def logged(self):
        return [c.kwargs["Item"] for c in self.table.return_value.put_item.call_args_list]

    def jobs(self):
        return [json.loads(c.kwargs["Payload"]) for c in self.lambda_client.return_value.invoke.call_args_list]

    def test_api_webhook_format(self):
        result = self.call({"subscriber": {"id": int(SUB), "email_address": "someone@example.com"}})
        self.assertEqual(result["statusCode"], 200)
        [row] = self.logged()
        self.assertEqual((row["subscriber_id"], row["event"], row["source"]), (SUB, "registered", "kit_form"))
        self.assertRegex(row["event_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z#[0-9a-f]{6}$")
        self.assertNotIn("someone@example.com", json.dumps(row))  # never store emails
        [job] = self.jobs()
        self.assertEqual(job, {"job": "registration", "subscriber_id": SUB, "event_at": row["event_at"]})

    def test_rule_webhook_format_with_several_subscribers(self):
        body = {"delivery_id": 1, "events": [
            {"data": {"subscriber": {"id": SUB}}},
            {"data": {"subscriber": {"id": SUB2}}},
            {"data": {"subscriber": {"id": SUB}}},  # duplicate in one delivery
        ]}
        self.assertEqual(self.call(body)["statusCode"], 200)
        self.assertEqual([r["subscriber_id"] for r in self.logged()], [SUB, SUB2])
        self.assertEqual([j["subscriber_id"] for j in self.jobs()], [SUB, SUB2])
        self.notify.assert_not_called()

    def test_each_delivery_is_a_new_registration(self):
        self.call({"subscriber": {"id": SUB}})
        self.call({"subscriber": {"id": SUB}})
        rows = self.logged()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["event_at"], rows[1]["event_at"])

    def test_wrong_key_forbidden(self):
        for path in ["/wrong", "/", "", "/" + HOOK + "x", "/x" + HOOK]:
            self.assertEqual(self.call({"subscriber": {"id": SUB}}, path=path)["statusCode"], 403, path)
        self.assertEqual(self.logged(), [])

    def test_key_with_stray_whitespace_accepted(self):
        for path in ["/" + HOOK + "%0A", "/" + HOOK + "%20", "/" + HOOK + "/"]:
            self.assertEqual(self.call({"subscriber": {"id": SUB}}, path=path)["statusCode"], 200, path)

    def test_get_refused(self):
        self.assertEqual(self.call({}, method="GET")["statusCode"], 405)

    def test_bad_payloads_reported_but_200(self):
        for body in ["not json", "[1]", {}, {"subscriber": {}}, {"subscriber": {"id": "abc"}},
                     {"events": [{"data": {}}]}]:
            self.assertEqual(self.call(body)["statusCode"], 200, body)
        self.assertEqual(self.logged(), [])
        self.assertEqual(self.notify.call_count, 6)

    def test_log_failure_asks_kit_to_retry(self):
        self.table.return_value.put_item.side_effect = Exception("dynamo down")
        result = self.call({"subscriber": {"id": SUB}})
        self.assertEqual(result["statusCode"], 503)
        self.assertEqual(result["headers"]["Retry-After"], "60")
        self.assertEqual(self.jobs(), [])

    def test_job_start_failure_reported(self):
        self.lambda_client.return_value.invoke.side_effect = Exception("boom")
        self.assertEqual(self.call({"subscriber": {"id": SUB}})["statusCode"], 503)
        self.notify.assert_called_once()

    def test_settings_failure(self):
        self.settings.side_effect = Exception("no ssm")
        self.assertEqual(self.call({"subscriber": {"id": SUB}})["statusCode"], 503)


# ---------------------------------------------------------------------------
# Job mode
# ---------------------------------------------------------------------------

class FakeKit:
    """Pretends to be Kit for one subscriber: records calls, keeps fields."""

    def __init__(self, token=None, count=None, fail=None, tags=(), utms=None):
        self.calls = []
        self.fields = {TOKEN_FIELD: token, COUNT_FIELD: count, **(utms or {})}
        self.tags = [int(t) for t in tags]
        self.fail = dict(fail or {})  # {(method, first path part): [(status, body), ...]} used in order

    def __call__(self, method, endpoint, body, key):
        self.calls.append((method, endpoint, json.dumps(body) if body else None))
        queued = self.fail.get((method, endpoint.split("/")[0]))
        if queued:
            return queued.pop(0)
        if method == "GET" and "/tags" in endpoint:
            return 200, json.dumps({"tags": [{"id": t, "name": f"tag {t}"} for t in self.tags],
                                    "pagination": {"has_next_page": False}})
        if method == "POST":
            return 201, "{}"
        if method == "PUT":
            self.fields.update(body["fields"])
        return 200, json.dumps({"subscriber": {"id": int(SUB), "fields": self.fields}})

    def methods(self):
        return [c[0] for c in self.calls]


class JobTests(unittest.TestCase):
    """The event log is faked: self.marks holds counted registrations, self.new_runs the recorded new_run values."""

    def setUp(self):
        self.marks = set()
        self.new_runs = {}
        self.utms = {}
        patchers = [
            mock.patch.object(fn, "settings", return_value=SECRETS),
            mock.patch.object(fn, "notify_failure"),
            mock.patch.object(fn, "mark_counted", side_effect=self.fake_mark),
            mock.patch.object(fn, "unmark_counted", side_effect=lambda s, e: self.marks.discard((s, e))),
            mock.patch.object(fn, "record_new_run", side_effect=self.fake_record_new_run),
            mock.patch.object(fn, "record_utms", side_effect=self.fake_record_utms),
        ]
        self.settings, self.notify, self.mark, self.unmark, self.record, self.record_utms = \
            [p.start() for p in patchers]
        for p in patchers:
            self.addCleanup(p.stop)

    def fake_record_new_run(self, subscriber_id, event_at, new_run):
        return self.new_runs.setdefault((subscriber_id, event_at), new_run)  # first decision wins

    def fake_record_utms(self, subscriber_id, event_at, utms):
        stored = self.utms.setdefault((subscriber_id, event_at), {})
        for name, value in utms.items():
            stored.setdefault(name, value)  # first value wins

    def fake_mark(self, subscriber_id, event_at):
        if (subscriber_id, event_at) in self.marks:
            return False
        self.marks.add((subscriber_id, event_at))
        return True

    def run_job(self, kit, subscriber_id=SUB, event_at=EVENT_AT):
        with mock.patch.object(fn, "kit_request", side_effect=kit):
            fn.lambda_handler({"job": "registration", "subscriber_id": subscriber_id, "event_at": event_at}, CONTEXT)

    def test_new_subscriber_gets_token_count_then_tag(self):
        kit = FakeKit()
        self.run_job(kit)
        self.assertEqual(kit_token.read_token(kit.fields[TOKEN_FIELD], KEY), SUB)
        self.assertEqual(kit.fields[COUNT_FIELD], "1")
        self.assertEqual(kit.calls[-1][:2], ("POST", f"tags/{READY_TAG}/subscribers/{SUB}"))
        self.assertEqual(kit.methods(), ["GET", "PUT", "GET", "PUT", "POST"])
        self.notify.assert_not_called()

    def test_utms_copied_from_kit_fields_to_the_row(self):
        values = ["meta", "paid", "evergreen_test", "ad_7"]
        self.run_job(FakeKit(utms=dict(zip(UTM_FIELDS, values))))
        self.assertEqual(self.utms[(SUB, EVENT_AT)], dict(zip(fn.UTM_ATTRIBUTES, values)))
        self.notify.assert_not_called()

    def test_utms_recorded_before_token_ready(self):
        order = []
        self.record_utms.side_effect = lambda *a: order.append("utms")
        kit = FakeKit(utms={UTM_FIELDS[0]: "direct"})
        def kit_and_log(method, endpoint, body, key):
            if method == "POST":
                order.append("token ready")
            return kit(method, endpoint, body, key)
        self.run_job(kit_and_log)
        self.assertEqual(order, ["utms", "token ready"])

    def test_missing_blank_or_bad_utms_left_out(self):
        utms = {UTM_FIELDS[0]: "  direct ", UTM_FIELDS[1]: "  ", UTM_FIELDS[2]: "x" * 101, UTM_FIELDS[3]: "a\nb"}
        self.run_job(FakeKit(utms=utms))
        self.assertEqual(self.utms[(SUB, EVENT_AT)], {"utm_source": "direct"})

    def test_no_utms_still_registers(self):
        kit = FakeKit()
        self.run_job(kit)
        self.assertEqual(self.utms.get((SUB, EVENT_AT), {}), {})
        self.assertEqual(kit.calls[-1][:2], ("POST", f"tags/{READY_TAG}/subscribers/{SUB}"))

    def test_retry_keeps_the_first_utms(self):
        self.run_job(FakeKit(utms={UTM_FIELDS[0]: "meta"}))
        self.run_job(FakeKit(utms={UTM_FIELDS[0]: "youtube"}))  # fields changed before the retry
        self.assertEqual(self.utms[(SUB, EVENT_AT)], {"utm_source": "meta"})

    def test_utm_values_maps_fields_in_order(self):
        fields = {UTM_FIELDS[3]: "c", UTM_FIELDS[0]: "s", "other": "x"}
        self.assertEqual(fn.utm_values(fields, UTM_FIELDS), {"utm_source": "s", "utm_content": "c"})
        self.assertEqual(fn.utm_values({UTM_FIELDS[0]: 5, UTM_FIELDS[1]: None}, UTM_FIELDS), {})

    def test_existing_valid_token_never_rewritten(self):
        token = kit_token.make_token(SUB, KEY)
        kit = FakeKit(token=token, count="2")
        self.run_job(kit)
        self.assertEqual(kit.methods(), ["GET", "GET", "PUT", "POST"])  # only the count PUT
        self.assertNotIn(TOKEN_FIELD, kit.calls[2][2])
        self.assertEqual(kit.fields[COUNT_FIELD], "3")

    def test_invalid_or_old_token_replaced(self):
        for bad in ["", "7050754e6b2f6c5a6b6f72776e386665675a4c4348773d3d",
                    kit_token.make_token(SUB, OTHER_KEY), kit_token.make_token(SUB2, KEY)]:
            kit = FakeKit(token=bad)
            self.marks.clear()
            self.new_runs.clear()
            self.run_job(kit)
            self.assertEqual(kit_token.read_token(kit.fields[TOKEN_FIELD], KEY), SUB, bad)

    def test_blank_or_junk_count_starts_at_one(self):
        for start in [None, "", "abc", "-1"]:
            kit = FakeKit(token=kit_token.make_token(SUB, KEY), count=start)
            self.marks.clear()
            self.new_runs.clear()
            self.run_job(kit)
            self.assertEqual(kit.fields[COUNT_FIELD], "1", start)

    def test_same_registration_never_counted_twice(self):
        kit = FakeKit(token=kit_token.make_token(SUB, KEY), count="5")
        self.run_job(kit)
        self.run_job(kit)  # e.g. Lambda retrying after the tag step failed
        self.assertEqual(kit.fields[COUNT_FIELD], "6")
        self.assertEqual(kit.methods().count("POST"), 2)  # tag still added each time

    def test_different_registrations_both_counted(self):
        kit = FakeKit(token=kit_token.make_token(SUB, KEY), count="5")
        self.run_job(kit, event_at=EVENT_AT)
        self.run_job(kit, event_at="2026-01-02T12:00:00.000Z#def456")
        self.assertEqual(kit.fields[COUNT_FIELD], "7")

    def test_kit_busy_on_count_unmarks_so_retry_counts(self):
        kit = FakeKit(token=kit_token.make_token(SUB, KEY), count="5", fail={("PUT", "subscribers"): [(429, "busy")]})
        with self.assertRaises(fn.RetryableError):
            self.run_job(kit)
        self.assertEqual(self.marks, set())
        self.assertNotIn("POST", kit.methods())  # token-ready not added yet
        self.run_job(kit)  # Lambda's retry
        self.assertEqual(kit.fields[COUNT_FIELD], "6")
        self.assertEqual(kit.methods().count("POST"), 1)

    def test_kit_busy_on_tag_retries_without_recounting(self):
        kit = FakeKit(token=kit_token.make_token(SUB, KEY), count="5", fail={("POST", "tags"): [(503, "down")]})
        with self.assertRaises(fn.RetryableError):
            self.run_job(kit)
        self.run_job(kit)
        self.assertEqual(kit.fields[COUNT_FIELD], "6")

    def test_window_open_not_counted_but_token_ready_still_added(self):
        for open_tag in (WATCH_WINDOW_TAG, CART_WINDOW_TAG):
            kit = FakeKit(token=kit_token.make_token(SUB, KEY), count="2", tags=[OTHER_TAG, open_tag])
            self.new_runs.clear()
            self.run_job(kit)
            self.assertEqual(kit.fields[COUNT_FIELD], "2", open_tag)
            self.assertEqual(self.new_runs[(SUB, EVENT_AT)], False)
            self.assertEqual(kit.calls[-1][:2], ("POST", f"tags/{READY_TAG}/subscribers/{SUB}"))
        self.mark.assert_not_called()

    def test_no_window_open_starts_a_new_run(self):
        kit = FakeKit(token=kit_token.make_token(SUB, KEY), count="2", tags=[OTHER_TAG])
        self.run_job(kit)
        self.assertEqual(kit.fields[COUNT_FIELD], "3")
        self.assertEqual(self.new_runs[(SUB, EVENT_AT)], True)

    def test_window_check_happens_before_token_ready(self):
        kit = FakeKit(token=kit_token.make_token(SUB, KEY))
        self.run_job(kit)
        tag_read = next(i for i, c in enumerate(kit.calls) if c[0] == "GET" and "/tags" in c[1])
        token_ready = next(i for i, c in enumerate(kit.calls) if c[0] == "POST")
        self.assertLess(tag_read, token_ready)

    def test_retry_keeps_the_first_decision(self):
        # First attempt: no window open, so a new run - but Kit fails before counting
        kit = FakeKit(token=kit_token.make_token(SUB, KEY), count="2",
                      fail={("PUT", "subscribers"): [(503, "down")]})
        with self.assertRaises(fn.RetryableError):
            self.run_job(kit)
        # By the time Lambda retries, the windows have moved on and a window tag is present
        kit.tags = [int(WATCH_WINDOW_TAG)]
        self.run_job(kit)
        self.assertEqual(kit.fields[COUNT_FIELD], "3")  # still counted, as first decided

    def test_cant_read_tags_retries_rather_than_guessing(self):
        kit = FakeKit(token=kit_token.make_token(SUB, KEY), count="2")
        real = kit.__call__
        def flaky(method, endpoint, body, key):
            if "/tags" in endpoint and method == "GET":
                return 500, "oops"
            return real(method, endpoint, body, key)
        with self.assertRaises(fn.RetryableError):
            self.run_job(flaky)
        self.assertEqual(self.new_runs, {})
        self.assertEqual(kit.fields[COUNT_FIELD], "2")

    def test_tag_list_paging(self):
        pages = [
            {"tags": [{"id": 1}], "pagination": {"has_next_page": True, "end_cursor": "abc=="}},
            {"tags": [{"id": int(CART_WINDOW_TAG)}], "pagination": {"has_next_page": False}},
        ]
        seen = []
        def kit(method, endpoint, body, key):
            seen.append(endpoint)
            return 200, json.dumps(pages[len(seen) - 1])
        with mock.patch.object(fn, "kit_request", side_effect=kit):
            self.assertTrue(fn.has_open_window(SUB, SECRETS))
        self.assertIn("after=abc%3D%3D", seen[1])

    def test_token_save_failure_blocks_token_ready(self):
        kit = FakeKit(fail={("PUT", "subscribers"): [(422, "bad field")]})
        self.run_job(kit)
        self.assertNotIn("POST", kit.methods())  # confirmation email must not be released
        self.notify.assert_called_once()

    def test_kit_busy_on_token_retries(self):
        kit = FakeKit(fail={("PUT", "subscribers"): [(500, "oops")]})
        with self.assertRaises(fn.RetryableError):
            self.run_job(kit)
        self.assertEqual(self.marks, set())

    def test_unknown_subscriber_reported(self):
        kit = FakeKit(fail={("GET", "subscribers"): [(404, "not found")]})
        self.run_job(kit)
        self.assertEqual(kit.methods(), ["GET"])
        self.notify.assert_called_once()

    def test_bad_job_input_reported(self):
        kit = FakeKit()
        self.run_job(kit, subscriber_id="abc")
        self.run_job(kit, event_at="")
        self.run_job(kit, event_at=None)
        self.assertEqual(kit.calls, [])
        self.assertEqual(self.notify.call_count, 3)


# ---------------------------------------------------------------------------
# The count-once marker, against an in-memory DynamoDB
# ---------------------------------------------------------------------------

try:
    import boto3
    from moto import mock_aws
except ImportError:
    mock_aws = None


@unittest.skipIf(mock_aws is None, "moto not installed")
class MarkerTests(unittest.TestCase):
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

    def test_first_mark_wins(self):
        event_at = fn.log_registration(SUB)
        self.assertTrue(fn.mark_counted(SUB, event_at))
        self.assertFalse(fn.mark_counted(SUB, event_at))

    def test_unmark_allows_marking_again(self):
        event_at = fn.log_registration(SUB)
        fn.mark_counted(SUB, event_at)
        fn.unmark_counted(SUB, event_at)
        self.assertTrue(fn.mark_counted(SUB, event_at))

    def test_cannot_mark_a_registration_that_was_never_logged(self):
        self.assertFalse(fn.mark_counted(SUB, EVENT_AT))
        # ...and it must not have created a stray row
        self.assertNotIn("Item", fn.events_table().get_item(Key={"subscriber_id": SUB, "event_at": EVENT_AT}))

    def test_new_run_first_decision_wins(self):
        event_at = fn.log_registration(SUB)
        self.assertTrue(fn.record_new_run(SUB, event_at, True))
        self.assertTrue(fn.record_new_run(SUB, event_at, False))  # a retry can't change it
        row = fn.events_table().get_item(Key={"subscriber_id": SUB, "event_at": event_at})["Item"]
        self.assertIs(row["new_run"], True)

    def test_new_run_false_recorded(self):
        event_at = fn.log_registration(SUB)
        self.assertFalse(fn.record_new_run(SUB, event_at, False))

    def test_new_run_needs_an_existing_row(self):
        with self.assertRaises(ClientError):
            fn.record_new_run(SUB, EVENT_AT, True)

    def test_utms_stored_and_first_values_win(self):
        event_at = fn.log_registration(SUB)
        fn.record_utms(SUB, event_at, {"utm_source": "meta", "utm_content": "ad_7"})
        fn.record_utms(SUB, event_at, {"utm_source": "youtube", "utm_medium": "organic"})  # a retry
        row = fn.events_table().get_item(Key={"subscriber_id": SUB, "event_at": event_at})["Item"]
        self.assertEqual({k: v for k, v in row.items() if k.startswith("utm_")},
                         {"utm_source": "meta", "utm_content": "ad_7", "utm_medium": "organic"})

    def test_no_utms_leaves_the_row_alone(self):
        event_at = fn.log_registration(SUB)
        fn.record_utms(SUB, event_at, {})
        row = fn.events_table().get_item(Key={"subscriber_id": SUB, "event_at": event_at})["Item"]
        self.assertFalse(any(k.startswith("utm_") for k in row))

    def test_utms_need_an_existing_row(self):
        with self.assertRaises(ClientError):
            fn.record_utms(SUB, EVENT_AT, {"utm_source": "meta"})
        self.assertNotIn("Item", fn.events_table().get_item(Key={"subscriber_id": SUB, "event_at": EVENT_AT}))

    def test_logged_row_contents(self):
        event_at = fn.log_registration(SUB)
        row = fn.events_table().get_item(Key={"subscriber_id": SUB, "event_at": event_at})["Item"]
        self.assertEqual(row, {"subscriber_id": SUB, "event_at": event_at, "event": "registered", "source": "kit_form"})


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class SettingsTests(unittest.TestCase):
    OWN = "/zki/evergreen-registration/"

    def setUp(self):
        fn._settings = None
        self.addCleanup(setattr, fn, "_settings", None)
        self.params = {
            "/zki/kit-api-key": "kit-key \n",
            "/zki/token-secret": KEY + "\n",
            self.OWN + "webhook-key": HOOK + "\n",
            self.OWN + "token-field": TOKEN_FIELD,
            self.OWN + "registration-count-field": " " + COUNT_FIELD,
            self.OWN + "token-ready-tag-id": READY_TAG,
            self.OWN + "watch-window-tag-id": WATCH_WINDOW_TAG,
            self.OWN + "cart-window-tag-id": CART_WINDOW_TAG,
            self.OWN + "utm-fields": " , ".join(UTM_FIELDS) + "\n",
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
            for missing in (None, "  "):
                with self.assertRaises(RuntimeError, msg=name):
                    self.load({name: missing})

    def test_tags_must_be_ids(self):
        for name in ("token-ready-tag-id", "watch-window-tag-id", "cart-window-tag-id"):
            with self.assertRaises(RuntimeError, msg=name):
                self.load({self.OWN + name: "a_tag_name"})


    def test_utm_fields_must_be_four_different_keys(self):
        for bad in ["a,b,c", "a,b,c,d,e", "a,b,,d", "a,b,c,a", "a b c d"]:
            with self.assertRaises(RuntimeError, msg=bad):
                self.load({self.OWN + "utm-fields": bad})


class NoBusinessDetailsTests(unittest.TestCase):
    """The function's code must work for any business: its settings come from Parameter Store."""

    def test_no_websites_in_code(self):
        hosts = re.findall(r"https?://([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)", inspect.getsource(fn))
        unexpected = [h for h in hosts if h != "api.kit.com" and not h.endswith("example.com")]
        self.assertEqual(unexpected, [], "Websites belong in Parameter Store, not the code")

    def test_no_kit_ids_in_code(self):
        self.assertEqual(re.findall(r"\b\d{6,}\b", inspect.getsource(fn)), [])

    def test_no_kit_field_or_tag_names_in_code(self):
        # Field keys and tag names are read from Parameter Store, never written as quoted text
        quoted = set(re.findall(r'"([a-z0-9_]+)"', inspect.getsource(fn)))
        allowed_settings_keys = {"kit_api_key", "token_secret", "webhook_key", "token_field",
                                 "registration_count_field", "token_ready_tag_id",
                                 "watch_window_tag_id", "cart_window_tag_id", "utm_fields"}
        suspicious = {q for q in quoted - allowed_settings_keys
                      if q.endswith(("_token", "_count", "_ready")) or q.startswith("indiv")}
        self.assertEqual(suspicious, set())


if __name__ == "__main__":
    unittest.main()
