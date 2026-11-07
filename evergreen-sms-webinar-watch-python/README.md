# evergreen-sms-webinar-watch (Python)

Records how far each viewer gets through the evergreen SMS webinar. The watch
page calls it at each milestone. Every milestone is written to the DynamoDB
event log, and the two important ones are also recorded in Kit.

New for the evergreen funnel. The page can't choose which tag to apply, so it
can never apply a purchase tag or anything else. (The WordPress plugin's `/kit/`
and `/kittag/` replay endpoints are separate and not yet migrated; see the main README.)

```mermaid
flowchart TD
    Page["Watch page<br/>(allowed website)"] -->|"1. POST /milestone<br/>token + milestone"| Fn
    subgraph AWS
        Fn["Lambda: page mode<br/>checks token + website"]
        Job["Lambda: job mode<br/>(background run)"]
        DB[("DynamoDB<br/>evergreen_events")]
        SNS["SNS topic"]
    end
    Fn -->|"2. write event row"| DB
    Fn -->|"3. reply 200"| Page
    Fn -->|"4. started / reached_pitch only"| Job
    Job -->|"5. tag; +1 to watch count,<br/>once per run"| Kit["Kit API v4"]
    Job -->|"find start of latest run,<br/>mark it counted"| DB
    Job -. "Kit busy: automatic retries<br/>still failing: email" .-> SNS
```

## Milestones

| Milestone | When the page sends it | Kit | Event log |
|---|---|---|---|
| `started` | 30 seconds of playback, so auto-play alone doesn't count | tag `started-tag-id` | ✅ |
| `progress` | every 10 minutes: `minute` 10, 20, 30… | nothing | ✅ with the minute |
| `reached_pitch` | the pitch timestamp | tag `watched-tag-id`, then add 1 to `watch-count-field`, **once per run** | ✅ |

The page sends:

```json
{ "token": "<indiv_token>", "milestone": "started" }
{ "token": "<indiv_token>", "milestone": "progress", "minute": 20 }
{ "token": "<indiv_token>", "milestone": "reached_pitch" }
```

The replies are:
- `200`: recorded.
- `400`: unknown milestone, or a minute that isn't 10, 20, 30… up to 240.
- `403`: invalid token, or a call from another website.
- `503`: temporary problem. Details are emailed to admin.

A blank or non-numeric watch count counts as 0. The tag IDs, field name and
allowed website all come from Parameter Store (step 2 below), so none of them
appear in the code or this guide.

## The watch count: once per run

A **run** is one watch-and-purchase window: the watch window, then the cart
window that opens on reaching the pitch. A run starts with a registration made
while no window was open; `evergreen-sms-webinar-registration` marks that
`registered` row `new_run = true`. Re-registering while a window is open
doesn't start a new run.

The watch count means "how many runs they watched to the pitch", e.g. before
buying. It can never exceed the registration count, which counts runs started.

- **Every `reached_pitch` is logged**, re-watches included, so returning viewers
  stay visible in the event log. The tag is added every time too (harmless).
- **The count goes up once per run.** The background job finds the latest
  `registered` row with `new_run = true` and marks it `pitch_counted_at`.
  DynamoDB accepts only the first mark, so a re-watch during the open cart, a
  second browser tab or a retried job can't count twice. Only the call that
  made the mark adds 1.
- **If Kit is busy** when saving the count, the mark is removed again, so
  Lambda's automatic retry can count it.
- **No run on record** (e.g. a subscriber who never used the registration form,
  or whose registrations all predate run tracking): the event is logged and
  tagged, but not counted, and admin is emailed.

## The event log: table `evergreen_events`

| Attribute | Example | |
|---|---|---|
| `subscriber_id` | `1234567890` | partition key |
| `event_at` | `2026-11-06T14:12:03.123Z#a1b2c3` | sort key: UTC time, plus a random suffix so two events in the same millisecond don't collide |
| `event` | `started`, `progress`, `reached_pitch` | |
| `minute` | `20` | `progress` only |
| `source` | `watch_page` | |
| `pitch_counted_at` | `2026-11-07T10:20:01+00:00` | on `registered` rows only: set when that registration's watch has been counted |

The index `by_event` (partition `event`, sort `event_at`) answers "all `started` events between two dates". Rows
contain Kit subscriber IDs only, never email addresses. Later steps add more
event types (registrations, sales page visits, purchases) to the same table.

**Drop-off curve:** count *distinct* `subscriber_id`s per `minute` over the
`progress` rows. Someone who re-watches logs the same minutes again.

## Files

| File | What it is |
|---|---|
| `lambda_function.py` | The function |
| `kit_token.py` | Reads tokens. Identical to the copy in `kit-set-indiv-token-python`: keep them the same |
| `test_lambda_function.py` | Tests (`python3 -m unittest -v`). No AWS or Kit needed. The event log tests also need `pip install "moto[dynamodb]"`, and are skipped without it |

It needs the same `cryptography-py313-arm64` layer as `kit-set-indiv-token`.

---

## Setting it up

Work in the same AWS region as your other functions and parameters throughout.
Below, replace `REGION` with it, e.g. `eu-west-2`.

### 1. Check the concurrency quota

Search for **Service Quotas** → **AWS services** → **AWS Lambda** → **Concurrent executions**.
Note the **Applied account-level quota value**. 1,000 is the usual default, and anything from 100 up is fine.
You've reserved 10 for `kit-set-indiv-token` and will reserve 10 here, and AWS always keeps at least 100 unreserved.

### 2. Add this function's settings to Parameter Store

Nothing specific to the business is in the code. Create these four parameters
in **Systems Manager → Parameter Store**, each with **Tier: Standard** and
**Type: String** (they aren't secrets):

| Name | Value |
|---|---|
| `/zki/evergreen-watch/allowed-origin` | The watch page's website, e.g. `https://www.example.com` (https, no trailing path) |
| `/zki/evergreen-watch/started-tag-id` | ID of the Kit tag for "started watching" |
| `/zki/evergreen-watch/watched-tag-id` | ID of the Kit tag for "reached the pitch" |
| `/zki/evergreen-watch/watch-count-field` | Key of the Kit custom field holding the lifetime watch count |

To find a tag's ID in Kit, open the tag: the ID is the number at the end of the
page address. A custom field's key is shown in Kit's custom field settings.

The function also uses the existing `/zki/kit-api-key` and `/zki/token-secret`.
If any of these is missing, or a tag ID isn't a number, the function refuses to
run and emails you the reason.

To change a tag later, edit the parameter. The function reads its settings
once and keeps them, so then make any small change to the function (e.g.
set the timeout to 31 seconds and save) to make it pick up the new value.

### 3. Create the DynamoDB table

Go to **DynamoDB** → **Tables** → **Create table**:
- **Table name:** `evergreen_events`
- **Partition key:** `subscriber_id` (String)
- **Sort key:** `event_at` (String)
- **Table settings:** Customize settings → **Capacity mode: On-demand**. Leave everything else as default.

Click **Create table** and wait until its status is **Active**. Then add the index:
open the table → **Indexes** → **Create index**:
- **Partition key:** `event` (String)
- **Sort key:** `event_at` (String)
- **Index name:** `by_event`
- **Attribute projections:** All

### 4. Create the function

Go to Lambda → Create function → **Event Functions** → Author from scratch:
- **Name:** `evergreen-sms-webinar-watch`
- **Runtime:** **Python 3.13**
- **Architecture:** **arm64**

Then:
- **Code:** Upload from → .zip file → a zip of `lambda_function.py` and `kit_token.py` → Save (then Deploy if that button appears).
- **Layer:** Code tab → Layers → Add a layer → Custom layers → `cryptography-py313-arm64`, version 1.
- **General configuration:** set the timeout to **30 sec** and leave memory at 128 MB.
- **Environment variables:** `FAILURE_TOPIC_ARN` = your failure topic ARN.
  The other optional settings are listed at the top of `lambda_function.py`.

### 5. Permissions

Configuration → Permissions → role name → Add permissions → Create inline policy → JSON.
Replace `REGION` and `ACCOUNT` (3 places each) with your region and 12-digit account number, and paste in your SNS topic ARN:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "ssm:GetParameters",
      "Resource": "arn:aws:ssm:REGION:ACCOUNT:parameter/zki/*"
    },
    {
      "Effect": "Allow",
      "Action": ["dynamodb:PutItem", "dynamodb:Query", "dynamodb:UpdateItem"],
      "Resource": "arn:aws:dynamodb:REGION:ACCOUNT:table/evergreen_events"
    },
    {
      "Effect": "Allow",
      "Action": "lambda:InvokeFunction",
      "Resource": "arn:aws:lambda:REGION:ACCOUNT:function:evergreen-sms-webinar-watch"
    },
    {
      "Effect": "Allow",
      "Action": "sns:Publish",
      "Resource": "YOUR_FAILURE_TOPIC_ARN"
    }
  ]
}
```

Name the policy `evergreen-sms-webinar-watch-access`.

The function adds rows, reads one subscriber's history (`Query`) to find their latest registration, and sets or removes the `pitch_counted_at` mark (`UpdateItem`). It can't delete rows.

### 6. Retries, failure emails, concurrency

- **Asynchronous invocation:** retry attempts **2**, maximum age of event **1 h**.
- **Destinations:** Asynchronous invocation → On failure → SNS topic → your failure topic.
- **Concurrency:** reserve **10**. Each call takes a fraction of a second, so 10 handles around 100 calls a second.

### 7. Function URL with CORS

Configuration → Function URL → Create:
- **Auth type:** NONE
- **Configure cross-origin resource sharing (CORS):** ✅ ticked
  - **Allow origin:** your website, exactly the same value as `allowed-origin`, with nothing else in the list
  - **Allow headers:** `content-type`
  - **Allow methods:** `POST`
  - **Max age:** `86400`
  - **Allow credentials:** off

Then, in **CloudShell**:

```
aws lambda add-permission --function-name evergreen-sms-webinar-watch --statement-id FunctionURLAllowPublicAccess --action lambda:InvokeFunctionUrl --principal "*" --function-url-auth-type NONE
```

```
aws lambda add-permission --function-name evergreen-sms-webinar-watch --statement-id FunctionURLInvokeAllowPublicAccess --action lambda:InvokeFunction --principal "*" --invoked-via-function-url
```

The endpoint is the Function URL followed by `milestone`, e.g.
`https://abc123….lambda-url.REGION.on.aws/milestone`.

---

## Test calls

Use a test subscriber whose latest registration **started a run** (a
`registered` row with `new_run` true; register them again with no window tags
if not), and their **current** `indiv_token` from Kit. Before you start, remove the
"started" and "reached the pitch" tags from them and note their watch count.

In Postman, POST to `…on.aws/milestone` with body type raw → JSON:

| # | Body | Expect |
|---|---|---|
| 1 | `{"token": "TOKEN", "milestone": "started"}` | `200`. Within seconds, the subscriber has the "started" tag |
| 2 | `{"token": "TOKEN", "milestone": "progress", "minute": 10}` | `200`. Nothing changes in Kit |
| 3 | `{"token": "TOKEN", "milestone": "reached_pitch"}` | `200`. They have the "reached the pitch" tag, and the watch count has gone up by 1 |
| 3a | the same as 3, again | `200`. Another `reached_pitch` row, but the watch count does **not** go up |
| 4 | `{"token": "TOKEN", "milestone": "progress", "minute": 15}` | `400` |
| 5 | `{"token": "TOKEN", "milestone": "purchased"}` | `400` |
| 6 | `{"token": "TOKEN-WITH-ONE-LETTER-CHANGED", "milestone": "started"}` | `403` |

Then check the log: **DynamoDB** → **Explore items** → `evergreen_events`. There
should be four new rows for the subscriber's ID (`started`, `progress` with minute 10,
and two `reached_pitch`), and none for calls 4–6. The `registered` row that
started their run should now have `pitch_counted_at`.

**Re-registering within the run.** Give the test subscriber the cart-window tag
by hand, submit the registration form, then repeat call 3: the watch count does
**not** go up (the registration was logged with `new_run` false).

**A new run.** Remove both window tags, submit the registration form again,
then repeat call 3: the watch count goes up by 1.

**No run.** Repeat call 3 with the token of a subscriber who has never used the
registration form: `200` and the tag, but no change to their watch count, and a
failure email saying they had no run on record.

**Website check.** Open any other website (e.g. `https://example.com`), open the
browser's developer console (View → Developer → JavaScript Console in Chrome)
and paste:

```js
fetch("https://YOUR-URL.lambda-url.REGION.on.aws/milestone", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({token: "TOKEN", milestone: "progress", minute: 20})}).then(r => console.log(r.status)).catch(e => console.log("Blocked:", e.message))
```

You should see **Blocked** (a CORS error), and no new row in the log.
Running the same line from a page on your allowed website should print `200`.
