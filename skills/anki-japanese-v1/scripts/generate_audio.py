"""Resumable, model-neutral word/sentence audio generation for Anki v1.0.

The model runtime and checkpoints are external dependencies. A profile can use
the existing TTSBackend protocol directly, or an HTTP endpoint with /health /tts.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib
from io import BytesIO
import json
from pathlib import Path
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
import uuid
import wave


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def resolve(value, base):
    path = Path(value)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def profile_identity(profile, base):
    options = profile['backend']['options']
    repo = resolve(options['repo_root'], base)
    files = {}
    for key in ('t2s_weights_path', 'vits_weights_path'):
        path = resolve(options[key], repo)
        if not path.is_file():
            raise FileNotFoundError(f'Missing external checkpoint: {path}')
        files[key] = dict(path=str(path), sha256=sha256(path))
    for voice in profile['voices']:
        path = resolve(voice['options']['reference_audio'], base)
        if not path.is_file():
            raise FileNotFoundError(f'Missing reference audio: {path}')
        files['reference:' + voice['id']] = dict(path=str(path), sha256=sha256(path),
            text=voice['options']['reference_text'], language=voice['options'].get('reference_language', 'ja'))
    return dict(profile=profile, files=files)


def text_for_word(note):
    override = note.get('tts_word_text')
    text = str(override if override else note['word']).strip()
    if not text:
        raise ValueError('Empty word')
    return text if text[-1] in '。！？!?…' else text + '。'


def load_notes(path):
    raw = read_json(path)
    notes = raw['notes'] if isinstance(raw, dict) else raw
    if not isinstance(notes, list) or not notes:
        raise ValueError('No notes')
    result = []
    for n in notes:
        note = dict(n)
        if not note.get('guid'):
            note['guid'] = hashlib.sha256((note['word']+'\0'+note.get('kana', '')).encode()).hexdigest()[:20]
        if not note.get('examples'):
            raise ValueError(f"Missing examples: {note['word']}")
        translations = note.get('translations', [])
        examples = []
        for i, item in enumerate(note['examples']):
            example = dict(item) if isinstance(item, dict) else dict(ja=item, zh=translations[i] if i<len(translations) else '')
            if not example.get('ja', '').strip():
                raise ValueError(f"Empty example: {note['word']}")
            examples.append(example)
        note['examples'] = examples
        result.append(note)
    if len({n['guid'] for n in result}) != len(result):
        raise ValueError('Duplicate GUIDs')
    return result


def create_plan(notes_path, config_path, output):
    config_path = config_path.resolve()
    config = read_json(config_path)
    base = config_path.parent
    notes = load_notes(notes_path)
    identities = {kind: profile_identity(config['profiles'][kind], base) for kind in ('word', 'sentence')}
    identity_payload = dict(version=config['version'], seed=config['seed'], speed=config.get('speed', 1),
                            normalization=config['normalization'], profiles=identities)
    fingerprint = hashlib.sha256(json.dumps(identity_payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / 'audio-manifest.json'
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        if manifest['config_fingerprint'] != fingerprint or manifest['source_sha256'] != sha256(notes_path):
            raise RuntimeError('Configuration or notes changed; use another output directory to keep caches separate.')
        return manifest, config
    manifest = dict(schema_version='1.0', config_version=config['version'], config_fingerprint=fingerprint,
        source_path=str(notes_path.resolve()), source_sha256=sha256(notes_path),
        generation_identity=identity_payload, profiles=identities, audio={}, jobs={}, cards={}, errors=[],
        created_at=time.strftime('%Y-%m-%dT%H:%M:%S'), status='planned')
    def job(kind, text):
        job_id = kind + '-' + hashlib.sha256((fingerprint+'\0'+kind+'\0'+text).encode()).hexdigest()[:24]
        manifest['jobs'].setdefault(job_id, dict(kind=kind, text=text))
        return job_id
    for note in notes:
        manifest['cards'][note['guid']] = dict(word=job('word', text_for_word(note)),
            examples=[job('sentence', (item.get('tts_text') or item['ja']).strip()) for item in note['examples']])
    atomic_json(manifest_path, manifest)
    return manifest, config


def inspect_wav(data, expected_rate=None):
    import numpy as np
    with wave.open(BytesIO(data)) as source:
        if source.getsampwidth() != 2 or source.getnchannels() != 1:
            raise ValueError('Expected mono 16-bit PCM WAV')
        rate = source.getframerate()
        if expected_rate and rate != expected_rate:
            raise ValueError(f'Expected {expected_rate} Hz, got {rate}')
        pcm = np.frombuffer(source.readframes(source.getnframes()), dtype='<i2').astype(np.float64)/32768
    if len(pcm)<rate*.1 or not np.isfinite(pcm).all():
        raise ValueError('Empty or invalid audio')
    rms = float(np.sqrt(np.mean(pcm**2)))
    if rms < .0001:
        raise ValueError('Silent result')
    return pcm, rate, dict(sample_rate=rate, duration_seconds=len(pcm)/rate,
        rms=rms, peak=float(np.max(np.abs(pcm))), clipping_fraction=float(np.mean(np.abs(pcm)>=32767/32768)))


def normalize_audio(data, options, expected_rate):
    import numpy as np
    pcm, rate, stats = inspect_wav(data, expected_rate)
    block = max(1, int(rate*.02))
    framed = pcm[:len(pcm)//block*block].reshape(-1, block)
    energies = np.mean(framed**2, axis=1)
    active = energies[energies > max(float(energies.max())*.01, 1e-8)]
    active_rms = float(np.sqrt(np.mean(active)))
    gain = min(10**(options['active_rms_dbfs']/20)/active_rms,
               options['peak_limit']/max(stats['peak'], 1e-9), options['max_gain'])
    pcm16 = np.rint(np.clip(pcm*gain, -.999, .999)*32767).astype('<i2')
    stream = BytesIO()
    with wave.open(stream, 'wb') as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(pcm16.tobytes())
    result = stream.getvalue()
    _, _, normalized = inspect_wav(result, expected_rate)
    if normalized['clipping_fraction']:
        raise ValueError('Normalized audio clips')
    return result, dict(raw=stats, normalized=normalized, gain=gain)


def cached_ok(output, entry):
    if not entry:
        return False
    try:
        path = output / entry['file']
        if sha256(path) != entry['sha256']:
            return False
        inspect_wav(path.read_bytes(), entry['sample_rate'])
        return True
    except (OSError, ValueError, KeyError, EOFError, wave.Error):
        return False


class DirectEngine:
    def __init__(self, profile, config, config_base):
        runtime_root = resolve(config['runtime']['module_root'], config_base)
        sys.path.insert(0, str(runtime_root))
        module, name = profile['backend']['factory'].split(':')
        self.backend = getattr(importlib.import_module(module), name)()
        self.profile = profile
        options = dict(profile['backend']['options'])
        options['voices'] = {voice['id']: voice for voice in profile['voices']}
        self.backend.load(options, config_base)
        self.before = self.backend.info()
        from tts_service.backend import SynthesisInput
        self.Input = SynthesisInput
        if profile.get('warmup_text'):
            self.backend.synthesize(self.Input(profile['warmup_text'], 'ja', 'default', config.get('speed',1), config['seed']))
    def synthesize(self, text, config):
        result = self.backend.synthesize(self.Input(text, 'ja', 'default', config.get('speed',1), config['seed']))
        return result.data
    def synthesize_batch(self, items, config):
        if not self.before.get('batch_supported'):
            raise RuntimeError('The selected backend does not advertise true batch inference')
        requests = [self.Input(item['text'], 'ja', 'default', config.get('speed',1), config['seed']) for item in items]
        results = self.backend.synthesize_batch(requests, batch_size=len(requests))
        if len(results) != len(items):
            raise RuntimeError('Direct backend returned the wrong number of batch results')
        self.last_batch_metadata = dict(execution=getattr(self.backend, 'last_batch_execution', None))
        return [result.data for result in results]
    def info(self):
        return self.backend.info()
    def close(self):
        self.backend.close()


class HttpEngine:
    def __init__(self, profile, config, config_base):
        self.url = profile['endpoint'].rstrip('/')
        self.profile = profile
        self.before = self.info()
        expected = profile['backend']['options']
        info = self.before
        for field, value in [('version',expected['version']),('precision','fp16' if expected.get('is_half') else 'fp32')]:
            if info.get(field) != value:
                raise RuntimeError(f'HTTP backend {field} mismatch')
        for field in ('device', 'text_split_method', 'max_segment_chars'):
            if info.get(field) != expected[field]:
                raise RuntimeError(f'HTTP backend {field} mismatch')
        repo = resolve(expected['repo_root'], config_base)
        for key in ('t2s_weights_path', 'vits_weights_path'):
            if Path(info['weights'][key]).resolve() != resolve(expected[key],repo):
                raise RuntimeError('HTTP backend weights mismatch')
        for voice in profile['voices']:
            actual = info.get('voices', {}).get(voice['id'], {})
            wanted = voice['options']
            reference = resolve(wanted['reference_audio'], config_base)
            if (actual.get('reference_sha256') != sha256(reference)
                    or actual.get('reference_text') != wanted['reference_text']
                    or actual.get('reference_language') != wanted.get('reference_language', 'ja')):
                raise RuntimeError('Running HTTP voice reference mismatch; restart with the configured voice')
        # Existing HTTP health does not expose the reference. Validate the exact
        # server configuration path supplied by the local caller.
        server_config = read_json(resolve(profile['server_config'], config_base))
        for actual, wanted in zip(server_config['voices'],profile['voices']):
            a, b = actual['options'], wanted['options']
            actual_path = resolve(a['reference_audio'],resolve(profile['server_config'],config_base).parent)
            if actual_path != resolve(b['reference_audio'],config_base) or a['reference_text'] != b['reference_text']:
                raise RuntimeError('HTTP voice reference mismatch')
    def info(self):
        with urllib.request.urlopen(self.url+'/health',timeout=5) as response:
            health=json.load(response)
        if health['status']!='ready':
            raise RuntimeError('HTTP backend is not ready')
        return health['backend']
    def synthesize(self,text,config):
        request=urllib.request.Request(self.url+'/tts',
            json.dumps(dict(text=text,language='ja',voice='default',speed=config.get('speed',1),seed=config['seed'])).encode(),
            headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=300) as response:
            return response.read()
    def synthesize_batch(self,items,config):
        if not self.before.get('batch_supported'):
            raise RuntimeError('The selected HTTP backend does not advertise true batch inference')
        body = dict(items=items, language='ja', voice='default', speed=config.get('speed',1), seed=config['seed'])
        request = urllib.request.Request(self.url+'/tts/batch', json.dumps(body).encode('utf-8'),
                                        headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=300) as response:
            payload = json.load(response)
        results = payload.get('items')
        if not isinstance(results, list) or len(results) != len(items):
            raise RuntimeError('HTTP backend returned the wrong number of batch results')
        audio = []
        for index, (item, result) in enumerate(zip(items, results)):
            if result.get('index') != index or result.get('id') != item['id'] or result.get('text') != item['text']:
                raise RuntimeError(f'HTTP batch result identity/order mismatch at index {index}')
            if result.get('media_type') != 'audio/wav':
                raise RuntimeError('HTTP batch result is not WAV audio')
            try:
                data = base64.b64decode(result['audio_base64'], validate=True)
            except (KeyError, ValueError, TypeError) as error:
                raise RuntimeError('HTTP batch result contains invalid base64 audio') from error
            # Check the returned metadata against the actual independent file,
            # before any result from this batch can be committed to the manifest.
            _, rate, stats = inspect_wav(data, result.get('sample_rate'))
            if 'duration_seconds' in result and abs(float(result['duration_seconds'])-stats['duration_seconds']) > 1/rate:
                raise RuntimeError('HTTP batch WAV duration metadata mismatch')
            audio.append(data)
        self.last_batch_metadata = {key:payload[key] for key in
            ('request_id','synthesis_seconds','queue_seconds','execution') if key in payload}
        return audio
    def close(self):
        pass


def take_pending_batch(pending, output, manifest, batch_size, max_segment_chars):
    """Group only still-pending short inputs; keep long text on the old path."""
    batch = []
    while pending and len(batch) < batch_size:
        key, job = pending[0]
        if cached_ok(output, manifest['audio'].get(key)):
            pending.pop(0)
            continue
        long_text = len(job['text']) > max_segment_chars
        if long_text and batch:
            break
        batch.append(pending.pop(0))
        if long_text:
            break
    return batch


def execution_summary(manifest, valid_keys=None):
    entries = [entry for key, entry in manifest['audio'].items() if valid_keys is None or key in valid_keys]
    legacy = sum('execution' not in entry for entry in entries)
    parallel = sum(entry.get('execution',{}).get('parallel_infer') is True for entry in entries)
    serial = sum(entry.get('execution',{}).get('parallel_infer') is False for entry in entries)
    groups = sum(count > 0 for count in (legacy, parallel, serial))
    return dict(mode='mixed' if groups > 1 else 'uniform', legacy_without_execution_metadata=legacy,
                recorded_parallel=parallel, recorded_serial=serial,
                note='Validated pre-existing cache is retained. Approved batch execution changes are recorded separately from the unchanged voice/model configuration fingerprint; batch sampling may differ from serial sampling.')


def generate(args):
    output=args.output.resolve()
    manifest,config=create_plan(args.notes,args.config,output)
    kinds = ('word','sentence') if args.kind=='all' else (args.kind,)
    requested_batch_size = getattr(args, 'batch_size', 1)
    if requested_batch_size not in (1,2,4):
        raise ValueError('--batch-size must be 1, 2, or 4')
    if args.transport=='direct' and len(kinds)>1:
        raise ValueError('Run one direct model per process: --kind word, then --kind sentence')
    for kind in kinds:
        kind_job_count=sum(job['kind']==kind for job in manifest['jobs'].values())
        pending=[(key,job) for key,job in manifest['jobs'].items()
                 if job['kind']==kind and not cached_ok(output,manifest['audio'].get(key))]
        cache_reused=kind_job_count-len(pending)
        if args.limit:
            pending=pending[:args.limit]
        if not pending:
            print(json.dumps(dict(kind=kind,pending=0),ensure_ascii=False),flush=True)
            continue
        engine=None
        manifest['status']='running'
        manifest.pop('last_error', None)
        session = dict(id=uuid.uuid4().hex, kind=kind,transport=args.transport,
            started=time.strftime('%Y-%m-%dT%H:%M:%S'),jobs=len(pending),
            requested_batch_size=requested_batch_size, batches=[],
            cache_reused=cache_reused, status='running',
            execution_optimization=dict(policy='true_batch_v1' if requested_batch_size > 1 else 'serial_v1',
                parallel_infer_requested=requested_batch_size > 1, seed=config['seed'],
                fingerprint_preserved=True, independent_audio_results=True))
        manifest.setdefault('sessions',[]).append(session)
        manifest['execution_provenance'] = execution_summary(manifest)
        atomic_json(output/'audio-manifest.json',manifest)
        try:
            engine=(DirectEngine if args.transport=='direct' else HttpEngine)(config['profiles'][kind],config,args.config.resolve().parent)
            session['before']=engine.before
            atomic_json(output/'audio-manifest.json',manifest)
            max_segment_chars = int(config['profiles'][kind]['backend']['options'].get('max_segment_chars') or 150)
            while pending:
                batch = take_pending_batch(pending, output, manifest, requested_batch_size, max_segment_chars)
                if not batch:
                    continue
                members = [key for key, _ in batch]
                items = [dict(id=key,text=job['text']) for key,job in batch]
                parallel = len(batch) > 1
                execution = dict(policy='true_batch_v1' if parallel else 'serial_v1',
                    session_id=session['id'], batch_id=uuid.uuid4().hex,
                    requested_batch_size=requested_batch_size, batch_size=len(batch),
                    member_order=members, seed=config['seed'], parallel_infer=parallel,
                    transport=args.transport)
                if requested_batch_size > 1 and len(batch)==1:
                    execution['serial_reason'] = ('text_exceeds_max_segment_chars'
                        if len(batch[0][1]['text']) > max_segment_chars else 'single_pending_item')
                record = dict(id=execution['batch_id'],member_order=members, batch_size=len(batch),
                    requested_batch_size=requested_batch_size, parallel_infer=parallel, seed=config['seed'],
                    started=time.strftime('%Y-%m-%dT%H:%M:%S'), status='running', attempts=0,
                    wall_seconds_basis='inference_and_validation_including_retries_excludes_media_writes')
                session['batches'].append(record)
                atomic_json(output/'audio-manifest.json',manifest)
                started=time.perf_counter()
                for attempt in range(1,3):
                    record['attempts'] = attempt
                    try:
                        data_items=(engine.synthesize_batch(items,config) if parallel else
                                    [engine.synthesize(items[0]['text'],config)])
                        if not isinstance(data_items,(list,tuple)) or len(data_items)!=len(batch):
                            raise RuntimeError('Engine batch output count mismatch')
                        validated = [normalize_audio(data,config['normalization'],config['profiles'][kind]['sample_rate'])
                                     for data in data_items]
                        break
                    except Exception as failure:
                        record['last_error'] = str(failure)
                        manifest['errors'].append(dict(job_ids=members,batch_id=record['id'],
                            attempt=attempt,error=str(failure),timestamp=time.time()))
                        atomic_json(output/'audio-manifest.json',manifest)
                        if attempt==2:
                            record.update(status='failed',wall_seconds=round(time.perf_counter()-started,4))
                            raise
                record.update(status='validated',wall_seconds=round(time.perf_counter()-started,4))
                if parallel and getattr(engine,'last_batch_metadata',None):
                    record['backend_response'] = engine.last_batch_metadata
                for member_index, ((key,job),data,(result,stats)) in enumerate(zip(batch,data_items,validated)):
                    raw=output/'raw'/f'ankiv1_{key}.wav'
                    normalized=output/'audio'/f'ankiv1_{key}.wav'
                    raw.parent.mkdir(exist_ok=True)
                    normalized.parent.mkdir(exist_ok=True)
                    raw.write_bytes(data)
                    normalized.write_bytes(result)
                    entry=dict(kind=kind,text=job['text'],file=normalized.relative_to(output).as_posix(),
                        raw_file=raw.relative_to(output).as_posix(),sha256=sha256(normalized),raw_sha256=sha256(raw),
                        sample_rate=stats['normalized']['sample_rate'],duration_seconds=stats['normalized']['duration_seconds'],
                        generation_seconds=round(record['wall_seconds']/len(batch),4),
                        generation_seconds_basis='batch_wall_time_equal_share' if parallel else 'individual_wall_time',stats=stats,
                        execution=dict(execution,member_index=member_index),
                        config_fingerprint=manifest['config_fingerprint'],seed=config['seed'],
                        voice=config['profiles'][kind]['name'])
                    manifest['audio'][key]=entry
                    manifest['updated_at']=time.strftime('%Y-%m-%dT%H:%M:%S')
                    atomic_json(output/'audio-manifest.json',manifest)
                    completed=sum(1 for item in manifest['audio'].values() if item['kind']==kind)
                    total=sum(1 for item in manifest['jobs'].values() if item['kind']==kind)
                    print(json.dumps(dict(event='audio_ready',kind=kind,completed=completed,total=total,
                        text=job['text'],generation_seconds=entry['generation_seconds'],
                        generation_seconds_basis=entry['generation_seconds_basis'],batch_id=record['id'],
                        duration_seconds=entry['duration_seconds']),ensure_ascii=False),flush=True)
                record.update(status='completed',completed=time.strftime('%Y-%m-%dT%H:%M:%S'))
                manifest['execution_provenance'] = execution_summary(manifest)
                atomic_json(output/'audio-manifest.json',manifest)
            after=engine.info()
            if after['model_placement']!=engine.before['model_placement'] or after['model_load_count']!=1:
                raise RuntimeError('Model identity changed during generation')
            session['after']=after
            session['completed']=time.strftime('%Y-%m-%dT%H:%M:%S')
            session['status']='completed'
        except BaseException as failure:
            manifest['status']='interrupted'
            manifest['last_error']=str(failure)
            session.update(status='interrupted',last_error=str(failure))
            if session['batches'] and session['batches'][-1]['status']=='running':
                session['batches'][-1].update(status='interrupted',wall_seconds=round(time.perf_counter()-started,4))
            raise
        finally:
            if engine is not None:
                engine.close()
            atomic_json(output/'audio-manifest.json',manifest)
    valid_keys={key for key in manifest['jobs'] if cached_ok(output,manifest['audio'].get(key))}
    done=len(valid_keys)
    manifest['execution_provenance'] = execution_summary(manifest,valid_keys)
    manifest['status']='completed' if done==len(manifest['jobs']) else 'partial'
    manifest['completed_jobs']=done
    manifest['total_jobs']=len(manifest['jobs'])
    atomic_json(output/'audio-manifest.json',manifest)
    print(json.dumps(dict(status=manifest['status'],completed=done,total=len(manifest['jobs'])),ensure_ascii=False),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('plan','run','verify'))
    parser.add_argument('--notes',type=Path,required=True)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--kind',choices=('word','sentence','all'),default='all')
    parser.add_argument('--transport',choices=('direct','http'),default='direct')
    parser.add_argument('--limit',type=int)
    parser.add_argument('--batch-size',type=int,choices=(1,2,4),default=1,
                        help='True inference batch size; 1 preserves serial behavior. Inputs over max_segment_chars stay serial.')
    args=parser.parse_args()
    if args.command=='run':
        generate(args)
    else:
        manifest,_=create_plan(args.notes,args.config,args.output.resolve())
        counts={kind:sum(job['kind']==kind for job in manifest['jobs'].values()) for kind in ('word','sentence')}
        result=dict(cards=len(manifest['cards']),jobs=counts,total_jobs=len(manifest['jobs']))
        if args.command=='verify':
            missing=[key for key in manifest['jobs'] if not cached_ok(args.output.resolve(),manifest['audio'].get(key))]
            result.update(status='passed' if not missing else 'incomplete',missing=missing)
            atomic_json(args.output.resolve()/'audio-validation.json',result)
            if missing:
                print(json.dumps(result,ensure_ascii=False))
                raise SystemExit(1)
        print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
