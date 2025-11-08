from flask import Flask, request, jsonify, render_template_string
import os
import time
import jwt
import requests
from typing import Tuple
import json
from datetime import datetime

app = Flask(__name__)

# Load from environment variables
HUB_CLIENT_ID = os.environ.get("HUB_CLIENT_ID")
HUB_USERNAME = os.environ.get("HUB_USERNAME")
HUB_PRIVATE_KEY = os.environ.get("HUB_PRIVATE_KEY")  # Store the key content directly
HUB_DOMAIN = os.environ.get("HUB_DOMAIN", "test")

# Store webhook logs in memory (last 50 requests)
webhook_logs = []
MAX_LOGS = 50

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

def getAll(headers, instance_url):
    response = get(f"/services/apexrest/MercyHealthOrgAPI", headers, instance_url)
    if response.status_code == 200:
        return json.loads(response.text)
    return False

def getSamplesForNonInternalBatches(headers, instance_url, start_date, end_date):
    response = get(f"/services/apexrest/hubspotintegration/getsamplesfornoninternalbatches?start={start_date}&end={end_date}", headers, instance_url)
    if response.status_code == 200:
        return json.loads(response.text)
    return False

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

def add_log(log_entry):
    """Add a log entry and maintain max size"""
    webhook_logs.insert(0, log_entry)  # Add to beginning
    if len(webhook_logs) > MAX_LOGS:
        webhook_logs.pop()  # Remove oldest

@app.route('/webhook/hubspot', methods=['POST'])
def hubspot_webhook():
    log_entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "received_data": None,
        "processed_data": None,
        "action": None,
        "status": None,
        "message": None,
        "salesforce_response": None
    }
    
    try:
        # Get data from HubSpot webhook
        webhook_data = request.json
        log_entry["received_data"] = webhook_data
        
        # Print/log incoming data
        print("=" * 80)
        print("RECEIVED WEBHOOK DATA:")
        print(json.dumps(webhook_data, indent=2))
        print("=" * 80)
        
        # Extract NPI from webhook data (adjust field name as needed)
        npi = webhook_data.get('NPI__c') or webhook_data.get('npi')
        
        if not npi:
            log_entry["status"] = "error"
            log_entry["message"] = "NPI is required"
            add_log(log_entry)
            return jsonify({
                "status": "error",
                "message": "NPI is required",
                "received_data": webhook_data
            }), 400
        
        # Authenticate with Salesforce
        access_token, instance_url = jwt_authenticate_HUB()
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        
        # Check if record exists by NPI
        existing_records = getByNPI(headers, instance_url, npi)
        
        print(f"Existing records found: {len(existing_records) if existing_records else 0}")
        
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
            "Street__c": webhook_data.get("Street__c") or webhook_data.get("street"),
            "ZipCode__c": webhook_data.get("ZipCode__c") or webhook_data.get("zip"),
            "Preferred_Contact_Method__c": webhook_data.get("Preferred_Contact_Method__c") or webhook_data.get("preferred_contact_method"),
        }
        
        # Remove None values
        record_data = {k: v for k, v in record_data.items() if v is not None}
        log_entry["processed_data"] = record_data
        
        print(f"Prepared record data: {json.dumps(record_data, indent=2)}")
        
        if existing_records and len(existing_records) > 0:
            # Update existing record (use first match)
            record_id = existing_records[0]['Id']
            print(f"Updating record ID: {record_id}")
            
            response = put(f"/services/apexrest/MercyHealthOrgAPI/{record_id}", 
                          headers, instance_url, record_data)
            
            if response.status_code == 200:
                log_entry["status"] = "success"
                log_entry["action"] = "updated"
                log_entry["message"] = f"Record {record_id} updated successfully"
                log_entry["salesforce_response"] = response.json()
                add_log(log_entry)
                
                result = {
                    "status": "success",
                    "action": "updated",
                    "record_id": record_id,
                    "received_data": webhook_data,
                    "processed_data": record_data,
                    "salesforce_response": response.json()
                }
                print(f"Update successful: {json.dumps(result, indent=2)}")
                return jsonify(result), 200
            else:
                log_entry["status"] = "error"
                log_entry["action"] = "update_failed"
                log_entry["message"] = response.text
                add_log(log_entry)
                
                result = {
                    "status": "error",
                    "action": "update_failed",
                    "received_data": webhook_data,
                    "processed_data": record_data,
                    "message": response.text
                }
                print(f"Update failed: {json.dumps(result, indent=2)}")
                return jsonify(result), response.status_code
        else:
            # Create new record
            print("Creating new record")
            
            response = post(f"/services/apexrest/MercyHealthOrgAPI", 
                           headers, instance_url, record_data)
            
            if response.status_code in [200, 201]:
                log_entry["status"] = "success"
                log_entry["action"] = "created"
                log_entry["message"] = "New record created successfully"
                log_entry["salesforce_response"] = response.json()
                add_log(log_entry)
                
                result = {
                    "status": "success",
                    "action": "created",
                    "received_data": webhook_data,
                    "processed_data": record_data,
                    "salesforce_response": response.json()
                }
                print(f"Create successful: {json.dumps(result, indent=2)}")
                return jsonify(result), 201
            else:
                log_entry["status"] = "error"
                log_entry["action"] = "create_failed"
                log_entry["message"] = response.text
                add_log(log_entry)
                
                result = {
                    "status": "error",
                    "action": "create_failed",
                    "received_data": webhook_data,
                    "processed_data": record_data,
                    "message": response.text
                }
                print(f"Create failed: {json.dumps(result, indent=2)}")
                return jsonify(result), response.status_code
                
    except Exception as e:
        log_entry["status"] = "error"
        log_entry["message"] = str(e)
        add_log(log_entry)
        
        error_result = {
            "status": "error",
            "message": str(e),
            "received_data": request.json if request.json else None
        }
        print(f"Exception occurred: {json.dumps(error_result, indent=2)}")
        return jsonify(error_result), 500

@app.route('/api/all', methods=['GET'])
def all_records():
    """Get all health organization records"""
    try:
        access_token, instance_url = jwt_authenticate_HUB()
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        
        if not access_token or not instance_url:
            return jsonify({
                "status": "error",
                "message": "❌ Couldn't authenticate with Salesforce"
            }), 401
        
        records = getAll(headers, instance_url)
        
        if records is False:
            return jsonify({
                "status": "error",
                "message": "Failed to retrieve records"
            }), 500
        
        return jsonify({
            "status": "success",
            "count": len(records) if records else 0,
            "data": records
        }), 200
        
    except Exception as e:
        app.logger.error(f"Error retrieving all records: {str(e)}")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

@app.route('/api/samples', methods=['GET'])
def get_samples():
    """Get samples for non-internal batches by date range
    
    Query parameters:
    - start: Start date (YYYY-MM-DD) - required
    - end: End date (YYYY-MM-DD) - required
    
    Example: /api/samples?start=2025-09-25&end=2025-10-25
    """
    try:
        # Get date parameters from query string
        start_date = request.args.get('start')
        end_date = request.args.get('end')
        
        # Validate required parameters
        if not start_date or not end_date:
            return jsonify({
                "status": "error",
                "message": "Both 'start' and 'end' date parameters are required",
                "example": "/api/samples?start=2025-09-25&end=2025-10-25"
            }), 400
        
        # Validate date format (basic check)
        try:
            datetime.strptime(start_date, '%Y-%m-%d')
            datetime.strptime(end_date, '%Y-%m-%d')
        except ValueError:
            return jsonify({
                "status": "error",
                "message": "Invalid date format. Use YYYY-MM-DD",
                "example": "/api/samples?start=2025-09-25&end=2025-10-25"
            }), 400
        
        # Authenticate with Salesforce
        access_token, instance_url = jwt_authenticate_HUB()
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        
        if not access_token or not instance_url:
            return jsonify({
                "status": "error",
                "message": "❌ Couldn't authenticate with Salesforce"
            }), 401
        
        # Get samples data
        samples = getSamplesForNonInternalBatches(headers, instance_url, start_date, end_date)
        
        if samples is False:
            return jsonify({
                "status": "error",
                "message": "Failed to retrieve samples"
            }), 500
        
        return jsonify({
            "status": "success",
            "start_date": start_date,
            "end_date": end_date,
            "count": len(samples) if samples else 0,
            "data": samples
        }), 200
        
    except Exception as e:
        app.logger.error(f"Error retrieving samples: {str(e)}")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

@app.route('/logs', methods=['GET'])
def view_logs():
    """Display webhook logs on a web page"""
    html_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Webhook Logs</title>
        <meta http-equiv="refresh" content="30">
        <style>
            body {
                font-family: Arial, sans-serif;
                margin: 20px;
                background-color: #f5f5f5;
            }
            h1 {
                color: #333;
            }
            .log-entry {
                background-color: white;
                border-radius: 8px;
                padding: 15px;
                margin-bottom: 20px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }
            .log-header {
                display: flex;
                justify-content: space-between;
                margin-bottom: 10px;
                padding-bottom: 10px;
                border-bottom: 2px solid #eee;
            }
            .timestamp {
                color: #666;
                font-size: 14px;
            }
            .status {
                padding: 5px 10px;
                border-radius: 4px;
                font-weight: bold;
                font-size: 12px;
            }
            .status-success {
                background-color: #d4edda;
                color: #155724;
            }
            .status-error {
                background-color: #f8d7da;
                color: #721c24;
            }
            .action {
                color: #007bff;
                font-weight: bold;
                margin-bottom: 10px;
            }
            .message {
                color: #333;
                margin-bottom: 10px;
            }
            pre {
                background-color: #f8f9fa;
                padding: 10px;
                border-radius: 4px;
                overflow-x: auto;
                font-size: 12px;
            }
            .section-title {
                font-weight: bold;
                color: #555;
                margin-top: 10px;
                margin-bottom: 5px;
            }
            .no-logs {
                text-align: center;
                color: #999;
                padding: 40px;
            }
            .refresh-info {
                color: #666;
                font-size: 14px;
                margin-bottom: 20px;
            }
        </style>
    </head>
    <body>
        <h1>🔔 Webhook Logs</h1>
        <div class="refresh-info">Auto-refreshes every 30 seconds | Showing last {{ logs|length }} requests</div>
        
        {% if logs %}
            {% for log in logs %}
            <div class="log-entry">
                <div class="log-header">
                    <span class="timestamp">⏰ {{ log.timestamp }}</span>
                    <span class="status status-{{ log.status }}">{{ log.status|upper }}</span>
                </div>
                
                {% if log.action %}
                <div class="action">📝 Action: {{ log.action|upper }}</div>
                {% endif %}
                
                {% if log.message %}
                <div class="message">💬 {{ log.message }}</div>
                {% endif %}
                
                {% if log.received_data %}
                <div class="section-title">📥 Received Data:</div>
                <pre>{{ log.received_data|tojson(indent=2) }}</pre>
                {% endif %}
                
                {% if log.processed_data %}
                <div class="section-title">⚙️ Processed Data:</div>
                <pre>{{ log.processed_data|tojson(indent=2) }}</pre>
                {% endif %}
                
                {% if log.salesforce_response %}
                <div class="section-title">✅ Salesforce Response:</div>
                <pre>{{ log.salesforce_response|tojson(indent=2) }}</pre>
                {% endif %}
            </div>
            {% endfor %}
        {% else %}
            <div class="no-logs">No webhook requests received yet</div>
        {% endif %}
    </body>
    </html>
    """
    return render_template_string(html_template, logs=webhook_logs)

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"}), 200

@app.route('/')
def index():
    """Home page with links"""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>HubSpot Webhook Service</title>
        <style>
            body {
                font-family: Arial, sans-serif;
                max-width: 800px;
                margin: 50px auto;
                padding: 20px;
                background-color: #f5f5f5;
            }
            .container {
                background-color: white;
                padding: 30px;
                border-radius: 8px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }
            h1 {
                color: #333;
            }
            .endpoint {
                background-color: #f8f9fa;
                padding: 15px;
                margin: 10px 0;
                border-radius: 4px;
                border-left: 4px solid #007bff;
            }
            .method {
                display: inline-block;
                padding: 3px 8px;
                background-color: #007bff;
                color: white;
                border-radius: 3px;
                font-size: 12px;
                font-weight: bold;
                margin-right: 10px;
            }
            .method-get {
                background-color: #28a745;
            }
            a {
                color: #007bff;
                text-decoration: none;
            }
            a:hover {
                text-decoration: underline;
            }
            code {
                background-color: #f8f9fa;
                padding: 2px 6px;
                border-radius: 3px;
                font-size: 13px;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🚀 HubSpot Webhook Service</h1>
            <p>Welcome to the HubSpot to Salesforce webhook integration service.</p>
            
            <h2>Available Endpoints:</h2>
            
            <div class="endpoint">
                <span class="method">POST</span>
                <strong>/webhook/hubspot</strong>
                <p>Receives webhook data from HubSpot and creates/updates Salesforce records</p>
            </div>
            
            <div class="endpoint">
                <span class="method method-get">GET</span>
                <strong><a href="/logs">/logs</a></strong>
                <p>View webhook request logs in real-time</p>
            </div>
            
            <div class="endpoint">
                <span class="method method-get">GET</span>
                <strong><a href="/api/all">/api/all</a></strong>
                <p>Retrieve all health organization records from Salesforce</p>
            </div>
            
            <div class="endpoint">
                <span class="method method-get">GET</span>
                <strong>/api/samples</strong>
                <p>Get samples for non-internal batches by date range</p>
                <p><strong>Parameters:</strong> <code>start</code> and <code>end</code> (YYYY-MM-DD)</p>
                <p><strong>Example:</strong> <a href="/api/samples?start=2025-09-25&end=2025-10-25">/api/samples?start=2025-09-25&end=2025-10-25</a></p>
            </div>
            
            <div class="endpoint">
                <span class="method method-get">GET</span>
                <strong><a href="/health">/health</a></strong>
                <p>Health check endpoint</p>
            </div>
        </div>
    </body>
    </html>
    """
    return html

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)