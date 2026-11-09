# Watch page block

`watch-page.html` is everything the watch page needs, in one block: the Wistia
video, the viewing tracking and the buy button. It talks to the
`evergreen-sms-webinar-watch` Lambda function.

## What it does

| When | What the page does | What Lambda does |
|---|---|---|
| 30 seconds actually **played** (`T_START`) | sends `started` | logs it, adds the "started" tag |
| The video **plays across** 10:00, 20:00, 30:00… (`LOG_EVERY`) | sends `progress` with the minute | logs it (no Kit call) |
| The position reaches the pitch (`T_PITCH`), however they got there | reveals the buy button, sends `reached_pitch` | logs it and decides whether they **watched properly** (see the function's README). If so: "watched" tag, watch count (once per run) and the cart starts |
| The buy button is clicked | opens the sales page straight away, with the token added, and sends `cta_click` | logs it, adds the "clicked buy" tag (starts the cart for people who skipped ahead) |

- **Dragging doesn't count as watching.** A jump of more than 3 seconds between
  position updates is treated as dragging: no `progress` mark is sent for marks
  dragged past, and dragged time doesn't count towards `started`. Playing at up
  to 2x speed counts.
- **Each event is sent once per page load**, so pausing or rewinding doesn't
  send it again.
- **Watching properly can span visits and devices.** Lambda adds up the
  `progress` marks from every visit in the current run.
- **If a call fails** (network or a server problem), the page tries once more
  5 seconds later, then gives up quietly. The viewer never sees an error.
- **No `indiv_token` in the address:** the video and the button still work, but
  nothing is recorded.

## Settings

All at the top of the script, in the `SETTINGS` section:

| Setting | What it is |
|---|---|
| `MILESTONE_URL` | the watch function's Function URL followed by `milestone` |
| `VIDEO_ID` | the Wistia video ID |
| `T_START` | seconds of real playback before `started` (30) |
| `T_PITCH` | when the pitch starts, in seconds. **Must match** `/zki/evergreen-watch/pitch-second` in Parameter Store: change both together |
| `LOG_EVERY` | keep at 600 (every 10 minutes); Lambda only accepts 10, 20, 30… |
| `SALES_URL` | the sales page address; the token is added automatically |

The button wording is in the HTML near the top of the file.

## Putting it on a Kajabi page

1. Add a **Custom Code** block where the video should go.
2. Paste in the **whole** file, with the settings filled in.
3. The page must be opened with the subscriber's token in the address:
   `…?indiv_token=XXXX`. In Kit emails: `?indiv_token={{ subscriber.indiv_token }}`.

## Testing it (the spike re-run)

**Before you start**

1. In Parameter Store, add three settings under `/zki/evergreen-watch/` (see the
   function's README): `clicked-tag-id`, `pitch-second` and
   `allowed-missing-marks`. **Do this before uploading the new function code**,
   or it will refuse to run and email you.
2. Upload the new function code.
3. Pick a test subscriber. **Register them through the form** with no window
   tags, so they have a fresh run. Note their watch count, and remove the
   "started", "watched" and "clicked buy" tags.
4. Open the test page as `…?indiv_token=THEIR-TOKEN`. In Chrome, open the
   developer tools (View → Developer → Developer Tools) on the **Network** tab,
   and type `milestone` in its filter box, so you can watch each call go out.

**A. Dragging straight to the pitch** (about 5 minutes)

1. Play for 30 seconds. **Expect** one `milestone` call (`started`) with status
   `200`, and the "started" tag in Kit.
2. Drag to about 72:00 and let it play past 72:09. **Expect** the buy button to
   appear, and one `reached_pitch` call. In DynamoDB, that `reached_pitch` row
   shows `qualified` = false and lists the missing marks. In Kit: **no** "watched"
   tag, and the watch count is unchanged.
3. Click the buy button. **Expect** the sales page to open straight away with
   `?indiv_token=…` in its address, and the "clicked buy" tag in Kit.

**B. Watching properly** (about 10 minutes, using short plays)

1. Reload the page (so each event can be sent again).
2. For each mark, drag to about 30 seconds before it and let it play across it:
   9:30, 19:30, 29:30, 39:30, 49:30, 59:30. Skipping 70 is fine (one missing
   mark is allowed). **Expect** a `progress` call each time.
3. Drag to about 72:00 and let it play past 72:09. **Expect** the
   `reached_pitch` row to show `qualified` = true, the "watched" tag in Kit, and
   the watch count up by 1.
4. Reload, and reach the pitch again. **Expect** another qualified
   `reached_pitch` row, but the watch count does **not** go up (once per run).

**C. Other website** (1 minute)

Follow the website check in the function's README: a call from any other
website is blocked.

**Tidy up:** this page is a test rig. Unpublish or delete it before launch.
