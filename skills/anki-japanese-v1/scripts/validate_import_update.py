"""Test old-package -> audio-package updating in a new isolated Anki collection.

The supplied output directory receives the test collection; live profiles are
never opened. Run with an Anki Python package matching the local platform.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import uuid
import zipfile

from export_deck import import_optional


def run(args):
    import_optional('anki', args.dependency_dir)
    from anki.collection import Collection
    from anki.import_export_pb2 import ImportAnkiPackageOptions, ImportAnkiPackageRequest

    directory = Path(args.output).resolve() / ('import-update-' + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    collection_path = directory / 'validation.anki2'
    collection = Collection(str(collection_path))

    def import_package(package):
        request = ImportAnkiPackageRequest(package_path=str(Path(package).resolve()),
            options=ImportAnkiPackageOptions(merge_notetypes=True, update_notes=1,
                                             update_notetypes=1, with_scheduling=False,
                                             with_deck_configs=False))
        return collection.import_anki_package(request)

    try:
        # Inspect the incoming package itself, so a partial update checks only its
        # own GUIDs while a complete update checks every original note.
        package_database = directory / 'incoming-package.anki2'
        with zipfile.ZipFile(args.new_package) as package:
            members = [name for name in ('collection.anki2', 'collection.anki21') if name in package.namelist()]
            if not members:
                raise ValueError('The incoming package has no supported SQLite collection.')
            package_database.write_bytes(package.read(members[0]))
        with closing(sqlite3.connect(package_database)) as database:
            expected_fields = {guid: fields for guid, fields in database.execute('select guid,flds from notes')}
        package_database.unlink()
        if not expected_fields:
            raise ValueError('The incoming package is empty.')
        sound_pattern = re.compile(r'\[sound:([^\]]+)\]')
        expected_audio = {guid: sound_pattern.findall(fields) for guid, fields in expected_fields.items()}
        if any(not sounds for sounds in expected_audio.values()):
            raise AssertionError('An incoming note has no audio references.')
        import_package(args.old_package)
        original_counts = dict(notes=collection.note_count(), cards=collection.card_count())
        note_ids_before = {guid: identifier for guid, identifier in collection.db.all('select guid,id from notes')}
        card_ids_before = {(note_id, ordinal): identifier for note_id, ordinal, identifier
                           in collection.db.all('select nid,ord,id from cards')}
        if not set(expected_fields).issubset(note_ids_before):
            raise AssertionError('The incoming package contains GUIDs absent from the original collection.')
        note_id = collection.db.scalar('select id from notes where guid=?', args.guid)
        if note_id is None:
            raise ValueError('The chosen GUID is absent from the old package.')
        card_id = collection.db.scalar('select id from cards where nid=?', note_id)
        card = collection.get_card(card_id)
        # Assign a representative review state only in this disposable collection.
        card.type, card.queue, card.ivl, card.due, card.factor, card.reps = 2, 2, 17, collection.sched.today+17, 2500, 5
        collection.update_card(card)
        scheduling_before = dict(id=card.id, nid=card.nid, type=card.type, queue=card.queue,
                                 ivl=card.ivl, due=card.due, factor=card.factor, reps=card.reps)
        import_package(args.new_package)
        updated_counts = dict(notes=collection.note_count(), cards=collection.card_count())
        if original_counts != updated_counts:
            raise AssertionError(f'Duplicated or removed notes/cards: {original_counts} -> {updated_counts}')
        note_ids_after = {guid: identifier for guid, identifier in collection.db.all('select guid,id from notes')}
        card_ids_after = {(note_id, ordinal): identifier for note_id, ordinal, identifier
                          in collection.db.all('select nid,ord,id from cards')}
        if note_ids_before != note_ids_after:
            raise AssertionError('Updating changed at least one original GUID-to-note-ID mapping.')
        if card_ids_before != card_ids_after:
            raise AssertionError('Updating changed at least one original note/ordinal-to-card-ID mapping.')
        media_root = Path(collection.media.dir())
        referenced_media = set()
        native_cards_checked, native_back_audio = 0, 0
        for guid, sounds in expected_audio.items():
            current = collection.get_note(note_ids_after[guid])
            if '[sound:' not in current['日语单词'] or '[sound:' not in current['用例']:
                raise AssertionError(f'An existing target note did not receive both audio fields: {guid}')
            actual = sound_pattern.findall('\x1f'.join(current.fields))
            if Counter(actual) != Counter(sounds):
                raise AssertionError(f'Audio references did not update completely: {guid}')
            for current_card in current.cards():
                rendered = current_card.render_output()
                if rendered.question_av_tags or '[sound:' in rendered.question_text:
                    raise AssertionError(f'Question side must be silent: {guid}')
                played = [tag.filename for tag in rendered.answer_av_tags]
                if Counter(played) != Counter(sounds):
                    raise AssertionError(f'Answer audio missing or mismatched: {guid}')
                native_cards_checked += 1
                native_back_audio += len(played)
            for filename in sounds:
                path = media_root / filename
                if not path.is_file() or path.stat().st_size == 0:
                    raise AssertionError(f'Imported media is missing or empty: {filename}')
                referenced_media.add(filename)
        if collection.db.scalar('select id from notes where guid=?', args.guid) != note_id:
            raise AssertionError('Updating replaced the original note ID.')
        card = collection.get_card(card_id)
        scheduling_after = {key: getattr(card, key) for key in scheduling_before}
        if scheduling_before != scheduling_after:
            raise AssertionError('Existing review scheduling changed.')
        note = collection.get_note(note_id)
        if '[sound:' not in note['日语单词'] or '[sound:' not in note['用例']:
            raise AssertionError('The existing note did not receive audio fields.')
        question, answer = card.question(), card.answer()
        if '{{' in question+answer or '<audio' in question+answer:
            raise AssertionError('Native Anki card rendering is invalid.')
        question_audio = card.question_av_tags()
        answer_audio = card.answer_av_tags()
        if question_audio or len(answer_audio) != len(expected_audio[args.guid]):
            raise AssertionError('Native Anki audio must appear only on the answer side.')
        result = dict(status='passed', collection=str(collection_path), before=original_counts,
                      after=updated_counts, old_package=str(Path(args.old_package).resolve()),
                      new_package=str(Path(args.new_package).resolve()), tested_guid=args.guid,
                      note_id_preserved=True, scheduling_preserved=True,
                      all_note_ids_preserved=True, all_card_ids_preserved=True,
                      identity_notes_checked=len(note_ids_after), identity_cards_checked=len(card_ids_after),
                      target_notes_with_audio=len(expected_audio), all_target_audio_references_verified=True,
                      media_files_verified=len(referenced_media), scheduling_check_scope='one representative card',
                      scheduling_before=scheduling_before, scheduling_after=scheduling_after,
                      question_audio=len(question_audio), answer_audio=len(answer_audio),
                      native_cards_checked=native_cards_checked,
                      native_front_audio_total=0, native_back_audio_total=native_back_audio,
                      live_profile_modified=False)
    finally:
        collection.close()
    report = Path(args.output).resolve() / '导入更新兼容验证.json'
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old-package', required=True)
    parser.add_argument('--new-package', required=True)
    parser.add_argument('--guid', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--dependency-dir', action='append', default=[])
    run(parser.parse_args())


if __name__ == '__main__':
    main()
