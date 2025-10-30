
from flask import Flask, request, jsonify
import os
import time
import jwt
import requests
from typing import Tuple
import json

app = Flask(__name__)

# Load from environment variables
HUB_CLIENT_ID = os.environ.get("HUB_CLIENT_ID")
HUB_USERNAME = os.environ.get("HUB_USERNAME")
HUB_PRIVATE_KEY = os.environ.get("HUB_PRIVATE_KEY")  # Store the key content directly
HUB_DOMAIN = os.environ.get("HUB_DOMAIN", "test")

def get(endpoint, headers, instance_url):
    url = f"{instance_url}{endpoint}"
    response = requests.get(url, headers=headers)
    return response

def post(endpoint, headers, instance_url, data):
    url = f"{instance_url}{endpoint}"
    response = requests.post(url, headers=headers, json=data)
    return response

def put(endpoint, headers, instance_url, data):
    url = f"{instance_url}{endpoint}"
    response = requests.put(url, headers=headers, json=data)
    return response

def getByNPI(headers, instance_url, npi):
    response = get(f"/services/apexrest/MercyHealthOrgAPI/npi/{npi}", headers, instance_url)
    if response.status_code == 200:
        return response.json()
    return None

def build_jwt(client_id: str, username: str, private_key: str) -> str:
    payload = {
        "iss": client_id,
        "sub": username,
        "aud": f"https://{HUB_DOMAIN}.salesforce.com",
        "exp": int(time.time()) + 300,
    }
    return jwt.encode(payload, private_key, algorithm="RS256")

def jwt_authenticate_HUB() -> Tuple[str, str]:   
    SF_AUTH_URL = f"https://{HUB_DOMAIN}.salesforce.com/services/oauth2/token"
    
    # Use private key directly from environment variable
    jwt_token = build_jwt(HUB_CLIENT_ID, HUB_USERNAME, HUB_PRIVATE_KEY)

    response = requests.post(SF_AUTH_URL, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt_token,
    })

    if response.status_code != 200:
        raise RuntimeError(f"Salesforce JWT auth failed: {response.status_code} {response.text}")

    data = response.json()
    return data["access_token"], data["instance_url"]

@app.route('/webhook/hubspot', methods=['POST'])
def hubspot_webhook():
    try:
        # Get data from HubSpot webhook
        webhook_data = request.json
        
        # Log incoming data
        app.logger.info(f"Received webhook data: {json.dumps(webhook_data)}")
        
        # Extract NPI from webhook data (adjust field name as needed)
        npi = webhook_data.get('NPI__c') or webhook_data.get('npi')
        
        if not npi:
            return jsonify({
                "status": "error",
                "message": "NPI is required"
            }), 400
        
        # Authenticate with Salesforce
        access_token, instance_url = jwt_authenticate_HUB()
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        
        # Check if record exists by NPI
        existing_records = getByNPI(headers, instance_url, npi)
        
        # Prepare record data (map HubSpot fields to Salesforce fields)
        record_data = {
            "City__c": webhook_data.get("City__c") or webhook_data.get("city"),
            "Country__c": webhook_data.get("Country__c") or webhook_data.get("country"),
            "Healthcare_Organization_Name__c": webhook_data.get("Healthcare_Organization_Name__c") or webhook_data.get("organization_name"),
            "NPI__c": npi,
            "Phone_Number__c": webhook_data.get("Phone_Number__c") or webhook_data.get("phone"),
            "Provider_Name__c": webhook_data.get("Provider_Name__c") or webhook_data.get("provider_name"),
            "Secure_Email__c": webhook_data.get("Secure_Email__c") or webhook_data.get("email"),
            "Secure_Fax_Number__c": webhook_data.get("Secure_Fax_Number__c") or webhook_data.get("fax"),
            "State__c": webhook_data.get("State__c") or webhook_data.get("state"),
            "Street__c": webhook_data.get("Street__c") or webhook_data.get("street")
        }
        
        # Remove None values
        record_data = {k: v for k, v in record_data.items() if v is not None}
        
        if existing_records and len(existing_records) > 0:
            # Update existing record (use first match)
            record_id = existing_records[0]['Id']
            response = put(f"/services/apexrest/MercyHealthOrgAPI/{record_id}", 
                          headers, instance_url, record_data)
            
            if response.status_code == 200:
                return jsonify({
                    "status": "success",
                    "action": "updated",
                    "data": response.json()
                }), 200
            else:
                return jsonify({
                    "status": "error",
                    "action": "update_failed",
                    "message": response.text
                }), response.status_code
        else:
            # Create new record
            response = post(f"/services/apexrest/MercyHealthOrgAPI", 
                           headers, instance_url, record_data)
            
            if response.status_code in [200, 201]:
                return jsonify({
                    "status": "success",
                    "action": "created",
                    "data": response.json()
                }), 201
            else:
                return jsonify({
                    "status": "error",
                    "action": "create_failed",
                    "message": response.text
                }), response.status_code
                
    except Exception as e:
        app.logger.error(f"Error processing webhook: {str(e)}")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)