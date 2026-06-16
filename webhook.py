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
import joblib
from functools import lru_cache
from datetime import datetime, timedelta


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

# Load tickers and sector mapping
try:
    tickers_df = pd.read_csv('tickers.csv')
    TICKERS = dict(zip(tickers_df['Symbol'], tickers_df['Sector']))
except Exception as e:
    logger.warning(f"Could not load tickers.csv: {e}")
    TICKERS = {}

sector_to_etf = {
    'Technology': 'XLK',
    'Financials': 'XLF',
    'Healthcare': 'XLV',
    'Consumer Cyclical': 'XLY',
    'Communication Services': 'XLC',
    'Industrials': 'XLI',
    'Consumer Defensive': 'XLP',
    'Energy': 'XLE',
    'Utilities': 'XLU',
    'Real Estate': 'XLRE',
    'Basic Materials': 'XLB',
    'Broad Market ETFs': 'SPY'
}

# Initialize Stripe and Supabase
stripe.api_key = STRIPE_API_KEY
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

def ensure_numeric(df):
    """Convert DataFrame to float32, replace inf, fill NaN with 0."""
    df = df.astype('float32')
    df = df.replace([float('inf'), -float('inf')], 0.0)
    df = df.fillna(0.0)
    return df

# ------------------------- MACRO DATA (cached) -------------------------
@lru_cache(maxsize=1)
def get_macro_sector_data_cached(period="1y"):
    """Fetch macro and sector data with caching."""
    import yfinance as yf
    import pandas as pd
    from datetime import datetime, timedelta

    end = pd.Timestamp.now()
    if period == "6mo":
        start = end - pd.DateOffset(months=6)
    elif period == "1y":
        start = end - pd.DateOffset(years=1)
    elif period == "2y":
        start = end - pd.DateOffset(years=2)
    else:
        start = end - pd.DateOffset(years=1)

    macro_df = pd.DataFrame()

    # Fetch VIX, TNX
    try:
        vix = yf.download('^VIX', start=start, end=end, progress=False)['Close']
        tnx = yf.download('^TNX', start=start, end=end, progress=False)['Close']
        cl = yf.download('CL=F', start=start, end=end, progress=False)['Close']
        macro_df['VIX'] = vix
        macro_df['TNX'] = tnx
        macro_df['CL'] = cl
    except Exception as e:
        logger.warning(f"Failed to fetch macro data: {e}")
        # Fill with zeros if fails
        macro_df['VIX'] = 0
        macro_df['TNX'] = 0
        macro_df['CL'] = 0

    # Add sector ETFs (we'll fetch them separately if needed)
    # For simplicity, we only need macro columns; sector ETFs are optional but may be required by feature_engineering
    # We'll add basic sector ETFs if available
    sector_etfs = ['XLK', 'XLF', 'XLV', 'XLY', 'XLC', 'XLI', 'XLP', 'XLE', 'XLU', 'XLRE', 'XLB', 'SPY']
    for etf in sector_etfs:
        try:
            data = yf.download(etf, start=start, end=end, progress=False)['Close']
            macro_df[etf] = data
        except:
            macro_df[etf] = 0

    macro_df = macro_df.ffill().bfill()
    return macro_df

def process_event_async(event):
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

            # After upserting paid_users, capture client_reference_id
            client_ref = getattr(session, 'client_reference_id', None)
            if client_ref:
                # Fetch UTM data from user_acquisitions
                utm_result = supabase.table('user_acquisitions').select('utm_source, utm_medium, utm_campaign, ref').eq('email', client_ref).execute()
                if utm_result.data:
                    utm_data = utm_result.data[0]
                    logger.info(f"Subscription for {client_ref} came from utm_source={utm_data.get('utm_source')}")
                    # Optionally update paid_users with utm_source (if column exists)
                    # supabase.table('paid_users').update({'utm_source': utm_data.get('utm_source')}).eq('email', client_ref).execute()
            
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
    import yfinance as yf
    import pandas as pd
    from datetime import datetime
    import time
    import traceback

    logger.info("Weekly earnings report job started")
    time.sleep(3)  # Initial delay to avoid rate limit

    # 1. Load ticker list
    try:
        tickers_df = pd.read_csv('tickers.csv')
        tickers = tickers_df['Symbol'].tolist()
        logger.info(f"Loaded {len(tickers)} tickers")
        tickers = tickers[:15]  # Process only first 15 tickers
        logger.info(f"Processing first {len(tickers)} tickers: {tickers}")
    except Exception as e:
        logger.error(f"Failed to load tickers.csv: {e}")
        return jsonify({'error': 'Ticker list not found'}), 500

    # 2. Gather earnings surprises
    earnings_data = []
    for ticker in tickers:
        logger.info(f"Loop iteration for {ticker}")
        try:
            time.sleep(1.0)  # 1 second between requests
            stock = yf.Ticker(ticker)
            earnings = stock.earnings_dates
            if earnings is None or earnings.empty:
                logger.info(f"  -> No earnings data for {ticker}")
                continue

            logger.info(f"  -> Has earnings data, columns: {list(earnings.columns)}")
            # Find last reported quarter
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
                logger.info(f"  -> Added {ticker} with surprise {surprise_pct:.1f}%")
            else:
                logger.info(f"  -> No reported EPS for {ticker}")
        except Exception as e:
            logger.warning(f"Could not fetch earnings for {ticker}: {e}")
            # Continue to next ticker
            continue

    if not earnings_data:
        logger.warning("No earnings data found for any ticker")
        return jsonify({'error': 'No earnings data'}), 500

    # 3. Sort and get top 5 positive surprises
    sorted_by_surprise = sorted(earnings_data, key=lambda x: x['surprise_pct'], reverse=True)
    top_5 = [x for x in sorted_by_surprise if x['surprise_pct'] > 0][:5]

    if not top_5:
        logger.warning("No positive earnings surprises found")
        return jsonify({'message': 'No positive surprises this week'}), 200

    # 4. Build email (same as before)
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

    # 5. Send via Brevo
    brevo_payload = {
        "name": f"Tick Sniper Weekly - {datetime.now().strftime('%Y-%m-%d')}",
        "subject": email_subject,
        "sender": {"name": "Tick Sniper", "email": "stockpicksnathan316@gmail.com"},
        "type": "classic",
        "recipients": {"listIds": [int(BREVO_LIST_ID)]},
        "htmlContent": html_body
    }
    headers = {'Content-Type': 'application/json', 'api-key': BREVO_API_KEY}
    try:
        response = requests.post('https://api.brevo.com/v3/emailCampaigns', json=brevo_payload, headers=headers, timeout=30)
        if response.status_code in (201, 204):
            logger.info("Weekly report email sent successfully")
            return jsonify({'status': 'sent', 'top_beats': top_5}), 200
        else:
            logger.error(f"Brevo error: {response.text}")
            return jsonify({'error': 'Failed to send email'}), 500
    except Exception as e:
        logger.error(f"Exception: {e}")
        return jsonify({'error': str(e)}), 500

# ------------------------- ALERT CHECKING ENDPOINT -------------------------
@app.route('/check-alerts', methods=['GET'])
def check_alerts():
    """Check user alerts and send emails if probability exceeds threshold."""
    import yfinance as yf
    import pandas as pd
    import time
    import traceback
    from feature_engineering import add_enhanced_features, get_fundamentals

    logger.info("Alert check job started")

    # Load the pooled model (if not already loaded globally)
    try:
        model = joblib.load('pooled_model.pkl')
        cal_map = joblib.load('calibration_map.pkl')
        feat_cols = joblib.load('pooled_feature_cols.pkl')
    except Exception as e:
        logger.error(f"Failed to load model: {e}")
        return jsonify({'error': 'Model not found'}), 500

    # Get active alerts (limit to 10 to avoid timeout)
    try:
        alerts = supabase.table('user_alerts').select('*').limit(10).execute()
        alerts = alerts.data
        logger.info(f"Found {len(alerts)} alerts to process")
    except Exception as e:
        logger.error(f"Failed to fetch alerts: {e}")
        return jsonify({'error': 'Database error'}), 500

    if not alerts:
        return jsonify({'message': 'No alerts found'}), 200

    # Get macro data once (cached)
    macro_df = get_macro_sector_data_cached("1y")

    results = []
    for alert in alerts:
        ticker = alert['ticker'].upper()  # ensure uppercase
        user_email = alert['user_email']
        threshold = alert['threshold']
        alpha = alert.get('alpha', 0.7)

        try:
            df = yf.download(ticker, period="1y", progress=False)
            if df.empty:
                results.append({'ticker': ticker, 'status': 'No data'})
                continue

            # --- FIX: Drop the ticker level from MultiIndex ---
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.droplevel(1)  # removes the ticker level, keeps 'Open', 'High', etc.
            # ------------------------------------------------

            sector = TICKERS.get(ticker, 'Unknown')
            sector_etf = sector_to_etf.get(sector, None)
            fundamentals = get_fundamentals(ticker)
            df_enhanced = add_enhanced_features(df, ticker, macro_df, sector_etf, fundamentals)

            latest = df_enhanced[feat_cols].fillna(0).iloc[[-1]]
            latest = latest.astype('float32').replace([float('inf'), -float('inf')], 0.0).fillna(0.0)

            prob = model.predict_proba(latest)[0][1]

            if prob >= threshold:
                send_alert_email(user_email, ticker, prob, threshold)
                results.append({'ticker': ticker, 'status': 'Alert sent', 'prob': prob})
            else:
                results.append({'ticker': ticker, 'status': 'Below threshold', 'prob': prob})

        except Exception as e:
            logger.error(f"Error processing {ticker}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            results.append({'ticker': ticker, 'status': 'Error', 'error': str(e)})

        time.sleep(0.5)

    return jsonify({'processed': len(alerts), 'results': results}), 200

def send_alert_email(user_email, ticker, prob, threshold):
    """Send an alert email via Brevo SMTP."""
    subject = f"📈 Tick Sniper Alert: {ticker} probability hit {prob:.1%}"
    html = f"""
    <html>
    <body style="font-family: Arial, sans-serif; background:#0a0f1e; padding:20px;">
        <div style="max-width:600px; margin:auto; background:#0f172a; border-radius:20px; padding:24px;">
            <h2 style="color:#ffffff;">🔔 Alert: {ticker}</h2>
            <p style="color:#cbd5e1;">The predicted probability for {ticker} is <strong style="color:#facc15;">{prob:.1%}</strong>.</p>
            <p style="color:#cbd5e1;">This exceeds your threshold of {threshold:.0%}.</p>
            <p><a href="https://ai-swing-trade-scanner-316.streamlit.app/?utm_source=email&utm_medium=alert" style="background:#3b82f6; color:white; padding:10px 20px; text-decoration:none; border-radius:40px;">View in App</a></p>
            <p style="color:#64748b; font-size:12px;">You received this because you set an alert. <a href="{{ unsubscribe_link }}" style="color:#60a5fa;">Unsubscribe</a></p>
        </div>
    </body>
    </html>
    """

    payload = {
        "sender": {"name": "Tick Sniper", "email": "stockpicksnathan316@gmail.com"},
        "to": [{"email": user_email}],
        "subject": subject,
        "htmlContent": html
    }
    headers = {'Content-Type': 'application/json', 'api-key': BREVO_API_KEY}
    try:
        response = requests.post('https://api.brevo.com/v3/smtp/email', json=payload, headers=headers, timeout=10)
        logger.info(f"Alert email sent to {user_email} for {ticker}: {response.status_code}")
    except Exception as e:
        logger.error(f"Failed to send alert email: {e}")

if __name__ == '__main__':
    app.run(port=4242)