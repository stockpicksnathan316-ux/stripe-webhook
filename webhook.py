import os
import json
from flask import Flask, request, jsonify
import stripe
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

app = Flask(__name__)

# Stripe webhook secret
endpoint_secret = os.getenv('STRIPE_WEBHOOK_SECRET')

# Supabase credentials
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

@app.route('/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, endpoint_secret
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except stripe.error.SignatureVerificationError as e:
        return jsonify({'error': str(e)}), 400

    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        customer_email = session.get('customer_email')
        
        if customer_email:
            try:
                # Check if user already exists
                existing = supabase.table('paid_users') \
                    .select('*') \
                    .eq('email', customer_email) \
                    .execute()
                
                if existing.data:
                    # Update existing user
                    supabase.table('paid_users') \
                        .update({'is_pro': True}) \
                        .eq('email', customer_email) \
                        .execute()
                    print(f"✅ Updated {customer_email} to Pro in Supabase")
                else:
                    # Insert new user
                    data = {
                        'email': customer_email,
                        'is_pro': True,
                        'stripe_customer_id': session.get('customer')
                    }
                    supabase.table('paid_users').insert(data).execute()
                    print(f"✅ Inserted {customer_email} as Pro in Supabase")
                    
            except Exception as db_error:
                print(f"⚠️ Database error: {db_error}")
                return jsonify({'error': str(db_error)}), 500
        else:
            print("⚠️ No customer email in session")

    return jsonify({'status': 'success'}), 200

@app.route('/check', methods=['GET'])
def check_pro():
    email = request.args.get('email')
    if not email:
        return jsonify({'error': 'email required'}), 400
    
    try:
        result = supabase.table('paid_users') \
            .select('is_pro') \
            .eq('email', email) \
            .execute()
        
        if result.data and len(result.data) > 0:
            return jsonify({'pro': result.data[0]['is_pro']})
        else:
            return jsonify({'pro': False})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(port=5000)