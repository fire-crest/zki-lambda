# zki-lambda

AWS Lambda functions that replace the endpoints of the WordPress plugin
`zoom_kit_integration`. They're called by webhooks in Kit automation rules.

| Folder | Replaces | What it does |
|---|---|---|
| [`kit-set-indiv-token-python`](kit-set-indiv-token-python/) | `kit_create_subscriber_token()`, endpoint `/zoom_kit_integration/v2/4UJ8lH9EtZ0D/` | Saves an encrypted per-subscriber token in the Kit custom field `indiv_token`. See its README for setup, the token format and the backfill script |
| [`evergreen-sms-webinar-watch-python`](evergreen-sms-webinar-watch-python/) | nothing (new) | Records evergreen webinar milestones (started, every 10 minutes, reached the pitch) in the DynamoDB event log and Kit, with an allow-list so the page can't apply any other tag. **Live.** Step 1 of the evergreen funnel build |
| *not yet migrated* | `/kit/{token}` and `/kittag/{token}` endpoints | Tag a subscriber from a live webinar's replay page. Only needed if live webinars return, so **low priority**. When migrated, give it its own function. |
| [`zoom-kit-add-contact-to-webinar-python`](zoom-kit-add-contact-to-webinar-python/) | `add_contact_to_webinar()`, endpoint `/zoom_kit_integration/v2/zar2w` | Registers a Kit subscriber for a Zoom webinar or meeting (Python) |
| [`zoom-kit-add-contact-to-webinar`](zoom-kit-add-contact-to-webinar/) | as above | Node.js version of the same function |
