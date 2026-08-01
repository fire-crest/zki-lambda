import { SecretsManagerClient, GetSecretValueCommand } from "@aws-sdk/client-secrets-manager";
import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import { DynamoDBDocumentClient, GetCommand } from "@aws-sdk/lib-dynamodb";

const client = new SecretsManagerClient();
const dynamoClient = new DynamoDBClient();
const docClient = DynamoDBDocumentClient.from(dynamoClient);

/* This function is designed to be called from a webhook in a Kit rule.
   Always return 200 (success), even if the function fails, because Kit will disable a webhook rule
   if it fails too frequently.
   Kit support aren't clear about the exact definition of "fails too frequently" but this 
   has happened in production. 
   Contact jo@redkite.biz for more information!
   TODO: Better to alert admin if the function fails.
*/
const returnStatusCode = 200;

export const handler = async (event) => {
    
    // Extract subscriber info from event
    const subscriberNames = splitName(event.subscriber.first_name);
 	
	// Zoom will reject empty name fields
    if (subscriberNames[0] === '') {
        subscriberNames[0] = "-";
    }
    if (subscriberNames[1] === '') {
        subscriberNames[1] = "-";
    }
    // Fetch the webinar ID from DynamoDB
    let dbResponse;
    try {
        dbResponse = await docClient.send(
            new GetCommand({
                TableName: "zoom-kit-parameters",
                Key: { settingID: "zoom-kit-parameters" },
            })
        );
    } catch (error) {
        console.error("Failed to retrieve parameters from DynamoDB: ", error);
        return returnStatusCode;
    }
    if (!dbResponse.Item) {
        console.error("No Item found in DynamoDB for key 'zoom-kit-parameters'");
        return returnStatusCode;
    }
    const webinarId = dbResponse.Item.webinarId;

    // Retrieve the Zoom credentials from Secrets Manager
    let secretResponse, zoomClient, zoomAccount;
    try {
        secretResponse = await client.send(
            new GetSecretValueCommand({ SecretId: "zoom-kit-integration/settings" })
        );
        ({ zkiZoomClient: zoomClient, zkiZoomAccount: zoomAccount } = JSON.parse(secretResponse.SecretString));
    } catch (error) {
        console.error("Failed to retrieve Zoom credentials from Secrets Manager: ", error);
        return returnStatusCode;
    }

    if (!zoomClient || !zoomAccount) {
        console.error("Zoom credentials missing from Secrets Manager response");
        return returnStatusCode;
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
        console.error("Network error calling Zoom OAuth endpoint: ", error);
        return returnStatusCode;
    }

    if (!tokenResponse.ok) {
        console.error(`Zoom OAuth token request failed with status ${tokenResponse.status} ${tokenResponse.statusText}`);
        return returnStatusCode;
    }
    
    const tokenData = await tokenResponse.json();
    const accessToken = tokenData.access_token;
    if (!accessToken) {
        console.error("Zoom OAuth response did not contain an access token:", tokenData);
        return returnStatusCode;
    }

    console.log(event.subscriber.email_address);
    console.log(subscriberNames[0]);
    console.log(subscriberNames[1]);
 
    options = {
        method: "POST",
        headers: {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": `Bearer ${accessToken}`,
        },
        body: JSON.stringify({
            email: event.subscriber.email_address,
            first_name: subscriberNames[0],
            last_name: subscriberNames[1],
        }),
    };
    let meetingsResponse;
    try {
        meetingsResponse = await fetchWithRetry(`https://api.zoom.us/v2/meetings/${webinarId}/registrants`, options);    
    } catch (error) {
        console.error("Network error calling Zoom registrants endpoint: ", error);
        return returnStatusCode;
    }

    if (!meetingsResponse.ok) {
        console.error(`Zoom add registrant to meeting/webinar request failed with status ${meetingsResponse.status}`);
        return returnStatusCode;
    }
    
    const meetingData = await meetingsResponse.json();
    const joinURL = meetingData.join_url;

    if (!joinURL) {
        console.error("Zoom add registrant response did not contain a join URL:", meetingData);
        return returnStatusCode;
    }

    // Send the joinURL to Kit to store in the correct custom field:
    // Retrieve the Kit credentials from the Secrets Manager response
    const { zkiKitKey: kitKey } = JSON.parse(secretResponse.SecretString);
    // Fetch the Kit custom field from the DynamoDB response
    const kitJoinURLField = dbResponse.Item.kitJoinURLField;
    await fetch(`https://api.kit.com/v4/subscribers/${event.subscriber.id}`, {
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
    });
 
    // TODO Return something more sensible
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