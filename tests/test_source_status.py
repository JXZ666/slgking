import json
import unittest
from unittest.mock import patch

import slg_source_status as status
import slg_sync_server as sync


class SourceStatusTests(unittest.TestCase):
    def test_only_public_fields_and_normalized_times(self):
        value = status.public_status(dict(source_id='f95zone', state='partial',
            reason_code='daily_budget', last_success='1791300000.25', requests_used=27,
            raw_error='private token', url='https://user:password@example.org',
            headers={'Authorization': 'secret'}), 'f95zone')
        self.assertNotIn('raw_error', value)
        self.assertNotIn('url', value)
        self.assertNotIn('headers', value)
        self.assertIn('+00:00', value['last_success'])
        self.assertEqual(value['requests_used'], 27)

    def test_invalid_identity_state_times_and_counts(self):
        base = dict(source_id='dlsite', state='complete')
        for extra in ({'source_id':'f95zone'}, {'state':'done'}, {'requests_used':True},
                      {'pending_due':-1}, {'last_success':'password'},
                      {'finished_at':float('nan')}, {'finished_at':float('inf')}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                status.public_status(dict(base, **extra), 'dlsite')

    def test_error_reason_cannot_leak_text(self):
        result = status.public_status(dict(state='error', reason_code='token=abcdef'), 'dlsite')
        self.assertEqual(result['reason_code'], 'unknown')

    def test_arbitrary_status_route_is_never_requested(self):
        for path in ('https://evil.test/data', '//evil.test/data', '/catalogues/dlsite/status.json',
                     '/catalogues/western_slg/status.json?token=x'):
            with patch.object(sync, '_server_http_get') as fetch:
                self.assertIsNone(sync._collector_status({'source_status_path':path}, 'f95zone'))
                fetch.assert_not_called()

    def test_optional_telemetry_failure_never_breaks_catalogue(self):
        manifest = {'source_status_path':status.STATUS_PATHS['f95zone']}
        for raw in (b'not json', b'x'*8193, json.dumps({'state':'bad'}).encode()):
            with patch.object(sync, '_server_http_get', return_value=raw):
                self.assertIsNone(sync._collector_status(manifest, 'f95zone'))
        with patch.object(sync, '_server_http_get', side_effect=OSError('offline')):
            self.assertIsNone(sync._collector_status(manifest, 'f95zone'))

    def test_cached_catalogue_does_not_mean_collecting_complete(self):
        self.assertIn('等待首次采集', status.status_note({'state':'partial','source_id':'dlsite',
                                                       'reason_code':'awaiting_first_run'}))
        self.assertIn('采集暂停', status.status_note({'state':'paused','source_id':'f95zone'}))
        self.assertEqual(status.status_note(None), '')


if __name__ == '__main__':
    unittest.main()
