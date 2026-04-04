"""
Background thread that monitors FlexRadio connections and enforces the schedule.

- Checks every 30 seconds who is connected
- Disconnects unauthorized users immediately
- Warns at 10 and 5 minutes before slot end
- Disconnects the user when their slot ends
- Manages ZeroTier network authorization (authorize scheduled user, deauthorize others)
"""

import threading
import time
import logging
from datetime import datetime, date

from flexradio import FlexRadioClient, FlexRadioRelayClient
from zerotier import ZeroTierClient

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 30  # seconds


class RadioMonitor:
    def __init__(self, app):
        self.app = app
        self.client = None
        self.zt_client = None
        self._thread = None
        self._stop_event = threading.Event()
        self._check_now_event = threading.Event()  # Signal for immediate check
        self._warnings_sent = {}  # (date, hour) -> set of warning minutes sent
        self._last_authorized_node = None  # Track which node is currently authorized
        self.status = {
            'radio_connected': False,
            'enforcement_enabled': False,
            'connected_clients': [],
            'current_booking': None,
            'current_callsign': None,
            'minutes_remaining': None,
            'warning_10min': False,
            'warning_5min': False,
            'last_check': None,
            'last_error': None,
            'zerotier_enabled': False,
            'zerotier_connected': False,
            'zerotier_network_name': '',
            'zerotier_last_action': None,
        }

    def start(self):
        """Start the monitor background thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name='RadioMonitor')
        self._thread.start()
        logger.info("RadioMonitor started")

    def stop(self):
        """Stop the monitor background thread."""
        self._stop_event.set()
        self._check_now_event.set()  # Wake the thread so it can exit
        if self._thread:
            self._thread.join(timeout=10)
        if self.client:
            self.client.close()
        logger.info("RadioMonitor stopped")

    def check_now(self):
        """Signal the monitor to run an immediate check cycle."""
        self._check_now_event.set()

    def _get_radio_config(self):
        """Fetch radio config from the database."""
        from models import RadioConfig, db
        with self.app.app_context():
            config = RadioConfig.query.first()
            if config:
                # Auto-expire maintenance mode if scheduled end has passed
                if config.maintenance_mode and config.maintenance_until:
                    if datetime.now() >= config.maintenance_until:
                        config.maintenance_mode = False
                        config.maintenance_message = ''
                        config.maintenance_until = None
                        db.session.commit()
                        logger.info("Maintenance mode auto-expired")

                return {
                    'mode': config.connection_mode,
                    'host': config.radio_host,
                    'port': config.radio_port,
                    'relay_url': config.relay_url or '',
                    'relay_api_key': config.relay_api_key or '',
                    'enabled': config.enforcement_enabled,
                    'zt_enabled': config.zerotier_enabled,
                    'zt_network_id': config.zerotier_network_id or '',
                    'zt_api_token': config.zerotier_api_token or '',
                    'maintenance_mode': config.maintenance_mode,
                    'maintenance_message': config.maintenance_message or '',
                    'check_interval': config.check_interval or 30,
                }
        return None

    def _get_current_booking(self):
        """Get the booking for the current date and hour, including ZT node ID."""
        from models import Booking, User
        now = datetime.now()
        with self.app.app_context():
            booking = Booking.query.filter_by(
                date=now.date(),
                hour=now.hour
            ).first()
            if booking:
                user = booking.user
                return {
                    'id': booking.id,
                    'callsign': user.callsign,
                    'name': user.name,
                    'date': booking.date.isoformat(),
                    'hour': booking.hour,
                    'zerotier_node_id': user.zerotier_node_id or '',
                }
        return None

    def _get_all_member_node_ids(self):
        """Get all ZeroTier node IDs for registered users."""
        from models import User
        with self.app.app_context():
            users = User.query.filter(User.zerotier_node_id != '').all()
            return [u.zerotier_node_id for u in users if u.zerotier_node_id]

    def _ensure_connected(self, config):
        """Ensure we have a live connection to the radio (direct or via relay)."""
        if self.client and self.client.connected:
            return True

        if self.client:
            self.client.close()

        if config['mode'] == 'relay':
            self.client = FlexRadioRelayClient(config['relay_url'], config['relay_api_key'])
        else:
            self.client = FlexRadioClient(config['host'], config['port'])

        return self.client.connect()

    def _ensure_zt_client(self, config):
        """Ensure we have a working ZeroTier client."""
        if not config['zt_enabled'] or not config['zt_network_id'] or not config['zt_api_token']:
            self.zt_client = None
            self.status['zerotier_enabled'] = False
            self.status['zerotier_connected'] = False
            return False

        self.status['zerotier_enabled'] = True

        # Reuse existing client if config hasn't changed
        if (self.zt_client and
                self.zt_client.network_id == config['zt_network_id'] and
                self.zt_client.api_token == config['zt_api_token']):
            return True

        self.zt_client = ZeroTierClient(config['zt_api_token'], config['zt_network_id'])
        result = self.zt_client.test_connection()
        if result['ok']:
            self.status['zerotier_connected'] = True
            self.status['zerotier_network_name'] = result.get('network_name', '')
            logger.info(f"ZeroTier connected to network: {result.get('network_name', '')}")
            return True
        else:
            self.status['zerotier_connected'] = False
            self.status['last_error'] = f"ZeroTier: {result.get('error', 'Unknown error')}"
            logger.error(f"ZeroTier connection failed: {result.get('error')}")
            return False

    def _run(self):
        """Main monitor loop."""
        while not self._stop_event.is_set():
            try:
                self._check_cycle()
            except Exception as e:
                logger.exception(f"RadioMonitor error: {e}")
                self.status['last_error'] = str(e)

            # Wait for either the configured interval or an immediate check signal
            interval = self._current_check_interval
            self._check_now_event.clear()
            # Wait returns True if the event was set (check_now or stop)
            self._check_now_event.wait(timeout=interval)

    @property
    def _current_check_interval(self):
        """Get the current check interval from config, with fallback."""
        try:
            from models import RadioConfig
            with self.app.app_context():
                config = RadioConfig.query.first()
                if config and config.check_interval:
                    return max(5, config.check_interval)
        except Exception:
            pass
        return CHECK_INTERVAL

    def _check_cycle(self):
        """One check cycle: connect, read clients, enforce schedule, manage ZeroTier."""
        config = self._get_radio_config()
        if not config:
            self.status['enforcement_enabled'] = False
            self.status['radio_connected'] = False
            self.status['zerotier_enabled'] = False
            self.status['maintenance_mode'] = False
            return

        # --- Maintenance mode ---
        is_maintenance = config.get('maintenance_mode', False)
        self.status['maintenance_mode'] = is_maintenance
        self.status['maintenance_message'] = config.get('maintenance_message', '')

        # --- ZeroTier enforcement (runs independently of radio enforcement) ---
        zt_active = self._ensure_zt_client(config)

        # --- Radio enforcement ---
        if not config['enabled']:
            self.status['enforcement_enabled'] = False
            self.status['radio_connected'] = False
            if self.client:
                self.client.close()
                self.client = None
        else:
            self.status['enforcement_enabled'] = True

        # Get current booking (needed for both radio and ZeroTier)
        booking = self._get_current_booking()
        self.status['current_booking'] = booking

        now = datetime.now()
        minutes_remaining = 60 - now.minute
        self.status['minutes_remaining'] = minutes_remaining
        self.status['last_check'] = now.isoformat()

        # --- Radio client enforcement ---
        if config['enabled']:
            if not self._ensure_connected(config):
                self.status['radio_connected'] = False
                if config['mode'] == 'relay':
                    self.status['last_error'] = f"Cannot connect to relay at {config['relay_url']}"
                else:
                    self.status['last_error'] = f"Cannot connect to radio at {config['host']}:{config['port']}"
            else:
                self.status['radio_connected'] = True
                if not self.status.get('last_error', '').startswith('ZeroTier'):
                    self.status['last_error'] = None

                clients = self.client.get_clients()
                self.status['connected_clients'] = clients

                # Maintenance mode: disconnect everyone
                if is_maintenance:
                    for client in clients:
                        logger.warning(
                            f"Disconnecting client (maintenance mode): "
                            f"{client.get('callsign', 'unknown')}"
                        )
                        self.client.disconnect_client(client['handle'])
                elif booking:
                    allowed_callsign = booking['callsign'].upper()
                    for client in clients:
                        client_call = client.get('callsign', '').upper()
                        client_station = client.get('station', '').upper()
                        if client_call != allowed_callsign and allowed_callsign not in client_station:
                            logger.warning(
                                f"Disconnecting unauthorized client: {client.get('callsign', 'unknown')} "
                                f"(station: {client.get('station', 'unknown')})"
                            )
                            self.client.disconnect_client(client['handle'])
                else:
                    for client in clients:
                        logger.warning(
                            f"Disconnecting client (no booking this hour): "
                            f"{client.get('callsign', 'unknown')}"
                        )
                        self.client.disconnect_client(client['handle'])

        # --- Warnings ---
        if booking:
            self.status['current_callsign'] = booking['callsign']
            slot_key = (now.date(), now.hour)

            if slot_key not in self._warnings_sent:
                self._warnings_sent = {slot_key: set()}

            if minutes_remaining <= 10 and 10 not in self._warnings_sent[slot_key]:
                self._warnings_sent[slot_key].add(10)
                self.status['warning_10min'] = True
                logger.info(f"10-minute warning for {booking['callsign']}")

            if minutes_remaining <= 5 and 5 not in self._warnings_sent[slot_key]:
                self._warnings_sent[slot_key].add(5)
                self.status['warning_5min'] = True
                logger.info(f"5-minute warning for {booking['callsign']}")

            if minutes_remaining > 10:
                self.status['warning_10min'] = False
                self.status['warning_5min'] = False
        else:
            self.status['current_callsign'] = None
            self.status['warning_10min'] = False
            self.status['warning_5min'] = False

        # --- ZeroTier access control ---
        if zt_active and self.zt_client:
            all_node_ids = self._get_all_member_node_ids()

            if is_maintenance:
                # Maintenance mode: deauthorize everyone
                if self._last_authorized_node is not None:
                    result = self.zt_client.deauthorize_all(all_node_ids)
                    self._last_authorized_node = None
                    self.status['zerotier_last_action'] = (
                        f"Maintenance: deauthorized {len(result['deauthorized'])} members"
                    )
                    logger.info("ZeroTier: Maintenance mode, deauthorized all members")
            elif booking:
                allowed_node = booking.get('zerotier_node_id', '')
                if allowed_node:
                    # Only update if the authorized node has changed
                    if self._last_authorized_node != allowed_node:
                        result = self.zt_client.authorize_only(allowed_node, all_node_ids)
                        self._last_authorized_node = allowed_node
                        self.status['zerotier_last_action'] = (
                            f"Authorized {booking['callsign']} ({allowed_node}), "
                            f"deauthorized {len(result['deauthorized'])} others"
                        )
                        logger.info(
                            f"ZeroTier: Authorized {booking['callsign']} ({allowed_node}), "
                            f"deauthorized {len(result['deauthorized'])} others"
                        )
                else:
                    # User doesn't have a ZeroTier node ID configured
                    self.status['zerotier_last_action'] = (
                        f"Warning: {booking['callsign']} has no ZeroTier Node ID set"
                    )
            else:
                # No booking — deauthorize everyone
                if self._last_authorized_node is not None:
                    result = self.zt_client.deauthorize_all(all_node_ids)
                    self._last_authorized_node = None
                    self.status['zerotier_last_action'] = (
                        f"No booking: deauthorized {len(result['deauthorized'])} members"
                    )
                    logger.info(f"ZeroTier: No booking, deauthorized all members")
