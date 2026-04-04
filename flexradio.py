"""
FlexRadio TCP API client for the SmartSDR ecosystem.

Connects to a FlexRadio on port 4992 and provides methods to
list connected clients and disconnect them.
"""

import socket
import threading
import time
import logging
import re

logger = logging.getLogger(__name__)


class FlexRadioClient:
    """TCP client for the FlexRadio Discovery/Command protocol."""

    def __init__(self, host, port=4992, timeout=5):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock = None
        self._lock = threading.Lock()
        self._seq = 0
        self._connected = False
        self._recv_buffer = ''

    @property
    def connected(self):
        return self._connected

    def connect(self):
        """Open TCP connection to the radio and read the version handshake."""
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(self.timeout)
            self._sock.connect((self.host, self.port))

            # Read version line (radio sends it immediately on connect)
            version_line = self._read_line()
            logger.info(f"Connected to FlexRadio at {self.host}:{self.port}: {version_line}")

            # Subscribe to client status updates
            self.send_command("sub client all")
            self._connected = True
            return True
        except (socket.error, OSError) as e:
            logger.error(f"Failed to connect to FlexRadio at {self.host}:{self.port}: {e}")
            self._connected = False
            self.close()
            return False

    def close(self):
        """Close the TCP connection."""
        self._connected = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _read_line(self):
        """Read a single line from the socket."""
        while '\n' not in self._recv_buffer:
            try:
                data = self._sock.recv(4096)
                if not data:
                    raise ConnectionError("Connection closed by radio")
                self._recv_buffer += data.decode('utf-8', errors='replace')
            except socket.timeout:
                return None
        line, self._recv_buffer = self._recv_buffer.split('\n', 1)
        return line.strip()

    def _read_responses(self, target_seq):
        """Read lines until we get the response for our command sequence number."""
        responses = []
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            line = self._read_line()
            if line is None:
                continue
            responses.append(line)
            # Response line format: R<seq>|<error_code>|<message>
            if line.startswith(f'R{target_seq}|'):
                break
        return responses

    def send_command(self, command):
        """Send a command and return the response lines."""
        with self._lock:
            self._seq += 1
            seq = self._seq
            cmd_line = f"C{seq}|{command}\n"
            try:
                self._sock.sendall(cmd_line.encode('utf-8'))
                return self._read_responses(seq)
            except (socket.error, OSError, ConnectionError) as e:
                logger.error(f"Error sending command '{command}': {e}")
                self._connected = False
                return []

    def get_clients(self):
        """
        Get list of connected clients.

        Returns list of dicts:
        [{'handle': '0x...', 'station': 'NAME', 'callsign': 'W5ABC', 'ip': '...'}]
        """
        responses = self.send_command("client list")
        clients = []

        for line in responses:
            # Status lines for clients look like:
            # S<seq>|client <handle> connected=1 callsign=W5ABC station=MyStation ...
            if 'client ' not in line:
                continue

            client = {}

            # Extract handle
            handle_match = re.search(r'client\s+(0x[0-9A-Fa-f]+)', line)
            if handle_match:
                client['handle'] = handle_match.group(1)

            # Extract key=value pairs
            for key in ('callsign', 'station', 'ip'):
                match = re.search(rf'{key}=(\S+)', line)
                if match:
                    client[key] = match.group(1)

            if client.get('handle'):
                clients.append(client)

        return clients

    def disconnect_client(self, handle):
        """Disconnect a client by handle."""
        logger.info(f"Disconnecting client {handle}")
        responses = self.send_command(f"client disconnect {handle}")
        # Check for success (error code 0)
        for line in responses:
            if line.startswith('R') and '|0|' in line:
                return True
        return False

    def reconnect(self):
        """Close and re-establish connection."""
        self.close()
        time.sleep(1)
        return self.connect()


class FlexRadioRelayClient:
    """HTTP client that talks to a relay agent instead of directly to the radio.

    Provides the same interface as FlexRadioClient (connect, get_clients,
    disconnect_client, close) so the monitor can use either interchangeably.
    """

    def __init__(self, relay_url, api_key, timeout=10):
        self.relay_url = relay_url.rstrip('/')
        self.api_key = api_key
        self.timeout = timeout
        self._connected = False

    @property
    def connected(self):
        return self._connected

    def _headers(self):
        return {'X-API-Key': self.api_key}

    def connect(self):
        """Test relay reachability via /api/ping."""
        import requests
        try:
            resp = requests.get(
                f'{self.relay_url}/api/ping',
                headers=self._headers(),
                timeout=self.timeout
            )
            if resp.status_code == 200:
                data = resp.json()
                self._connected = data.get('radio_connected', False)
                logger.info(f"Connected to relay at {self.relay_url}, radio_connected={self._connected}")
                return True
            else:
                logger.error(f"Relay ping failed: HTTP {resp.status_code}")
                self._connected = False
                return False
        except Exception as e:
            logger.error(f"Failed to connect to relay at {self.relay_url}: {e}")
            self._connected = False
            return False

    def close(self):
        """No-op — HTTP is stateless."""
        self._connected = False

    def get_clients(self):
        """GET /api/clients from the relay."""
        import requests
        try:
            resp = requests.get(
                f'{self.relay_url}/api/clients',
                headers=self._headers(),
                timeout=self.timeout
            )
            if resp.status_code == 200:
                self._connected = True
                return resp.json().get('clients', [])
            else:
                logger.error(f"Relay get_clients failed: HTTP {resp.status_code}")
                return []
        except Exception as e:
            logger.error(f"Relay get_clients error: {e}")
            self._connected = False
            return []

    def disconnect_client(self, handle):
        """POST /api/disconnect to the relay."""
        import requests
        try:
            resp = requests.post(
                f'{self.relay_url}/api/disconnect',
                headers=self._headers(),
                json={'handle': handle},
                timeout=self.timeout
            )
            if resp.status_code == 200:
                return resp.json().get('success', False)
            else:
                logger.error(f"Relay disconnect failed: HTTP {resp.status_code}")
                return False
        except Exception as e:
            logger.error(f"Relay disconnect error: {e}")
            return False

    def reconnect(self):
        """Re-check relay connectivity."""
        self._connected = False
        return self.connect()
