"""GPT-SoVITS adapter. All torch/upstream knowledge belongs in this file."""
from io import BytesIO
import hashlib
import logging
import os
from pathlib import Path
import sys
import time
import wave

from ..backend import AudioOutput, SynthesisInput
from ..text_chunking import split_by_length

logger = logging.getLogger(__name__)


class GPTSoVITSBackend:
    def __init__(self):
        self.model = None
        self.torch = None
        self.loaded = 0
        self.placements = {}

    def load(self, options, base_directory):
        if self.model is not None:
            raise RuntimeError('Model is already loaded')
        self.options = options
        self.base = base_directory
        self.root = self.resolve(options['repo_root'])
        if not (self.root / 'GPT_SoVITS').is_dir():
            raise FileNotFoundError(f'GPT-SoVITS directory does not exist: {self.root}')
        # Upstream expects its repository as cwd. Only this adapter needs that behavior.
        os.chdir(self.root)
        sys.path[:0] = [str(self.root), str(self.root / 'GPT_SoVITS')]
        for key, value in {
            'G2PW': '0', 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
            'CUDAGraph': '0', 'HF_HOME': str(self.root / 'local-test/hf-cache'),
            'NLTK_DATA': str(self.root / 'local-test/nltk_data'),
            'OMP_NUM_THREADS': str(options.get('threads', 6)),
            'MKL_NUM_THREADS': str(options.get('threads', 6)),
        }.items():
            os.environ[key] = value
        import numpy as np
        import torch
        self.np, self.torch = np, torch
        torch.set_num_threads(options.get('threads', 6))
        torch.set_num_interop_threads(1)
        self.device = torch.device(options.get('device', 'xpu'))
        if self.device.type == 'xpu' and not torch.xpu.is_available():
            raise RuntimeError('Intel XPU is unavailable. Install the XPU runtime/driver; no CPU fallback is used.')
        if self.device.type == 'cuda' and not torch.cuda.is_available():
            raise RuntimeError('CUDA is unavailable; no CPU fallback is used.')
        self.version = options['version']
        self.voices = options['voices']
        self.voice_identity = {}
        for voice in self.voices.values():
            if not self.resolve(voice['options']['reference_audio']).is_file():
                raise FileNotFoundError('Reference audio does not exist')
            if not voice['options'].get('reference_text', '').strip():
                raise ValueError('Reference transcript is required')
            reference = self.resolve(voice['options']['reference_audio'])
            self.voice_identity[voice['id']] = dict(reference_audio=str(reference),
                reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),
                reference_text=voice['options']['reference_text'],
                reference_language=voice['options'].get('reference_language', 'ja'))
        paths = {}
        for key in ['t2s_weights_path', 'vits_weights_path', 'bert_base_path', 'cnhuhbert_base_path']:
            path = (self.root / options[key]).resolve()
            if not path.exists():
                raise FileNotFoundError(f'Model component is missing: {path}')
            paths[key] = str(path)
        self.weight_paths = {key: paths[key] for key in ('t2s_weights_path', 'vits_weights_path')}
        if self.version in ('v2Pro', 'v2ProPlus'):
            speaker_weights = self.root / 'GPT_SoVITS/pretrained_models/sv/pretrained_eres2netv2w24s4ep4.ckpt'
            if not speaker_weights.is_file():
                raise FileNotFoundError(f'Speaker encoder weights are missing: {speaker_weights}')
        from TTS_infer_pack.TTS import TTS, TTS_Config
        from TTS_infer_pack.text_segmentation_method import get_method_names
        self.text_split_method = options.get('text_split_method', 'cut0')
        if self.text_split_method not in get_method_names():
            raise ValueError(f'Unknown text split method: {self.text_split_method}')
        self.max_segment_chars = options.get('max_segment_chars', 150)
        split_by_length('', self.max_segment_chars)  # Validate before loading GPU weights.
        self.last_segment_char_lengths = []
        started = time.monotonic()
        config = TTS_Config({'custom': dict(paths, version=self.version,
                                          device=str(self.device), is_half=options.get('is_half', False))})
        self.model = TTS(config)
        self.sync()
        if self.model.configs.version != self.version:
            raise RuntimeError(f'Loaded model version {self.model.configs.version}, expected {self.version}')
        self.placements = self.model_placement()
        self.loaded += 1
        self.load_seconds = round(time.monotonic() - started, 3)

    def resolve(self, value):
        path = Path(value)
        return (self.base / path).resolve() if not path.is_absolute() else path.resolve()

    def sync(self):
        if self.device.type == 'xpu':
            self.torch.xpu.synchronize(self.device)
        elif self.device.type == 'cuda':
            self.torch.cuda.synchronize(self.device)

    def model_placement(self):
        placements = {}
        for name in ['t2s_model', 'vits_model', 'bert_model', 'cnhuhbert_model', 'vocoder']:
            module = getattr(self.model, name, None)
            if module is not None:
                parameter = next(module.parameters())
                if parameter.device.type != self.device.type:
                    raise RuntimeError(f'{name} is unexpectedly on {parameter.device}')
                placements[name] = dict(device=str(parameter.device), dtype=str(parameter.dtype),
                                        identity=id(module))
        speaker = getattr(self.model, 'sv_model', None)
        if speaker is not None:
            module = speaker.embedding_model
            parameter = next(module.parameters())
            if parameter.device.type != self.device.type:
                raise RuntimeError(f'sv_model is unexpectedly on {parameter.device}')
            placements['sv_model'] = dict(device=str(parameter.device), dtype=str(parameter.dtype),
                                         identity=id(module))
        return placements

    def synthesize(self, request: SynthesisInput):
        if self.model is None:
            raise RuntimeError('Backend has not loaded')
        voice = self.voices[request.voice]['options']
        torch, np = self.torch, self.np
        if request.language not in self.model.configs.languages:
            raise ValueError('Language is not supported by this model')
        segments = split_by_length(request.text, self.max_segment_chars)
        self.last_segment_char_lengths = [len(segment) for segment in segments]
        logger.info('TTS character chunks: %s', self.last_segment_char_lengths)
        inputs = dict(
            text=request.text, text_lang=request.language,
            ref_audio_path=str(self.resolve(voice['reference_audio'])),
            prompt_text=voice['reference_text'], prompt_lang=voice.get('reference_language', 'ja'),
            speed_factor=request.speed, seed=request.seed,
            sample_steps=self.options.get('sample_steps', 32),
            cfg_rate=self.options.get('cfg_rate', 1.3),
            batch_size=1, text_split_method=self.text_split_method, parallel_infer=False,
            split_bucket=False,
            use_cuda_graph=False, use_flash_attention=False,
            super_sampling=False, return_fragment=False, streaming_mode=False,
            fragment_interval=.3,
        )
        with torch.inference_mode():
            if self.device.type == 'xpu':
                torch.xpu.manual_seed_all(request.seed)
            chunks = []
            for segment in segments:
                # Separate calls prevent upstream from merging a short final
                # segment back into the previous one. The model stays loaded.
                chunks.extend(self.model.run(dict(inputs, text=segment)))
            self.sync()
        if not chunks or len({rate for rate, _ in chunks}) != 1:
            raise RuntimeError('Model did not return consistent audio')
        rate = chunks[0][0]
        audio = np.concatenate([np.asarray(data).reshape(-1) for _, data in chunks])
        return self._audio_output(rate, audio)

    def _audio_output(self, rate, audio):
        np = self.np
        if not len(audio) or not np.isfinite(audio).all():
            raise RuntimeError('Model returned invalid audio')
        normalized = audio.astype(np.float64) / (32768 if np.issubdtype(audio.dtype, np.integer) else 1)
        if len(audio) < rate * .1 or float(np.sqrt(np.mean(normalized**2))) < .0001:
            raise RuntimeError('Model returned empty or silent audio')
        pcm = np.rint(np.clip(normalized, -1, 32767 / 32768) * 32768).astype('<i2')
        buffer = BytesIO()
        with wave.open(buffer, 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes(pcm.tobytes())
        return AudioOutput(buffer.getvalue(), rate, len(audio) / rate)

    def synthesize_batch(self, requests, batch_size=None):
        """Use one upstream semantic batch and preserve exact per-text fragments.

        The service calls this on its sole inference thread. Instance hooks are
        restored even on failure; no second model or guessed silence boundary is used.
        """
        requests = list(requests)
        if self.model is None:
            raise RuntimeError('Backend has not loaded')
        if self.version not in ('v2Pro', 'v2ProPlus'):
            raise NotImplementedError('Independent batch output is enabled for V2Pro/Plus only')
        if not 1 <= len(requests) <= 4:
            raise ValueError('A batch must contain 1 to 4 texts')
        size = len(requests) if batch_size is None else batch_size
        if not isinstance(size, int) or not 1 <= size <= 4:
            raise ValueError('batch_size must be 1 to 4')
        first = requests[0]
        identity = (first.language, first.voice, first.speed, first.seed)
        for request in requests:
            if (request.language, request.voice, request.speed, request.seed) != identity:
                raise ValueError('Batch texts must share language, voice, speed and seed')
            if not request.text.strip() or len(request.text) > self.max_segment_chars:
                raise ValueError('Each batch text must be nonempty and at most max_segment_chars')
        if first.language not in self.model.configs.languages:
            raise ValueError('Language is not supported by this model')
        processor = self.model.text_preprocessor
        original_preprocess = processor.preprocess
        original_postprocess = self.model.audio_postprocess
        records, owners, captured = [], [], []
        voice = self.voices[first.voice]['options']

        def preprocess(_text, language, method, version):
            if language != first.language or method != self.text_split_method:
                raise RuntimeError('Unexpected upstream batch preprocessing')
            return records

        def postprocess(audio, sr, batch_index_list=None, speed_factor=1.0,
                        split_bucket=True, fragment_interval=.3, super_sampling=False):
            if split_bucket or super_sampling:
                raise RuntimeError('Independent batch output requires original order and no resampling')
            flat = [fragment for group in audio for fragment in group]
            if len(flat) != len(owners):
                raise RuntimeError('Upstream batch lost or added a text fragment')
            if batch_index_list is not None:
                order = [index for group in batch_index_list for index in group]
                if order != list(range(len(owners))):
                    raise RuntimeError('Unexpected upstream batch reordering')
            grouped = [[] for _ in requests]
            for owner, fragment in zip(owners, flat):
                grouped[owner].append(fragment)
            for fragments in grouped:
                if not fragments:
                    raise RuntimeError('Missing output for a batch text')
                rate, pcm = original_postprocess([fragments], sr, None, speed_factor,
                                                False, fragment_interval, False)
                captured.append(self._audio_output(rate, pcm))
            # Consume the normal upstream generator to completion, including its cleanup.
            return original_postprocess(audio, sr, batch_index_list, speed_factor,
                                        False, fragment_interval, False)

        inputs = dict(text='\n'.join(request.text for request in requests),text_lang=first.language,
            ref_audio_path=str(self.resolve(voice['reference_audio'])),
            prompt_text=voice['reference_text'],prompt_lang=voice.get('reference_language','ja'),
            speed_factor=first.speed,seed=first.seed,batch_size=size,parallel_infer=True,
            text_split_method=self.text_split_method,split_bucket=False,return_fragment=False,
            use_cuda_graph=False,use_flash_attention=False,super_sampling=False,streaming_mode=False,
            fragment_interval=.3)
        with self.torch.inference_mode():
            if self.device.type == 'xpu':
                self.torch.xpu.manual_seed_all(first.seed)
            for index, request in enumerate(requests):
                parts = original_preprocess(request.text, first.language,
                                            self.text_split_method, self.version)
                if not parts:
                    raise ValueError('Batch text produced no phonemes')
                records.extend(parts)
                owners.extend([index] * len(parts))
            try:
                processor.preprocess = preprocess
                self.model.audio_postprocess = postprocess
                for _ in self.model.run(inputs):
                    pass
                self.sync()
            finally:
                processor.preprocess = original_preprocess
                self.model.audio_postprocess = original_postprocess
        if len(captured) != len(requests):
            raise RuntimeError('Model did not return one audio file per batch text')
        self.last_batch_execution = dict(parallel_infer=True,batch_size=size,
            texts=[request.text for request in requests],seed=first.seed)
        return captured

    def info(self):
        info = dict(engine='GPT-SoVITS', version=self.version,
                    model_name=self.options.get('model_name'),
                    device=str(self.device), precision='fp16' if self.options.get('is_half') else 'fp32',
                    model_load_count=self.loaded,
                    text_split_method=self.text_split_method,
                    max_segment_chars=self.max_segment_chars,
                    last_segment_char_lengths=self.last_segment_char_lengths,
                    load_seconds=getattr(self, 'load_seconds', None),
                    resident=self.model is not None, torch=self.torch.__version__,
                    weights=self.weight_paths,
                    voices=self.voice_identity,
                    batch_supported=self.version in ('v2Pro', 'v2ProPlus'),
                    max_batch_size=4 if self.version in ('v2Pro', 'v2ProPlus') else 1,
                    last_batch_execution=getattr(self, 'last_batch_execution', None),
                    model_placement=self.model_placement() if self.model is not None else {})
        if self.version in ('v5dev', 'v5turbo'):
            info.update(sample_steps=self.options.get('sample_steps', 32),
                        cfg_rate=self.options.get('cfg_rate', 1.3))
        if self.device.type in ('xpu', 'cuda'):
            api = getattr(self.torch, self.device.type)
            info.update(gpu_name=api.get_device_name(self.device),
                        allocated_gib=round(api.memory_allocated(self.device) / 2**30, 3),
                        reserved_gib=round(api.memory_reserved(self.device) / 2**30, 3))
        return info

    def close(self):
        # Model references are only released when the service exits.
        self.model = None
