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
    const tokenResponse = await fetch("https://zoom.us/oauth/token", {
        method: "POST",
        headers: {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": `Basic ${zoomClient}`,
        },
        body: new URLSearchParams({
            grant_type: "account_credentials",
            account_id: zoomAccount,
        }),
    });

    const tokenData = await tokenResponse.json();
    // TODO Handle failure case
    // Assume it worked...
    const accessToken = tokenData.access_token;

    console.log(event.subscriber.email_address);
    console.log(subscriberNames[0]);
    console.log(subscriberNames[1]);
 
    const meetingsResponse = await fetch(`https://api.zoom.us/v2/meetings/${webinarId}/registrants`, {
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
    });
    
    const meetingData = await meetingsResponse.json();
    // console.log(meetingData.join_url);

    // Send the joinURL to Kit to store in the correct custom field:
    // Retrieve the Kit credentials from the Secrets Manager response
    const { zkiKitKey: kitKey } = JSON.parse(secretResponse.SecretString);
    // Fetch the Kit custom field from the DynamoDB response
    const kitJoinURLField = dbResponse.Item.kitJoinURLField;
    //console.log(kitJoinURLField);
    //console.log(meetingData.join_url);
    await fetch(`https://api.kit.com/v4/subscribers/${event.subscriber.id}`, {
        method: "PUT",
        headers: {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Kit-Api-Key": kitKey,
        },
        body: JSON.stringify({
            fields: {
                [kitJoinURLField]: meetingData.join_url,
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
