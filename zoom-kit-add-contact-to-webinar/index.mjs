import { SecretsManagerClient, GetSecretValueCommand } from "@aws-sdk/client-secrets-manager";
import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import { DynamoDBDocumentClient, GetCommand } from "@aws-sdk/lib-dynamodb";

const client = new SecretsManagerClient();
const dynamoClient = new DynamoDBClient();
const docClient = DynamoDBDocumentClient.from(dynamoClient);


export const handler = async (event) => {
    
    // Extract subscriber info from event
    const subscriberNames = splitName(event.subscriber.first_name);
 	
	// Zoom will not accept empty name fields
    if (subscriberNames[0] === '') {
        subscriberNames[0] = "-";
    }
    if (subscriberNames[1] === '') {
        subscriberNames[1] = "-";
    }
    // Fetch the webinar ID from DynamoDB
    const settingsResponse = await docClient.send(
        new GetCommand({
            TableName: "zoom-kit-parameters",
            Key: { settingID: "zoom-kit-parameters" },
            })
    );
    const webinarId = settingsResponse.Item.webinarId;

    // Retrieve the Zoom credentials from Secrets Manager
    const secretResponse = await client.send(
        new GetSecretValueCommand({ SecretId: "zoom-kit-integration/settings" })
    );
    const { zkiZoomClient: zoomClient, zkiZoomAccount: zoomAccount } = JSON.parse(secretResponse.SecretString);

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
    const kitJoinURLField = settingsResponse.Item.kitJoinURLField;
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
