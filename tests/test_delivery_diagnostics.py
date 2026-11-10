"""Do not lose safe failure evidence or retry an ambiguous send.

The subprocess is the external-effect seam; Store, Outbox and HermesSend are real.
"""
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from continuity.store import Store
from continuity.outbox import Outbox
from continuity.delivery import HermesSend


class DeliveryDiagnosticsTests(unittest.TestCase):
    def test_disconnected_bridge_is_unknown_without_private_error_or_retry(self):
        with tempfile.TemporaryDirectory() as home:
            store = Store(home + '/data')
            store.configure('mode', 'active')
            item = store.create_item('commitment', 'person', 'Review', 'source', 'quote')
            outbox = Outbox(store)
            key = outbox.prepare(item)
            now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc).timestamp()
            # Exact host standalone sender shape, not a receipt.
            reply = SimpleNamespace(returncode=1, stdout=json.dumps({
                'error': 'WhatsApp bridge error (503): {"error":"Not connected to WhatsApp"}'
            }), stderr='PRIVATE-CONTENT')
            with patch('continuity.delivery.subprocess.run', return_value=reply):
                with self.assertLogs('continuity.outbox', level='WARNING') as logged:
                    self.assertFalse(outbox.dispatch(key, lambda _: HermesSend('whatsapp:owner')('notice'), now))
            receipt = outbox.get(key)
            assert receipt is not None
            self.assertEqual(receipt['state'], 'unknown')
            self.assertIsNone(receipt['receipt'])
            self.assertIn('reason=bridge_unavailable', '\n'.join(logged.output))
            self.assertNotIn('PRIVATE-CONTENT', '\n'.join(logged.output))
            self.assertNotIn('whatsapp:owner', '\n'.join(logged.output))
            self.assertFalse(outbox.dispatch(key, lambda _: self.fail('unknown resent'), now + 10800))

    def test_untrusted_exception_text_never_enters_diagnostic(self):
        with tempfile.TemporaryDirectory() as home:
            store = Store(home + '/data'); store.configure('mode', 'active')
            item = store.create_item('commitment', 'person', 'Review', 'source', 'quote')
            outbox = Outbox(store); key = outbox.prepare(item)
            def fail(_):
                raise ValueError('PRIVATE-CONTENT')
            with self.assertLogs('continuity.outbox', level='WARNING') as logged:
                self.assertFalse(outbox.dispatch(key, fail, datetime(2026, 11, 2, 12, tzinfo=timezone.utc).timestamp()))
            self.assertIn('reason=transport_exception', '\n'.join(logged.output))
            self.assertNotIn('PRIVATE-CONTENT', '\n'.join(logged.output))

    def test_timeout_does_not_expose_command_or_output(self):
        with patch('continuity.delivery.subprocess.run', side_effect=subprocess.TimeoutExpired('PRIVATE-COMMAND', 30, output='PRIVATE-OUTPUT')):
            with self.assertRaisesRegex(ValueError, '^send_timeout$'):
                HermesSend('whatsapp:owner')('notice')
