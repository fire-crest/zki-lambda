# Page view counters

Two tiny blocks that count visits for the conversion KPIs. Each one shows
nothing on the page: it just tells the `evergreen-sms-webinar-watch` function
(`POST /page-view`) that the page was loaded. Rows go to the DynamoDB event log
only; nothing is ever sent to Kit.

| File | Goes on | Logs | Used for |
|---|---|---|---|
| `registration-page.html` | the registration page | `registration_page_view`, with any UTMs in the address | registration page conversion (log), overall and by source |
| `sales-page.html` | the sales page | `sales_page_visit`, under the subscriber when the address has `?indiv_token=`, anonymously when it doesn't | sales page conversion, overall and by viewing segment |

How the rows are stored, and what the function accepts, is in the function's
[README](../README.md#page-views).

## Before you start

- The function must be running the version that includes `/page-view` (this
  change). There are **no new Parameter Store settings**, no permission changes
  and no CORS changes, as long as both pages are on the same website as the watch page.
- You need the watch function's **Function URL** (Lambda → `evergreen-sms-webinar-watch`
  → Configuration → Function URL). It's the same one the watch page uses.

## Adding them to the pages

For each of the two pages:

1. In Kajabi, open the page in the editor and add a **Custom Code** block
   anywhere (the bottom is fine; it shows nothing).
2. Paste in the whole of the matching file.
3. In the `SETTINGS` section, replace `PAGE_VIEW_URL` with your Function URL
   followed by `page-view`, e.g. `https://abc123….lambda-url.eu-north-1.on.aws/page-view`.
   (The watch page uses the same URL followed by `milestone`.)
4. Save and publish.

On the sales page, the block sits alongside the Countdown Hero code and doesn't
affect it.

**Links to the sales page** should carry the subscriber's token, so the visit
can be matched to them and their run:
- The watch page buy button adds it automatically.
- In Kit emails (the cart sequence), end the link with
  `?indiv_token={{ subscriber.indiv_token }}`. Adding
  `&utm_source=email&utm_campaign=cart_1` (cart_2, cart_3…) as well records
  which email brought them back.

## Testing in a browser

Use the test subscriber's **current** `indiv_token` from Kit. For each step,
open the address in a private window, then check **DynamoDB** → **Explore items**
→ `evergreen_events` → **Query**, with the `subscriber_id` shown. The newest row
is at the bottom (or tick **Sort descending**).

| # | Open | Check under `subscriber_id` | Expect |
|---|---|---|---|
| 1 | the registration page with `?utm_source=test&utm_campaign=pv_test` | `registration_page` | a `registration_page_view` row with `utm_source` `test` and `utm_campaign` `pv_test` |
| 2 | the registration page with no `?…` | `registration_page` | another row, with no `utm_` attributes |
| 3 | the sales page with `?indiv_token=TOKEN` | the test subscriber's ID | a `sales_page_visit` row with `token_status` `valid`. Their Kit tags are unchanged |
| 4 | the sales page with no `?…` | `sales_page` | a row with `token_status` `missing` |
| 5 | the sales page with `?indiv_token=` and the token with one letter changed | `sales_page` | a row with `token_status` `invalid` |
| 6 | the watch page's buy button (after reaching the pitch) | the test subscriber's ID | a `cta_click` row **and** a `sales_page_visit` row |

**If no row appears:** on the page, open the browser's developer tools
(View → Developer → Developer Tools in Chrome) → **Network**, reload, and click
the `page-view` request.
- `200`: it was logged; check you're querying the right `subscriber_id`.
- `403`: the page isn't on the `allowed-origin` website.
- `400`: the block has been edited outside `SETTINGS`; paste it in again.
- No `page-view` request at all: the block isn't on the published page, or
  `PAGE_VIEW_URL` is wrong.
- In the **Console** tab, a "CORS" error means the Function URL's CORS setting
  doesn't list this website.

## After testing

Test rows would be counted in the KPIs. Either delete them (in Explore items,
tick the rows → **Actions** → **Delete items**), or leave them: the
`utm_campaign` `pv_test` and the test subscriber's ID make them easy to exclude.

Your own visits to the live pages are counted too. At normal traffic levels
that's too few to matter.
