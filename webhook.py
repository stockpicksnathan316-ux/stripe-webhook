import os
import json
from flask import Flask, request, jsonify
import stripe
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Stripe webhook secret (from Stripe CLI or Dashboard)
endpoint_secret = os.getenv('STRIPE_WEBHOOK_SECRET')

# Simple JSON file "database"
DB_FILE = 'users.json'

def read_db():
    try:
        with open(DB_FILE, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def write_db(data):
    with open(DB_FILE, 'w') as f:
        json.dump(data, f, indent=2)

@app.route('/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, endpoint_secret
        )
    except ValueError as e:
        # Invalid payload
        return jsonify({'error': str(e)}), 400
    except stripe.error.SignatureVerificationError as e:
        # Invalid signature
        return jsonify({'error': str(e)}), 400

    # Handle the checkout.session.completed event
    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        customer_email = session.get('customer_email')
        if customer_email:
            db = read_db()
            db[customer_email] = {'pro': True}
            write_db(db)
            print(f"✅ Upgraded {customer_email} to Pro")
        else:
            print("⚠️ No customer email in session")

    return jsonify({'status': 'success'}), 200

if __name__ == '__main__':
    app.run(port=5000)