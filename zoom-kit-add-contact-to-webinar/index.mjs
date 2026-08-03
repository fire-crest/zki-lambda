import { SecretsManagerClient, GetSecretValueCommand } from "@aws-sdk/client-secrets-manager";
import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import { DynamoDBDocumentClient, GetCommand } from "@aws-sdk/lib-dynamodb";
import { SNSClient, PublishCommand } from "@aws-sdk/client-sns";

const secretsClient = new SecretsManagerClient();
const dbClient = DynamoDBDocumentClient.from(new DynamoDBClient());
const snsClient = new SNSClient();
const FAILURE_TOPIC_ARN = process.env.FAILURE_TOPIC_ARN;

/* This function is designed to be called from a webhook in a Kit rule.
   In the case of internal failures (DynamoDB, Secrets Manager, non-rate-limit API errors) always return 200,
   since Kit disables a webhook rule after too many failures (we definitely do not want that to happen)
   and there's nothing Kit can usefully do differently in these cases. 
   Failures are reported to admin via SNS email instead. 
   The exception is a 429 from Zoom's or Kit's own API's even after retries are exhausted in fetchWithRetry(),
   in which case return 503 back to Kit as a throttle signal. 
   Contact jo@redkite.biz for more information!
*/
const successStatusCode = 200;
const dbFailEmailSubject = "Zoom-Kit: DynamoDB failure";
const secretsFailEmailSubject = "Zoom-Kit: Secrets Manager failure";
const zoomFailEmailSubject = "Zoom-Kit: Zoom API failure";
const kitFailEmailSubject = "Zoom-Kit: Kit API failure";

export const handler = async (event) => {

    // Extract subscriber info from event
    const subscriberEmail = event.subscriber.email_address;
    const subscriberNames = splitName(event.subscriber.first_name);
 	
	// Zoom will reject empty name fields
    if (subscriberNames[0] === '') {
        subscriberNames[0] = "-";
    }
    if (subscriberNames[1] === '') {
        subscriberNames[1] = "-";
    }

    // Fetch the meeting/webinar option and ID from DynamoDB
    let dbResponse;
    try {
        dbResponse = await dbClient.send(
            new GetCommand({
                TableName: "zoom-kit-parameters",
                Key: { settingID: "zoom-kit-parameters" },
            })
        );
    } catch (error) {
        await notifyFailure(dbFailEmailSubject, `Failed to retrieve parameters from DynamoDB: ${error}`, 
            subscriberEmail, subscriberNames);
        return {statusCode: successStatusCode};
    }
    if (!dbResponse.Item) {
        await notifyFailure(dbFailEmailSubject, "No Item found in DynamoDB for key 'zoom-kit-parameters'", 
            subscriberEmail, subscriberNames);
        return {statusCode: successStatusCode};
    }
    const meetingOrWebinar = dbResponse.Item.meetingOrWebinar;
    const webinarId = dbResponse.Item.webinarId; //This ID number represents either a meeting or a webinar!

    // Retrieve the Zoom credentials from Secrets Manager
    let secretResponse, zoomClient, zoomAccount;
    try {
        secretResponse = await secretsClient.send(
            new GetSecretValueCommand({ SecretId: "zoom-kit-integration/settings" })
        );
        ({ zkiZoomClient: zoomClient, zkiZoomAccount: zoomAccount } = JSON.parse(secretResponse.SecretString));
    } catch (error) {
        await notifyFailure(secretsFailEmailSubject, `Failed to retrieve Zoom credentials from Secrets Manager: ${error}`, 
            subscriberEmail, subscriberNames);
        return {statusCode: successStatusCode};
    }

    if (!zoomClient || !zoomAccount) {
        await notifyFailure(secretsFailEmailSubject, "Zoom credentials missing from Secrets Manager response", 
            subscriberEmail, subscriberNames);
        return {statusCode: successStatusCode};
    }

    // Ask Zoom for an OAuth access token, valid for 1 hour
    let options = {
        method: "POST",
        headers: {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": `Basic ${zoomClient}`,
        },
        body: new URLSearchParams({
            grant_type: "account_credentials",
            account_id: zoomAccount,
        }),
    };
    let tokenResponse;
    try {
        tokenResponse = await fetchWithRetry("https://zoom.us/oauth/token", options);
    } catch (error) {
        await notifyFailure(zoomFailEmailSubject, 
            `Network error calling Zoom OAuth endpoint: ${error.message}${error.cause ? ` (cause: ${error.cause})` : ''}`, 
            subscriberEmail, subscriberNames);
        return {statusCode: successStatusCode};
    }

    if (!tokenResponse.ok) {
        await notifyFailure(zoomFailEmailSubject, 
            `Zoom OAuth token request failed with status ${tokenResponse.status} ${tokenResponse.statusText}`, 
            subscriberEmail, subscriberNames, tokenResponse);
        return throttleAwareResponse(tokenResponse, "zoom");
    }
    
    const tokenData = await tokenResponse.json();
    const accessToken = tokenData.access_token;
    if (!accessToken) {
        await notifyFailure(zoomFailEmailSubject, 
            `Zoom OAuth response did not contain an access token: ${tokenData}`, subscriberEmail, subscriberNames);
        return {statusCode: successStatusCode};
    }

    // Register event subscriber with Zoom meeting/webinar
    options = {
        method: "POST",
        headers: {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": `Bearer ${accessToken}`,
        },
        body: JSON.stringify({
            email: subscriberEmail,
            first_name: subscriberNames[0],
            last_name: subscriberNames[1],
        }),
    };
    let meetingsResponse;
    try {
        // Zoom API URL contains "/meetings/" if registering for a meeting, or "/webinars/" if registering for a webinar
        meetingsResponse = await fetchWithRetry(`https://api.zoom.us/v2/${meetingOrWebinar}s/${webinarId}/registrants`, options);    
    } catch (error) {
        await notifyFailure(zoomFailEmailSubject, 
            `Network error calling Zoom registrants endpoint: ${error}${error.cause ? ` (cause: ${error.cause})` : ''}`, 
            subscriberEmail, subscriberNames);
        return {statusCode: successStatusCode};
    }

    if (!meetingsResponse.ok) {
        await notifyFailure(zoomFailEmailSubject, 
            `Zoom add registrant to ${meetingOrWebinar} request failed with status ${meetingsResponse.status} ${meetingsResponse.statusText}`, 
            subscriberEmail, subscriberNames, meetingsResponse);
        return throttleAwareResponse(meetingsResponse, "zoom");
    }
    
    const meetingData = await meetingsResponse.json();
    const joinURL = meetingData.join_url;

    if (!joinURL) {
        await notifyFailure(zoomFailEmailSubject, 
            `Zoom add registrant response did not contain a join URL: ${meetingData}`, subscriberEmail, subscriberNames);
        return {statusCode: successStatusCode};
    }

    // Send the joinURL to Kit to store in the correct custom field:
    // Retrieve the Kit credentials from the Secrets Manager response
    const { zkiKitKey: kitKey } = JSON.parse(secretResponse.SecretString);
    // Fetch the Kit custom field from the DynamoDB response
    const kitJoinURLField = dbResponse.Item.kitJoinURLField;
    options = {
        method: "PUT",
        headers: {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Kit-Api-Key": kitKey,
        },
        body: JSON.stringify({
            fields: {
                [kitJoinURLField]: joinURL,
            }
        }),
    };
    let kitResponse;
    try {
       kitResponse = await fetchWithRetry(`https://api.kit.com/v4/subscribers/${event.subscriber.id}`, options);
    } catch (error) {
        await notifyFailure(kitFailEmailSubject, 
            `Network error calling Kit update subscriber custom field endpoint for 
            subscriber ${event.subscriber.id}: ${error}${error.cause ? ` (cause: ${error.cause})` : ''}`, 
            subscriberEmail, subscriberNames);
        return {statusCode: successStatusCode};
    }

    if (!kitResponse.ok) {
        await notifyFailure(kitFailEmailSubject, 
            `Kit update subscriber ${event.subscriber.id} with Zoom join URL failed with status ${kitResponse.status} ${kitResponse.statusText}`, 
            subscriberEmail, subscriberNames, kitResponse);
        return throttleAwareResponse(kitResponse, "kit");
    }

    // Everything really did work!
    return {
        statusCode: 200,
    };
};

function splitName(name) {
    let firstName = "";
    let lastName = "";

    if (typeof name === 'string' && name !== '') {
        const cleanName = name.trim();
        
        // Split the full name into parts
        const nameParts = cleanName.split(" ");
        const nameCount = nameParts.length;
        
        if (nameCount > 1) {
            // Get the last element and remove it from array
            lastName = nameParts.pop(); 
            // Rejoin remaining parts with a space
            firstName = nameParts.join(' '); 
            // TODO: Handle 'Von', 'Van', 'zu', 'de', Spanish names etc.
        } else {
            firstName = cleanName;
        }
    }
    return [firstName, lastName];
}

/**
 * Calls to Zoom and Kit API endpoints may fail due to exceeding rate limits (429),
 * or (potentially) temporary server/network issues (500, 502, 503, 504).
 * This function will perform an exponential backoff to retry the request up to `maxRetries` times if the
 * status code is 429 (rate limit), 500 (internal server error), 502 (bad gateway), 
 * 503 (service unavailable, try later), 504 (bad gateway).
 * Added jitter factor to avoid "thundering herd" problem of multiple Lambda instances retrying simultaneously 
 * when under a heavy load of registration request events.
 */
async function fetchWithRetry(url, options, maxRetries = 2, waitMs = 15000) {
    const response = await fetch(url, options);

    if (response.ok) {
        return response;
    }

    const retryableStatusCodes = [429, 500, 502, 503, 504];
    if (retryableStatusCodes.includes(response.status) && maxRetries > 0) {
        const jitter = Math.random() * waitMs * 0.3; // Up to 30% of wait time
        const actualWait = waitMs + jitter;
        console.log(`Request to ${url} failed with status ${response.status}. Retrying in ${(actualWait / 1000).toFixed(1)}s...`);
        await new Promise(resolve => setTimeout(resolve, actualWait));
        return fetchWithRetry(url, options, maxRetries - 1, waitMs * 2);
    }

    return response;
}


/**
 * Decide what to return to the calling Kit webhook after a downstream call has failed.
 * If Kit itself failed with a 429 (rate limited, even after retries), ask Kit to back off and
 * retry later via a 503 response with a Retry-After header, as per Kit's API documentation.
 * 
 * If Zoom failed with a 429, it may be that a per day limit or a per second limit has been 
 * exceeded. Only the per second limit (QPS) is worth retrying, as it will clear next second. Check
 * the x-ratelimit-type header to distinguish QPS from daily limits.
 * 
 * For any other failures, return 200 to Kit regardless of the failure code, since Kit disables a   
 * webhook rule after too many non-2xx responses, and asking it to retry is unlikely to succeed.
 * 
 * All failures, including 429, have been reported via SNS in notifyFailure();
 * 
 * TODO: Retry-After would be better as a configurable setting, rather than hard-coded at 60 seconds
 *
 * @param {Response} response The failed fetch Response object.
 * @param {"zoom"|"kit"} service Which API the response came from.
*/
function throttleAwareResponse(response, service) {
    if (response && response.status === 429) {
        const isRetryable =
            service === "kit" ||
            response.headers?.get("x-ratelimit-type") === "QPS";

        if (isRetryable) {
            return {
                statusCode: 503,
                headers: {
                    "Retry-After": "60",
                },
                body: JSON.stringify({ response: "Service temporarily unavailable, please retry later" }),
            };
        }
    }

    // No point in requesting Kit to throttle - just return success
    // (problem has been logged and reported to admin by email)
    return { statusCode: successStatusCode };
}

/**
 * Send email via SNS when a Zoom or Kit API call fails.
 * If the failed fetch Response is supplied, attempts to read its JSON body and append
 * the API's own error code/message (e.g. Zoom's { code: 3001, message: "Meeting does not exist..." })
 * to the notification.
 */
async function notifyFailure(subject, message, subscriberEmail, subscriberNames, response = null) {
    message += ` while attempting to register ${subscriberNames[0]} ${subscriberNames[1]} ${subscriberEmail}`;

    if (response) {
        try {
            // Clone so the body stream is still available if anything else needs to read it
            const errorBody = await response.clone().json();
            if (errorBody && (errorBody.code !== undefined || errorBody.message !== undefined)) {
                message += ` | API response: code ${errorBody.code}, message: ${errorBody.message}`;
                message += ` | API response body: ${JSON.stringify(errorBody)}`;
            }
        } catch (error) {
            // Response body wasn't JSON, or was empty - nothing useful to add
        }
    }

    console.error(message);
    try {
        await snsClient.send(new PublishCommand({
            TopicArn: FAILURE_TOPIC_ARN,
            Subject: subject,
            Message: message,
        }));
    } catch (error) {
        console.error("Failed to publish SNS notification:", error);
    }
}