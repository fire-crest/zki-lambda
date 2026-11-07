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
}
SUB = os.environ.get("TEST_SUBSCRIBER_ID", "1234567890")
SUB2 = "1234567891"

KEY = base64.urlsafe_b64encode(bytes(range(64))).decode()
OTHER_KEY = base64.urlsafe_b64encode(bytes(range(1, 65))).decode()
SECRETS = {"kit_api_key": "kit-key", "token_secret": KEY, **TEST_SETTINGS}
HOOK = TEST_SETTINGS["webhook_key"]
TOKEN_FIELD = TEST_SETTINGS["token_field"]
COUNT_FIELD = TEST_SETTINGS["registration_count_field"]
READY_TAG = TEST_SETTINGS["token_ready_tag_id"]
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

    def __init__(self, token=None, count=None, fail=None):
        self.calls = []
        self.fields = {TOKEN_FIELD: token, COUNT_FIELD: count}
        self.fail = dict(fail or {})  # {(method, first path part): [(status, body), ...]} used in order

    def __call__(self, method, endpoint, body, key):
        self.calls.append((method, endpoint, json.dumps(body) if body else None))
        queued = self.fail.get((method, endpoint.split("/")[0]))
        if queued:
            return queued.pop(0)
        if method == "POST":
            return 201, "{}"
        if method == "PUT":
            self.fields.update(body["fields"])
        return 200, json.dumps({"subscriber": {"id": int(SUB), "fields": self.fields}})

    def methods(self):
        return [c[0] for c in self.calls]


class JobTests(unittest.TestCase):
    def setUp(self):
        self.marks = set()
        patchers = [
            mock.patch.object(fn, "settings", return_value=SECRETS),
            mock.patch.object(fn, "notify_failure"),
            mock.patch.object(fn, "mark_counted", side_effect=self.fake_mark),
            mock.patch.object(fn, "unmark_counted", side_effect=lambda s, e: self.marks.discard((s, e))),
        ]
        self.settings, self.notify, self.mark, self.unmark = [p.start() for p in patchers]
        for p in patchers:
            self.addCleanup(p.stop)

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
        self.assertEqual(kit.methods(), ["GET", "PUT", "PUT", "POST"])
        self.notify.assert_not_called()

    def test_existing_valid_token_never_rewritten(self):
        token = kit_token.make_token(SUB, KEY)
        kit = FakeKit(token=token, count="2")
        self.run_job(kit)
        self.assertEqual(kit.methods(), ["GET", "PUT", "POST"])  # only the count PUT
        self.assertNotIn(TOKEN_FIELD, kit.calls[1][2])
        self.assertEqual(kit.fields[COUNT_FIELD], "3")

    def test_invalid_or_old_token_replaced(self):
        for bad in ["", "7050754e6b2f6c5a6b6f72776e386665675a4c4348773d3d",
                    kit_token.make_token(SUB, OTHER_KEY), kit_token.make_token(SUB2, KEY)]:
            kit = FakeKit(token=bad)
            self.marks.clear()
            self.run_job(kit)
            self.assertEqual(kit_token.read_token(kit.fields[TOKEN_FIELD], KEY), SUB, bad)

    def test_blank_or_junk_count_starts_at_one(self):
        for start in [None, "", "abc", "-1"]:
            kit = FakeKit(token=kit_token.make_token(SUB, KEY), count=start)
            self.marks.clear()
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

    def test_tag_must_be_an_id(self):
        with self.assertRaises(RuntimeError):
            self.load({self.OWN + "token-ready-tag-id": "token_ready"})


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
                                 "registration_count_field", "token_ready_tag_id"}
        suspicious = {q for q in quoted - allowed_settings_keys
                      if q.endswith(("_token", "_count", "_ready")) or q.startswith("indiv")}
        self.assertEqual(suspicious, set())


if __name__ == "__main__":
    unittest.main()
