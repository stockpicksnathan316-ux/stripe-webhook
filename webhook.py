import os
import stripe
import threading
import logging
import requests
from datetime import datetime
from flask import Flask, request, jsonify
from supabase import create_client
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)

# CORS headers (manual)
@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = 'https://stockpicksnathan316-ux.github.io'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
    return response

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment variables
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_SERVICE_KEY = os.getenv('SUPABASE_SERVICE_KEY')
STRIPE_API_KEY = os.getenv('STRIPE_API_KEY')
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')
BREVO_API_KEY = os.getenv('BREVO_API_KEY')
BREVO_LIST_ID = os.getenv('BREVO_LIST_ID', '3')

# Initialize Stripe and Supabase
stripe.api_key = STRIPE_API_KEY
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

def ensure_numeric(df):
    """Convert DataFrame to float32, replace inf, fill NaN with 0."""
    df = df.astype('float32')
    df = df.replace([float('inf'), -float('inf')], 0.0)
    df = df.fillna(0.0)
    return df

def process_event_async(event):
    """Process the webhook event in a background thread."""
    try:
        event_type = event['type']
        logger.info(f"Processing event: {event_type}")
        
        if event_type == 'checkout.session.completed':
            session = event['data']['object']
            email = session['customer_details']['email']
            customer_id = session['customer'] if 'customer' in session else None
            
            result = supabase.table('paid_users').upsert({
                'email': email,
                'is_pro': True,
                'stripe_customer_id': customer_id
            }, on_conflict='email').execute()
            logger.info(f"Upsert result: {result}")
            logger.info(f"✅ Pro access granted to {email}")
            
        elif event_type == 'customer.subscription.deleted':
            sub = event['data']['object']
            customer_id = sub['customer']
            supabase.table('paid_users').update({'is_pro': False}).eq('stripe_customer_id', customer_id).execute()
            logger.info(f"❌ Pro access revoked for customer {customer_id}")
        
        elif event_type == 'invoice.payment_failed':
            invoice = event['data']['object']
            customer_id = invoice['customer'] if 'customer' in invoice else None
            if customer_id:
                supabase.table('paid_users').update({'is_pro': False}).eq('stripe_customer_id', customer_id).execute()
                logger.warning(f"⚠️ Payment failed for customer {customer_id}. Pro access revoked.")
            else:
                logger.error("Invoice.payment_failed event missing customer ID")
        
        else:
            logger.info(f"ℹ️ Unhandled event type: {event_type}")
            
    except Exception as e:
        import traceback
        logger.error(f"Background processing error: {e}")
        logger.error(traceback.format_exc())

@app.route('/webhook', methods=['POST'])
def webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError as e:
        logger.error(f"Invalid payload: {e}")
        return jsonify({'error': 'Invalid payload'}), 400
    except stripe.error.SignatureVerificationError as e:
        logger.error(f"Signature verification failed: {e}")
        return jsonify({'error': 'Signature verification failed'}), 400
    
    threading.Thread(target=process_event_async, args=(event,)).start()
    return jsonify({'status': 'success'}), 200

# ------------------------- Brevo subscription endpoint -------------------------
@app.route('/subscribe', methods=['POST', 'OPTIONS'])
def subscribe():
    # Handle preflight OPTIONS request
    if request.method == 'OPTIONS':
        return '', 200

    # Handle POST request
    data = request.get_json()
    if not data or 'email' not in data:
        return jsonify({'error': 'Email missing'}), 400

    email = data['email']
    utm_source = data.get('utm_source', 'direct')
    utm_medium = data.get('utm_medium', 'organic')
    utm_campaign = data.get('utm_campaign', 'landing_page')
    
    if not BREVO_API_KEY:
        logger.error("BREVO_API_KEY not set in environment")
        return jsonify({'error': 'Server configuration error'}), 500
    
    brevo_payload = {
        "email": email,
        "emailBlacklisted": False,
        "smsBlacklisted": False,
        "updateEnabled": True,
        "listIds": [int(BREVO_LIST_ID)],
        "attributes": {
            "UTM_SOURCE": utm_source,
            "UTM_MEDIUM": utm_medium,
            "UTM_CAMPAIGN": utm_campaign
        }
    }
    
    headers = {
        'Content-Type': 'application/json',
        'api-key': BREVO_API_KEY
    }
    
    try:
        response = requests.post('https://api.brevo.com/v3/contacts', 
                                 json=brevo_payload, 
                                 headers=headers, 
                                 timeout=10)
        if response.status_code in (201, 204):
            return jsonify({'status': 'subscribed'}), 200
        elif response.status_code == 400 and 'duplicate' in response.text:
            return jsonify({'status': 'already_exists'}), 200
        else:
            logger.error(f"Brevo error: {response.status_code} - {response.text}")
            return jsonify({'error': 'Brevo API error'}), 500
    except requests.exceptions.RequestException as e:
        logger.error(f"Request to Brevo failed: {e}")
        return jsonify({'error': 'Network error'}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'}), 200

if __name__ == '__main__':
    app.run(port=4242)