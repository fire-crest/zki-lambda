import json
import logging
import os
import random
import time
from urllib.parse import urlencode

import boto3
import requests

logger = logging.getLogger()
logger.setLevel(logging.INFO)

secrets_client = boto3.client("secretsmanager")
dynamodb = boto3.resource("dynamodb")
sns_client = boto3.client("sns")

FAILURE_TOPIC_ARN = os.environ.get("FAILURE_TOPIC_ARN")

# This function is designed to be called from a webhook in a Kit rule.
# In the case of internal failures (DynamoDB, Secrets Manager, non-rate-limit API errors) always return 200,
# since Kit disables a webhook rule after too many failures (we definitely do not want that to happen)
# and there's nothing Kit can usefully do differently in these cases.
# Failures are reported to admin via SNS email instead.
# The exception is a 429 from Zoom's or Kit's own APIs even after retries are exhausted in fetch_with_retry(),
# in which case return 503 back to Kit as a throttle signal.
# Contact jo@redkite.biz for more information!

SUCCESS_STATUS_CODE = 200
DB_FAIL_EMAIL_SUBJECT = "Zoom-Kit: DynamoDB failure"
SECRETS_FAIL_EMAIL_SUBJECT = "Zoom-Kit: Secrets Manager failure"
ZOOM_FAIL_EMAIL_SUBJECT = "Zoom-Kit: Zoom API failure"
KIT_FAIL_EMAIL_SUBJECT = "Zoom-Kit: Kit API failure"


def lambda_handler(event, context):
    # Extract subscriber info from event
    subscriber_email = event["subscriber"]["email_address"]
    subscriber_names = split_name(event["subscriber"]["first_name"])

    # Zoom will reject empty name fields
    if subscriber_names[0] == "":
        subscriber_names[0] = "-"
    if subscriber_names[1] == "":
        subscriber_names[1] = "-"

    # Fetch the meeting/webinar option and ID from DynamoDB
    table = dynamodb.Table("zoom-kit-parameters")
    try:
        db_response = table.get_item(Key={"settingID": "zoom-kit-parameters"})
    except Exception as error:
        notify_failure(
            DB_FAIL_EMAIL_SUBJECT,
            f"Failed to retrieve parameters from DynamoDB: {error}",
            subscriber_email,
            subscriber_names,
        )
        return {"statusCode": SUCCESS_STATUS_CODE}

    item = db_response.get("Item")
    if not item:
        notify_failure(
            DB_FAIL_EMAIL_SUBJECT,
            "No Item found in DynamoDB for key 'zoom-kit-parameters'",
            subscriber_email,
            subscriber_names,
        )
        return {"statusCode": SUCCESS_STATUS_CODE}

    meeting_or_webinar = item.get("meetingOrWebinar")
    webinar_id = item.get("webinarId")  # This ID number represents either a meeting or a webinar!
 
    # Retrieve the Zoom credentials from Secrets Manager
    try:
        secret_response = secrets_client.get_secret_value(SecretId="zoom-kit-integration/settings")
        secret_data = json.loads(secret_response["SecretString"])
        zoom_client = secret_data.get("zkiZoomClient")
        zoom_account = secret_data.get("zkiZoomAccount")
    except Exception as error:
        notify_failure(
            SECRETS_FAIL_EMAIL_SUBJECT,
            f"Failed to retrieve Zoom credentials from Secrets Manager: {error}",
            subscriber_email,
            subscriber_names,
        )
        return {"statusCode": SUCCESS_STATUS_CODE}

    if not zoom_client or not zoom_account:
        notify_failure(
            SECRETS_FAIL_EMAIL_SUBJECT,
            "Zoom credentials missing from Secrets Manager response",
            subscriber_email,
            subscriber_names,
        )
        return {"statusCode": SUCCESS_STATUS_CODE}

    # Ask Zoom for an OAuth access token, valid for 1 hour
    token_headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {zoom_client}",
    }
    token_body = urlencode({
        "grant_type": "account_credentials",
        "account_id": zoom_account,
    })

    try:
        token_response = fetch_with_retry(
            "https://zoom.us/oauth/token", method="POST", headers=token_headers, data=token_body
        )
    except requests.RequestException as error:
        notify_failure(
            ZOOM_FAIL_EMAIL_SUBJECT,
            f"Network error calling Zoom OAuth endpoint: {error}",
            subscriber_email,
            subscriber_names,
        )
        return {"statusCode": SUCCESS_STATUS_CODE}

    if not token_response.ok:
        notify_failure(
            ZOOM_FAIL_EMAIL_SUBJECT,
            f"Zoom OAuth token request failed with status {token_response.status_code} {token_response.reason}",
            subscriber_email,
            subscriber_names,
            token_response,
        )
        return throttle_aware_response(token_response, "zoom")

    token_data = token_response.json()
    access_token = token_data.get("access_token")
    if not access_token:
        notify_failure(
            ZOOM_FAIL_EMAIL_SUBJECT,
            f"Zoom OAuth response did not contain an access token: {token_data}",
            subscriber_email,
            subscriber_names,
        )
        return {"statusCode": SUCCESS_STATUS_CODE}

    # Register event subscriber with Zoom meeting/webinar
    registrant_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }
    registrant_body = json.dumps({
        "email": subscriber_email,
        "first_name": subscriber_names[0],
        "last_name": subscriber_names[1],
    })

    try:
        # Zoom API URL contains "/meetings/" if registering for a meeting, or "/webinars/" if registering for a webinar
        meetings_response = fetch_with_retry(
            f"https://api.zoom.us/v2/{meeting_or_webinar}s/{webinar_id}/registrants",
            method="POST",
            headers=registrant_headers,
            data=registrant_body,
        )
    except requests.RequestException as error:
        notify_failure(
            ZOOM_FAIL_EMAIL_SUBJECT,
            f"Network error calling Zoom registrants endpoint: {error}",
            subscriber_email,
            subscriber_names,
        )
        return {"statusCode": SUCCESS_STATUS_CODE}

    if not meetings_response.ok:
        notify_failure(
            ZOOM_FAIL_EMAIL_SUBJECT,
            f"Zoom add registrant to {meeting_or_webinar} request failed with status "
            f"{meetings_response.status_code} {meetings_response.reason}",
            subscriber_email,
            subscriber_names,
            meetings_response,
        )
        return throttle_aware_response(meetings_response, "zoom")

    meeting_data = meetings_response.json()
    join_url = meeting_data.get("join_url")

    if not join_url:
        notify_failure(
            ZOOM_FAIL_EMAIL_SUBJECT,
            f"Zoom add registrant response did not contain a join URL: {meeting_data}",
            subscriber_email,
            subscriber_names,
        )
        return {"statusCode": SUCCESS_STATUS_CODE}

    # Send the joinURL to Kit to store in the correct custom field:
    # Retrieve the Kit credentials from the Secrets Manager response
    kit_key = secret_data.get("zkiKitKey")
    # Fetch the Kit custom field from the DynamoDB response
    kit_join_url_field = item.get("kitJoinURLField")

    kit_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Kit-Api-Key": kit_key,
    }
    kit_body = json.dumps({
        "fields": {
            kit_join_url_field: join_url,
        }
    })

    subscriber_id = event["subscriber"]["id"]
    try:
        kit_response = fetch_with_retry(
            f"https://api.kit.com/v4/subscribers/{subscriber_id}",
            method="PUT",
            headers=kit_headers,
            data=kit_body,
        )
    except requests.RequestException as error:
        notify_failure(
            KIT_FAIL_EMAIL_SUBJECT,
            f"Network error calling Kit update subscriber custom field endpoint for subscriber {subscriber_id}: {error}",
            subscriber_email,
            subscriber_names,
        )
        return {"statusCode": SUCCESS_STATUS_CODE}

    if not kit_response.ok:
        notify_failure(
            KIT_FAIL_EMAIL_SUBJECT,
            f"Kit update subscriber {subscriber_id} with Zoom join URL failed with status "
            f"{kit_response.status_code} {kit_response.reason}",
            subscriber_email,
            subscriber_names,
            kit_response,
        )
        return throttle_aware_response(kit_response, "kit")

    # Everything really did work!
    return {"statusCode": 200}


def split_name(name):
    first_name = ""
    last_name = ""

    if isinstance(name, str) and name != "":
        clean_name = name.strip()

        # Split the full name into parts
        name_parts = clean_name.split(" ")
        name_count = len(name_parts)

        if name_count > 1:
            # Get the last element and remove it from the list
            last_name = name_parts.pop()
            # Rejoin remaining parts with a space
            first_name = " ".join(name_parts)
            # TODO: Handle 'Von', 'Van', 'zu', 'de', Spanish names etc.
        else:
            first_name = clean_name

    return [first_name, last_name]


def fetch_with_retry(url, method="GET", headers=None, data=None, max_retries=2, wait_ms=15000):
    """
    Calls to Zoom and Kit API endpoints may fail due to exceeding rate limits (429),
    or (potentially) temporary server/network issues (500, 502, 503, 504).
    This function will perform an exponential backoff to retry the request up to `max_retries` times if the
    status code is 429 (rate limit), 500 (internal server error), 502 (bad gateway),
    503 (service unavailable, try later), 504 (bad gateway).
    Added jitter factor to avoid "thundering herd" problem of multiple Lambda instances retrying simultaneously
    when under a heavy load of registration request events.
    """
    response = requests.request(method, url, headers=headers, data=data)

    if response.ok:
        return response

    retryable_status_codes = [429, 500, 502, 503, 504]
    if response.status_code in retryable_status_codes and max_retries > 0:
        jitter = random.random() * wait_ms * 0.3  # Up to 30% of wait time
        actual_wait = wait_ms + jitter
        logger.info(
            "Request to %s failed with status %s. Retrying in %.1fs...",
            url, response.status_code, actual_wait / 1000,
        )
        time.sleep(actual_wait / 1000)
        return fetch_with_retry(
            url, method=method, headers=headers, data=data,
            max_retries=max_retries - 1, wait_ms=wait_ms * 2,
        )

    return response


def throttle_aware_response(response, service):
    """
    Decide what to return to the calling Kit webhook after a downstream call has failed.
    If Kit itself failed with a 429 (rate limited, even after retries), ask Kit to back off and
    retry later via a 503 response with a Retry-After header, as per Kit's API documentation.

    If Zoom failed with a 429, it may be that a per day limit or a per second limit has been
    exceeded. Only the per second limit (QPS) is worth retrying, as it will clear next second. Check
    the x-ratelimit-type header to distinguish QPS from daily limits.

    For any other failures, return 200 to Kit regardless of the failure code, since Kit disables a
    webhook rule after too many non-2xx responses, and asking it to retry is unlikely to succeed.

    All failures, including 429, have been reported via SNS in notify_failure().

    TODO: Retry-After would be better as a configurable setting, rather than hard-coded at 60 seconds

    :param response: The failed requests.Response object.
    :param service: Which API the response came from ("zoom" or "kit").
    """
    if response is not None and response.status_code == 429:
        is_retryable = service == "kit" or response.headers.get("x-ratelimit-type") == "QPS"

        if is_retryable:
            return {
                "statusCode": 503,
                "headers": {
                    "Retry-After": "60",
                },
                "body": json.dumps({"response": "Service temporarily unavailable, please retry later"}),
            }

    # No point in requesting Kit to throttle - just return success
    # (problem has been logged and reported to admin by email)
    return {"statusCode": SUCCESS_STATUS_CODE}


def notify_failure(subject, message, subscriber_email, subscriber_names, response=None):
    """
    Send email via SNS when a Zoom or Kit API call fails
    """
    message = f"{message} while attempting to register {subscriber_names[0]} {subscriber_names[1]} {subscriber_email}"

    if response is not None:
        try:
            error_body = response.json()
            if error_body:
                if (error_body.get('code') is not None or error_body.get('message') is not None):
                    message += f" | API response: code {error_body.get('code')}, message: {error_body.get('message')}"
                message += f" | API response body: {json.dumps(error_body)}"
        except Exception:
            # If the response body is not JSON, or empty, nothing to add to the message
            pass

    logger.error(message)
    try:
        sns_client.publish(TopicArn=FAILURE_TOPIC_ARN, Subject=subject, Message=message)
    except Exception as error:
        logger.error("Failed to publish SNS notification: %s", error)
