import os
import requests
from requests_oauthlib import OAuth2Session
from flask import Flask, request, redirect, session, url_for, jsonify, render_template
import plotly.express as px
import plotly.graph_objects as go
import pandas as pd
import time

# Set this environment variable to disable the HTTPS requirement
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# Replace these values with your application's credentials
CLIENT_ID = ''
CLIENT_SECRET = ''
REDIRECT_URI = 'http://localhost:8080/callback'  # Ensure this matches your app settings

# OAuth endpoints given in the FitBit API documentation
AUTHORIZATION_BASE_URL = 'https://www.fitbit.com/oauth2/authorize'
TOKEN_URL = 'https://api.fitbit.com/oauth2/token'

app = Flask(__name__)
app.secret_key = os.urandom(24)  # Ensure secret key is set

def fetch_with_retry(session, url, retries=3, backoff_factor=1.0):
    """Fetches data from the API with retries and exponential backoff."""
    for i in range(retries):
        response = session.get(url).json()
        if 'errors' not in response:
            return response
        error_type = response['errors'][0]['errorType']
        if error_type != 'system':
            break
        app.logger.warning(f"Rate limit hit: {response['errors'][0]['message']}")
        time.sleep(backoff_factor * (2 ** i))  # Exponential backoff
    return response

def fetch_data_in_chunks(session, base_url, start_date, end_date, chunk_size='1y'):
    """Fetches data in chunks to handle large date ranges."""
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    all_data = []

    while start < end:
        chunk_end = min(start + pd.DateOffset(years=int(chunk_size[:-1])), end)
        url = f"{base_url}/{start.strftime('%Y-%m-%d')}/{chunk_end.strftime('%Y-%m-%d')}.json"
        response = fetch_with_retry(session, url)
        if 'activities-heart' in response:
            all_data.extend(response['activities-heart'])
        start = chunk_end + pd.DateOffset(days=1)  # Move to the next chunk

    return all_data

@app.route('/')
def index():
    # Ensure the necessary scopes are included
    scope = ['profile', 'activity', 'heartrate', 'sleep']
    fitbit = OAuth2Session(CLIENT_ID, redirect_uri=REDIRECT_URI, scope=scope)
    authorization_url, state = fitbit.authorization_url(AUTHORIZATION_BASE_URL)
    session['oauth_state'] = state
    app.logger.debug(f'Session state set to: {state}')  # Debugging line
    return redirect(authorization_url)

@app.route('/callback', methods=["GET"])
def callback():
    state = session.get('oauth_state')
    app.logger.debug(f'Session state retrieved: {state}')  # Debugging line
    if state is None:
        return 'Session state is missing. Please try again.', 400
    fitbit = OAuth2Session(CLIENT_ID, state=state, redirect_uri=REDIRECT_URI)
    token = fitbit.fetch_token(TOKEN_URL, client_secret=CLIENT_SECRET,
                               authorization_response=request.url)
    session['oauth_token'] = token
    return redirect(url_for('dashboard'))

@app.route('/dashboard')
def dashboard():
    if 'oauth_token' not in session:
        return redirect(url_for('index'))
    return render_template('index.html')

@app.route('/data/<data_type>')
def data(data_type):
    if 'oauth_token' not in session:
        return redirect(url_for('index'))

    fitbit = OAuth2Session(CLIENT_ID, token=session['oauth_token'])
    url_map = {
        'steps': 'https://api.fitbit.com/1/user/-/activities/steps/date/today/1d.json',
        'calories': 'https://api.fitbit.com/1/user/-/activities/calories/date/today/1d.json',
        'distance': 'https://api.fitbit.com/1/user/-/activities/distance/date/today/1d.json',
        'heartrate': 'https://api.fitbit.com/1/user/-/activities/heart/date/today/1d.json',
        'sleep': 'https://api.fitbit.com/1.2/user/-/sleep/date/today.json'
    }

    response = fetch_with_retry(fitbit, url_map[data_type])

    print(f"Response for {data_type}: {response}")  # Debugging line

    if data_type == 'steps':
        data = response.get('activities-steps', [])
        if not data:
            return jsonify({"error": "No steps data found"})
        title = 'Steps Data'
        fig = px.bar(data, x='dateTime', y='value', title=title)
    elif data_type == 'calories':
        data = response.get('activities-calories', [])
        if not data:
            return jsonify({"error": "No calories data found"})
        title = 'Calories Data'
        fig = px.bar(data, x='dateTime', y='value', title=title)
    elif data_type == 'distance':
        data = response.get('activities-distance', [])
        if not data:
            return jsonify({"error": "No distance data found"})
        title = 'Distance Data'
        fig = px.bar(data, x='dateTime', y='value', title=title)
    elif data_type == 'heartrate':
        data = response['activities-heart'][0]['value']['heartRateZones']
        title = 'Heart Rate Zones'
        fig = go.Figure(data=[
            go.Bar(name=zone['name'], x=[zone['name']], y=[zone['minutes']])
            for zone in data
        ])
    elif data_type == 'sleep':
        data = response['sleep'][0]['levels']['data']
        title = 'Sleep Data'
        fig = px.bar(data, x='dateTime', y='level', title=title)
    else:
        return 'Invalid data type requested.', 400

    graphJSON = fig.to_json()
    return jsonify(graphJSON)

@app.route('/heartrate')
def heartrate():
    if 'oauth_token' not in session:
        return redirect(url_for('index'))

    fitbit = OAuth2Session(CLIENT_ID, token=session['oauth_token'])

    # Get start and end dates from query parameters
    start_date = request.args.get('start_date', (pd.Timestamp.now() - pd.DateOffset(years=1)).strftime('%Y-%m-%d'))
    end_date = request.args.get('end_date', pd.Timestamp.now().strftime('%Y-%m-%d'))

    base_url = 'https://api.fitbit.com/1/user/-/activities/heart/date'
    all_data = fetch_data_in_chunks(fitbit, base_url, start_date, end_date)

    if not all_data:
        app.logger.error(f"No heart rate data found for the range: {start_date} to {end_date}")
        return 'No heart rate data found.', 400

    heart_rate_data = []
    for entry in all_data:
        date_str = entry['dateTime']
        if 'restingHeartRate' in entry['value']:
            heart_rate_data.append({
                'date': date_str,
                'value': entry['value']['restingHeartRate']
            })
        else:
            app.logger.debug(f"No restingHeartRate for {date_str}")

    if not heart_rate_data:
        app.logger.error(f"No resting heart rate data found in the response.")
        return 'No resting heart rate data found.', 400

    # Create DataFrame for easy manipulation
    df = pd.DataFrame(heart_rate_data)
    df['date'] = pd.to_datetime(df['date'])
    df.set_index('date', inplace=True)

    # Aggregate data by month and year
    df_monthly = df.resample('M').mean()
    df_yearly = df.resample('Y').mean()

    fig_monthly = px.line(df_monthly, x=df_monthly.index, y='value', title='Monthly Average Resting Heart Rate')
    fig_yearly = px.line(df_yearly, x=df_yearly.index, y='value', title='Yearly Average Resting Heart Rate')

    graphJSON_monthly = fig_monthly.to_json()
    graphJSON_yearly = fig_yearly.to_json()

    return render_template('heartrate.html', graphJSON_monthly=graphJSON_monthly, graphJSON_yearly=graphJSON_yearly)

if __name__ == "__main__":
    app.run(debug=True, port=8080)
