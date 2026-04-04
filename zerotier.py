"""
ZeroTier API client for managing network member authorization.

Uses the ZeroTier Central API (https://api.zerotier.com/api/v1)
to authorize/deauthorize members on a ZeroTier network, controlling
which club members can reach the FlexRadio at any given time.

Each user stores their ZeroTier Node ID (10-char hex) in the database.
The scheduler authorizes the user when their slot starts, and
deauthorizes them (and everyone else) when the slot ends.
"""

import logging
import requests

logger = logging.getLogger(__name__)

ZEROTIER_API_BASE = 'https://api.zerotier.com/api/v1'


class ZeroTierClient:
    """Client for the ZeroTier Central API."""

    def __init__(self, api_token, network_id):
        self.api_token = api_token
        self.network_id = network_id
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {api_token}',
            'Content-Type': 'application/json',
        })

    def _url(self, path):
        return f'{ZEROTIER_API_BASE}{path}'

    def test_connection(self):
        """Verify the API token and network ID are valid."""
        try:
            resp = self.session.get(
                self._url(f'/network/{self.network_id}'),
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                return {
                    'ok': True,
                    'network_name': data.get('config', {}).get('name', ''),
                    'member_count': data.get('totalMemberCount', 0),
                }
            elif resp.status_code == 401:
                return {'ok': False, 'error': 'Invalid API token'}
            elif resp.status_code == 404:
                return {'ok': False, 'error': 'Network not found'}
            else:
                return {'ok': False, 'error': f'HTTP {resp.status_code}'}
        except requests.RequestException as e:
            return {'ok': False, 'error': str(e)}

    def get_members(self):
        """Get all members of the network."""
        try:
            resp = self.session.get(
                self._url(f'/network/{self.network_id}/member'),
                timeout=10
            )
            resp.raise_for_status()
            members = resp.json()
            return [
                {
                    'node_id': m.get('nodeId', ''),
                    'name': m.get('name', ''),
                    'description': m.get('description', ''),
                    'authorized': m.get('config', {}).get('authorized', False),
                    'online': m.get('online', False),
                    'ip_assignments': m.get('config', {}).get('ipAssignments', []),
                }
                for m in members
            ]
        except requests.RequestException as e:
            logger.error(f"Failed to get ZeroTier members: {e}")
            return []

    def get_member(self, node_id):
        """Get a specific member's info."""
        try:
            resp = self.session.get(
                self._url(f'/network/{self.network_id}/member/{node_id}'),
                timeout=10
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            m = resp.json()
            return {
                'node_id': m.get('nodeId', ''),
                'name': m.get('name', ''),
                'authorized': m.get('config', {}).get('authorized', False),
                'online': m.get('online', False),
                'ip_assignments': m.get('config', {}).get('ipAssignments', []),
            }
        except requests.RequestException as e:
            logger.error(f"Failed to get ZeroTier member {node_id}: {e}")
            return None

    def authorize_member(self, node_id, description=''):
        """Authorize a member on the network (allow them to connect)."""
        try:
            payload = {
                'config': {'authorized': True},
            }
            if description:
                payload['description'] = description
            resp = self.session.post(
                self._url(f'/network/{self.network_id}/member/{node_id}'),
                json=payload,
                timeout=10
            )
            resp.raise_for_status()
            logger.info(f"ZeroTier: Authorized node {node_id}")
            return True
        except requests.RequestException as e:
            logger.error(f"Failed to authorize ZeroTier node {node_id}: {e}")
            return False

    def deauthorize_member(self, node_id):
        """Deauthorize a member on the network (block them from connecting)."""
        try:
            payload = {
                'config': {'authorized': False},
            }
            resp = self.session.post(
                self._url(f'/network/{self.network_id}/member/{node_id}'),
                json=payload,
                timeout=10
            )
            resp.raise_for_status()
            logger.info(f"ZeroTier: Deauthorized node {node_id}")
            return True
        except requests.RequestException as e:
            logger.error(f"Failed to deauthorize ZeroTier node {node_id}: {e}")
            return False

    def authorize_only(self, allowed_node_id, all_member_node_ids):
        """Authorize one member and deauthorize all others.

        Args:
            allowed_node_id: The node ID to authorize (or None to deauthorize all).
            all_member_node_ids: List of all club member node IDs to manage.

        Returns:
            dict with 'authorized' and 'deauthorized' lists.
        """
        result = {'authorized': [], 'deauthorized': [], 'errors': []}

        for node_id in all_member_node_ids:
            if not node_id:
                continue

            if node_id == allowed_node_id:
                if self.authorize_member(node_id):
                    result['authorized'].append(node_id)
                else:
                    result['errors'].append(node_id)
            else:
                if self.deauthorize_member(node_id):
                    result['deauthorized'].append(node_id)
                else:
                    result['errors'].append(node_id)

        return result

    def deauthorize_all(self, member_node_ids):
        """Deauthorize all specified members."""
        result = {'deauthorized': [], 'errors': []}
        for node_id in member_node_ids:
            if not node_id:
                continue
            if self.deauthorize_member(node_id):
                result['deauthorized'].append(node_id)
            else:
                result['errors'].append(node_id)
        return result
