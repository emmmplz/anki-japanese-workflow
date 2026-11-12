"""One bounded queue and one persistent inference thread for any backend."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import importlib
import logging
import math
from pathlib import Path
import time

from .backend import AudioOutput, SynthesisInput
from .config import Settings

logger = logging.getLogger(__name__)


class QueueFull(Exception):
    pass


class NotReady(Exception):
    pass


class BatchNotSupported(Exception):
    pass


@dataclass
class Job:
    request: SynthesisInput | tuple[SynthesisInput, ...]
    future: asyncio.Future
    submitted: float

    @property
    def size(self):
        return len(self.request) if isinstance(self.request, tuple) else 1


@dataclass(frozen=True)
class Completed:
    audio: AudioOutput
    synthesis_seconds: float
    queue_seconds: float


@dataclass(frozen=True)
class CompletedBatch:
    audios: list[AudioOutput]
    synthesis_seconds: float
    queue_seconds: float


class Runtime:
    def __init__(self, settings: Settings, base_directory: Path, backend=None):
        self.settings = settings
        self.base_directory = base_directory
        self.backend = backend
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='tts-inference')
        self.queue = asyncio.Queue(maxsize=settings.max_queue_size)
        self.ready = False
        self.busy = False
        self.task = None
        self.backend_info = {}
        self.completed = 0
        self.failed = 0
        self.cancelled = 0
        self.completed_batches = 0
        self.failed_batches = 0
        self.started_at = time.monotonic()

    async def in_thread(self, function, *args):
        return await asyncio.get_running_loop().run_in_executor(self.executor, function, *args)

    def load(self):
        if self.backend is None:
            module_name, class_name = self.settings.backend.factory.split(':', 1)
            self.backend = getattr(importlib.import_module(module_name), class_name)()
        options = dict(self.settings.backend.options)
        options['voices'] = {v.id: v.model_dump() for v in self.settings.voices}
        self.backend.load(options, self.base_directory)
        if self.settings.warmup_text:
            self.backend.synthesize(SynthesisInput(
                self.settings.warmup_text, self.settings.default_language, self.settings.default_voice))
        self.backend_info = self.backend.info()

    async def start(self):
        try:
            await self.in_thread(self.load)
        except BaseException:
            if self.backend is not None:
                await self.in_thread(self.backend.close)
            self.executor.shutdown(wait=True)
            raise
        self.ready = True
        self.task = asyncio.create_task(self.consume(), name='tts-queue')

    def synthesize(self, request):
        audio = self.backend.synthesize(request)
        if not audio.data:
            raise RuntimeError('Backend returned empty audio')
        return audio

    def synthesize_batch(self, requests):
        method = getattr(self.backend, 'synthesize_batch', None)
        if not callable(method):
            raise BatchNotSupported()
        audios = method(requests)
        if not isinstance(audios, list) or len(audios) != len(requests):
            raise RuntimeError('Backend batch result count does not match the request count')
        if any(not isinstance(audio, AudioOutput) or not audio.data
               or not math.isfinite(audio.sample_rate) or audio.sample_rate <= 0
               or not math.isfinite(audio.duration_seconds) or audio.duration_seconds <= 0 for audio in audios):
            raise RuntimeError('Backend returned invalid or empty batch audio')
        return audios

    async def consume(self):
        while True:
            job = await self.queue.get()
            try:
                if job is None:
                    return
                if job.future.cancelled():
                    self.cancelled += job.size
                    continue
                self.busy = True
                started = time.monotonic()
                try:
                    if isinstance(job.request, tuple):
                        audios = await self.in_thread(self.synthesize_batch, job.request)
                        result = CompletedBatch(audios, time.monotonic() - started, started - job.submitted)
                        self.completed_batches += 1
                    else:
                        audio = await self.in_thread(self.synthesize, job.request)
                        result = Completed(audio, time.monotonic() - started, started - job.submitted)
                    self.completed += job.size
                    if not job.future.done():
                        job.future.set_result(result)
                except Exception as error:
                    self.failed += job.size
                    if isinstance(job.request, tuple):
                        self.failed_batches += 1
                    logger.exception('TTS synthesis failed')
                    if not job.future.done():
                        # Do not share the suspended worker's traceback with callers.
                        # Clearing that traceback (e.g. in assertRaises) can close
                        # the worker coroutine while it is refreshing metadata.
                        job.future.set_exception(error.with_traceback(None))
                finally:
                    # Reading device statistics also stays on the inference thread.
                    try:
                        self.backend_info = await self.in_thread(self.backend.info)
                    except Exception:
                        # A metrics failure must not silently kill the queue worker.
                        logger.exception('Could not refresh backend metadata')
                    self.busy = False
            finally:
                self.queue.task_done()

    async def submit(self, request):
        if not self.ready:
            raise NotReady()
        future = asyncio.get_running_loop().create_future()
        try:
            self.queue.put_nowait(Job(request, future, time.monotonic()))
        except asyncio.QueueFull:
            raise QueueFull() from None
        try:
            return await future
        except asyncio.CancelledError:
            future.cancel()
            raise

    async def submit_batch(self, requests):
        if not self.ready:
            raise NotReady()
        requests = tuple(requests)
        if not 1 <= len(requests) <= 4:
            raise ValueError('A batch must contain 1-4 requests')
        if not all(isinstance(item, SynthesisInput) and item.text.strip() and len(item.text) <= 150
                   for item in requests):
            raise ValueError('Batch texts must be nonempty and at most 150 characters')
        first = requests[0]
        if any((item.language, item.voice, item.speed, item.seed) !=
               (first.language, first.voice, first.speed, first.seed) for item in requests):
            raise ValueError('Batch requests must share language, voice, speed, and seed')
        if not callable(getattr(self.backend, 'synthesize_batch', None)):
            raise BatchNotSupported()
        # One tuple is one atomic queue entry and one inference-thread call.
        return await self.submit(requests)

    def status(self):
        return dict(status='ready' if self.ready else 'not_ready', busy=self.busy,
                    queued=self.queue.qsize(), queue_capacity=self.settings.max_queue_size,
                    completed_requests=self.completed, failed_requests=self.failed,
                    completed_batches=self.completed_batches, failed_batches=self.failed_batches,
                    cancelled_requests=self.cancelled,
                    batch_supported=callable(getattr(self.backend, 'synthesize_batch', None)),
                    max_batch_size=4, max_batch_text_length=150,
                    uptime_seconds=round(time.monotonic() - self.started_at, 2),
                    backend=self.backend_info)

    async def stop(self):
        self.ready = False
        # Finish an active request and queued requests before unloading at shutdown.
        if self.task is not None:
            await self.queue.put(None)
            await self.task
        if self.backend is not None:
            await self.in_thread(self.backend.close)
        self.executor.shutdown(wait=True)
