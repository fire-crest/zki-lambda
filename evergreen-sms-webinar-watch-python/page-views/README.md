# Registration and sales page blocks

Two small blocks of JavaScript that show nothing on the page. They count
visits for the conversion KPIs, and the registration one also passes the
visit's UTMs to Kit and keeps the email for the thank-you page.

| File | Goes on | What it does |
|---|---|---|
| `registration-page.js` | the registration page | 1. logs `registration_page_view`, with any UTMs in the address<br>2. puts the UTMs into the Kit form as hidden fields, so the registration function can copy them onto the `registered` row (`direct` for any not in the address)<br>3. when the form is sent, keeps the email address in that browser tab for the thank-you page |
| `sales-page.js` | the sales page | logs `sales_page_visit`: under the subscriber when the address has `?indiv_token=`, anonymously when it doesn't |

The page view calls go to the watch function's `POST /page-view` and only
write to the event log, never to Kit (see the function's
[README](../README.md#page-views)). The UTMs reach Kit through the form itself.

## Where to paste them

Both files are plain JavaScript, with **no script tags**. Paste the whole file
into the page's **settings → Custom Code → Custom Javascript**: Kajabi adds the
script tags itself. (If you'd rather use a Custom Code *block* in the page
content, add an opening and a closing script tag around it.) Then fill in the
`SETTINGS` section, save and publish.

The script tag rule matters: a script tag inside a script tag ends it early,
and the block silently never runs. The tests check these files never contain one.

## Settings

| Setting | In | What to put |
|---|---|---|
| `PAGE_VIEW_URL` | both | the watch function's Function URL followed by `page-view`, e.g. `https://abc123….lambda-url.eu-north-1.on.aws/page-view` (the watch page uses the same URL followed by `milestone`) |
| `KIT_FIELDS` | registration | the **keys** of the four Kit custom fields holding the UTM source, medium, campaign and content: the key text, **not** the field's ID number, which Kit silently ignores. The same four keys, in the same order, as `/zki/evergreen-registration/utm-fields` in Parameter Store |

## The registration page

**The Kit form** can be Kit's JavaScript embed (the one-line script). The
block waits for Kit to draw the form, then adds the four hidden fields, and
checks them again as the form is sent.

**UTMs:** `utm_source`, `utm_medium`, `utm_campaign` and `utm_content` from
the page address. Any that are missing are sent to Kit as `direct`, so a
value from an earlier registration is never left behind and counted again.
The page view row only gets the UTMs that were really in the address.

**The email hand-off:** as the form is sent, the email address is kept in the
browser's session storage for that tab, under `registration_email`. Kit's
redirect to the thank-you page happens in the same tab, so the thank-you page
can read it. It's gone when the tab is closed. (The thank-you page lookup that
uses it is a later step.)

**Links to the registration page** should carry UTMs, e.g. in a Meta ad's URL
parameters: `utm_source=facebook&utm_medium=paid&utm_campaign={{campaign.name}}&utm_content={{ad.name}}`.

## The sales page

On the sales page, the block sits alongside the Countdown Hero code and doesn't
affect it.

**Links to the sales page** should carry the subscriber's token, so the visit
can be matched to them and their run:
- The watch page buy button adds it automatically.
- In Kit emails (the cart sequence), end the link with
  `?indiv_token={{ subscriber.indiv_token }}`. Adding
  `&utm_source=kit&utm_medium=email&utm_campaign=cart&utm_content=cart_1`
  (cart_2, cart_3…) as well records which email brought them back.

## Testing

Use a private window for each test, and the test subscriber's **current**
`indiv_token` from Kit. To check the event log: **DynamoDB** → **Explore
items** → `evergreen_events` → **Query** with the `subscriber_id` shown (tick
**Sort descending** to see the newest first).

### Page views

| # | Open | Query `subscriber_id` | Expect |
|---|---|---|---|
| 1 | the registration page with `?utm_source=test&utm_campaign=pv_test` | `registration_page` | a `registration_page_view` row with `utm_source` `test` and `utm_campaign` `pv_test` |
| 2 | the registration page with no `?…` | `registration_page` | another row, with no `utm_` attributes |
| 3 | the sales page with `?indiv_token=TOKEN` | the test subscriber's ID | a `sales_page_visit` row with `token_status` `valid`. Their Kit tags are unchanged |
| 4 | the sales page with no `?…` | `sales_page` | a row with `token_status` `missing` |
| 5 | the sales page with `?indiv_token=` and the token with one letter changed | `sales_page` | a row with `token_status` `invalid` |
| 6 | the watch page's buy button (after reaching the pitch) | the test subscriber's ID | a `cta_click` row **and** a `sales_page_visit` row |

### UTMs and the email hand-off

| # | Do | Expect |
|---|---|---|
| 7 | Open the registration page with `?utm_source=test&utm_medium=spike&utm_content=ad_1` and register the test subscriber | **Kit:** their four UTM fields read `test`, `spike`, `direct`, `ad_1`. **Event log:** their new `registered` row has the same four as `utm_source`, `utm_medium`, `utm_campaign` (`direct`), `utm_content` |
| 8 | On the thank-you page that opens, open the browser's developer tools → **Console**, type `sessionStorage.getItem("registration_email")` and press Enter | the email address you registered with |
| 9 | Open the registration page with no `?…` and register again | Kit's four fields all read `direct`, and so does the new `registered` row |

**If the UTMs don't reach Kit** (test 7): in developer tools → **Elements**,
search for `fields[` inside the form. The four hidden fields should be there,
named with the field **keys** (`fields[some_key]`). Names with numbers in
(`fields[1234567]`) mean `KIT_FIELDS` has the ID numbers instead of the keys.
If the names are keys but Kit's fields stay empty, Kit's form isn't accepting extra
fields, and we'll need the fallback (the page sends the UTMs to Lambda itself).

**If no row or call appears at all:** developer tools → **Network**, reload,
and click the `page-view` request.
- `200`: it was logged; check you're querying the right `subscriber_id`.
- `403`: the page isn't on the `allowed-origin` website.
- `404`: `PAGE_VIEW_URL` has a double slash (`…on.aws//page-view`).
- `400`: the block has been edited outside `SETTINGS`; paste it in again.
- No `page-view` request at all: the block isn't on the published page,
  `PAGE_VIEW_URL` is wrong, or it was pasted with extra script tags.
- In the **Console** tab, a "CORS" error means the Function URL's CORS setting
  doesn't list this website.

## After testing

Test rows would be counted in the KPIs. Either delete them (in Explore items,
tick the rows → **Actions** → **Delete items**), or leave them: the
`utm_campaign` `pv_test`, the `utm_medium` `spike` and the test subscriber's
ID make them easy to exclude.

Your own visits to the live pages are counted too. At normal traffic levels
that's too few to matter.
