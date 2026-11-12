"""Exercise real ASGI routes with a fake backend and no GPU/test-client dependency."""
import asyncio
import base64
from contextlib import asynccontextmanager
from io import BytesIO
import json
from pathlib import Path
import struct
import threading
import unittest
from unittest.mock import patch
import wave

from tts_service.app import create_app
from tts_service.backend import AudioOutput
from tts_service.config import Settings
from tts_service.runtime import Runtime


class FakeBatchBackend:
    def __init__(self):
        self.load_count = 0
        self.batch_calls = []
        self.single_calls = []
        self.thread_ids = []
        self.mismatch = False

    def load(self, options, base):
        self.load_count += 1
        self.thread_ids.append(threading.get_ident())

    @staticmethod
    def audio(text):
        stream = BytesIO()
        with wave.open(stream, 'wb') as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(24000)
            marker = 500 + sum(ord(char) for char in text) % 2000
            target.writeframes(struct.pack('<h', marker) * 2400)
        return AudioOutput(stream.getvalue(), 24000, .1)

    def synthesize(self, request):
        self.thread_ids.append(threading.get_ident())
        self.single_calls.append(request)
        return self.audio(request.text)

    def synthesize_batch(self, requests):
        self.thread_ids.append(threading.get_ident())
        self.batch_calls.append(requests)
        outputs = [self.audio(request.text) for request in requests]
        return outputs[:-1] if self.mismatch else outputs

    def info(self):
        self.thread_ids.append(threading.get_ident())
        return dict(engine='fake', resident=True, model_load_count=self.load_count)

    def close(self):
        self.thread_ids.append(threading.get_ident())


@asynccontextmanager
async def app_for(backend):
    settings = Settings.model_validate(dict(
        warmup_text='', backend=dict(factory='unused:FakeBackend', options={}),
        voices=[dict(id='default', name='Test')],
    ))
    with patch('tts_service.app.read_settings', return_value=settings), \
            patch('tts_service.app.Runtime', side_effect=lambda settings, base: Runtime(settings, base, backend)):
        app = create_app(Path(__file__).with_name('unused-test-config.json'))
        async with app.router.lifespan_context(app):
            yield app


async def call(app, path, body=None):
    payload = json.dumps(body).encode('utf-8') if body is not None else b''
    sent = False
    messages = []

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {'type': 'http.request', 'body': payload, 'more_body': False}
        await asyncio.Future()

    async def send(message):
        messages.append(message)

    await app(dict(type='http', asgi={'version': '3.0', 'spec_version': '2.3'},
                   http_version='1.1', method='POST' if body is not None else 'GET',
                   scheme='http', path=path, raw_path=path.encode(), root_path='',
                   query_string=b'', headers=[(b'content-type', b'application/json')],
                   server=('testserver', 80), client=('127.0.0.1', 1234)), receive, send)
    start = next(message for message in messages if message['type'] == 'http.response.start')
    data = b''.join(message.get('body', b'') for message in messages if message['type'] == 'http.response.body')
    return start['status'], dict(start['headers']), data


class BatchRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_maps_each_wav_and_keeps_single_route_and_model(self):
        backend = FakeBatchBackend()
        async with app_for(backend) as app:
            body = dict(items=[dict(id=str(index), text=text) for index, text in enumerate(
                ['力士。', '操作。', '出席。', '天気。'])], language='ja', voice='default', speed=1.1, seed=42)
            status, headers, payload = await call(app, '/tts/batch', body)
            self.assertEqual(status, 200)
            result = json.loads(payload)
            self.assertEqual(len(backend.batch_calls), 1)
            self.assertEqual(len(backend.single_calls), 0)
            for index, item in enumerate(result['items']):
                self.assertEqual((item['id'], item['index'], item['text']),
                                 (str(index), index, body['items'][index]['text']))
                self.assertEqual(base64.b64decode(item['audio_base64'], validate=True),
                                 backend.audio(item['text']).data)
                self.assertEqual(item['sample_rate'], 24000)
            self.assertEqual({(x.language, x.voice, x.speed, x.seed) for x in backend.batch_calls[0]},
                             {('ja', 'default', 1.1, 42)})
            status, headers, payload = await call(app, '/tts', dict(text='単独。'))
            self.assertEqual(status, 200)
            self.assertEqual(headers[b'content-type'], b'audio/wav')
            self.assertEqual(payload, backend.audio('単独。').data)
            self.assertEqual(backend.load_count, 1)
            self.assertEqual(len(set(backend.thread_ids)), 1)
            self.assertNotEqual(backend.thread_ids[0], threading.get_ident())
            self.assertEqual(app.state.runtime.status()['completed_requests'], 5)
            self.assertEqual(app.state.runtime.status()['completed_batches'], 1)

    async def test_wrong_output_count_is_rejected_and_worker_recovers(self):
        backend = FakeBatchBackend()
        backend.mismatch = True
        async with app_for(backend) as app:
            body = dict(items=[dict(id='a', text='一。'), dict(id='b', text='二。')])
            status, _, payload = await call(app, '/tts/batch', body)
            self.assertEqual(status, 500)
            self.assertNotIn('audio_base64', json.loads(payload))
            self.assertEqual(app.state.runtime.status()['failed_requests'], 2)
            status, _, _ = await call(app, '/tts', dict(text='回復。'))
            self.assertEqual(status, 200)
            self.assertEqual(backend.load_count, 1)

    async def test_bad_batch_is_rejected_before_inference(self):
        backend = FakeBatchBackend()
        async with app_for(backend) as app:
            invalid = [
                dict(items=[]),
                dict(items=[dict(id=str(index), text='一。') for index in range(5)]),
                dict(items=[dict(id='a', text='一。'), dict(id='a', text='二。')]),
                dict(items=[dict(id='a', text=' ')]),
                dict(items=[dict(id='a', text='あ' * 151)]),
                dict(items=[dict(id='a', text='一。', seed=42)]),
                dict(items=[dict(id='a', text='一。')], voice='missing'),
            ]
            for body in invalid:
                status, _, _ = await call(app, '/tts/batch', body)
                self.assertEqual(status, 422, body)
            self.assertEqual(backend.batch_calls, [])

    async def test_optional_batch_keeps_legacy_backend_usable(self):
        backend = FakeBatchBackend()
        backend.synthesize_batch = None
        async with app_for(backend) as app:
            status, _, _ = await call(app, '/tts/batch', dict(items=[dict(id='a', text='一。')]))
            self.assertEqual(status, 501)
            status, _, _ = await call(app, '/tts', dict(text='一。'))
            self.assertEqual(status, 200)
            self.assertFalse(app.state.runtime.status()['batch_supported'])


if __name__ == '__main__':
    unittest.main()
