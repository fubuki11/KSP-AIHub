import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest

from ksp_aihub.config import Configuration
from ksp_aihub.hub import Hub
from ksp_aihub.server import GatewayServer, connection_file
from test_hub import MemoryStore, configuration
from ksp_aihub.common import HubError

CLIENT = Path(__file__).resolve().parents[2] / 'KSPAutoCraft/client'


@unittest.skipUnless(CLIENT.is_dir(), 'Sibling AutoCraft source required for integration check')
class AutoCraftBridgeTests(unittest.TestCase):
    def test_repetition_recovery_crosses_http_without_changing_profile_defaults(self):
        from unittest.mock import patch
        sys.path.insert(0, str(CLIENT))
        sys.path.insert(0, str(CLIENT.parent / 'tests/python'))
        try:
            from ksp_autocraft.gateway import GatewayModelClient
            from ksp_autocraft.designer import design
            import test_designer as ac
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
            config = configuration(port=port)
            config['profiles']['one'].update(thinkingMode='enabled', stream=False, repetitionRecovery='disable_thinking')
            seen = []
            class Adapter:
                def generate(self, profile, messages, fmt):
                    seen.append(dict(profile))
                    if len(seen) == 1:
                        raise HubError('model_repetition', 'Explicit repetitive termination', 502,
                                       {'finishReason': 'repetition_truncation', 'recoveryAllowed': True, 'requestStreaming': False, 'responseStreaming': False})
                    result = {'plan': ac.PLAN, 'missionSteps': ['Execute mission'], 'assumptions': [], 'rationale': 'fixture'}
                    return {'text': json.dumps(result), 'jsonText': json.dumps(result), 'inputTokens': 100, 'outputTokens': 50}
            with tempfile.TemporaryDirectory() as directory:
                game = Path(directory) / 'game'; installed = game / 'GameData/KSPAIHub'
                (installed / 'Plugins').mkdir(parents=True)
                (installed / 'Plugins/KSPAIHub.dll').write_bytes(b'fixture')
                (installed / 'KSPAIHub.version').write_text(json.dumps({'VERSION': {'MAJOR': 0, 'MINOR': 4, 'PATCH': 0}}))
                store = MemoryStore(); store.put('key', {'apiKey': 'private-test-provider-key'})
                hub = Hub(Configuration(config), Path(directory) / 'state', store, Adapter())
                ipc = connection_file(installed / 'PluginData/connection.json', port)
                server = GatewayServer(hub, ipc['token'])
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                try:
                    model = GatewayModelClient(game)
                    with patch('ksp_autocraft.designer.assess_performance', return_value={'status':'passed','hasBlockingFailure':False,'metrics':{},'issues':[]}):
                        _, report = design(ac.FakeGame(), model, 'rocket')
                    self.assertEqual([p['thinkingMode'] for p in seen], ['enabled', 'disabled'])
                    self.assertEqual([p['maxOutputTokens'] for p in seen], [16000, 16000])
                    self.assertEqual(hub.config.profiles['one']['thinkingMode'], 'enabled')
                    self.assertEqual(report['modelRecoveries'][0]['details']['finishReason'], 'repetition_truncation')
                    self.assertEqual(len(report['modelRequests']), 2)
                    self.assertNotEqual(report['modelRequests'][0]['requestId'], report['modelRequests'][1]['requestId'])
                    self.assertEqual(len(list((Path(directory) / 'state/Diagnostics').glob('generation-*.json'))), 2)
                finally:
                    server.shutdown(); server.server_close(); thread.join()
        finally:
            sys.path.remove(str(CLIENT.parent / 'tests/python')); sys.path.remove(str(CLIENT))

    def test_real_http_route_can_switch_models_between_design_sessions(self):
        sys.path.insert(0, str(CLIENT))
        try:
            from ksp_autocraft.gateway import GatewayModelClient
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
            config = configuration(port=port)
            config['profiles']['two'] = {**config['profiles']['one'], 'model': 'another-model'}
            seen = []
            class Adapter:
                def generate(self, profile, messages, fmt):
                    seen.append(profile['model'])
                    text = json.dumps({'plan': {'name': 'integration fixture'}})
                    return {'text': text, 'jsonText': text, 'inputTokens': 1, 'outputTokens': 1}
            with tempfile.TemporaryDirectory() as directory:
                store = MemoryStore(); store.put('key', {'apiKey': 'private-test-provider-key'})
                hub = Hub(Configuration(config), directory, store, Adapter())
                game = Path(directory) / 'game'
                installed = game / 'GameData/KSPAIHub'
                (installed / 'Plugins').mkdir(parents=True)
                (installed / 'Plugins/KSPAIHub.dll').write_bytes(b'Hub installation fixture')
                (installed / 'KSPAIHub.version').write_text(json.dumps({'VERSION': {'MAJOR': 0, 'MINOR': 4, 'PATCH': 0}}))
                connection = installed / 'PluginData/connection.json'
                credentials = connection_file(connection, port)
                server = GatewayServer(hub, credentials['token'])
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                try:
                    first = GatewayModelClient(game); first.check_ready()
                    hub.select({'scope': 'global', 'profile': 'two', 'model': 'user-selected-model'})
                    result = first.generate([{'role': 'user', 'content': 'first session'}])
                    self.assertEqual(result['plan']['name'], 'integration fixture')
                    second = GatewayModelClient(game); second.check_ready()
                    second.generate([{'role': 'user', 'content': 'second session'}])
                    self.assertEqual(seen, ['model-a', 'user-selected-model'])
                    self.assertNotIn('private-test-provider-key', connection.read_text())
                finally:
                    server.shutdown(); server.server_close(); thread.join()
        finally: sys.path.remove(str(CLIENT))


if __name__ == '__main__': unittest.main()
