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
    
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    
    # Debug: print first 50 chars of signature
    if sig_header:
        print(f"📝 Signature (first 50 chars): {sig_header[:50]}...")
    else:
        print("⚠️ No Stripe-Signature header")
    
    # Verify signature
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except ValueError as e:
        print(f"❌ Invalid payload: {e}")
        print(f"Payload preview: {payload[:200]}...")
        return jsonify({'error': 'Invalid payload'}), 400
    except stripe.error.SignatureVerificationError as e:
        print(f"❌ Signature verification failed: {e}")
        print(f"Endpoint secret used (first 10 chars): {endpoint_secret[:10] if endpoint_secret else 'None'}...")
        return jsonify({'error': 'Signature verification failed'}), 400
    
    print(f"✅ Event verified: {event['type']}")
    
    # Process in background to respond quickly
    threading.Thread(target=process_event_async, args=(event,)).start()
    
    return jsonify({'status': 'success'}), 200

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'}), 200

if __name__ == '__main__':
    app.run(port=4242)