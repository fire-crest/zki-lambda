"""
evergreen-sms-webinar-watch

Records how far each viewer gets through the evergreen SMS webinar.
New for the evergreen funnel (it does not replace the WordPress plugin's /kit/ and /kittag/ endpoints).

The watch page calls this function's Function URL at each milestone:

    POST /milestone
    {"token": "<indiv_token>", "milestone": "started"}
    {"token": "<indiv_token>", "milestone": "progress", "minute": 20}
    {"token": "<indiv_token>", "milestone": "reached_pitch"}

What each milestone does is fixed here (milestone_actions below), using the tag
IDs and field name from Parameter Store. The page can't choose tags or fields,
so it can never apply sms-member or anything else.

  started        log it, then tag <started-tag-id>
  progress       log it (with the minute) - never calls Kit
  reached_pitch  log it, then tag <watched-tag-id> and add 1 to <watch-count-field>,
                 once per registration (see below)

The watch count means "how many registrations they watched to the pitch".
Every reached_pitch is logged, re-watches included, but the count only goes
up once per registration: the job finds the subscriber's latest 'registered'
row and marks it pitch_counted_at. DynamoDB accepts only the first mark, so a
re-watch, a second tab or a retried job can't count twice. If Kit then fails
in a way worth retrying, the mark is removed so the retry can count it.
Someone with no registration on record is logged and tagged but not counted,
and admin is emailed.

How it works - the function runs in two modes:

1. PAGE MODE (the watch page calls the Function URL)
   Checks the token and where the call came from, writes the event to the
   DynamoDB event log, and replies straight away. Any Kit work is handed to a
   background run of this same function, so a slow or busy Kit never delays
   the page and can't lose a milestone.

2. JOB MODE (the background run)
   Applies the milestone's tags and, once per registration, increments the watch count in Kit. If Kit is
   busy (429) or broken (5xx), or the network fails, the job raises an error and
   Lambda retries it automatically. If every retry fails, Lambda sends the failed
   job to the "on failure" SNS destination, which emails admin.

CORS is set on the Function URL itself, for the same website as allowed-origin.
This function also refuses any browser call whose Origin header is another site.

Nothing specific to one business is in this code. All of it is in Parameter Store,
and the function refuses to run if any of these is missing:

  Shared, SecureString, under PARAM_PREFIX (default /zki/):
    kit-api-key                 Kit v4 API key
    token-secret                secret used to make and read indiv_tokens

  This function's own, String, under PARAM_PREFIX + "evergreen-watch/":
    allowed-origin              the watch page's website, e.g. https://www.example.com
    started-tag-id              Kit tag ID for "started watching"
    watched-tag-id              Kit tag ID for "reached the pitch"
    watch-count-field           Kit custom field key for the lifetime watch count

Lambda environment variables (all optional except FAILURE_TOPIC_ARN):
  FAILURE_TOPIC_ARN     SNS topic for failure emails
  PARAM_PREFIX          default /zki/
  EVENTS_TABLE          DynamoDB event log table, default evergreen_events
  MAX_PROGRESS_MINUTE   default 240 - progress minutes above this are refused

Contact jo@redkite.biz for more information!
"""

import base64
import json
import logging
import os
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from kit_token import read_token

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PARAM_PREFIX = os.environ.get("PARAM_PREFIX", "/zki/")
FAILURE_TOPIC_ARN = os.environ.get("FAILURE_TOPIC_ARN")
EVENTS_TABLE = os.environ.get("EVENTS_TABLE", "evergreen_events")
MAX_PROGRESS_MINUTE = int(os.environ.get("MAX_PROGRESS_MINUTE", "240"))
PROGRESS_STEP = 10

KIT_API_BASE = "https://api.kit.com/v4/"
KIT_TIMEOUT_SECONDS = 10
JOB_NAME = "kit_actions"
FAIL_EMAIL_SUBJECT = "Evergreen watch: failure"
MAX_BODY_BYTES = 2000

MILESTONES = ("started", "progress", "reached_pitch")

_clients = {}
_settings = None


class RetryableError(Exception):
    """Raised in job mode when trying again later might work. Lambda will retry."""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    if "requestContext" in event:
        return handle_page_call(event, context)

    if event.get("job") == JOB_NAME:
        run_kit_actions(event.get("subscriber_id"), event.get("milestone"))
        return {"ok": True}

    logger.error("Unrecognised event: %s", json.dumps(event)[:1000])
    return {"ok": False}


# ---------------------------------------------------------------------------
# Page mode
# ---------------------------------------------------------------------------

def handle_page_call(event, context):
    http = event["requestContext"].get("http", {})
    if http.get("method") != "POST":
        return respond(405, "POST only")
    if event.get("rawPath", "").rstrip("/") != "/milestone":
        return respond(404, "Not found")

    try:
        cfg = settings()
    except Exception as error:
        notify_failure(f"Could not load settings from Parameter Store: {error}")
        return respond(503, "Service temporarily unavailable")

    # Browsers always send Origin on a cross-site POST. Refuse other websites.
    origin = (event.get("headers") or {}).get("origin")
    if origin and origin.rstrip("/") != cfg["allowed_origin"].rstrip("/"):
        logger.warning("Refused call from origin %s", origin)
        return respond(403, "Forbidden")

    request = read_body(event)
    if request is None:
        return respond(400, "Bad request")

    milestone = request.get("milestone")
    if milestone not in MILESTONES:
        return respond(400, "Unknown milestone")

    minute = None
    if milestone == "progress":
        minute = request.get("minute")
        if (not isinstance(minute, int) or isinstance(minute, bool) or minute <= 0
                or minute % PROGRESS_STEP or minute > MAX_PROGRESS_MINUTE):
            return respond(400, "Bad minute")

    subscriber_id = read_token(request.get("token"), cfg["token_secret"])
    if subscriber_id is None:
        logger.warning("Rejected invalid token for milestone %s", milestone)
        return respond(403, "Forbidden")

    try:
        log_event(subscriber_id, milestone, minute)
    except Exception as error:
        notify_failure(f"Could not write {milestone} event for subscriber {subscriber_id} "
                       f"to {EVENTS_TABLE}: {error}")
        return respond(503, "Service temporarily unavailable")

    actions = milestone_actions(milestone, cfg)
    if actions["tags"] or actions["increment"]:
        try:
            lambda_client().invoke(
                FunctionName=context.invoked_function_arn,
                InvocationType="Event",
                Payload=json.dumps({"job": JOB_NAME, "subscriber_id": subscriber_id,
                                    "milestone": milestone}).encode("utf-8"),
            )
        except Exception as error:
            # The event is already logged; only the Kit update is missing
            notify_failure(f"Logged {milestone} for subscriber {subscriber_id} but could not start "
                           f"the Kit update: {error}")
            return respond(503, "Service temporarily unavailable")

    logger.info("Recorded %s%s for subscriber %s", milestone,
                f" minute {minute}" if minute else "", subscriber_id)
    return respond(200, "OK")


def read_body(event):
    """Return the request body as a dict, or None if it isn't a small JSON object."""
    body = event.get("body") or ""
    try:
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8")
        if len(body) > MAX_BODY_BYTES:
            return None
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def log_event(subscriber_id, event_type, minute=None):
    """Write one row to the event log."""
    now = datetime.now(timezone.utc)
    item = {
        "subscriber_id": subscriber_id,
        # Timestamp first so rows sort by time; the random suffix keeps two
        # events in the same millisecond from overwriting each other
        "event_at": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z#{secrets.token_hex(3)}",
        "event": event_type,
        "source": "watch_page",
    }
    if minute is not None:
        item["minute"] = minute
    events_table().put_item(Item=item)


def respond(status_code, message):
    # No CORS headers here: the Function URL adds them. Adding them twice breaks browsers.
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"response": message}),
    }


# ---------------------------------------------------------------------------
# Job mode
# ---------------------------------------------------------------------------

def milestone_actions(milestone, cfg):
    """What each milestone is allowed to do. This is the allow-list: nothing else can be tagged or incremented."""
    return {
        "started": {"tags": [cfg["started_tag_id"]], "increment": None},
        "progress": {"tags": [], "increment": None},
        "reached_pitch": {"tags": [cfg["watched_tag_id"]], "increment": cfg["watch_count_field"]},
    }[milestone]


def run_kit_actions(subscriber_id, milestone):
    if milestone not in MILESTONES or not str(subscriber_id or "").isdigit():
        notify_failure(f"Background job received bad input: subscriber {subscriber_id!r}, milestone {milestone!r}")
        return

    cfg = settings()  # an exception here makes Lambda retry
    actions = milestone_actions(milestone, cfg)

    for tag_id in actions["tags"]:
        tag_subscriber(subscriber_id, tag_id, cfg["kit_api_key"])

    # Count last, so a retry caused by a tag failure can't count twice
    if actions["increment"]:
        count_once_per_registration(subscriber_id, actions["increment"], cfg["kit_api_key"])


def count_once_per_registration(subscriber_id, field, api_key):
    """Add 1 to the watch count, unless this subscriber's latest registration has already been counted."""
    registration = latest_registration(subscriber_id)  # a DynamoDB error here makes Lambda retry
    if registration is None:
        notify_failure(f"Subscriber {subscriber_id} reached the pitch with no registration on record. "
                       f"Logged and tagged, but {field} was not increased.")
        return
    if not mark_pitch_counted(subscriber_id, registration):
        logger.info("Registration %s for subscriber %s already counted for %s", registration, subscriber_id, field)
        return
    try:
        increment_field(subscriber_id, field, api_key)
    except RetryableError:
        unmark_pitch_counted(subscriber_id, registration)  # let the retry count it
        raise


def latest_registration(subscriber_id):
    """Return the event_at of the subscriber's most recent 'registered' row, or None if there isn't one."""
    query = {
        "KeyConditionExpression": Key("subscriber_id").eq(subscriber_id),
        "FilterExpression": Attr("event").eq("registered"),
        "ProjectionExpression": "event_at",
        "ScanIndexForward": False,  # newest first
    }
    while True:
        page = events_table().query(**query)
        if page.get("Items"):
            return page["Items"][0]["event_at"]
        if "LastEvaluatedKey" not in page:
            return None
        query["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def mark_pitch_counted(subscriber_id, registration_event_at):
    """Mark the registration as counted. True if this call made the mark, False if it was already there."""
    try:
        events_table().update_item(
            Key={"subscriber_id": subscriber_id, "event_at": registration_event_at},
            UpdateExpression="SET pitch_counted_at = :now",
            ConditionExpression="attribute_exists(event_at) AND attribute_not_exists(pitch_counted_at)",
            ExpressionAttributeValues={":now": datetime.now(timezone.utc).isoformat()},
        )
        return True
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def unmark_pitch_counted(subscriber_id, registration_event_at):
    try:
        events_table().update_item(
            Key={"subscriber_id": subscriber_id, "event_at": registration_event_at},
            UpdateExpression="REMOVE pitch_counted_at",
        )
    except Exception as error:
        notify_failure(f"Could not unmark registration {registration_event_at} for subscriber {subscriber_id} "
                       f"after a Kit failure; their watch count may be 1 short: {error}")


def tag_subscriber(subscriber_id, tag_id, api_key):
    status, body = kit_request("POST", f"tags/{tag_id}/subscribers/{subscriber_id}", {}, api_key)
    if check_kit_status(status, body, f"tagging subscriber {subscriber_id} with {tag_id}"):
        logger.info("Tagged subscriber %s with %s", subscriber_id, tag_id)


def increment_field(subscriber_id, field, api_key):
    status, body = kit_request("GET", f"subscribers/{subscriber_id}", None, api_key)
    if not check_kit_status(status, body, f"reading subscriber {subscriber_id}"):
        return
    try:
        current = json.loads(body)["subscriber"]["fields"].get(field)
    except (ValueError, KeyError, TypeError, AttributeError):
        notify_failure(f"Unexpected reply reading subscriber {subscriber_id}: {body[:300]}")
        return

    new_value = str(as_count(current) + 1)
    status, body = kit_request("PUT", f"subscribers/{subscriber_id}", {"fields": {field: new_value}}, api_key)
    if not check_kit_status(status, body, f"setting {field} for subscriber {subscriber_id}"):
        return
    try:
        saved = json.loads(body)["subscriber"]["fields"].get(field)
    except (ValueError, KeyError, TypeError, AttributeError):
        saved = None
    if str(saved) != new_value:
        notify_failure(f"Kit accepted {field} = {new_value} for subscriber {subscriber_id} "
                       f"but its record shows {saved!r}")
        return
    logger.info("Set %s to %s for subscriber %s", field, new_value, subscriber_id)


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
    """Load secrets from Parameter Store once per container, then reuse them."""
    global _settings
    if _settings is None:
        own = PARAM_PREFIX + "evergreen-watch/"
        names = {
            "kit_api_key": PARAM_PREFIX + "kit-api-key",
            "token_secret": PARAM_PREFIX + "token-secret",
            "allowed_origin": own + "allowed-origin",
            "started_tag_id": own + "started-tag-id",
            "watched_tag_id": own + "watched-tag-id",
            "watch_count_field": own + "watch-count-field",
        }
        response = ssm_client().get_parameters(Names=list(names.values()), WithDecryption=True)
        found = {p["Name"]: p["Value"] for p in response["Parameters"]}
        missing = [name for name in names.values() if not found.get(name)]
        if missing:
            raise RuntimeError(f"Missing or empty parameters: {', '.join(missing)}")
        loaded = {key: found[name].strip() for key, name in names.items()}
        for key in ("started_tag_id", "watched_tag_id"):
            if not loaded[key].isdigit():
                raise RuntimeError(f"{names[key]} must be a Kit tag ID (digits only), got {loaded[key]!r}")
        if not loaded["allowed_origin"].startswith("https://"):
            raise RuntimeError(f"{names['allowed_origin']} must start with https://, got {loaded['allowed_origin']!r}")
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
