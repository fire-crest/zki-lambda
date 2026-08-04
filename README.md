Port of function add_contact_to_webinar, endpoint /zoom_kit_integration/v2/zar2w
from WordPress plugin zoom_kit_integration
to AWS Lambda functions
zoom-kit-add-contact-to-webinar (Node.js)
and
zoom-kit-add-contact-to-webinar-python (Python)

add_contact_to_webinar endpoint is triggered by an automated rule in Kit cms when a subscriber 
fills a form requesting to join a Zoom webinar or meeting.

Example Event JSON from Kit:
{
  "subscriber": {
    "first_name": "Jo Clark",
    "email_address": "jo@email.co.uk",
    "id": "1234567890"
  }
}

1. Request OAuth token from Zoom (client's Zoom account keys from Secrets Manager)
   https://zoom.us/oauth/token
3. Add subscriber to the meeting/webinar in Zoom (webinar ID from DynamoDB parameters table)
   https://api.zoom.us/v2/meetings/<meetingId>/registrants
   OR
   https://api.zoom.us/v2/webinars/<webinarId>/registrants
   Subscriber's individual Zoom meeting join URL is returned.
5. Store join URL in custom field on subscriber's record in Kit (client's Kit account key from Secrets Manager)
   https://api.kit.com/v4/subscribers/<subscriberId>

Any errors are reported via SNS email to client's admin email address and logged to console (for CloudWatch)   
