import os
import stripe
import threading
import logging
import requests
from datetime import datetime
from flask import Flask, request, jsonify
from supabase import create_client
from dotenv import load_dotenv
import time

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

# ------------------------- WEEKLY EARNINGS RECAP ENDPOINT -------------------------
@app.route('/send-weekly-report', methods=['GET'])
def send_weekly_report():
    """
    Triggered by cron-job.org every Monday.
    Fetches top 5 earnings surprises from your ticker list and emails subscribers.
    """
    import yfinance as yf
    import pandas as pd
    from datetime import datetime
    import time
    import traceback

    logger.info("Weekly earnings report job started")

    # 1. Load ticker list
    try:
        tickers_df = pd.read_csv('tickers.csv')
        tickers = tickers_df['Symbol'].tolist()
        logger.info(f"Loaded {len(tickers)} tickers")
        logger.info(f"First 5 tickers: {tickers[:5]}")
    except Exception as e:
        logger.error(f"Failed to load tickers.csv: {e}")
        return jsonify({'error': 'Ticker list not found'}), 500

    # 2. Gather earnings surprises
    earnings_data = []
    for ticker in tickers[:20]:
        logger.info(f"Loop iteration for {ticker}")
        try:
            time.sleep(0.3)
            stock = yf.Ticker(ticker)
            earnings = stock.earnings_dates
            logger.info(f"Ticker: {ticker}, earnings is None? {earnings is None}")

            if earnings is not None and not earnings.empty:
                logger.info(f"  -> Has earnings data, columns: {list(earnings.columns)}")
                logger.info("  -> ENTERING EXTRACTION BLOCK")

                # Find last reported quarter (where 'Reported EPS' is not NaN)
                eps_actual = None
                eps_estimate = None
                report_date = None
                for idx in range(len(earnings)):
                    row = earnings.iloc[idx]
                    actual = row.get('Reported EPS')
                    if pd.notna(actual):
                        eps_actual = actual
                        eps_estimate = row.get('EPS Estimate')
                        report_date = row.name
                        break

                if eps_actual is not None and pd.notna(eps_actual) and pd.notna(eps_estimate):
                    surprise_pct = ((eps_actual - eps_estimate) / abs(eps_estimate)) * 100
                    earnings_data.append({
                        'ticker': ticker,
                        'surprise_pct': surprise_pct,
                        'eps_actual': eps_actual,
                        'eps_estimate': eps_estimate,
                        'report_date': report_date.strftime('%Y-%m-%d') if hasattr(report_date, 'strftime') else str(report_date)
                    })
                    logger.info(f"  -> Added {ticker} with surprise {surprise_pct:.1f}% (reported on {report_date})")
                else:
                    logger.info(f"  -> No reported EPS found for {ticker}")
            else:
                logger.info(f"  -> No earnings data for {ticker}")

        except KeyError as e:
            logger.warning(f"KeyError for {ticker}: {e} – skipping")
            continue
        except Exception as e:
            logger.warning(f"Could not fetch earnings for {ticker}: {e}")
            logger.warning(traceback.format_exc())
            continue

    # 3. Check if any data was collected
    if not earnings_data:
        logger.warning("No earnings data found for any ticker")
        return jsonify({'error': 'No earnings data'}), 500

    # 4. Sort and get top 5 positive surprises
    sorted_by_surprise = sorted(earnings_data, key=lambda x: x['surprise_pct'], reverse=True)
    top_5 = [x for x in sorted_by_surprise if x['surprise_pct'] > 0][:5]

    if not top_5:
        logger.warning("No positive earnings surprises found")
        return jsonify({'message': 'No positive surprises this week'}), 200

    # 5. Build HTML email content
    email_subject = f"Tick Sniper Weekly – Top 5 Earnings Beats ({datetime.now().strftime('%b %d, %Y')})"

    rows = ""
    for item in top_5:
        rows += f"""
        <tr style="border-bottom:1px solid #334155;">
            <td style="padding:10px; font-weight:bold;">{item['ticker']}</td>
            <td style="padding:10px; color:#10b981;">+{item['surprise_pct']:.1f}%</td>
            <td style="padding:10px;">Actual: ${item['eps_actual']:.2f}</td>
            <td style="padding:10px;">Estimate: ${item['eps_estimate']:.2f}</td>
        </tr>
        """

    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="UTF-8"></head>
    <body style="font-family: Arial, sans-serif; background:#0a0f1e; padding:20px;">
        <div style="max-width:600px; margin:0 auto; background:#0f172a; border-radius:20px; padding:24px;">
            <h2 style="color:#ffffff;">📊 Top 5 Earnings Beats This Week</h2>
            <p style="color:#cbd5e1;">The biggest positive surprises from recent reports (actual vs. estimate).</p>
            <table style="width:100%; border-collapse:collapse; color:#e2e8f0;">
                <thead>
                    <tr style="background:#1e293b;">
                        <th style="padding:10px; text-align:left;">Ticker</th>
                        <th style="padding:10px; text-align:left;">Surprise</th>
                        <th style="padding:10px; text-align:left;">Actual EPS</th>
                        <th style="padding:10px; text-align:left;">Estimate</th>
                    </tr>
                </thead>
                <tbody>
                    {rows}
                </tbody>
            </table>
            <p style="margin-top:24px;"><a href="https://ai-swing-trade-scanner-316.streamlit.app/?utm_source=email&utm_medium=weekly_report&utm_campaign=earnings_recap" style="background:#3b82f6; color:white; padding:10px 20px; text-decoration:none; border-radius:40px;">👉 Scan more stocks with AI</a></p>
            <p style="color:#64748b; font-size:12px;">You received this because you subscribed to Tick Sniper’s weekly report. <a href="{{ unsubscribe_link }}" style="color:#60a5fa;">Unsubscribe</a></p>
        </div>
    </body>
    </html>
    """

    # 6. Send via Brevo to your contact list
    brevo_payload = {
        "name": f"Tick Sniper Weekly - {datetime.now().strftime('%Y-%m-%d')}",
        "subject": email_subject,
    "sender": {"name": "Tick Sniper", "email": "weekly@ticksniper.com"},  # Change to your verified sender
        "type": "classic",
        "recipients": {
            "listIds": [int(BREVO_LIST_ID)]
        },
        "htmlContent": html_body,
        "replyTo": {"email": "support@ticksniper.com"}
    }

    headers = {
        'Content-Type': 'application/json',
        'api-key': BREVO_API_KEY
    }

    try:
        response = requests.post('https://api.brevo.com/v3/emailCampaigns', json=brevo_payload, headers=headers, timeout=30)
        if response.status_code in (201, 204):
            logger.info("Weekly report email sent successfully to list")
            return jsonify({'status': 'sent', 'top_beats': top_5}), 200
        else:
            logger.error(f"Brevo campaign error: {response.text}")
            return jsonify({'error': 'Failed to send email'}), 500
    except Exception as e:
        logger.error(f"Exception sending weekly report: {e}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(port=4242)