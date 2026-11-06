"""
Tests for evergreen-sms-webinar-watch. Nothing here touches AWS or Kit - those are faked.

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


class JobTests(unittest.TestCase):
    def setUp(self):
        patchers = [mock.patch.object(fn, "settings", return_value=SECRETS),
                    mock.patch.object(fn, "notify_failure")]
        self.settings, self.notify = [p.start() for p in patchers]
        for p in patchers:
            self.addCleanup(p.stop)

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
            self.run_job(kit)
            self.assertEqual(kit.fields[COUNT_FIELD], "1", start)

    def test_padded_number_read_correctly(self):
        kit = FakeKit(count=" 7 ")
        self.run_job(kit)
        self.assertEqual(kit.fields[COUNT_FIELD], "8")

    def test_kit_busy_retries_before_counting(self):
        for status in [429, 500, 503]:
            kit = FakeKit(count="1", fail={("POST", "tags"): (status, "busy")})
            with self.assertRaises(fn.RetryableError):
                self.run_job(kit)
            self.assertNotIn("PUT", [c[0] for c in kit.calls])  # never counted

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
