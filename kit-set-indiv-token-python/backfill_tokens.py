"""
Catch up 'indiv_token' for Kit subscribers.

Run this on your own computer (not in Lambda). It goes through your subscribers
and, for anyone whose token is missing or out of date (blank, an old
WordPress-era token, or made with an old secret), saves the correct one.
Subscribers who already have the right token are skipped, so if it's
interrupted, just run it again and it carries on where it left off.

Needs:  pip3 install --user boto3 cryptography

Two ways to supply the secrets:
  --ask-secrets   You paste the Kit API key and token secret when asked (from
                  Parameter Store -> Show decrypted value). Nothing is shown on
                  screen or saved. No AWS setup needed on your computer.
  (default)       Reads them from Parameter Store using AWS credentials on this
                  computer, e.g. in CloudShell. Set AWS_DEFAULT_REGION=eu-north-1.

Examples (from this folder):
    python3 backfill_tokens.py --ask-secrets --limit 3 --apply   # try 3 subscribers
    caffeinate -i python3 backfill_tokens.py --ask-secrets --apply   # everyone (Mac stays awake)
    python3 backfill_tokens.py --ask-secrets                      # dry run: report only
    python3 backfill_tokens.py --ask-secrets --status inactive --apply   # unconfirmed subscribers

Kit allows 120 API calls a minute per API key; this paces itself at about 110,
so 7,000 subscribers take roughly 65-70 minutes.
"""

import argparse
import getpass
import json
import sys
import time
import urllib.parse

import lambda_function as fn
from kit_token import is_valid_subscriber_id, make_token

CALLS_PER_MINUTE = 110          # Kit's limit is 120; stay safely under it
PAGE_SIZE = 1000
MAX_ATTEMPTS = 5
PROGRESS_EVERY = 100
KIT_STATUSES = ["active", "inactive", "bounced", "complained", "cancelled", "all"]

_last_call = [0.0]


def main():
    parser = argparse.ArgumentParser(description="Catch up indiv_token for Kit subscribers.")
    parser.add_argument("--apply", action="store_true", help="save changes (default is a dry run)")
    parser.add_argument("--limit", type=int, default=0, help="stop after this many updates (for testing)")
    parser.add_argument("--ask-secrets", action="store_true",
                        help="paste the Kit API key and token secret instead of reading Parameter Store")
    parser.add_argument("--status", choices=KIT_STATUSES, default="active",
                        help="which subscribers to include (default: active)")
    args = parser.parse_args()

    cfg = ask_for_secrets() if args.ask_secrets else fn.settings()
    field = fn.KIT_TOKEN_FIELD

    # Fail fast on a mistyped secret, before touching anyone
    try:
        make_token("1", cfg["token_secret"])
    except ValueError as error:
        sys.exit(f"Problem with the token secret: {error}. Check you copied all of /zki/token-secret.")

    mode = "APPLYING CHANGES" if args.apply else "DRY RUN (nothing will be changed)"
    print(f"{mode} - field '{field}', {args.status} subscribers\n")

    checked = needs_update = updated = failed = 0
    started = time.time()

    for subscriber in all_subscribers(cfg["kit_api_key"], args.status):
        checked += 1
        sid = str(subscriber.get("id", ""))
        if not is_valid_subscriber_id(sid):
            print(f"  skipping odd subscriber record: {subscriber!r}")
            continue

        correct = make_token(sid, cfg["token_secret"])
        current = (subscriber.get("fields") or {}).get(field)
        if current == correct:
            continue

        needs_update += 1
        label = f"{sid} {subscriber.get('email_address', '')}"
        if not args.apply:
            print(f"  would update {label} (currently: {'blank' if not current else 'old token'})")
        elif save_token(sid, correct, field, cfg["kit_api_key"]):
            updated += 1
            if args.limit or updated % PROGRESS_EVERY == 0:
                minutes = (time.time() - started) / 60
                print(f"  {updated} updated so far ({minutes:.0f} min) - latest: {label}")
        else:
            failed += 1

        if args.limit and needs_update >= args.limit:
            print(f"\nStopped after {args.limit} (--limit)")
            break

    print(f"\nChecked {checked} subscribers; {needs_update} needed a new token.")
    if args.apply:
        print(f"Updated {updated}; failed {failed}. Took {(time.time() - started) / 60:.0f} minutes.")
        if failed:
            print("Run the same command again to retry the failures - everyone else will be skipped.")
    else:
        print("Dry run only. Run again with --apply to save.")
    return 1 if failed else 0


def ask_for_secrets():
    print("Paste each value from Parameter Store (Show decrypted value). Nothing will appear as you paste.")
    kit_api_key = getpass.getpass("  /zki/kit-api-key: ").strip()
    token_secret = getpass.getpass("  /zki/token-secret: ").strip()
    if not kit_api_key or not token_secret:
        sys.exit("Both values are needed.")
    print()
    return {"kit_api_key": kit_api_key, "token_secret": token_secret}


def all_subscribers(api_key, status):
    """Yield every subscriber with this status, one page of Kit's list at a time."""
    cursor = None
    while True:
        query = {"per_page": PAGE_SIZE, "status": status}
        if cursor:
            query["after"] = cursor
        data = kit_call("GET", "subscribers?" + urllib.parse.urlencode(query), None, api_key)
        yield from data.get("subscribers", [])
        pagination = data.get("pagination", {})
        if not pagination.get("has_next_page"):
            return
        cursor = pagination.get("end_cursor")


def save_token(sid, token, field, api_key):
    try:
        reply = kit_call("PUT", f"subscribers/{sid}", {"fields": {field: token}}, api_key)
    except RuntimeError as error:
        print(f"  FAILED {sid}: {error}")
        return False
    saved = fn.saved_field_value(json.dumps(reply))
    if saved != token:
        print(f"  FAILED {sid}: Kit accepted the update but its record shows {saved!r}")
        return False
    return True


def kit_call(method, endpoint, body, api_key):
    """Call Kit at a steady pace, waiting and retrying if rate limited or Kit has a hiccup. Returns parsed JSON."""
    wait = 10
    for attempt in range(1, MAX_ATTEMPTS + 1):
        pace()
        try:
            status, text = fn.kit_request(method, endpoint, body, api_key)
        except fn.RetryableError as error:
            status, text = None, str(error)

        if status is not None and 200 <= status < 300:
            return json.loads(text) if text else {}
        if status is not None and status != 429 and status < 500:
            raise RuntimeError(f"Kit returned {status}: {text[:300]}")

        if attempt < MAX_ATTEMPTS:
            print(f"  Kit busy or unreachable ({status or text}); waiting {wait}s...")
            time.sleep(wait)
            wait *= 2
    raise RuntimeError(f"gave up after {MAX_ATTEMPTS} attempts: {status or text}")


def pace():
    """Space out the start of each Kit call so we never exceed CALLS_PER_MINUTE."""
    gap = 60.0 / CALLS_PER_MINUTE
    elapsed = time.monotonic() - _last_call[0]
    if elapsed < gap:
        time.sleep(gap - elapsed)
    _last_call[0] = time.monotonic()


if __name__ == "__main__":
    sys.exit(main())
