"""Opt-in real WebSocket generation; never run by the offline pytest suite."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import httpx
from websockets.asyncio.client import connect


async def verify():
    if os.environ.get('AUTOBUILD_LIVE_VERIFY') != '1':
        raise RuntimeError('Set AUTOBUILD_LIVE_VERIFY=1 to permit one real WebSocket inference')
    customer = key = None
    csrf = None
    model = os.environ.get('AUTOBUILD_LIVE_WS_MODEL', 'gpt-6-astra')
    since = datetime.now(timezone.utc).isoformat()
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8787', trust_env=False,
                                 headers={'Origin': 'http://127.0.0.1:8787'}, timeout=30) as admin:
        async def control(path, method='GET', payload=None):
            response = await admin.request(method, path, json=payload,
                headers={'X-CSRF-Token': csrf} if csrf else {})
            if not response.is_success:
                raise RuntimeError(f'Admin HTTP {response.status_code}')
            return response.json()

        try:
            config = json.loads(Path('data/local-gateway/admin-data/local-config.json').read_text())
            csrf = (await control('/api/session', 'POST', {'token': config['admin_token']}))['csrf_token']
            customer = await control('/api/service/customers', 'POST', {'name': 'Live WebSocket verification'})
            key = await control('/api/service/keys', 'POST', {'customer_id': customer['id'],
                'name': 'Temporary WebSocket verification', 'policy': {
                'model_ids': [model], 'protocols': ['openai'], 'total_micro': '100000000000000',
                    'expires_at': (datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat(),
                    'model_overrides': [{'model_id': model, 'input_micro': '1000000',
                        'output_micro': '2000000', 'cache_read_micro': '100000', 'cache_write_micro': '1000000'}]}})
            async with asyncio.timeout(180):
                async with connect('ws://127.0.0.1:8788/v1/responses', proxy=None,
                        additional_headers={'Authorization': 'Bearer '+key['secret']}) as socket:
                    await socket.send(json.dumps({'type': 'response.create', 'model': model,
                        'input': 'Reply only API_WS_OK.', 'reasoning': {'effort': 'low'}}))
                    while True:
                        event = json.loads(await socket.recv())
                        if event['type'] == 'error':
                            message = str(event.get('error', {}).get('message', '')).lower()
                            hints = ','.join(word for word in
                                ('model', 'unsupported', 'invalid', 'reasoning', 'authentication', 'quota')
                                if word in message)
                            raise RuntimeError('Gateway WebSocket error: '+event['error']['code']
                                               +(' ['+hints+']' if hints else ''))
                        if event['type'] == 'response.completed':
                            result = event['response']
                            break
                        if event['type'] in {'response.failed', 'response.incomplete'}:
                            raise RuntimeError('WebSocket response did not complete')
            answer = ''.join(part['text'] for item in result['output'] if item['type'] == 'message'
                             for part in item['content'] if part['type'] == 'output_text')
            assert answer.strip().rstrip('.') == 'API_WS_OK'
            usage = result['usage']
            assert usage['input_tokens'] > 0 and usage['output_tokens'] > 0
            cached = usage['input_tokens_details']['cached_tokens']
            write = usage['input_tokens_details'].get('cache_write_tokens', 0)
            charge = ((usage['input_tokens']-cached-write)*1_000_000
                      +cached*100_000+write*1_000_000+usage['output_tokens']*2_000_000)
            report = await control('/api/service/usage/requests?'+str(httpx.QueryParams({'from': since, 'limit': 100})))
            rows = [row for row in report['items'] if row['key_id'] == key['key_id'] and row['status'] == 'completed']
            assert len(rows) == 1
            row = rows[0]
            assert row['usage']['input_tokens'] == str(usage['input_tokens'])
            assert row['usage']['output_tokens'] == str(usage['output_tokens'])
            assert row['usage']['cached_read'] == str(cached)
            assert row['charged_micro'] == row['computed_micro'] == str(charge)
            saved = next(item for item in await control('/api/service/keys') if item['key_id'] == key['key_id'])
            assert saved['balance']['spent_micro'] == str(charge)
            assert saved['balance']['held_micro'] == '0'
            assert saved['balance']['available_micro'] == str(100_000_000_000_000-charge)
            print(json.dumps({'check': 'real_websocket_generation', 'completed': True,
                'input': usage['input_tokens'], 'output': usage['output_tokens'], 'cached': cached,
                'reasoning': usage['output_tokens_details']['reasoning_tokens'],
                'charged_micro': str(charge), 'held_micro': '0'}))
        finally:
            cleanups = []
            if key:
                cleanups.append(control(f"/api/service/keys/{key['key_id']}/revoke", 'POST', {'version': key['version']}))
            if customer:
                cleanups.append(control(f"/api/service/customers/{customer['id']}", 'DELETE', {'version': 1}))
            outcomes = await asyncio.gather(*cleanups, return_exceptions=True)
            if any(isinstance(outcome, BaseException) for outcome in outcomes):
                raise RuntimeError('Temporary verification record cleanup failed')


if __name__ == '__main__':
    # HTTP/WebSocket exception strings may contain transport headers. Emit only
    # the exception class on failure; never dump private requests or tracebacks.
    try:
        asyncio.run(verify())
    except Exception as error:
        detail = str(error) if type(error) is RuntimeError and str(error).startswith((
            'Admin HTTP ', 'Gateway WebSocket error: ', 'WebSocket response did not complete',
            'Temporary verification record cleanup failed', 'Set AUTOBUILD_LIVE_VERIFY=')) else None
        print(json.dumps({'check': 'real_websocket_generation', 'failed': type(error).__name__, 'detail': detail}))
        raise SystemExit(1) from None
