"""
evergreen-sms-webinar-registration

Called by a Kit webhook every time someone submits the evergreen webinar
registration form. New for the evergreen funnel.

For each registration it:
  1. writes a 'registered' row to the DynamoDB event log, and replies to Kit straight away
  2. then, in a background run:
     a. makes sure the subscriber has a valid indiv_token (creates and saves it
        only if it's missing or invalid - an existing token is never changed)
     b. adds 1 to the registration count field, once per registration
     c. adds the "token ready" tag, which releases the confirmation email -
        last, so the token is always saved before the email can be sent

Counting once per registration: before increasing the count, the job marks
that registration's row in the event log as counted. DynamoDB only accepts
the first mark, so a retried job can never count the same registration twice.
If Kit then fails in a way worth retrying, the mark is removed again so the
retry can count it.

How it works - the function runs in two modes, like kit-set-indiv-token:

1. WEBHOOK MODE (Kit calls the Function URL)
   Checks the secret key in the URL path, reads the subscriber ID(s), logs each
   registration, hands the Kit work to a background run, and replies 200.

2. JOB MODE (the background run)
   Does steps a-c. If Kit is busy (429) or broken (5xx), or the network fails,
   the job raises an error and Lambda retries it automatically. If every retry
   fails, Lambda sends the failed job to the "on failure" SNS destination,
   which emails admin. Problems a retry can't fix are emailed via SNS directly.

Kit sends two payload formats, and both are accepted:
  webhooks created through the API:  {"subscriber": {"id": 123, ...}}
  automation rule webhooks:          {"events": [{"data": {"subscriber": {"id": 123, ...}}}, ...]}

Nothing specific to one business is in this code. All of it is in Parameter
Store, and the function refuses to run if any of these is missing:

  Shared, SecureString, under PARAM_PREFIX (default /zki/):
    kit-api-key                   Kit v4 API key
    token-secret                  secret used to make and read indiv_tokens

  This function's own, under PARAM_PREFIX + "evergreen-registration/":
    webhook-key                   SecureString: secret that must appear in the webhook URL path
    token-field                   String: Kit custom field key holding the token
    registration-count-field      String: Kit custom field key for the registration count
    token-ready-tag-id            String: Kit tag ID added once the token exists

Lambda environment variables (all optional except FAILURE_TOPIC_ARN):
  FAILURE_TOPIC_ARN     SNS topic for failure emails
  PARAM_PREFIX          default /zki/
  EVENTS_TABLE          DynamoDB event log table, default evergreen_events

Contact jo@redkite.biz for more information!
"""

import base64
import hmac
import json
import logging
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from kit_token import is_valid_subscriber_id, make_token, read_token

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PARAM_PREFIX = os.environ.get("PARAM_PREFIX", "/zki/")
FAILURE_TOPIC_ARN = os.environ.get("FAILURE_TOPIC_ARN")
EVENTS_TABLE = os.environ.get("EVENTS_TABLE", "evergreen_events")

KIT_API_BASE = "https://api.kit.com/v4/"
KIT_TIMEOUT_SECONDS = 10
JOB_NAME = "registration"
FAIL_EMAIL_SUBJECT = "Evergreen registration: failure"
MAX_SUBSCRIBERS_PER_DELIVERY = 100

_clients = {}
_settings = None


class RetryableError(Exception):
    """Raised in job mode when trying again later might work. Lambda will retry."""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    if "requestContext" in event:
        return handle_webhook(event, context)

    if event.get("job") == JOB_NAME:
        process_registration(event.get("subscriber_id"), event.get("event_at"))
        return {"ok": True}

    logger.error("Unrecognised event: %s", json.dumps(event)[:1000])
    return {"ok": False}


# ---------------------------------------------------------------------------
# Webhook mode
# ---------------------------------------------------------------------------

def handle_webhook(event, context):
    if event["requestContext"].get("http", {}).get("method") != "POST":
        return respond(405, "POST only")

    try:
        cfg = settings()
    except Exception as error:
        notify_failure(f"Could not load settings from Parameter Store: {error}")
        return respond(503, "Service temporarily unavailable, please retry later", retry_after=60)

    path_key = urllib.parse.unquote(event.get("rawPath", "")).strip().strip("/").strip()
    if not hmac.compare_digest(path_key.encode("utf-8"), cfg["webhook_key"].encode("utf-8")):
        logger.warning("Rejected webhook call with wrong key from %s (key length %d, expected %d)",
                       event["requestContext"].get("http", {}).get("sourceIp"),
                       len(path_key), len(cfg["webhook_key"]))
        return respond(403, "Forbidden")

    subscriber_ids, problems = subscriber_ids_from_body(event)
    if problems:
        notify_failure(f"Registration webhook had {problems} missing or invalid subscriber ID(s): "
                       f"{(event.get('body') or '')[:500]}")
    if not subscriber_ids:
        # 200 anyway: resending the same bad payload can't help, and too many
        # non-2xx replies make Kit disable the webhook
        return respond(200, "No valid subscriber ID")

    for subscriber_id in subscriber_ids:
        try:
            event_at = log_registration(subscriber_id)
        except Exception as error:
            notify_failure(f"Could not log registration for subscriber {subscriber_id}: {error}")
            return respond(503, "Service temporarily unavailable, please retry later", retry_after=60)
        try:
            lambda_client().invoke(
                FunctionName=context.invoked_function_arn,
                InvocationType="Event",
                Payload=json.dumps({"job": JOB_NAME, "subscriber_id": subscriber_id,
                                    "event_at": event_at}).encode("utf-8"),
            )
        except Exception as error:
            # The registration is logged; only the Kit work is missing
            notify_failure(f"Logged registration for subscriber {subscriber_id} but could not start "
                           f"the background job: {error}. Their token, count and token-ready tag "
                           f"need checking.")
            return respond(503, "Service temporarily unavailable, please retry later", retry_after=60)
        logger.info("Registered subscriber %s at %s", subscriber_id, event_at)

    return respond(200, "OK")


def subscriber_ids_from_body(event):
    """Return (list of valid subscriber IDs, number of entries with a missing or invalid ID)."""
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
    for subscriber in subscribers[:MAX_SUBSCRIBERS_PER_DELIVERY]:
        sid = str(subscriber.get("id", "")) if isinstance(subscriber, dict) else ""
        if is_valid_subscriber_id(sid):
            if sid not in ids:
                ids.append(sid)
        else:
            problems += 1
    problems += max(0, len(subscribers) - MAX_SUBSCRIBERS_PER_DELIVERY)
    return ids, problems


def _dig(value, *keys):
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def log_registration(subscriber_id):
    """Write a 'registered' row to the event log. Returns its event_at (the row's sort key)."""
    now = datetime.now(timezone.utc)
    event_at = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z#{secrets.token_hex(3)}"
    events_table().put_item(Item={
        "subscriber_id": subscriber_id,
        "event_at": event_at,
        "event": "registered",
        "source": "kit_form",
    })
    return event_at


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

def process_registration(subscriber_id, event_at):
    if not is_valid_subscriber_id(subscriber_id or "") or not isinstance(event_at, str) or not event_at:
        notify_failure(f"Background job received bad input: subscriber {subscriber_id!r}, event_at {event_at!r}")
        return

    cfg = settings()  # an exception here makes Lambda retry
    key = cfg["kit_api_key"]

    fields = get_subscriber_fields(subscriber_id, key)
    if fields is None:
        return  # already reported

    # a. Token: create only if missing or invalid
    if read_token(fields.get(cfg["token_field"]), cfg["token_secret"]) != subscriber_id:
        token = make_token(subscriber_id, cfg["token_secret"])
        if not save_field(subscriber_id, cfg["token_field"], token, key):
            return  # reported; don't release the confirmation email without a token
        logger.info("Saved %s for subscriber %s", cfg["token_field"], subscriber_id)

    # b. Registration count, once per registration
    count_registration(subscriber_id, event_at, fields.get(cfg["registration_count_field"]), cfg)

    # c. Token ready - last, because it releases the confirmation email
    status, body = kit_request("POST", f"tags/{cfg['token_ready_tag_id']}/subscribers/{subscriber_id}", {}, key)
    if check_kit_status(status, body, f"adding the token-ready tag to subscriber {subscriber_id}"):
        logger.info("Added token-ready tag to subscriber %s", subscriber_id)


def count_registration(subscriber_id, event_at, current_value, cfg):
    """Add 1 to the registration count, unless this registration has already been counted."""
    if not mark_counted(subscriber_id, event_at):
        logger.info("Registration %s for subscriber %s already counted", event_at, subscriber_id)
        return

    new_value = str(as_count(current_value) + 1)
    try:
        saved = save_field(subscriber_id, cfg["registration_count_field"], new_value, cfg["kit_api_key"])
    except RetryableError:
        unmark_counted(subscriber_id, event_at)  # let the retry count it
        raise
    if saved:
        logger.info("Set %s to %s for subscriber %s", cfg["registration_count_field"], new_value, subscriber_id)


def mark_counted(subscriber_id, event_at):
    """Mark the registration row as counted. True if this call made the mark, False if it was already there."""
    try:
        events_table().update_item(
            Key={"subscriber_id": subscriber_id, "event_at": event_at},
            UpdateExpression="SET counted_at = :now",
            ConditionExpression="attribute_exists(event_at) AND attribute_not_exists(counted_at)",
            ExpressionAttributeValues={":now": datetime.now(timezone.utc).isoformat()},
        )
        return True
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def unmark_counted(subscriber_id, event_at):
    try:
        events_table().update_item(
            Key={"subscriber_id": subscriber_id, "event_at": event_at},
            UpdateExpression="REMOVE counted_at",
        )
    except Exception as error:
        notify_failure(f"Could not unmark registration {event_at} for subscriber {subscriber_id} after a "
                       f"Kit failure; their registration count may be 1 short: {error}")


def get_subscriber_fields(subscriber_id, api_key):
    status, body = kit_request("GET", f"subscribers/{subscriber_id}", None, api_key)
    if not check_kit_status(status, body, f"reading subscriber {subscriber_id}"):
        return None
    try:
        return json.loads(body)["subscriber"]["fields"] or {}
    except (ValueError, KeyError, TypeError):
        notify_failure(f"Unexpected reply reading subscriber {subscriber_id}: {body[:300]}")
        return None


def save_field(subscriber_id, field, value, api_key):
    """Save one custom field and confirm Kit stored it. True on success; problems are reported."""
    status, body = kit_request("PUT", f"subscribers/{subscriber_id}", {"fields": {field: value}}, api_key)
    if not check_kit_status(status, body, f"saving {field} for subscriber {subscriber_id}"):
        return False
    try:
        saved = json.loads(body)["subscriber"]["fields"].get(field)
    except (ValueError, KeyError, TypeError, AttributeError):
        saved = None
    if str(saved) != value:
        notify_failure(f"Kit accepted {field} for subscriber {subscriber_id} but its record shows {saved!r}")
        return False
    return True


def as_count(value):
    """Blank or non-numeric counts as 0."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return max(number, 0)


def check_kit_status(status, body, what):
    """True on success. Raises RetryableError if a retry might help; emails admin otherwise."""
    if 200 <= status < 300:
        return True
    if status == 429 or status >= 500:
        raise RetryableError(f"Kit returned {status} {what}: {body[:300]}")
    notify_failure(f"Kit returned {status} {what}: {body[:1000]}")
    return False


def kit_request(method, endpoint, body, api_key):
    """Call the Kit v4 API. Returns (status_code, response_text). Raises RetryableError on network failure."""
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
        return error.code, error.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise RetryableError(f"Network error calling Kit {endpoint}: {error}") from error


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def settings():
    """Load settings from Parameter Store once per container, then reuse them."""
    global _settings
    if _settings is None:
        own = PARAM_PREFIX + "evergreen-registration/"
        names = {
            "kit_api_key": PARAM_PREFIX + "kit-api-key",
            "token_secret": PARAM_PREFIX + "token-secret",
            "webhook_key": own + "webhook-key",
            "token_field": own + "token-field",
            "registration_count_field": own + "registration-count-field",
            "token_ready_tag_id": own + "token-ready-tag-id",
        }
        response = ssm_client().get_parameters(Names=list(names.values()), WithDecryption=True)
        found = {p["Name"]: p["Value"] for p in response["Parameters"]}
        missing = [name for name in names.values() if not found.get(name, "").strip()]
        if missing:
            raise RuntimeError(f"Missing or empty parameters: {', '.join(missing)}")
        loaded = {key: found[name].strip() for key, name in names.items()}
        if not loaded["token_ready_tag_id"].isdigit():
            raise RuntimeError(f"{names['token_ready_tag_id']} must be a Kit tag ID (digits only), "
                               f"got {loaded['token_ready_tag_id']!r}")
        _settings = loaded
    return _settings


def _client(name):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def ssm_client():
    return _client("ssm")


def lambda_client():
    return _client("lambda")


def sns_client():
    return _client("sns")


def events_table():
    if "table" not in _clients:
        _clients["table"] = boto3.resource("dynamodb").Table(EVENTS_TABLE)
    return _clients["table"]


def notify_failure(message):
    """Log the problem and email it to admin via SNS. Never raises."""
    logger.error(message)
    if not FAILURE_TOPIC_ARN:
        logger.error("FAILURE_TOPIC_ARN not set, so no email was sent")
        return
    try:
        sns_client().publish(TopicArn=FAILURE_TOPIC_ARN, Subject=FAIL_EMAIL_SUBJECT, Message=message[:5000])
    except Exception as error:
        logger.error("Failed to publish SNS notification: %s", error)
