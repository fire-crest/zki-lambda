# zki-lambda

AWS Lambda functions that replace the endpoints of the WordPress plugin
`zoom_kit_integration`. They're called by webhooks in Kit automation rules.

| Folder | Replaces | What it does |
|---|---|---|
| [`kit-set-indiv-token-python`](kit-set-indiv-token-python/) | `kit_create_subscriber_token()`, endpoint `/zoom_kit_integration/v2/4UJ8lH9EtZ0D/` | Saves an encrypted per-subscriber token in the Kit custom field `indiv_token`. See its README for setup, the token format and the backfill script |
| [`evergreen-sms-webinar-watch-python`](evergreen-sms-webinar-watch-python/) | nothing (new) | Records evergreen webinar milestones (started, every 10 minutes played, reached the pitch, buy button clicked) in the DynamoDB event log and Kit, with an allow-list so the page can't apply any other tag. A pitch only counts if they watched properly (dragging doesn't count); the watch count goes up once per run. Also counts registration page views and sales page visits (log only, with UTMs; sales visits without a token still count). Includes the watch page block in `watch-page/`, and the registration and sales page blocks in `page-views/` (the registration one also passes the UTMs to the Kit form and keeps the email for the thank-you page). **Live.** Steps 1, 3, 4 and 5 of the evergreen funnel build |
| [`evergreen-sms-webinar-registration-python`](evergreen-sms-webinar-registration-python/) | nothing (new) | Called by a Kit webhook on every evergreen webinar registration: logs it in the DynamoDB event log, makes sure the subscriber's token exists, adds 1 to their registration count once per watch-and-purchase run (not when a window is already open) copies the registration's UTMs from Kit onto the logged row, and adds the tag that releases the confirmation email. **Live.** Steps 2 and 6 of the evergreen funnel build |
| *not yet migrated* | `/kit/{token}` and `/kittag/{token}` endpoints | Tag a subscriber from a live webinar's replay page. Only needed if live webinars return, so **low priority**. When migrated, give it its own function. |
| [`zoom-kit-add-contact-to-webinar-python`](zoom-kit-add-contact-to-webinar-python/) | `add_contact_to_webinar()`, endpoint `/zoom_kit_integration/v2/zar2w` | Registers a Kit subscriber for a Zoom webinar or meeting (Python) |
| [`zoom-kit-add-contact-to-webinar`](zoom-kit-add-contact-to-webinar/) | as above | Node.js version of the same function |
