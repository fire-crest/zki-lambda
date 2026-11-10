/*
  Evergreen SMS webinar: sales page visit counter

  Paste this whole file into the sales page's settings → Custom Code →
  Custom Javascript. It has no script tags, because Kajabi adds them there.
  (To use a Custom Code block in the page content instead, add an opening and
  closing script tag around it.)
  It shows nothing on the page, and doesn't affect the Countdown Hero timer.
  Each time the page loads, it logs a sales_page_visit row to the event log:
  under the subscriber if the address carries ?indiv_token=XXXX (the watch page
  buy button adds it; in Kit emails use ?indiv_token={{ subscriber.indiv_token }}),
  and as an anonymous visit if it doesn't, so broken links still get counted.

  Only edit the SETTINGS section below.
*/
(function () {
  /* ======================= SETTINGS: edit this only ======================= */

  // Your evergreen-sms-webinar-watch Function URL, then "page-view"
  var PAGE_VIEW_URL = "https://YOUR-FUNCTION-URL.lambda-url.REGION.on.aws/page-view";

  /* ===================== Nothing below needs editing ===================== */

  var params = new URLSearchParams(window.location.search);
  var body = { page: "sales" };
  var token = params.get("indiv_token");
  if (token) body.token = token;
  ["utm_source", "utm_medium", "utm_campaign", "utm_content"].forEach(function (name) {
    var value = params.get(name);
    if (value) body[name] = value;
  });

  fetch(PAGE_VIEW_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    keepalive: true // finish sending even if the visitor leaves straight away
  }).catch(function () { /* a missed visit isn't worth bothering the visitor about */ });
})();
