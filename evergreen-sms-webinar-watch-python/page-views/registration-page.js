/*
  Evergreen SMS webinar: registration page block

  Paste this whole file into the registration page's settings → Custom Code →
  Custom Javascript. It has no script tags, because Kajabi adds them there.
  (To use a Custom Code block in the page content instead, add an opening and
  closing script tag around it.)

  On the page it shows nothing. It does three things:
    1. Each page load: logs a registration_page_view row, with any UTMs in the
       page address, to the event log.
    2. Puts this visit's UTMs into the Kit form as hidden fields, so Kit saves
       them on the subscriber and the registration function copies them onto
       the 'registered' row. Any UTM missing from the address is sent as
       "direct", so an old value in Kit is never left behind.
    3. When the form is sent, keeps the email address for the thank-you page
       (in this browser tab only), for the personal watch link lookup.

  Only edit the SETTINGS section below.
*/
(function () {
  /* ======================= SETTINGS: edit this only ======================= */

  // Your evergreen-sms-webinar-watch Function URL, then "page-view"
  var PAGE_VIEW_URL = "https://YOUR-FUNCTION-URL.lambda-url.REGION.on.aws/page-view";

  // The Kit custom field KEYS that hold each UTM: the field's key text, NOT its ID number
  // (Kit ignores fields sent by ID).
  // Same four, same order, as /zki/evergreen-registration/utm-fields in Parameter Store.
  var KIT_FIELDS = {
    utm_source: "KIT_FIELD_KEY_FOR_UTM_SOURCE",
    utm_medium: "KIT_FIELD_KEY_FOR_UTM_MEDIUM",
    utm_campaign: "KIT_FIELD_KEY_FOR_UTM_CAMPAIGN",
    utm_content: "KIT_FIELD_KEY_FOR_UTM_CONTENT"
  };

  /* ===================== Nothing below needs editing ===================== */

  var UTM_NAMES = ["utm_source", "utm_medium", "utm_campaign", "utm_content"];
  var EMAIL_KEY = "registration_email"; // the thank-you page reads this
  var NO_VALUE = "direct";
  var MAX_LENGTH = 100; // Lambda ignores anything longer

  var params = new URLSearchParams(window.location.search);
  var utms = {};
  UTM_NAMES.forEach(function (name) {
    var value = (params.get(name) || "").trim();
    if (value && value.length <= MAX_LENGTH) utms[name] = value;
  });

  // 1. Count this page view (only the UTMs actually in the address)
  var view = { page: "registration" };
  Object.keys(utms).forEach(function (name) { view[name] = utms[name]; });
  fetch(PAGE_VIEW_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(view),
    keepalive: true
  }).catch(function () { /* a missed page view isn't worth bothering the visitor about */ });

  // 2. Hidden UTM fields on the Kit form
  function isKitForm(element) {
    return element && element.tagName === "FORM" &&
      (element.hasAttribute("data-uid") || element.classList.contains("formkit-form"));
  }

  function addHiddenFields(form) {
    UTM_NAMES.forEach(function (name) {
      var fieldName = "fields[" + KIT_FIELDS[name] + "]";
      var input = form.querySelector('input[name="' + fieldName + '"]');
      if (!input) {
        input = document.createElement("input");
        input.type = "hidden";
        input.name = fieldName;
        form.appendChild(input);
      }
      input.value = utms[name] || NO_VALUE;
    });
  }

  function addToAllKitForms() {
    var forms = document.querySelectorAll("form");
    for (var i = 0; i < forms.length; i++) {
      if (isKitForm(forms[i])) addHiddenFields(forms[i]);
    }
  }

  // Kit's script draws the form after the page loads, so watch for it
  addToAllKitForms();
  new MutationObserver(addToAllKitForms).observe(document.documentElement, { childList: true, subtree: true });

  // 3. As the form is sent (before Kit's own code runs): make sure the hidden
  //    fields are there, and keep the email for the thank-you page
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!isKitForm(form)) return;
    addHiddenFields(form);
    var email = form.querySelector('input[name="email_address"], input[type="email"]');
    if (email && email.value) {
      try { sessionStorage.setItem(EMAIL_KEY, email.value.trim()); } catch (e) { /* storage blocked: the thank-you page falls back to its own message */ }
    }
  }, true);
})();
