"""Offline end-to-end check; synthetic tones are test fixtures, not speech."""
from pathlib import Path
import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import unittest
import uuid
import wave
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills/anki-japanese-v1/scripts'


class ExportDemo(unittest.TestCase):
    def test_input_and_export_with_synthetic_audio(self):
        work = ROOT / 'work' / ('test-' + uuid.uuid4().hex)
        work.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, work, True)
        normalized = work / 'normalized.json'
        subprocess.run([sys.executable, str(SCRIPT / 'prepare_input.py'),
                        '--input', str(ROOT / 'examples/words.csv'), '--output', str(normalized),
                        '--namespace', 'demo'], check=True, capture_output=True)
        parsed = json.loads(normalized.read_text(encoding='utf-8'))
        self.assertEqual(len(parsed['notes']), 3)
        self.assertTrue(all(n['needs_enrichment'] for n in parsed['notes']))

        notes = json.loads((ROOT / 'examples/prepared-notes.json').read_text(encoding='utf-8'))
        manifest = {'config_version': '1.0', 'config_fingerprint': 'synthetic-test', 'audio': {}, 'cards': {}}
        for index, note in enumerate(notes):
            items = [('word', (note.get('tts_word_text') or note['word']) + '。')]
            items += [('sentence', e.get('tts_text') or e['ja']) for e in note['examples']]
            ids = []
            for item_index, (kind, text) in enumerate(items):
                ident = f'{index}-{item_index}'
                filename = f'test-{ident}.wav'
                audio = work / filename
                rate = 24000
                with wave.open(str(audio), 'wb') as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(rate)
                    frames = [int(5000 * math.sin(2 * math.pi * 440 * t / rate)) for t in range(rate // 4)]
                    wav.writeframes(struct.pack('<' + 'h' * len(frames), *frames))
                manifest['audio'][ident] = {'kind': kind, 'text': text, 'file': filename,
                    'sha256': hashlib.sha256(audio.read_bytes()).hexdigest(), 'sample_rate': rate,
                    'duration_seconds': .25, 'voice': 'synthetic-test'}
                ids.append(ident)
            manifest['cards'][note['guid']] = {'word': ids[0], 'examples': ids[1:]}
        manifest_path = work / 'manifest.json'
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
        out = work / 'result'
        subprocess.run([sys.executable, str(SCRIPT / 'export_deck.py'),
                        '--notes', str(ROOT / 'examples/prepared-notes.json'),
                        '--model', str(ROOT / 'skills/anki-japanese-v1/assets/card-model.v1.json'),
                        '--manifest', str(manifest_path), '--output', str(out),
                        '--model-id', '1830401001', '--deck-id', '1830402001'],
                        check=True, capture_output=True)
        report = json.loads((out / '词包验证.json').read_text(encoding='utf-8'))
        self.assertEqual((report['notes'], report['cards'], report['example_slots']), (3, 3, 6))
        self.assertEqual(report['media_files'], 9)
        self.assertTrue(report['media_sha256_verified'])
        enriched = json.loads((out / 'prepared-notes-with-audio.json').read_text(encoding='utf-8'))
        model = json.loads((out / 'prepared-model-with-audio.json').read_text(encoding='utf-8'))
        for note in enriched:
            fields = dict(zip(model['fields'], note['fields']))
            self.assertNotIn('[sound:', fields['正面单词'] + fields['正面用例'])
            self.assertEqual(fields['日语单词'].count('[sound:'), 1)
            self.assertEqual(fields['用例'].count('[sound:'), 2)
        with zipfile.ZipFile(next(out.glob('*.apkg'))) as package:
            self.assertEqual(len(json.loads(package.read('media'))), 9)


if __name__ == '__main__':
    unittest.main()
