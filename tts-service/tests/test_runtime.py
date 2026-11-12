import asyncio
import threading
from pathlib import Path
import unittest

from tts_service.backend import AudioOutput, SynthesisInput
from tts_service.config import Settings
from tts_service.runtime import QueueFull, Runtime


class TestBackend:
    """A substitute backend with no torch/model dependency."""
    def __init__(self):
        self.load_count = 0
        self.calls = []
        self.thread_ids = []
        self.entered = threading.Event()
        self.gate = threading.Event()
        self.closed = False
        self.fail_info = False

    def load(self, options, base):
        self.load_count += 1
        self.thread_ids.append(threading.get_ident())

    def synthesize(self, request):
        self.thread_ids.append(threading.get_ident())
        self.calls.append(request.text)
        if request.text == 'block':
            self.entered.set()
            if not self.gate.wait(5):
                raise TimeoutError('Test gate timed out')
        if request.text == 'fail':
            raise ValueError('Deliberate backend failure')
        return AudioOutput(b'example-wav', 24000, 1.0)

    def info(self):
        if self.fail_info:
            self.fail_info = False
            raise RuntimeError('Deliberate metrics failure')
        return dict(engine='test', resident=True, model_load_count=self.load_count)

    def close(self):
        self.thread_ids.append(threading.get_ident())
        self.closed = True


def settings(queue_size=8):
    return Settings.model_validate(dict(
        max_queue_size=queue_size, warmup_text='',
        backend=dict(factory='tests.test_runtime:TestBackend', options={}),
        voices=[dict(id='default', name='Test')],
    ))


def request(text):
    return SynthesisInput(text, 'ja', 'default')


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def make_runtime(self, queue_size=8, backend=None):
        runtime = Runtime(settings(queue_size), Path(__file__).parent, backend)
        await runtime.start()
        self.addAsyncCleanup(runtime.stop)
        return runtime

    async def test_replaceable_backend_loads_once_and_reuses_thread(self):
        runtime = await self.make_runtime()
        await runtime.submit(request('first'))
        await runtime.submit(request('second'))
        backend = runtime.backend
        self.assertEqual(backend.load_count, 1)
        self.assertEqual(backend.calls, ['first', 'second'])
        self.assertEqual(len(set(backend.thread_ids)), 1)
        self.assertNotEqual(backend.thread_ids[0], threading.get_ident())
        self.assertEqual(runtime.status()['completed_requests'], 2)

    async def test_bounded_queue_serializes_inference(self):
        runtime = await self.make_runtime(1)
        backend = runtime.backend
        first = asyncio.create_task(runtime.submit(request('block')))
        self.assertTrue(await asyncio.to_thread(backend.entered.wait, 2))
        second = asyncio.create_task(runtime.submit(request('second')))
        await asyncio.sleep(.01)
        self.assertTrue(runtime.status()['busy'])
        self.assertEqual(runtime.status()['queued'], 1)
        self.assertEqual(backend.calls, ['block'])
        with self.assertRaises(QueueFull):
            await runtime.submit(request('third'))
        backend.gate.set()
        await asyncio.gather(first, second)
        self.assertEqual(backend.calls, ['block', 'second'])

    async def test_cancelled_waiting_request_is_not_synthesized(self):
        runtime = await self.make_runtime()
        backend = runtime.backend
        first = asyncio.create_task(runtime.submit(request('block')))
        self.assertTrue(await asyncio.to_thread(backend.entered.wait, 2))
        cancelled = asyncio.create_task(runtime.submit(request('cancelled')))
        await asyncio.sleep(.01)
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        backend.gate.set()
        await first
        await runtime.queue.join()
        self.assertEqual(backend.calls, ['block'])
        self.assertEqual(runtime.cancelled, 1)

    async def test_backend_failure_does_not_stop_following_requests(self):
        runtime = await self.make_runtime()
        with self.assertRaises(ValueError):
            await runtime.submit(request('fail'))
        result = await runtime.submit(request('recovered'))
        self.assertEqual(result.audio.sample_rate, 24000)
        self.assertEqual(runtime.failed, 1)
        self.assertEqual(runtime.backend.load_count, 1)

    async def test_metrics_failure_does_not_kill_worker(self):
        runtime = await self.make_runtime()
        runtime.backend.fail_info = True
        await runtime.submit(request('first'))
        await runtime.submit(request('second'))
        self.assertEqual(runtime.completed, 2)


if __name__ == '__main__':
    unittest.main()
