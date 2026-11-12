"""CPU-only Japanese front-end comparison; never edits the source notes."""
import argparse
from collections import Counter
from difflib import SequenceMatcher
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import sys
import unicodedata


def kana(value):
    value = unicodedata.normalize('NFKC', value)
    value = ''.join(chr(ord(char)-0x60) if '\u30a1' <= char <= '\u30f6' else char for char in value)
    return re.sub(r'[\s。．.、，,；;：:・\-‐‑‒–—―~〜～\'’]', '', value)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--notes', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    import pyopenjtalk
    notes = json.loads(args.notes.read_text(encoding='utf-8-sig'))
    if isinstance(notes, dict):
        notes = notes['notes']
    records = []
    for note in notes:
        word, expected = note['word'], note.get('kana', '')
        text = word + '。'
        frontend = pyopenjtalk.run_frontend(text)
        spoken = pyopenjtalk.g2p(text, kana=True)
        frontend_read = ''.join(token['read'] for token in frontend if token['pos'] != '記号')
        clean_expected = kana(expected)
        source_is_kana = bool(re.fullmatch(r'[ぁ-ゖー]+', clean_expected))
        expected_spoken = pyopenjtalk.g2p(clean_expected+'。', kana=True) if source_is_kana else None
        word_phones = pyopenjtalk.g2p(text)
        kana_phones = pyopenjtalk.g2p(clean_expected+'。') if source_is_kana else None
        if kana(frontend_read) == clean_expected:
            status = 'reading_match'
        elif source_is_kana and (kana(spoken) == kana(expected_spoken) or word_phones == kana_phones):
            status = 'phonetic_equivalent'
        else:
            status = 'needs_review'
        actual_text = str(note.get('tts_word_text') or word).strip()
        if actual_text and actual_text[-1] not in '。！？!?…':
            actual_text += '。'
        actual_frontend = pyopenjtalk.run_frontend(actual_text)
        actual_read = ''.join(token['read'] for token in actual_frontend if token['pos'] != '記号')
        actual_phones = pyopenjtalk.g2p(actual_text)
        effective_match = (kana(actual_read) == clean_expected
                           or source_is_kana and actual_phones == kana_phones)
        record = dict(index=note.get('index'), guid=note['guid'], word=word, source_kana=expected,
            tts_input=text, frontend_read=frontend_read, frontend_pronunciation=spoken,
            kana_input_pronunciation=expected_spoken, frontend_phones=word_phones,
            kana_input_phones=kana_phones, status=status,
            similarity=round(SequenceMatcher(None,kana(frontend_read),clean_expected).ratio(),3),
            proper_name=any('固名' in value or '人名' in value for value in note.get('pos', [])+note.get('meanings', [])),
            affix_marker=bool(re.search(r'[-‐‑‒–—―~〜～]', word+expected)),
            effective_tts_input=actual_text, effective_frontend_read=actual_read,
            effective_phones=actual_phones,
            effective_status='reading_match' if effective_match else 'needs_review',
            has_override=bool(note.get('tts_word_text')),
            frontend_tokens=[{key:token[key] for key in ('string','read','pron','pos','pos_group1','acc','mora_size')} for token in frontend],
            meanings=note.get('meanings', []),
            examples=note.get('examples', []))
        records.append(record)
    assert 'torch' not in sys.modules, 'Audit must not import torch or use GPU'
    report = dict(method='CPU pyopenjtalk: displayed word + Japanese period, compared with source kana. Hiragana/katakana, marks and spacing normalized; frontend read and kana-input pronunciation/phonemes compared before flagging.',
        source=str(args.notes.resolve()), source_sha256=hashlib.sha256(args.notes.read_bytes()).hexdigest(),
        pyopenjtalk_version=importlib.metadata.version('pyopenjtalk-plus'),
        torch_imported=False, counts=dict(Counter(row['status'] for row in records)),
        effective_counts=dict(Counter(row['effective_status'] for row in records)), records=records)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report['counts'],ensure_ascii=False))
    print(json.dumps(dict(effective_counts=report['effective_counts']),ensure_ascii=False))
    for row in records:
        if row['status']=='needs_review':
            print(row['index'],row['word'],row['source_kana'],'=>',row['frontend_read'],row['frontend_pronunciation'],row['kana_input_pronunciation'])


if __name__=='__main__':
    main()
