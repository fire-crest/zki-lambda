/*
  Evergreen SMS webinar: replay "try again" page

  When someone's watch window ends, the watch function checks Kit to see whether
  their cart is still open. If Kit doesn't answer, it sends them to this page,
  with ?indiv_token=XXXX in the address. This block points the page's button
  back at the watch function with the same token, so one click tries again.

  Paste this whole file into the page's settings → Custom Code →
  Custom Javascript. It has no script tags, because Kajabi adds them there.
  (To use a Custom Code block in the page content instead, add an opening and
  closing script tag around it.)

  On the page, add an ordinary button and set its link to #watch-again
  (any button or link whose address ends in #watch-again is changed).

  Only edit the SETTINGS section below.
*/
(function () {
  /* ======================= SETTINGS: edit this only ======================= */

  // Your evergreen-sms-webinar-watch Function URL, then "watch-expired"
  var WATCH_EXPIRED_URL = "https://YOUR-FUNCTION-URL.lambda-url.REGION.on.aws/watch-expired";

  /* ===================== Nothing below needs editing ===================== */

  var MARKER = "#watch-again";

  function target() {
    var token = new URLSearchParams(window.location.search).get("indiv_token");
    // No token: the watch function sends them to the expired page, which is the best it can do
    return token ? WATCH_EXPIRED_URL + "?indiv_token=" + encodeURIComponent(token) : WATCH_EXPIRED_URL;
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
