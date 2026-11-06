"""
Tests for kit-set-indiv-token. Nothing here touches AWS or Kit - those are faked.

Run from this folder:   python3 -m unittest -v
"""

import base64
import json
import unittest
from types import SimpleNamespace
from unittest import mock

import kit_token
import lambda_function as fn

KEY = base64.urlsafe_b64encode(bytes(range(64))).decode()
OTHER_KEY = base64.urlsafe_b64encode(bytes(range(1, 65))).decode()
SECRETS = {"kit_api_key": "kit-key", "token_secret": KEY, "webhook_key": "hook-key-123"}
CONTEXT = SimpleNamespace(invoked_function_arn="arn:aws:lambda:eu-west-2:111:function:kit-set-indiv-token")


def http_event(body, path="/hook-key-123", method="POST", base64_body=False):
    raw = json.dumps(body) if not isinstance(body, str) else body
    return {
        "rawPath": path,
        "requestContext": {"http": {"method": method, "sourceIp": "1.2.3.4"}},
        "body": base64.b64encode(raw.encode()).decode() if base64_body else raw,
        "isBase64Encoded": base64_body,
    }


class TokenTests(unittest.TestCase):
    def test_round_trip(self):
        token = kit_token.make_token("1234567890", KEY)
        self.assertEqual(kit_token.read_token(token, KEY), "1234567890")

    def test_id_not_visible(self):
        token = kit_token.make_token("1234567890", KEY)
        self.assertNotIn("1234567890", token)
        self.assertNotIn(b"1234567890", base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        self.assertTrue(set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"))

    def test_same_id_same_token(self):
        self.assertEqual(kit_token.make_token(42, KEY), kit_token.make_token("42", KEY))

    def test_different_ids_different_tokens(self):
        self.assertNotEqual(kit_token.make_token(1, KEY), kit_token.make_token(2, KEY))

    def test_wrong_secret_rejected(self):
        self.assertIsNone(kit_token.read_token(kit_token.make_token("1", KEY), OTHER_KEY))

    def test_tampered_token_rejected(self):
        token = kit_token.make_token("1234567890", KEY)
        for i in range(len(token)):
            flipped = "A" if token[i] != "A" else "B"
            tampered = token[:i] + flipped + token[i + 1:]
            self.assertIsNone(kit_token.read_token(tampered, KEY), i)

    def test_garbage_rejected_without_error(self):
        for junk in [None, "", "=", "abc", "a.b", "é" * 30, 12345, "A" * 65, "A" * 34, "AAAA====", "A"]:
            self.assertIsNone(kit_token.read_token(junk, KEY), junk)

    def test_bad_id_refused(self):
        for bad in ["", "12a", "-1", "1" * 21, "１２"]:
            with self.assertRaises(ValueError):
                kit_token.make_token(bad, KEY)

    def test_bad_secret_fails_loudly(self):
        for bad in ["short", "", "!!!not base64!!!"]:
            with self.assertRaises(ValueError):
                kit_token.make_token("1", bad)
            with self.assertRaises(ValueError):
                kit_token.read_token("AAAA", bad)


class WebhookTests(unittest.TestCase):
    def setUp(self):
        patchers = [
            mock.patch.object(fn, "settings", return_value=SECRETS),
            mock.patch.object(fn, "lambda_client"),
            mock.patch.object(fn, "notify_failure"),
        ]
        self.settings, self.lambda_client, self.notify = [p.start() for p in patchers]
        for p in patchers:
            self.addCleanup(p.stop)

    def test_good_call_starts_job_and_replies_200(self):
        result = fn.lambda_handler(http_event({"subscriber": {"id": 1234567890}}), CONTEXT)
        self.assertEqual(result["statusCode"], 200)
        call = self.lambda_client.return_value.invoke.call_args.kwargs
        self.assertEqual(call["InvocationType"], "Event")
        self.assertEqual(call["FunctionName"], CONTEXT.invoked_function_arn)
        self.assertEqual(json.loads(call["Payload"]), {"job": "set_token", "subscriber_id": "1234567890"})

    def test_base64_body_accepted(self):
        result = fn.lambda_handler(http_event({"subscriber": {"id": "99"}}, base64_body=True), CONTEXT)
        self.assertEqual(result["statusCode"], 200)
        self.lambda_client.return_value.invoke.assert_called_once()

    def test_kit_batch_format(self):
        body = {"delivery_id": 13417761, "events": [
            {"id": "a", "data": {"subscriber": {"id": 4367493729, "state": "inactive", "fields": {"indiv_token": None}}}},
            {"id": "b", "data": {"subscriber": {"id": "555"}}},
            {"id": "c", "data": {"subscriber": {"id": 4367493729}}},
        ]}
        result = fn.lambda_handler(http_event(body), CONTEXT)
        self.assertEqual(result["statusCode"], 200)
        sent = [json.loads(c.kwargs["Payload"])["subscriber_id"]
                for c in self.lambda_client.return_value.invoke.call_args_list]
        self.assertEqual(sent, ["4367493729", "555"])  # duplicate skipped
        self.notify.assert_not_called()

    def test_kit_batch_with_one_bad_entry(self):
        body = {"events": [{"data": {"subscriber": {"id": 1}}}, {"data": {}}, "junk"]}
        result = fn.lambda_handler(http_event(body), CONTEXT)
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(self.lambda_client.return_value.invoke.call_count, 1)
        self.notify.assert_called_once()

    def test_empty_batch_reported(self):
        result = fn.lambda_handler(http_event({"events": []}), CONTEXT)
        self.assertEqual(result["statusCode"], 200)
        self.lambda_client.return_value.invoke.assert_not_called()

    def test_wrong_key_forbidden(self):
        for path in ["/wrong", "/", "", "/hook-key-1234", "/ünïcode"]:
            result = fn.lambda_handler(http_event({"subscriber": {"id": 1}}, path=path), CONTEXT)
            self.assertEqual(result["statusCode"], 403, path)
        self.lambda_client.return_value.invoke.assert_not_called()

    def test_get_refused(self):
        result = fn.lambda_handler(http_event({}, method="GET"), CONTEXT)
        self.assertEqual(result["statusCode"], 405)

    def test_bad_payload_reported_but_200(self):
        for body in ["not json", {}, {"subscriber": {}}, {"subscriber": {"id": "abc"}}, {"subscriber": None}, "[1, 2]"]:
            result = fn.lambda_handler(http_event(body), CONTEXT)
            self.assertEqual(result["statusCode"], 200, body)
        self.lambda_client.return_value.invoke.assert_not_called()
        self.assertEqual(self.notify.call_count, 6)

    def test_hand_off_failure_asks_kit_to_retry(self):
        self.lambda_client.return_value.invoke.side_effect = Exception("boom")
        result = fn.lambda_handler(http_event({"subscriber": {"id": 1}}), CONTEXT)
        self.assertEqual(result["statusCode"], 503)
        self.assertEqual(result["headers"]["Retry-After"], "60")
        self.notify.assert_called_once()

    def test_settings_failure_asks_kit_to_retry(self):
        self.settings.side_effect = Exception("no ssm")
        result = fn.lambda_handler(http_event({"subscriber": {"id": 1}}), CONTEXT)
        self.assertEqual(result["statusCode"], 503)


class JobTests(unittest.TestCase):
    def setUp(self):
        patchers = [
            mock.patch.object(fn, "settings", return_value=SECRETS),
            mock.patch.object(fn, "kit_request"),
            mock.patch.object(fn, "notify_failure"),
        ]
        self.settings, self.kit, self.notify = [p.start() for p in patchers]
        for p in patchers:
            self.addCleanup(p.stop)

    def run_job(self, subscriber_id="1234567890"):
        return fn.lambda_handler({"job": "set_token", "subscriber_id": subscriber_id}, CONTEXT)

    @staticmethod
    def kit_echo(method, endpoint, body, key):
        """Fake Kit that saves what it's sent and replies with the updated record."""
        return 200, json.dumps({"subscriber": {"id": 1, "fields": {"indiv_token": body["fields"]["indiv_token"]}}})

    def test_success_saves_correct_token(self):
        self.kit.side_effect = self.kit_echo
        self.run_job()
        method, endpoint, body, key = self.kit.call_args.args
        self.assertEqual((method, endpoint, key), ("PUT", "subscribers/1234567890", "kit-key"))
        token = body["fields"]["indiv_token"]
        self.assertEqual(kit_token.read_token(token, KEY), "1234567890")
        self.notify.assert_not_called()

    def test_kit_ok_but_token_not_saved_is_reported(self):
        self.kit.return_value = (200, json.dumps({"subscriber": {"fields": {"indiv_token": "old-token"}}}))
        self.run_job()  # must not raise
        self.notify.assert_called_once()
        self.assertIn("old-token", self.notify.call_args.args[0])

    def test_kit_ok_but_unreadable_reply_is_reported(self):
        self.kit.return_value = (200, "")
        self.run_job()
        self.notify.assert_called_once()

    def test_rate_limit_and_server_errors_retry(self):
        for status in [429, 500, 502, 503]:
            self.kit.return_value = (status, "busy")
            with self.assertRaises(fn.RetryableError):
                self.run_job()

    def test_client_error_reported_not_retried(self):
        self.kit.return_value = (404, "not found")
        self.run_job()  # must not raise
        self.notify.assert_called_once()

    def test_invalid_id_reported_not_retried(self):
        self.run_job("abc")
        self.kit.assert_not_called()
        self.notify.assert_called_once()

    def test_settings_failure_retries(self):
        self.settings.side_effect = Exception("no ssm")
        with self.assertRaises(Exception):
            self.run_job()


class KitRequestTests(unittest.TestCase):
    def test_network_error_is_retryable(self):
        import urllib.error
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            with self.assertRaises(fn.RetryableError):
                fn.kit_request("PUT", "subscribers/1", {}, "k")

    def test_timeout_is_retryable(self):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError()):
            with self.assertRaises(fn.RetryableError):
                fn.kit_request("PUT", "subscribers/1", {}, "k")

    def test_http_error_returns_status(self):
        import io
        import urllib.error
        error = urllib.error.HTTPError("u", 422, "Unprocessable", {}, io.BytesIO(b'{"errors":["x"]}'))
        with mock.patch("urllib.request.urlopen", side_effect=error):
            self.assertEqual(fn.kit_request("PUT", "subscribers/1", {}, "k"), (422, '{"errors":["x"]}'))


class SettingsTests(unittest.TestCase):
    def setUp(self):
        fn._settings = None
        self.addCleanup(setattr, fn, "_settings", None)

    def test_loads_once(self):
        params = [{"Name": "/zki/" + n, "Value": "v-" + n} for n in ["kit-api-key", "token-secret", "webhook-key"]]
        with mock.patch.object(fn, "ssm_client") as ssm:
            ssm.return_value.get_parameters.return_value = {"Parameters": params}
            first = fn.settings()
            fn.settings()
        self.assertEqual(first["token_secret"], "v-token-secret")
        ssm.return_value.get_parameters.assert_called_once()

    def test_missing_parameter_fails_loudly(self):
        with mock.patch.object(fn, "ssm_client") as ssm:
            ssm.return_value.get_parameters.return_value = {"Parameters": [{"Name": "/zki/kit-api-key", "Value": "x"}]}
            with self.assertRaises(RuntimeError):
                fn.settings()


if __name__ == "__main__":
    unittest.main()
