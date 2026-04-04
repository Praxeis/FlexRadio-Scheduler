#!/usr/bin/env python3
"""
FlexRadio Relay Agent — runs at the radio site on the same LAN as the FlexRadio.

This small Flask server exposes a REST API that the remote scheduler uses
to list connected clients and disconnect them. It bridges the gap when the
scheduler is hosted outside the radio's network.

Usage:
    python relay_agent.py --radio-ip 192.168.1.100 --api-key YOUR_SECRET_KEY

Optional flags:
    --radio-port  TCP port on the radio (default: 4992)
    --host        Bind address for this relay (default: 0.0.0.0)
    --port        HTTP port for this relay (default: 5001)
"""

import argparse
import logging
from functools import wraps

from flask import Flask, jsonify, request

from flexradio import FlexRadioClient

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# These are set from command-line args at startup
radio_client = None
API_KEY = None


def require_api_key(f):
    """Decorator to enforce X-API-Key header authentication."""
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get('X-API-Key', '')
        if key != API_KEY:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated


def ensure_radio_connection():
    """Make sure we have an active connection to the radio."""
    global radio_client
    if radio_client and radio_client.connected:
        return True
    if radio_client:
        radio_client.reconnect()
        return radio_client.connected
    return False


@app.route('/api/ping', methods=['GET'])
@require_api_key
def ping():
    """Health check — reports whether the relay can reach the radio."""
    connected = ensure_radio_connection()
    return jsonify({
        'status': 'ok',
        'radio_connected': connected,
    })


@app.route('/api/clients', methods=['GET'])
@require_api_key
def get_clients():
    """Return list of clients currently connected to the radio."""
    if not ensure_radio_connection():
        return jsonify({'error': 'Radio not connected', 'clients': []}), 502

    clients = radio_client.get_clients()
    return jsonify({'clients': clients})


@app.route('/api/disconnect', methods=['POST'])
@require_api_key
def disconnect():
    """Disconnect a client by handle."""
    if not ensure_radio_connection():
        return jsonify({'error': 'Radio not connected', 'success': False}), 502

    data = request.get_json()
    if not data or 'handle' not in data:
        return jsonify({'error': 'Missing handle', 'success': False}), 400

    handle = data['handle']
    success = radio_client.disconnect_client(handle)
    return jsonify({'success': success, 'handle': handle})


def main():
    global radio_client, API_KEY

    parser = argparse.ArgumentParser(description='FlexRadio Relay Agent')
    parser.add_argument('--radio-ip', required=True, help='IP address of the FlexRadio on the LAN')
    parser.add_argument('--radio-port', type=int, default=4992, help='FlexRadio TCP port (default: 4992)')
    parser.add_argument('--api-key', required=True, help='Shared API key for authentication')
    parser.add_argument('--host', default='0.0.0.0', help='Bind address (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=5001, help='HTTP port (default: 5001)')
    args = parser.parse_args()

    API_KEY = args.api_key

    # Connect to the radio
    radio_client = FlexRadioClient(args.radio_ip, args.radio_port)
    if radio_client.connect():
        logger.info(f"Connected to FlexRadio at {args.radio_ip}:{args.radio_port}")
    else:
        logger.warning(f"Could not connect to FlexRadio at {args.radio_ip}:{args.radio_port} "
                       f"— will retry on first request")

    logger.info(f"Relay agent starting on {args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == '__main__':
    main()
