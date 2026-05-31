import os
import stripe
from flask import Flask, request, jsonify
from supabase import create_client
from dotenv import load_dotenv
from datetime import datetime
import threading

load_dotenv()

app = Flask(__name__)

# Initialize Supabase
supabase_url = os.getenv('SUPABASE_URL')
supabase_key = os.getenv('SUPABASE_SERVICE_KEY')
stripe.api_key = os.getenv('STRIPE_API_KEY')
endpoint_secret = os.getenv('STRIPE_WEBHOOK_SECRET')

supabase = create_client(supabase_url, supabase_key)
print(f"SUPABASE_URL = {os.getenv('SUPABASE_URL')}")
print(f"SUPABASE_SERVICE_KEY (first 10 chars) = {os.getenv('SUPABASE_SERVICE_KEY')[:10]}...")

def process_event_async(event):
    """Process webhook events in background to avoid timeout"""
    try:
        if event['type'] == 'checkout.session.completed':
            session = event['data']['object']
            email = session['customer_details']['email']
            customer_id = session.get('customer')
            print(f"📧 Email: {email}")
            print(f"🆔 Customer ID: {customer_id}")
            
            supabase.table('paid_users').upsert({
                'email': email,
                'is_pro': True,
                'stripe_customer_id': customer_id,
                'created_at': datetime.utcnow().isoformat()
            }).execute()
            
            print(f"✅ Pro access granted to {email}")
            
        elif event['type'] == 'customer.subscription.deleted':
            sub = event['data']['object']
            customer_id = sub['customer']
            supabase.table('paid_users').update({'is_pro': False}).eq('stripe_customer_id', customer_id).execute()
            print(f"❌ Pro access revoked for customer {customer_id}")
        else:
            print(f"ℹ️ Unhandled event: {event['type']}")
    except Exception as e:
        print(f"❌ Background processing error: {e}")

@app.route('/webhook', methods=['POST'])
def webhook():
    print("🔔 Webhook received!")
    
    # Get raw payload for debugging
    payload = request.get_data(as_text=True)
    print(f"Raw payload (first 300 chars): {payload[:300]}")
    
    # Skip signature verification for now – just parse JSON
    event = request.get_json()
    print(f"Parsed event type: {event.get('type')}")
    
    # Process the event directly (no background thread for simplicity)
    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        email = session['customer_details']['email']
        customer_id = session.get('customer')
        print(f"📧 Email: {email}")
        print(f"🆔 Customer ID: {customer_id}")
        
        supabase.table('paid_users').upsert({
            'email': email,
            'is_pro': True,
            'stripe_customer_id': customer_id,
            'created_at': datetime.utcnow().isoformat()
        }).execute()
        
        print(f"✅ Pro access granted to {email}")
        
    elif event['type'] == 'customer.subscription.deleted':
        sub = event['data']['object']
        customer_id = sub['customer']
        supabase.table('paid_users').update({'is_pro': False}).eq('stripe_customer_id', customer_id).execute()
        print(f"❌ Pro access revoked for customer {customer_id}")
    else:
        print(f"ℹ️ Unhandled event: {event['type']}")
    
    return jsonify({'status': 'success'}), 200

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'}), 200

if __name__ == '__main__':
    app.run(port=4242)