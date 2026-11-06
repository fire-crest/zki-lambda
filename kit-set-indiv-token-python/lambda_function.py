"""
kit-set-indiv-token

Replaces kit_create_subscriber_token() from the zoom_kit_integration WordPress plugin.

Called by a webhook in a Kit automation rule. Writes a signed token for the
subscriber into the Kit custom field 'indiv_token' (see kit_token.py).

How it works - the function runs in two modes:

1. WEBHOOK MODE (Kit calls the Function URL)
   Checks the secret key in the URL path, reads the subscriber ID, hands the
   real work to a second, background run of this same function, and replies
   200 to Kit straight away. Kit never waits on the Kit API, so it can't time out.

2. JOB MODE (the background run)
   Makes the token and saves it in Kit. If Kit's API is busy (429) or broken
   (5xx), or the network fails, the job raises an error and Lambda retries it
   automatically. If every retry fails, Lambda sends the failed job to the SNS
   topic configured as the function's "on failure" destination, which emails admin.
   Problems that a retry can't fix (e.g. subscriber not found) are emailed via SNS directly.

The function code is just this file and kit_token.py, so it stays editable in
the Lambda console. The one third-party library, 'cryptography' (used by
kit_token.py), lives in a separate Lambda layer. boto3 is built into Lambda.

Settings (Lambda environment variables):
  PARAM_PREFIX       Parameter Store path holding the settings, default /zki/
  FAILURE_TOPIC_ARN  SNS topic for failure emails
  KIT_TOKEN_FIELD    Kit custom field key, default indiv_token

Parameter Store (SecureString) under PARAM_PREFIX:
  kit-api-key        Kit v4 API key
  token-secret       secret used to sign tokens
  webhook-key        secret that must appear in the webhook URL path

Contact jo@redkite.biz for more information!
"""

import base64
import hmac
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

import boto3

from kit_token import is_valid_subscriber_id, make_token

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PARAM_PREFIX = os.environ.get("PARAM_PREFIX", "/zki/")
FAILURE_TOPIC_ARN = os.environ.get("FAILURE_TOPIC_ARN")
KIT_TOKEN_FIELD = os.environ.get("KIT_TOKEN_FIELD", "indiv_token")

KIT_API_BASE = "https://api.kit.com/v4/"
KIT_TIMEOUT_SECONDS = 10

JOB_NAME = "set_token"
FAIL_EMAIL_SUBJECT = "Kit token: failure"

# Created once per Lambda container and reused between calls
_clients = {}
_settings = None


class RetryableError(Exception):
    """Raised in job mode when trying again later might work. Lambda will retry."""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    if "requestContext" in event:
        # Came in over HTTP via the Function URL
        return handle_webhook(event, context)

    if event.get("job") == JOB_NAME:
        set_token(event.get("subscriber_id"))
        return {"ok": True}

    logger.error("Unrecognised event: %s", json.dumps(event)[:1000])
    return {"ok": False}


# ---------------------------------------------------------------------------
# Webhook mode
# ---------------------------------------------------------------------------

def handle_webhook(event, context):
    method = event["requestContext"].get("http", {}).get("method", "")
    if method != "POST":
        return respond(405, "POST only")

    try:
        cfg = settings()
    except Exception as error:
        notify_failure(f"Could not load settings from Parameter Store: {error}")
        # 503 tells Kit to try again later
        return respond(503, "Service temporarily unavailable, please retry later", retry_after=60)

    # The secret key is the URL path, e.g. https://abc.lambda-url.eu-west-2.on.aws/<webhook-key>
    # unquote() turns e.g. %20 back into a space, then strip() removes stray spaces/line breaks from copy-paste
    path_key = urllib.parse.unquote(event.get("rawPath", "")).strip().strip("/").strip()
    expected_key = cfg["webhook_key"]
    if not hmac.compare_digest(path_key.encode("utf-8"), expected_key.encode("utf-8")):
        # Log lengths only, never the keys, to help spot copy-paste problems
        logger.warning("Rejected webhook call with wrong key from %s (key length %d, expected %d)",
                       event["requestContext"].get("http", {}).get("sourceIp"),
                       len(path_key), len(expected_key))
        return respond(403, "Forbidden")

    subscriber_ids, problems = subscriber_ids_from_body(event)
    if problems:
        notify_failure(f"Webhook body had {problems} missing or invalid subscriber ID(s): "
                       f"{(event.get('body') or '')[:500]}")
    if not subscriber_ids:
        # Return 200 anyway: retrying the same bad payload can't help, and too many
        # non-2xx replies make Kit disable the rule
        return respond(200, "No valid subscriber ID")

    # One background job per subscriber, so a problem with one never holds up the others
    for subscriber_id in subscriber_ids:
        try:
            lambda_client().invoke(
                FunctionName=context.invoked_function_arn,
                InvocationType="Event",  # background run - don't wait for it
                Payload=json.dumps({"job": JOB_NAME, "subscriber_id": subscriber_id}).encode("utf-8"),
            )
        except Exception as error:
            # Asking Kit to resend the whole delivery is safe: re-saving a token
            # that's already been saved just writes the same value again
            notify_failure(f"Could not start background job for subscriber {subscriber_id}: {error}")
            return respond(503, "Service temporarily unavailable, please retry later", retry_after=60)
        logger.info("Accepted subscriber %s", subscriber_id)

    return respond(200, "OK")


def subscriber_ids_from_body(event):
    """
    Read subscriber IDs from Kit's webhook payload.
    Returns (list of valid IDs, number of entries with a missing or invalid ID).

    Kit's rule webhooks send a batch of events:
        {"delivery_id": 123, "events": [{"id": "...", "data": {"subscriber": {"id": 4367493729, ...}}}, ...]}
    The older single-subscriber format is also accepted (handy for testing in Postman):
        {"subscriber": {"id": 4367493729}}
    """
    body = event.get("body") or ""
    try:
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8")
        payload = json.loads(body)
    except (ValueError, TypeError):
        return [], 1
    if not isinstance(payload, dict):
        return [], 1

    if isinstance(payload.get("events"), list):
        subscribers = [_dig(item, "data", "subscriber") for item in payload["events"]]
    else:
        subscribers = [payload.get("subscriber")]

    ids, problems = [], 0
    for subscriber in subscribers:
        sid = str(subscriber.get("id", "")) if isinstance(subscriber, dict) else ""
        if is_valid_subscriber_id(sid):
            if sid not in ids:  # skip duplicates within one delivery
                ids.append(sid)
        else:
            problems += 1
    return ids, problems


def _dig(value, *keys):
    """Safely follow nested dictionary keys; returns None if any step is missing."""
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def respond(status_code, message, retry_after=None):
    response = {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"response": message}),
    }
    if retry_after:
        response["headers"]["Retry-After"] = str(retry_after)
    return response


# ---------------------------------------------------------------------------
# Job mode
# ---------------------------------------------------------------------------

def set_token(subscriber_id):
    if not is_valid_subscriber_id(subscriber_id or ""):
        notify_failure(f"Background job received an invalid subscriber ID: {subscriber_id!r}")
        return

    cfg = settings()  # if Parameter Store fails, the exception makes Lambda retry
    token = make_token(subscriber_id, cfg["token_secret"])

    status, body = kit_request(
        "PUT",
        f"subscribers/{subscriber_id}",
        {"fields": {KIT_TOKEN_FIELD: token}},
        cfg["kit_api_key"],
    )

    if 200 <= status < 300:
        # Kit replies with the updated subscriber record: check the token really is in it
        saved = saved_field_value(body)
        if saved == token:
            logger.info("Saved %s for subscriber %s", KIT_TOKEN_FIELD, subscriber_id)
        else:
            notify_failure(f"Kit replied {status} for subscriber {subscriber_id} but its record shows "
                           f"{KIT_TOKEN_FIELD} = {saved!r}, not the new token")
        return

    if status == 429 or status >= 500:
        # Kit is rate limiting or having problems: let Lambda retry later
        raise RetryableError(f"Kit returned {status} for subscriber {subscriber_id}: {body[:500]}")

    # 4xx: retrying won't help (e.g. subscriber deleted, field doesn't exist)
    notify_failure(f"Kit returned {status} when saving {KIT_TOKEN_FIELD} for subscriber "
                   f"{subscriber_id}: {body[:1000]}")


def saved_field_value(response_text):
    """Return our custom field's value from Kit's subscriber reply, or a description of what went wrong."""
    try:
        return json.loads(response_text)["subscriber"]["fields"].get(KIT_TOKEN_FIELD)
    except (ValueError, KeyError, TypeError, AttributeError):
        return f"<unreadable reply: {response_text[:200]}>"


def kit_request(method, endpoint, body, api_key):
    """
    Call the Kit v4 API. Returns (status_code, response_text).
    Raises RetryableError for network failures and timeouts.
    """
    request = urllib.request.Request(
        KIT_API_BASE + endpoint,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Kit-Api-Key": api_key,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=KIT_TIMEOUT_SECONDS) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        # Kit answered, but with an error status
        return error.code, error.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise RetryableError(f"Network error calling Kit {endpoint}: {error}") from error


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def settings():
    """Load secrets from Parameter Store once per container, then reuse them."""
    global _settings
    if _settings is None:
        names = {
            "kit_api_key": PARAM_PREFIX + "kit-api-key",
            "token_secret": PARAM_PREFIX + "token-secret",
            "webhook_key": PARAM_PREFIX + "webhook-key",
        }
        response = ssm_client().get_parameters(Names=list(names.values()), WithDecryption=True)
        found = {p["Name"]: p["Value"] for p in response["Parameters"]}
        missing = [name for name in names.values() if not found.get(name)]
        if missing:
            raise RuntimeError(f"Missing or empty parameters: {', '.join(missing)}")
        # strip() guards against spaces or line breaks pasted into Parameter Store
        _settings = {key: found[name].strip() for key, name in names.items()}
    return _settings


def ssm_client():
    if "ssm" not in _clients:
        _clients["ssm"] = boto3.client("ssm")
    return _clients["ssm"]


def lambda_client():
    if "lambda" not in _clients:
        _clients["lambda"] = boto3.client("lambda")
    return _clients["lambda"]


def sns_client():
    if "sns" not in _clients:
        _clients["sns"] = boto3.client("sns")
    return _clients["sns"]


def notify_failure(message):
    """Log the problem and email it to admin via SNS. Never raises."""
    logger.error(message)
    if not FAILURE_TOPIC_ARN:
        logger.error("FAILURE_TOPIC_ARN not set, so no email was sent")
        return
    try:
        sns_client().publish(TopicArn=FAILURE_TOPIC_ARN, Subject=FAIL_EMAIL_SUBJECT, Message=message)
    except Exception as error:
        logger.error("Failed to publish SNS notification: %s", error)
