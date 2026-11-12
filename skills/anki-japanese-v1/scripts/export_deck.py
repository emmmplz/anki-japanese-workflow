"""Attach a completed audio manifest to approved notes and export a portable deck.

Only generated artifacts are written. No Anki profile or live collection is used.
The original field order, note GUIDs, model ID and lesson deck IDs are retained.
Two generated text-only fields are appended for a silent question side.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import copy
import hashlib
import html
import importlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import uuid
import wave
import zipfile


FRONT_FIELDS = ('正面单词', '正面用例')


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def import_optional(name, dependency_dirs=()):
    """Use normal packages first; optional directories also support cached installs."""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as first:
        for directory in dependency_dirs:
            directory = Path(directory).expanduser()
            if directory.is_dir():
                sys.path.append(str(directory))
                if not (directory / name).is_dir():
                    sys.path.extend(str(item) for item in directory.iterdir() if item.is_dir())
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError:
            raise RuntimeError(f'Install the {name} package or supply --dependency-dir.') from first


def list_notes(value):
    notes = value if isinstance(value, list) else value['notes']
    if not notes or len({note['guid'] for note in notes}) != len(notes):
        raise ValueError('Notes must be nonempty and have unique GUIDs.')
    return notes


def examples_for(note):
    translations = note.get('translations', [])
    return [item if isinstance(item, dict) else
            {'ja': item, 'zh': translations[i] if i < len(translations) else ''}
            for i, item in enumerate(note.get('examples', []))]


def field_names(model):
    return [item['name'] if isinstance(item, dict) else item for item in model['fields']]


def text_for_word(note):
    """Match the generator's pronunciation override and terminal punctuation rules."""
    override = note.get('tts_word_text')
    text = str(override if override else note['word']).strip()
    if not text:
        raise ValueError('Empty word')
    return text if text[-1] in '。！？!?…' else text + '。'


def location_for(note):
    value = note.get('location')
    if value:
        return str(value)
    locations = note.get('locations') or []
    return '；'.join(map(str, locations))


def lesson_for(note):
    if note.get('lesson') is not None:
        return int(note.get('lesson') or 0)
    locations = note.get('locations') or [note.get('location', '')]
    numbers = set()
    for location in locations:
        match = re.fullmatch(r'\s*(?:第\s*(\d+)\s*[课課]|L\s*(\d+)|Lesson\s+(\d+))\s*', str(location), re.I)
        if not match:
            return 0
        numbers.add(int(next(value for value in match.groups() if value is not None)))
    return next(iter(numbers)) if len(numbers) == 1 else 0


def reading_with_pitch(kana, accents):
    """Draw only unambiguous whole-word accent numbers from the source data."""
    moras = []
    small = 'ぁぃぅぇぉゃゅょゎァィゥェォャュョヮ'
    for character in kana:
        if character in small and moras:
            moras[-1] += character
        elif 'ぁ' <= character <= 'ゖ' or 'ァ' <= character <= 'ヺ' or character == 'ー':
            moras.append(character)
        else:
            return html.escape(kana)
    if not moras or not accents or any(not isinstance(number, int) or number < 0 or number > len(moras) for number in accents):
        return html.escape(kana)
    patterns = []
    for accent in accents:
        spans, previous = [], None
        for index, mora in enumerate(moras, 1):
            high = index == 1 if accent == 1 else index > 1 and (accent == 0 or index <= accent)
            classes = 'PitchMora ' + ('PitchHigh' if high else 'PitchLow')
            if previous is not None and previous != high:
                classes += ' PitchChange'
            if index == len(moras) and accent == index:
                classes += ' PitchTail'
            spans.append('<span class="' + classes + '">' + html.escape(mora) + '</span>')
            previous = high
        number = '⓪' if accent == 0 else chr(0x2460 + accent - 1) if accent <= 20 else str(accent)
        patterns.append('<span class="PitchReading" aria-label="' + html.escape(kana) + ' ' + str(accent)
            + '型">' + ''.join(spans) + '<small>' + number + '</small></span>')
    return ' '.join(patterns)


def base_fields(note, names):
    if 'fields' in note:
        values = list(note['fields'])
        if (len(values) == len(names) - len(FRONT_FIELDS)
                and names[-len(FRONT_FIELDS):] == list(FRONT_FIELDS)):
            values.extend([''] * len(FRONT_FIELDS))
        if len(values) != len(names):
            raise ValueError(f'Field count mismatch: {note["guid"]}')
        return values
    def join(value):
        return '；'.join(map(str, value)) if isinstance(value, list) else str(value or '')
    examples = examples_for(note)
    values = {
        'ID': note.get('id', note['guid']), '日语单词': note['word'],
        '假名': note.get('kana', ''), '声调': note.get('pitch_raw', note.get('pitch', join(note.get('pitches', [])))),
        '词性': join(note.get('pos', [])), '中文释义': join(note.get('meanings', [])),
        '课次': location_for(note), '校对提示': join(note.get('warnings', [])),
        '原文说明': join(note.get('remarks', [])), '来源': '', '校订说明': '',
    }
    result = [html.escape(str(values.get(name, note.get(name, '')))) for name in names]
    if '假名' in names:
        result[names.index('假名')] = reading_with_pitch(note.get('kana', ''), note.get('accents', []))
    if '用例' in names:
        result[names.index('用例')] = ''.join(
            '<li class="Sentence"><div class="SentGroup">'
            '<h3 class="SentKanji SentFurigana LabelIndent" lang="ja"><em>［例］</em>'
            + html.escape(item['ja']) + '</h3>'
            '<p class="SentDef ExampleTranslation LabelIndent" lang="zh-Hans"><em>［訳］</em>'
            + html.escape(item.get('zh', '')) + '</p></div></li>' for item in examples)
    return result


def templates_for(model):
    if 'templates' in model:
        return copy.deepcopy(model['templates'])
    return [{'name': '日→中', 'qfmt': model['qfmt'], 'afmt': model['afmt']}]


def silent_front_templates(model):
    templates = templates_for(model)
    for template in templates:
        for key in ('qfmt', 'bqfmt'):
            if key in template:
                for original, front in zip(('日语单词', '用例'), FRONT_FIELDS):
                    template[key] = re.sub(
                        r'{{((?:[^{}:]+:)*)' + re.escape(original) + r'}}',
                        lambda match: '{{' + match[1] + front + '}}', template[key])
    return templates


def answer_layout_templates(templates):
    """Keep pitch contours, but omit the redundant source-pitch paragraph."""
    for template in templates:
        for key in ('afmt', 'bafmt'):
            if key in template:
                template[key] = re.sub(
                    r'<p\b[^>]*\bclass\s*=\s*[\"\'][^\"\']*\bPitchRaw\b[^\"\']*[\"\'][^>]*>.*?</p\s*>',
                    '', template[key], flags=re.I | re.S)
    return templates


def front_examples(original):
    # Source examples use separate translation paragraphs. Remove them from
    # question data itself instead of relying on CSS to conceal the answer.
    return re.sub(r'<p\b[^>]*\bclass\s*=\s*[\"\'][^\"\']*\bExampleTranslation\b[^\"\']*[\"\'][^>]*>.*?</p\s*>',
                  '', original, flags=re.I | re.S)


def audit_card_sides(notes, model):
    names = field_names(model)
    checked, back_slots = 0, 0
    for note in notes:
        for template in model['templates']:
            front = render(template['qfmt'], names, note['fields'])
            back = render(template['afmt'], names, note['fields'])
            if re.search(r'\[sound:|<audio\b|\[anki:play:|{{tts\b', front, re.I):
                raise ValueError(f'Question side contains audio: {note["guid"]}')
            if 'ExampleTranslation' in front:
                raise ValueError(f'Question side contains an example translation: {note["guid"]}')
            actual = re.findall(r'\[sound:([^\]]+)\]', back)
            expected = re.findall(r'\[sound:([^\]]+)\]', '\x1f'.join(note['fields']))
            if Counter(actual) != Counter(expected):
                raise ValueError(f'Answer-side audio coverage mismatch: {note["guid"]}')
            checked += 1
            back_slots += len(actual)
    return dict(front_cards_checked=checked, front_audio_slots=0,
                back_audio_slots=back_slots, audio_only_on_back=True)


def add_example_audio(original, audio_tags, note):
    """Insert each sound before the matching li closes; preserve the source HTML."""
    parts = re.split(r'(</li\s*>)', original, flags=re.I)
    closing_count = sum(bool(re.fullmatch(r'</li\s*>', item, flags=re.I)) for item in parts)
    if closing_count != len(audio_tags):
        raise ValueError(f'Example/audio count mismatch for {note["word"]}: {closing_count} / {len(audio_tags)}')
    output, index = [], 0
    for part in parts:
        if re.fullmatch(r'</li\s*>', part, flags=re.I):
            output.append('<div class="V1SentenceAudio">' + audio_tags[index] + '</div>')
            index += 1
        output.append(part)
    return ''.join(output)


AUDIO_CSS = '''
.V1WordWithAudio{display:inline-flex;align-items:center;justify-content:center;gap:.35em;flex-wrap:wrap;max-width:100%;}
.V1WordAudio{display:inline-flex;font-size:18px;align-items:center;}
.V1SentenceAudio{display:flex;justify-content:flex-end;margin:6px 0 4px;}
.V1WordAudio audio{width:190px;max-width:100%;height:36px;}
.V1SentenceAudio audio{width:min(100%,340px);height:36px;}
.V1SentenceAudio .replay-button{margin-right:4px;}
@media(max-width:474px){.V1WordAudio{width:100%;justify-content:center}.V1WordAudio audio{width:100%;max-width:290px}.V1SentenceAudio audio{width:100%}}
'''


ANSWER_LAYOUT_CSS = '''
.VocabPoS{display:block;width:100%;padding:0 12px;line-height:1.6;font-family:"Microsoft YaHei","PingFang SC",sans-serif;font-size:var(--text-lg);font-weight:400;}
.VocabPoS .PosLabel,.VocabPoS .VocabDef{display:inline;margin:0;font:inherit;color:inherit;}
.VocabPoS .PosLabel{margin-right:.45em;}
.VocabPoS .PosLabel *,.VocabPoS .VocabDef *{font:inherit;color:inherit;}
'''


def prepare(notes, model, manifest, manifest_root, output):
    names = field_names(model)
    if '日语单词' not in names or '用例' not in names:
        raise ValueError('The model must contain 日语单词 and 用例 fields.')
    entries = manifest['audio']
    mappings = manifest['cards']
    assets, verified, enriched = {}, {}, []
    media_directory = output / 'audio'
    media_directory.mkdir(parents=True, exist_ok=True)

    def sound(job_id, expected_kind, expected_text=None):
        entry = entries[job_id]
        if entry['kind'] != expected_kind:
            raise ValueError(f'Unexpected kind for {job_id}')
        if expected_text is not None and entry.get('text') != expected_text:
            raise ValueError(f'Audio text mismatch for {job_id}: expected {expected_text!r}')
        path = (manifest_root / entry['file']).resolve()
        if not path.is_relative_to(manifest_root.resolve()):
            raise ValueError('Audio file path must stay inside its manifest directory.')
        if job_id not in verified:
            if not path.is_file() or sha256(path) != entry['sha256']:
                raise ValueError(f'Missing or changed audio: {job_id}')
            with wave.open(str(path), 'rb') as wav:
                if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                    raise ValueError(f'Expected mono PCM16: {path}')
                rate, frames = wav.getframerate(), wav.getnframes()
                if rate != int(entry['sample_rate']) or frames < rate * .1:
                    raise ValueError(f'Invalid audio format/duration: {path}')
                if abs(frames / rate - float(entry['duration_seconds'])) > .025:
                    raise ValueError(f'Audio duration mismatch: {path}')
                if not any(wav.readframes(frames)):
                    raise ValueError(f'Silent PCM: {path}')
            basename = path.name
            if basename in assets and sha256(assets[basename]) != entry['sha256']:
                raise ValueError(f'Conflicting media filename: {basename}')
            assets[basename] = path
            target = media_directory / basename
            if not target.exists() or sha256(target) != entry['sha256']:
                shutil.copy2(path, target)
            verified[job_id] = dict(file=basename, **{k: entry[k] for k in
                ('kind', 'text', 'sha256', 'sample_rate', 'duration_seconds')})
        return '[sound:' + verified[job_id]['file'] + ']'

    for note in notes:
        mapping = mappings[note['guid']]
        example_items = examples_for(note)
        if len(mapping['examples']) != len(example_items):
            raise ValueError(f'Example mapping mismatch: {note["word"]}')
        fields = base_fields(note, names)
        if any('[sound:' in value for value in fields):
            raise ValueError('Source fields already contain audio; use the approved source notes.')
        word_input = text_for_word(note)
        word_sound = sound(mapping['word'], 'word', word_input)
        sentence_sounds = [sound(job_id, 'sentence', (example.get('tts_text') or example['ja']).strip())
                           for job_id, example in zip(mapping['examples'], example_items)]
        word_index, example_index = names.index('日语单词'), names.index('用例')
        front_values = (fields[word_index], front_examples(fields[example_index]))
        for front_name, value in zip(FRONT_FIELDS, front_values):
            if front_name in names:
                fields[names.index(front_name)] = value
            else:
                fields.append(value)
        fields[word_index] = ('<span class="V1WordWithAudio"><span lang="ja">'
            + fields[word_index] + '</span><span class="V1WordAudio">' + word_sound + '</span></span>')
        fields[example_index] = add_example_audio(fields[example_index], sentence_sounds, note)
        for index, value in enumerate(base_fields(note, names)):
            if (index not in (word_index, example_index) and names[index] not in FRONT_FIELDS
                    and value != fields[index]):
                raise AssertionError('Non-audio field changed.')
        def audio_reference(job_id):
            # A per-word export view of the authoritative manifest; all paths
            # resolve from the directory containing the exported notes JSON.
            value = verified[job_id]
            return dict(job_id=job_id, kind=value['kind'], text=value['text'],
                        file='audio/' + value['file'], sha256=value['sha256'],
                        sample_rate=value['sample_rate'], duration_seconds=value['duration_seconds'],
                        voice=entries[job_id].get('voice', ''),
                        config_fingerprint=manifest['config_fingerprint'])
        enriched.append(dict(note, fields=fields, audio=dict(
            word=audio_reference(mapping['word']),
            examples=[audio_reference(key) for key in mapping['examples']])))
    result_model = copy.deepcopy(model)
    result_model.update(fields=names + [name for name in FRONT_FIELDS if name not in names],
                        templates=answer_layout_templates(silent_front_templates(model)),
                        css=model.get('css', '') + AUDIO_CSS + ANSWER_LAYOUT_CSS)
    for key in ('qfmt', 'afmt'):
        result_model[key] = result_model['templates'][0][key]
    result_model['name'] = model.get('name', '日语词卡').replace('无音频', 'v1.0 双音色')
    audit_card_sides(enriched, result_model)
    return enriched, result_model, assets, verified


def render(template, names, fields):
    values = dict(zip(names, fields))
    template = re.sub(r'{{#([^}]+)}}(.*?){{/\1}}',
                      lambda match: match[2] if values.get(match[1]) else '', template, flags=re.S)
    result = re.sub(r'{{([^}]+)}}', lambda match: values.get(match[1], ''), template)
    return result


def preview_html(notes, model, title):
    names, template = model['fields'], model['templates'][0]
    payload = []
    for index, note in enumerate(notes):
        item = dict(index=index + 1, word=note['word'], kana=note.get('kana', ''),
                    meaning='；'.join(note.get('meanings', [])), lesson=lesson_for(note),
                    location=location_for(note), guid=note['guid'])
        for side, key in [('front', 'qfmt'), ('back', 'afmt')]:
            content = render(template[key], names, note['fields'])
            content = re.sub(r'\[sound:([^\]]+)\]',
                lambda match: '<audio controls preload="none" src="audio/' + html.escape(match[1], quote=True) + '"></audio>', content)
            item[side] = content
        payload.append(item)
    encoded = json.dumps(payload, ensure_ascii=False).replace('</', '<\\/')
    css = json.dumps(model['css'], ensure_ascii=False).replace('</', '<\\/')
    return '''<!doctype html><html lang="zh-Hans"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITLE__</title>
<style>*{box-sizing:border-box}body{font:15px/1.7 system-ui,"Microsoft YaHei",sans-serif;margin:0;padding:24px;background:#f4f1eb;color:#303830}h1{font-size:26px;margin:0 0 10px}.intro{color:#68736b;font-size:14px}.tools{display:flex;gap:8px;flex-wrap:wrap;margin:18px 0}input,select,button{font:inherit;padding:8px 12px;border:1px solid #d0d8cd;border-radius:6px;background:white}input{flex:1;min-width:170px}button{cursor:pointer}button.active{background:#345344;color:white}.layout{display:grid;grid-template-columns:minmax(280px,.9fr) minmax(360px,1.2fr);gap:20px}.panel{background:white;padding:14px;border:1px solid #d9dfd3;border-radius:10px;min-width:0}.scroll{max-height:650px;overflow:auto}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:10px 8px;text-align:left;vertical-align:top;border-bottom:1px solid #e8ece4}th{position:sticky;top:0;background:#f3f6ef}.pick{cursor:pointer}.pick:hover,.selected{background:#edf4e9}iframe{width:100%;height:700px;border:0;background:#fffaf0}.controls{display:flex;gap:8px;margin-bottom:10px}#selected{font-size:13px;color:#68736b}@media(max-width:850px){body{padding:14px}.layout{grid-template-columns:1fr}.scroll{max-height:280px}iframe{height:720px}}</style></head><body>
<h1>__TITLE__</h1><p class="intro">单词与例句音色由本次外部配置指定。保留音调、中文释义与全部例句翻译。点击播放器试听。</p>
<div class="tools"><input id="search" aria-label="搜索词汇" placeholder="搜索单词、假名或中文"><select id="lesson" aria-label="筛选课次"><option value="">全部课次</option></select><span id="count"></span></div>
<div class="layout"><section class="panel"><div class="scroll"><table><thead><tr><th>单词</th><th>假名</th><th>释义</th></tr></thead><tbody id="list"></tbody></table></div></section><section class="panel"><div class="controls"><button id="previous">上一张</button><button id="front">正面</button><button id="back" class="active">背面</button><button id="next">下一张</button></div><p id="selected"></p><iframe title="词卡试听" sandbox="allow-same-origin"></iframe></section></div>
<script>const notes=__NOTES__;const css=__CSS__;const $=id=>document.getElementById(id);let selected=0,side='back';function cell(text){const td=document.createElement('td');td.textContent=text;return td}function show(index){selected=(index+notes.length)%notes.length;const note=notes[selected];const doc='<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><base href="'+location.href.replace(/[^/]*$/,'')+'"><style>'+css+'</style><body class="card">'+note[side]+'</body>';$('selected').textContent=note.index+' / '+notes.length+' · '+note.location;document.querySelector('iframe').srcdoc=doc;$('front').classList.toggle('active',side==='front');$('back').classList.toggle('active',side==='back');document.querySelectorAll('.pick').forEach(row=>row.classList.toggle('selected',Number(row.dataset.index)===selected))}function list(){const query=$('search').value.toLowerCase().trim(),lesson=$('lesson').value;let count=0;const frag=document.createDocumentFragment();notes.forEach((note,i)=>{if(lesson&&String(note.lesson)!==lesson)return;if(query&&![note.word,note.kana,note.meaning,note.location].join(' ').toLowerCase().includes(query))return;const row=document.createElement('tr');row.className='pick';row.dataset.index=i;[note.word,note.kana,note.meaning].forEach(value=>row.append(cell(value)));row.onclick=()=>show(i);frag.append(row);count++});$('list').replaceChildren(frag);$('count').textContent=count+' 张'}[...new Set(notes.map(n=>n.lesson).filter(Boolean))].sort((a,b)=>Number(a)-Number(b)).forEach(lesson=>{const option=document.createElement('option');option.value=lesson;option.textContent='第'+lesson+'课';$('lesson').append(option)});$('search').oninput=list;$('lesson').onchange=list;$('front').onclick=()=>{side='front';show(selected)};$('back').onclick=()=>{side='back';show(selected)};$('previous').onclick=()=>show(selected-1);$('next').onclick=()=>show(selected+1);list();show(0);
document.querySelector('iframe').addEventListener('load',event=>{try{const doc=event.target.contentDocument;doc.querySelectorAll('audio').forEach(player=>player.addEventListener('play',()=>doc.querySelectorAll('audio').forEach(other=>{if(other!==player)other.pause()})))}catch(error){console.warn(error)}});
</script></body></html>'''.replace('__TITLE__', html.escape(title)).replace('__NOTES__', encoded).replace('__CSS__', css)


def write_package(notes, model, assets, output, *, model_id, deck_id, deck_name, dependency_dirs):
    genanki = import_optional('genanki', dependency_dirs)
    template_names = [template.get('name', str(i)) for i, template in enumerate(model['templates'])]
    if len(template_names) != 1:
        raise ValueError('This workflow exports one Japanese-to-Chinese card per note.')
    note_type = genanki.Model(model_id, model['name'], fields=[{'name': name} for name in model['fields']],
        templates=model['templates'], css=model['css'], sort_field_index=model['fields'].index('日语单词'))
    lessons = sorted({lesson_for(note) for note in notes})
    decks = {lesson: genanki.Deck(deck_id + lesson, deck_name + (f'::第{lesson:02d}课' if lesson else '')) for lesson in lessons}
    for deck in decks.values():
        deck.description = 'Anki 日语词卡工作流 v1.0。音色由外部配置提供，详情见运行清单。'
    for note in notes:
        decks[lesson_for(note)].add_note(genanki.Note(model=note_type, fields=note['fields'], guid=note['guid'], tags=note.get('tags', [])))
    package = genanki.Package(list(decks.values()))
    package.media_files = [str(path) for path in assets.values()]
    package.write_to_file(str(output))


def audit_package(path, notes, model, assets, expected_model_id, expected_deck_id):
    database = path.parent / ('.package-audit-' + uuid.uuid4().hex + '.anki2')
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise ValueError('Package ZIP integrity failed.')
            media = json.loads(archive.read('media'))
            if set(media.values()) != set(assets):
                raise ValueError('Package media set mismatch.')
            # Validate every zipped media blob against the already checked source.
            for member, filename in media.items():
                digest = hashlib.sha256()
                with archive.open(member) as source:
                    for block in iter(lambda: source.read(1024 * 1024), b''):
                        digest.update(block)
                if digest.hexdigest() != sha256(assets[filename]):
                    raise ValueError(f'Packaged audio changed: {filename}')
            database.write_bytes(archive.read('collection.anki2'))
        with closing(sqlite3.connect(database)) as database_connection:
            rows = database_connection.execute('select guid,mid,flds from notes').fetchall()
            cards = database_connection.execute('select did from cards').fetchall()
            exported = {guid: (mid, fields.split('\x1f')) for guid, mid, fields in rows}
            if len(rows) != len(notes) or len(cards) != len(notes):
                raise ValueError('Exported note/card count mismatch.')
            for note in notes:
                if exported[note['guid']] != (expected_model_id, note['fields']):
                    raise ValueError(f'Exported field/GUID mismatch: {note["guid"]}')
            expected_decks = Counter(expected_deck_id + lesson_for(note) for note in notes)
            if Counter(did for did, in cards) != expected_decks:
                raise ValueError('Lesson deck mapping mismatch.')
            metadata = database_connection.execute('select models from col').fetchone()[0]
            exported_model = json.loads(metadata)[str(expected_model_id)]
            if [field['name'] for field in exported_model['flds']] != model['fields']:
                raise ValueError('Original model field order changed.')
            for actual, expected in zip(exported_model['tmpls'], model['templates']):
                if any(actual[key] != expected[key] for key in ('qfmt', 'afmt')):
                    raise ValueError('Packaged card template mismatch.')
    finally:
        if database.is_file():
            database.unlink()
    sides = audit_card_sides(notes, model)
    return dict(notes=len(notes), cards=len(cards), media_files=len(assets), **sides,
                generated_front_fields=list(FRONT_FIELDS),
                all_guids_preserved=True, fields_verified=True, original_field_order_preserved=True,
                lesson_deck_counts=dict(expected_decks), media_sha256_verified=True)


def resolve_model_id(args, model):
    value = args.model_id or model.get('model_id') or model.get('id')
    if value is None or int(value) <= 0:
        raise ValueError('Provide --model-id for a new deck, or preserve the original model_id for updates.')
    return int(value)


def run(args):
    notes_path, model_path, manifest_path = (Path(value).resolve() for value in (args.notes, args.model, args.manifest))
    notes, model, manifest = list_notes(read_json(notes_path)), read_json(model_path), read_json(manifest_path)
    resolve_model_id(args, model)
    if args.deck_id <= 0:
        raise ValueError('--deck-id must be positive')
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest_root = manifest_path.parent
    enriched, enriched_model, assets, verified = prepare(notes, model, manifest, manifest_root, output)
    model_id = resolve_model_id(args, model)
    enriched_model['model_id'] = model_id
    package_path = output / (args.filename or f'日语词卡_{len(notes)}张_v1.0_双音色.apkg')
    if package_path.suffix.lower() != '.apkg' or package_path.parent != output:
        raise ValueError('--filename must be a basename ending in .apkg.')
    title = args.title or f'日语词卡 · {len(notes)} 张词卡 · v1.0 双音色'
    write_package(enriched, enriched_model, assets, package_path, model_id=model_id, deck_id=args.deck_id,
                  deck_name=args.deck_name, dependency_dirs=args.dependency_dir)
    audit = audit_package(package_path, enriched, enriched_model, assets, model_id, args.deck_id)
    (output / '卡片预览.html').write_text(preview_html(enriched, enriched_model, title), encoding='utf-8')
    (output / 'prepared-notes-with-audio.json').write_text(json.dumps(enriched, ensure_ascii=False, indent=2), encoding='utf-8')
    (output / 'prepared-model-with-audio.json').write_text(json.dumps(enriched_model, ensure_ascii=False, indent=2), encoding='utf-8')
    report = dict(status='passed', config_version=manifest.get('config_version', '1.0'),
                  model_id=model_id, deck_base_id=args.deck_id, deck_name=args.deck_name,
                  package=str(package_path), package_sha256=sha256(package_path), package_bytes=package_path.stat().st_size,
                  source_notes_sha256=sha256(notes_path), source_model_sha256=sha256(model_path), manifest_sha256=sha256(manifest_path),
                  example_slots=sum(len(examples_for(note)) for note in notes), unique_word_audio=sum(v['kind']=='word' for v in verified.values()),
                  unique_sentence_audio=sum(v['kind']=='sentence' for v in verified.values()), **audit)
    (output / '词包验证.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return report


def verify_existing(args):
    """Verify a previously exported package against its enriched notes JSON."""
    package = Path(args.verify).resolve()
    notes = list_notes(read_json(Path(args.notes).resolve()))
    model = read_json(Path(args.model).resolve())
    manifest = read_json(Path(args.manifest).resolve())
    manifest_root = Path(args.manifest).resolve().parent
    enriched, prepared_model, assets, _ = prepare(notes, model, manifest, manifest_root, package.parent)
    enriched_model = read_json(Path(args.enriched_model).resolve()) if args.enriched_model else prepared_model
    enriched_model['fields'] = field_names(enriched_model)
    expected = enriched if not args.enriched_notes else list_notes(read_json(Path(args.enriched_notes).resolve()))
    result = audit_package(package, expected, enriched_model, assets,
                           resolve_model_id(args, model), args.deck_id)
    result.update(status='passed', package=str(package), package_sha256=sha256(package))
    target = package.parent / '词包验证.json'
    if target.exists():
        previous = read_json(target)
        previous.update(result)
        result = previous
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--notes', required=True, help='Approved notes list or normalized envelope containing notes.')
    parser.add_argument('--model', required=True, help='Original note model JSON; field order and ID are retained.')
    parser.add_argument('--manifest', required=True, help='Completed manifest; audio paths relative to this file.')
    parser.add_argument('--output', help='Output directory for a new export.')
    parser.add_argument('--filename')
    parser.add_argument('--title')
    parser.add_argument('--model-id', type=int)
    parser.add_argument('--deck-id', type=int, required=True, help='Base deck ID; lesson number is added for each subdeck.')
    parser.add_argument('--deck-name', default='日语::词汇')
    parser.add_argument('--dependency-dir', action='append', default=[], help='Optional Python package directory/cache root.')
    parser.add_argument('--verify', help='Verify an existing package instead of exporting.')
    parser.add_argument('--enriched-notes', help='Optional enriched notes JSON used by --verify.')
    parser.add_argument('--enriched-model', help='Optional enriched model JSON used by --verify.')
    args = parser.parse_args()
    if not args.verify and not args.output:
        parser.error('--output is required when exporting.')
    if args.verify:
        verify_existing(args)
    else:
        run(args)


if __name__ == '__main__':
    main()
