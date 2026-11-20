/*
  Evergreen SMS webinar: buy "try again" page (buy-fallback-url)

  When someone clicks the buy button on a watch page, the watch function gets
  their email from Kit and starts their cart timer in Countdown Hero before
  sending them to checkout. If Kit or Countdown Hero doesn't answer, it sends
  them to this page instead, with ?indiv_token=XXXX in the address. This block
  points the page's button back at the watch function's /buy with the same
  token, so one click tries again.

  The buy button also lands here, with no token, if the watch page's token was
  missing or broken. Trying again can't help then, so the button goes to
  NO_TOKEN_URL instead (e.g. the registration page, where re-entering their
  email resends their viewing link).

  Paste this whole file into the page's settings → Custom Code →
  Custom Javascript. It has no script tags, because Kajabi adds them there.
  (To use a Custom Code block in the page content instead, add an opening and
  closing script tag around it.)

  On the page, add an ordinary button and set its link to #buy-again
  (any button or link whose address ends in #buy-again is changed).

  Only edit the SETTINGS section below.
*/
(function () {
  /* ======================= SETTINGS: edit this only ======================= */

  // Your evergreen-sms-webinar-watch Function URL, then "buy"
  var BUY_URL = "https://YOUR-FUNCTION-URL.lambda-url.REGION.on.aws/buy";

  // Where the button goes when this page has no token (e.g. the registration page)
  var NO_TOKEN_URL = "https://YOUR-WEBSITE/YOUR-REGISTRATION-PAGE";

  /* ===================== Nothing below needs editing ===================== */

  var MARKER = "#buy-again";

  function target() {
    var token = new URLSearchParams(window.location.search).get("indiv_token");
    return token ? BUY_URL + "?indiv_token=" + encodeURIComponent(token) : NO_TOKEN_URL;
  }

  function pointButtons() {
    var href = target();
    var links = document.querySelectorAll("a[href]");
    for (var i = 0; i < links.length; i++) {
      var raw = links[i].getAttribute("href") || "";
      if (raw.slice(-MARKER.length) === MARKER) {
        links[i].setAttribute("href", href);
        links[i].removeAttribute("target"); // stay in the same tab
      }
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", pointButtons);
  } else {
    pointButtons();
  }
})();
