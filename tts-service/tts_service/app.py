from contextlib import asynccontextmanager
import base64
import logging
from pathlib import Path
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .backend import SynthesisInput
from .config import read_settings
from .runtime import BatchNotSupported, NotReady, QueueFull, Runtime

logger = logging.getLogger(__name__)


class SynthesisOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    language: str | None = None
    voice: str | None = None
    speed: float = Field(default=1.0, ge=.5, le=2)
    seed: int = Field(default=20261102, ge=0, le=4294967295)


class TTSRequest(SynthesisOptions):
    text: str = Field(min_length=1, max_length=10000)

    @field_validator('text')
    @classmethod
    def nonblank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError('请输入需要朗读的文本')
        return value


class TTSBatchItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=150)

    @field_validator('id', 'text')
    @classmethod
    def nonblank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError('批次标识和文本不能为空')
        return value


class TTSBatchRequest(SynthesisOptions):
    items: list[TTSBatchItem] = Field(min_length=1, max_length=4)

    @field_validator('items')
    @classmethod
    def unique_ids(cls, value):
        if len({item.id for item in value}) != len(value):
            raise ValueError('批次内的条目标识必须唯一')
        return value


def create_app(config_path: Path) -> FastAPI:
    config_path = config_path.resolve()
    settings = read_settings(config_path)

    @asynccontextmanager
    async def lifespan(app):
        runtime = Runtime(settings, config_path.parent)
        app.state.runtime = runtime
        logger.info('Loading the TTS backend and warming up; the model will stay resident.')
        await runtime.start()
        logger.info('TTS service ready: %s', runtime.backend_info)
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title='本地 TTS 服务', version='1.0.0', lifespan=lifespan,
                  description='接收文本，返回 WAV 音频。模型在启动时加载，后续请求复用。')

    @app.get('/', include_in_schema=False)
    async def home():
        return FileResponse(Path(__file__).with_name('web.html'), headers={'Cache-Control': 'no-store'})

    @app.get('/web.js', include_in_schema=False)
    async def web_script():
        return FileResponse(Path(__file__).with_name('web.js'), media_type='text/javascript',
                            headers={'Cache-Control': 'no-store'})

    @app.get('/health')
    async def health():
        runtime = app.state.runtime
        return JSONResponse(runtime.status(), status_code=200 if runtime.ready else 503)

    @app.get('/voices')
    async def voices():
        return dict(default_voice=settings.default_voice,
                    default_language=settings.default_language, languages=settings.languages,
                    max_text_length=settings.max_text_length,
                    voices=[dict(id=v.id, name=v.name, language=v.language) for v in settings.voices])

    def resolve_options(body):
        voice = body.voice or settings.default_voice
        language = body.language or settings.default_language
        if voice not in {v.id for v in settings.voices}:
            raise HTTPException(422, '未知的音色，请先查看 /voices')
        if language not in settings.languages:
            raise HTTPException(422, '当前后端未配置该语言')
        return voice, language

    @app.post('/tts', response_class=Response, responses={
        200: {'content': {'audio/wav': {}}, 'description': '生成的 WAV 音频'},
        429: {'description': '等待队列已满'},
        503: {'description': '服务尚未就绪'},
    })
    async def tts(body: TTSRequest):
        if len(body.text) > settings.max_text_length:
            raise HTTPException(422, f'文本最多 {settings.max_text_length} 字符')
        voice, language = resolve_options(body)
        request_id = uuid.uuid4().hex
        try:
            result = await app.state.runtime.submit(SynthesisInput(
                body.text, language, voice, body.speed, body.seed))
        except QueueFull:
            raise HTTPException(429, '等待队列已满，请稍后重试', headers={'Retry-After': '5'}) from None
        except NotReady:
            raise HTTPException(503, '服务尚未就绪') from None
        except Exception:
            logger.exception('TTS request failed: %s', request_id)
            raise HTTPException(500, dict(message='语音生成失败，请检查服务日志', request_id=request_id)) from None
        return Response(result.audio.data, media_type=result.audio.media_type, headers={
            'Content-Disposition': 'inline; filename="speech.wav"',
            'Cache-Control': 'no-store',
            'X-Request-Id': request_id,
            'X-Sample-Rate': str(result.audio.sample_rate),
            'X-Audio-Duration': f'{result.audio.duration_seconds:.3f}',
            'X-Synthesis-Seconds': f'{result.synthesis_seconds:.3f}',
            'X-Queue-Seconds': f'{result.queue_seconds:.3f}',
        })

    @app.post('/tts/batch', responses={
        200: {'description': '按输入顺序返回各条完整 WAV 的 base64 与标识'},
        422: {'description': '需 1–4 条文本，每条最多 150 字符，标识不可重复'},
        429: {'description': '等待队列已满'},
        501: {'description': '当前后端不支持批量推理'},
        503: {'description': '服务尚未就绪'},
    })
    async def tts_batch(body: TTSBatchRequest):
        if any(len(item.text) > settings.max_text_length for item in body.items):
            raise HTTPException(422, f'文本最多 {min(settings.max_text_length, 150)} 字符')
        voice, language = resolve_options(body)
        request_id = uuid.uuid4().hex
        requests = [SynthesisInput(item.text, language, voice, body.speed, body.seed)
                    for item in body.items]
        try:
            result = await app.state.runtime.submit_batch(requests)
        except QueueFull:
            raise HTTPException(429, '等待队列已满，请稍后重试', headers={'Retry-After': '5'}) from None
        except NotReady:
            raise HTTPException(503, '服务尚未就绪') from None
        except BatchNotSupported:
            raise HTTPException(501, '当前后端不支持批量推理，请使用 /tts') from None
        except Exception:
            logger.exception('TTS batch request failed: %s', request_id)
            raise HTTPException(500, dict(message='批量语音生成失败，请检查服务日志', request_id=request_id)) from None
        # The runtime validates exact output cardinality before IDs are attached.
        items = [dict(id=item.id, index=index, text=item.text,
                      sample_rate=audio.sample_rate, duration_seconds=audio.duration_seconds,
                      media_type=audio.media_type,
                      audio_base64=base64.b64encode(audio.data).decode('ascii'))
                 for index, (item, audio) in enumerate(zip(body.items, result.audios))]
        return JSONResponse(dict(request_id=request_id, items=items,
                                 synthesis_seconds=result.synthesis_seconds,
                                 queue_seconds=result.queue_seconds),
                            headers={'Cache-Control': 'no-store', 'X-Request-Id': request_id})

    return app
