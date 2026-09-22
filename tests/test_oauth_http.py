import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from ksp_aihub.config import Configuration
from ksp_aihub.hub import Hub
from ksp_aihub.server import GatewayServer
from test_hub import MemoryStore, configuration


class Issuer(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        query = parse_qs(urlsplit(self.path).query)
        self.server.authorization = query
        self.send_response(302)
        self.send_header('Location', query['redirect_uri'][0] + '?' + urlencode({'state': query['state'][0], 'code': 'mock-auth-code'}))
        self.end_headers()
    def do_POST(self):
        form = parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
        self.server.forms.append(form)
        if form['grant_type'][0] == 'authorization_code':
            verifier = form['code_verifier'][0]
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
            if challenge != self.server.authorization['code_challenge'][0] or form['code'][0] != 'mock-auth-code':
                self.send_error(400); return
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
        self.wfile.write(json.dumps({'token_type': 'Bearer', 'access_token': 'independent-access-token',
                                   'refresh_token': 'independent-refresh-token', 'expires_in': 3600}).encode())


class OAuthHttpTests(unittest.TestCase):
    def test_browser_pkce_round_trip_through_gateway_callback(self):
        issuer = ThreadingHTTPServer(('127.0.0.1', 0), Issuer)
        issuer.forms = []
        thread = threading.Thread(target=issuer.serve_forever, daemon=True); thread.start()
        try:
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
            issuer_url = f'http://127.0.0.1:{issuer.server_port}'
            config = configuration(issuer_url, port)
            config['credentials']['key'].update(kind='oauth_managed', provider='openai_compatible', registration='registered')
            config['profiles']['one']['provider'] = 'openai_compatible'
            config['oauthRegistrations']['registered'] = {
                'enabled': True, 'clientId': 'mock-registered-client', 'authorizationUrl': issuer_url + '/authorize',
                'tokenUrl': issuer_url + '/token', 'redirectUri': f'http://127.0.0.1:{port}/oauth/callback',
                'scopes': ['inference'], 'allowedApiOrigins': [issuer_url],
            }
            with tempfile.TemporaryDirectory() as state:
                store = MemoryStore(); hub = Hub(Configuration(config), state, store)
                token = 'local-gateway-test-token-32-characters'
                server = GatewayServer(hub, token)
                gateway_thread = threading.Thread(target=server.serve_forever, daemon=True); gateway_thread.start()
                try:
                    request = Request(f'http://127.0.0.1:{port}/v1/auth/begin', data=b'{"credential":"key"}',
                                      headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
                    with urlopen(request, timeout=5) as response: started = json.load(response)
                    # Simulated browser follows the issuer redirect; no IPC token on the callback.
                    with urlopen(started['authorizationUrl'], timeout=5) as response: finished = json.load(response)
                    self.assertTrue(finished['ok'])
                    self.assertEqual(store.get('key')['access'], 'independent-access-token')
                    self.assertEqual(hub.auth.status('key'), 'ready')
                    self.assertEqual(issuer.forms[0]['client_id'], ['mock-registered-client'])
                    self.assertNotIn('refresh_token', json.dumps(started))
                    self.assertNotIn('independent-access-token', json.dumps(finished))
                finally:
                    server.shutdown(); server.server_close(); gateway_thread.join()
        finally:
            issuer.shutdown(); issuer.server_close(); thread.join()


if __name__ == '__main__': unittest.main()
