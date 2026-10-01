#!/usr/bin/env python3
from flask import Flask, request
import requests, os, logging, hmac
import re
import urllib.parse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
app.logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())

YANDEX_TOKEN = os.getenv("YANDEX_TOKEN")
YANDEX_URL = os.getenv("YANDEX_URL", "https://botapi.messenger.yandex.net/bot/v1/messages/sendText/")
ALERTMANAGER_BASE_URL = os.getenv("ALERTMANAGER_BASE_URL", "").rstrip("/")
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN")

if not YANDEX_TOKEN:
    raise RuntimeError("YANDEX_TOKEN is not set")

if not WEBHOOK_TOKEN:
    app.logger.warning("WEBHOOK_TOKEN is not set, /alert accepts unauthenticated requests")

session = requests.Session()
session.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["POST"],
)))


def is_authorized(req):
    if not WEBHOOK_TOKEN:
        return True
    provided = req.headers.get("Authorization", "")
    return hmac.compare_digest(provided.encode(), f"Bearer {WEBHOOK_TOKEN}".encode())


def get_chat_ids_for_alert(labels):
    team = labels.get('team')

    if not team:
        app.logger.warning("No team label found, skipping alert")
        return []

    teams = [t.strip() for t in team.split(',') if t.strip()]
    chat_ids = set()

    for t in teams:
        env_var_name = re.sub(r'[^A-Z0-9]', '_', t.upper()) + "_CHAT_ID"
        chat_id = os.getenv(env_var_name)

        if chat_id:
            for cid in chat_id.split(','):
                cid = cid.strip()
                if cid:
                    chat_ids.add(cid)
        else:
            app.logger.warning(f"No chat ID found for team: {t} (tried env var {env_var_name})")

    result = sorted(chat_ids)
    if not result:
        app.logger.warning(f"No chat IDs found for teams: {teams}")

    return result


def get_grafana_url(annotations, labels):
    grafana_fields = [
        'grafana_url',
        'grafana-url',
        'grafana_dashboard',
        'grafana-dashboard'
    ]

    base_url = None

    for field in grafana_fields:
        if field in annotations and annotations[field]:
            base_url = annotations[field]
            break

    if not base_url:
        return None

    namespace = labels.get('namespace', '') or labels.get('exported_namespace', '')

    if namespace:
        parsed = urllib.parse.urlparse(base_url)
        query_params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        flat_params = {k: v[0] for k, v in query_params.items()}

        if 'var-namespace' not in flat_params:
            flat_params['var-namespace'] = namespace
            new_query = urllib.parse.urlencode(flat_params)
            base_url = urllib.parse.urlunparse(parsed._replace(query=new_query))

    return base_url


def get_silence_url(annotations, labels, alertmanager_base_url):
    silence_fields = [
        'silence_url',
        'silence-url'
    ]

    for field in silence_fields:
        if field in annotations and annotations[field]:
            return annotations[field]

    if not alertmanager_base_url:
        return None

    base_labels = ['alertname', 'instance', 'job', 'severity', 'node', 'workload']
    service_labels = ['service', 'service_name', 'app']

    filter_pairs = []

    for label_key in base_labels:
        if label_key in labels:
            value = str(labels[label_key])
            encoded_value = urllib.parse.quote(value, safe='')
            filter_pairs.append(f"{label_key}%3D%22{encoded_value}%22")

    for service_label in service_labels:
        if service_label in labels:
            value = str(labels[service_label])
            encoded_value = urllib.parse.quote(value, safe='')
            filter_pairs.append(f"{service_label}%3D%22{encoded_value}%22")
            break

    if filter_pairs:
        filter_string = "%2C".join(filter_pairs)
        return f"{alertmanager_base_url}/#/silences/new?filter=%7B{filter_string}%7D"

    return None


def format_message(status, labels, annotations):

    if status == 'firing':
        status_indicator = "🔴 FIRING"
    elif status == 'resolved':
        status_indicator = "🟢 RESOLVED"
    else:
        status_indicator = "⚪ UNKNOWN"

    message = f"{status_indicator}\n"
    message += f"Alert: {labels.get('alertname', 'Unknown')}\n"
    message += f"Severity: {labels.get('severity', 'unknown').upper()}\n"

    if 'component' in labels:
        message += f"Component: {labels['component']}\n"
    if 'team' in labels:
        message += f"Team: {labels['team']}\n"
    if 'env' in labels:
        message += f"Env: {labels['env']}\n"
    if 'summary' in annotations:
        message += f"\nSummary: {annotations['summary']}\n"
    if 'description' in annotations:
        desc = annotations['description']
        desc = re.sub(r'(\d+\.\d+)%', lambda m: f"{float(m.group(1)):.3f}%", desc)
        message += f"Description: {desc}\n"

    if 'runbook' in annotations:
        message += f"\nRunbook: {annotations['runbook']}\n"

    grafana_url = get_grafana_url(annotations, labels)
    if grafana_url:
        message += f"Dashboard: {grafana_url}\n"

    silence_url = get_silence_url(annotations, labels, ALERTMANAGER_BASE_URL)
    if silence_url:
        message += f"\nSilence: {silence_url}\n"

    return message.strip()


def format_alertmanager_message(data):
    alerts = data.get('alerts', [])

    messages_with_chats = []

    for alert in alerts:
        status = alert.get('status', 'unknown')
        labels = alert.get('labels', {})
        annotations = alert.get('annotations', {})

        chat_ids = get_chat_ids_for_alert(labels)

        if not chat_ids:
            app.logger.warning(f"Skipping alert with no chat IDs: {labels.get('alertname', 'Unknown')}")
            continue

        messages_with_chats.append((format_message(status, labels, annotations), chat_ids))

    return messages_with_chats


@app.route('/alert', methods=['POST'])
def handle_alert():
    if not is_authorized(request):
        app.logger.warning(f"Unauthorized request from {request.remote_addr}")
        return "Unauthorized", 401

    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict) or not data:
        app.logger.error("Empty or invalid JSON")
        return "Bad Request: empty or invalid JSON", 400

    messages_with_chats = format_alertmanager_message(data)

    if not messages_with_chats:
        app.logger.info("No alerts to send")
        return "OK (no alerts)", 200

    headers = {"Authorization": f"OAuth {YANDEX_TOKEN}", "Content-Type": "application/json"}

    success_count = 0
    fail_count = 0

    for message, chat_ids in messages_with_chats:
        for chat_id in chat_ids:
            payload = {"chat_id": chat_id, "text": message}

            try:
                r = session.post(YANDEX_URL, headers=headers, json=payload, timeout=10)
                r.raise_for_status()
                success_count += 1

            except Exception as e:
                app.logger.error(f"Send error: {e}")
                fail_count += 1

    total_messages = success_count + fail_count
    app.logger.info(f"Sent {success_count}/{total_messages} messages successfully")

    if fail_count == 0:
        return "OK", 200
    elif success_count > 0:
        return "Partial success", 207
    else:
        return "All messages failed", 500


@app.route('/health', methods=['GET'])
def health():
    return "OK", 200


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=False)
