import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import app as monitor


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        monitor.DB = Path(self.tmp.name) / "test.db"
        monitor.init_db()
        self.client = monitor.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_and_list(self):
        res = self.client.post('/api/hosts', json={'name': 'Local', 'address': '127.0.0.1'})
        self.assertEqual(res.status_code, 201)
        data = self.client.get('/api/hosts').get_json()
        self.assertEqual(data[0]['address'], '127.0.0.1')
        self.assertIsNone(data[0]['online'])

    def test_duplicate(self):
        self.client.post('/api/hosts', json={'address': 'example.com'})
        self.assertEqual(self.client.post('/api/hosts', json={'address': 'example.com'}).status_code, 409)

    def test_invalid_host(self):
        self.assertEqual(self.client.post('/api/hosts', json={'address': '-c 5'}).status_code, 400)

    def test_events_only_on_transition(self):
        self.client.post('/api/hosts', json={'name': 'Phone', 'address': '192.168.0.103'})
        with patch.object(monitor, 'probe', side_effect=[
            ('192.168.0.103', True, 4, False),
            ('192.168.0.103', True, 5, False),
            ('192.168.0.103', False, None, False),
            ('192.168.0.103', False, None, False),
            ('192.168.0.103', True, 3, False),
        ]):
            for _ in range(5):
                self.assertTrue(monitor.check_all())
        monitor.init_db()
        events = self.client.get('/api/events').get_json()
        self.assertEqual([e['online'] for e in events], [1, 0])
        self.assertEqual(events[0]['name'], 'Phone')
        self.assertEqual(events[0]['address'], '192.168.0.103')
        node_id = self.client.get('/api/hosts').get_json()[0]['id']
        self.client.delete(f'/api/hosts/{node_id}')
        self.assertEqual(len(self.client.get('/api/events').get_json()), 2)

    def test_delete(self):
        self.client.post('/api/hosts', json={'address': 'localhost'})
        node_id = self.client.get('/api/hosts').get_json()[0]['id']
        self.assertEqual(self.client.delete(f'/api/hosts/{node_id}').status_code, 204)
        self.assertEqual(self.client.get('/api/hosts').get_json(), [])


if __name__ == '__main__':
    unittest.main()
