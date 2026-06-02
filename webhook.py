import os
import stripe
import threading
import logging
from datetime import datetime
from flask import Flask, request, jsonify
from supabase import create_client
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment variables
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_SERVICE_KEY = os.getenv('SUPABASE_SERVICE_KEY')
STRIPE_API_KEY = os.getenv('STRIPE_API_KEY')
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')

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
            customer_id = session.get('customer')
            
            # Upsert with conflict handling on email
            supabase.table('paid_users').upsert({
                'email': email,
                'is_pro': True,
                'stripe_customer_id': customer_id,
                #'updated_at': datetime.utcnow().isoformat()   # remove or comment out
            }, on_conflict='email').execute()
            
            logger.info(f"✅ Pro access granted to {email}")
            
        elif event_type == 'customer.subscription.deleted':
            sub = event['data']['object']
            customer_id = sub['customer']
            supabase.table('paid_users').update({'is_pro': False}).eq('stripe_customer_id', customer_id).execute()
            logger.info(f"❌ Pro access revoked for customer {customer_id}")
            
        else:
            logger.info(f"ℹ️ Unhandled event type: {event_type}")
            
    except Exception as e:
        # Log error but don't raise – we already responded to Stripe
        logger.error(f"Background processing error: {str(e)}")

@app.route('/webhook', methods=['POST'])
def webhook():
    # Get raw payload and signature header
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    
    # Verify signature
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError as e:
        logger.error(f"Invalid payload: {e}")
        return jsonify({'error': 'Invalid payload'}), 400
    except stripe.error.SignatureVerificationError as e:
        logger.error(f"Signature verification failed: {e}")
        return jsonify({'error': 'Signature verification failed'}), 400
    
    # Start background thread to process event
    threading.Thread(target=process_event_async, args=(event,)).start()
    
    # Respond immediately to Stripe
    return jsonify({'status': 'success'}), 200

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'}), 200

if __name__ == '__main__':
    app.run(port=4242)