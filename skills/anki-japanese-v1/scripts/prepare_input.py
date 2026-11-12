#!/usr/bin/env python3
"""Normalize Japanese word tables without inventing enrichment.

prepare_input.py --input words.xlsx --output normalized.json
                 [--mapping columns.json] [--sheet Sheet1] [--namespace my-book]

Mapping: canonical keys word/kana/pitch/pos/meaning/location -> exact source
column name or 1-based column number. Optional example mappings:
{"word":"単語","examples":[{"ja":"例文1","zh":"訳1"}]}.
The --mapping value can also be an inline JSON object.
"""
from __future__ import annotations
import argparse
import copy
import csv
import hashlib
import json
from pathlib import Path
import re
from typing import Any
import unicodedata

ALIASES = {
    "word": ("日语单词", "日语原文", "日文原文", "日文单词", "日文", "日语", "单词", "词汇", "word", "japanese", "日本語"),
    "kana": ("假名", "平假名", "平假名注音", "平假名发音", "发音", "读音", "读法", "kana", "reading", "ふりがな"),
    "pitch": ("音调", "声调", "音调标注", "pitch", "accent", "アクセント"),
    "pos": ("词性", "品词", "pos", "part of speech", "品詞"),
    "meaning": ("中文释义", "中文翻译", "中文译文", "单词中文意思", "中文意思", "释义", "意思", "meaning", "definition", "translation"),
    "location": ("课次", "位置", "章节", "单元", "location", "lesson", "unit"),
}

def clean(value: Any) -> str:
    return "" if value is None else str(value).replace("\r\n","\n").replace("\r","\n").strip()

def canonical(value: Any) -> str:
    return re.sub(r"[\s_\-—:：/（）()\[\]{}]", "", clean(value).lower())

def values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [clean(item) for item in value if clean(item)]
    return [clean(value)] if clean(value) else []

def stable_guid(word: str, kana: str, namespace: str = "") -> str:
    # Preserve the original v1 identity when no namespace is supplied.
    identity = ("anki-japanese-v1|"+word+"|"+kana) if not namespace else (
        "anki-japanese-v1:"+json.dumps([namespace,word,kana],ensure_ascii=False,separators=(",",":")))
    return hashlib.sha256(identity.encode()).hexdigest()[:20]

def resolve_column(headers: list[str], value: str | int) -> int:
    if isinstance(value, int):
        if value < 1 or value > len(headers):
            raise ValueError(f"列号超出范围（从1开始）：{value}")
        return value-1
    exact = [i for i, name in enumerate(headers) if name == value]
    if len(exact) != 1:
        raise ValueError(f"映射列名不存在或不唯一：{value}")
    return exact[0]

def infer_mapping(headers: list[str], explicit: dict) -> dict:
    result = {}
    for field, aliases in ALIASES.items():
        candidates = {canonical(name) for name in aliases}
        matches = [i for i, name in enumerate(headers) if canonical(name) in candidates]
        if matches:
            result[field] = matches[0]
    for field, value in explicit.items():
        if field != "examples":
            if field not in ALIASES:
                raise ValueError(f"未知映射字段：{field}")
            result[field] = resolve_column(headers, value)
    examples = []
    if "examples" in explicit:
        for pair in explicit["examples"]:
            if not isinstance(pair, dict) or "ja" not in pair:
                raise ValueError("examples 映射必须是包含ja和可选zh的对象列表")
            examples.append({key: resolve_column(headers, value) for key, value in pair.items() if key in ("ja","zh")})
    else:
        for i, name in enumerate(headers):
            label = canonical(name)
            if not re.search(r"例文|例句|example|sentence", label) or re.search(r"中文|翻译|译文|translation|meaning", label):
                continue
            pair = {"ja": i}
            match = re.search(r"\d+", label)
            number = match.group(0) if match else ""
            candidates = ([i+1] if i+1 < len(headers) else []) + list(range(len(headers)))
            for j in candidates:
                target = canonical(headers[j])
                if re.search(r"中文|翻译|译文|translation", target) and (not number or number in target):
                    pair["zh"] = j
                    break
            examples.append(pair)
    result["examples"] = examples
    if "word" not in result:
        raise ValueError("找不到日语单词列；使用--mapping指定word，或命名为“日语单词”。")
    return result

def split_markdown_row(line: str) -> list[str]:
    return [part.replace(r"\|", "|").strip() for part in re.split(r"(?<!\\)\|", line.strip().strip("|"))]

def read_table(path: Path, sheet: str | None = None) -> tuple[list[str], list[dict]]:
    suffix = path.suffix.lower()
    rows = []
    if suffix in (".md", ".markdown"):
        for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            if line.strip().startswith("|") and line.strip().endswith("|"):
                row = split_markdown_row(line)
                if all(re.fullmatch(r":?-+:?", value.replace(" ", "")) for value in row):
                    continue
                rows.append({"row": number, "values": row})
    elif suffix in (".csv", ".tsv"):
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, delimiter="\t" if suffix == ".tsv" else ",")
            for row in reader:
                rows.append({"row": reader.line_num, "values": [clean(value) for value in row]})
    elif suffix in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError as error:
            raise ValueError("读取.xlsx需要openpyxl（仅读取，不修改原工作簿）。") from error
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
        if sheet and sheet not in workbook.sheetnames:
            raise ValueError(f"工作表不存在：{sheet}；可选：{workbook.sheetnames}")
        if not sheet and len(workbook.sheetnames) > 1:
            raise ValueError(f"文件有多个工作表，请用--sheet指定：{workbook.sheetnames}")
        worksheet = workbook[sheet] if sheet else workbook.active
        for number, row in enumerate(worksheet.iter_rows(values_only=True), 1):
            rows.append({"row":number,"sheet":worksheet.title,"values":[clean(value) for value in row]})
        workbook.close()
    else:
        raise ValueError(f"不支持的输入格式：{suffix}；支持xlsx/csv/tsv/md/json")
    while rows and not any(rows[0]["values"]):
        rows.pop(0)
    if not rows:
        raise ValueError("文件没有可读取的表格")
    headers = rows.pop(0)["values"]
    return headers, rows

def mora_count(kana: str) -> int:
    count = 0
    for char in kana:
        if char in "ゃゅょぁぃぅぇぉゎャュョァィゥェォヮ" and count:
            continue
        if re.fullmatch(r"[ぁ-ゖァ-ヺー]", char):
            count += 1
        elif not char.isspace():
            return 0
    return count

def parse_accents(pitch: Any, kana: str) -> list[int]:
    if isinstance(pitch, list) and all(isinstance(x,int) for x in pitch):
        return pitch
    if not isinstance(pitch,str):
        return []
    raw = unicodedata.normalize("NFKC", pitch.replace("◎","⓪"))
    if not re.fullmatch(r"\d+(?:\s*/\s*\d+)*",raw):
        return []
    accents = [int(x.strip()) for x in raw.split("/")]
    count = mora_count(kana)
    return accents if count and all(0 <= accent <= count for accent in accents) else []

def requirements(note: dict) -> dict:
    if not note.get("guid"):
        note["guid"] = stable_guid(note["word"],note.get("kana",""))
    if not isinstance(note["guid"],str):
        raise ValueError("guid必须是字符串；未将现有卡片标识自动转换")
    note.setdefault("tags",[])
    note.setdefault("warnings",[])
    missing = [key for key in ("kana","meanings","pos") if not note.get(key)]
    if not note.get("examples"):
        missing.append("example.ja")
    if len(note.get("examples", [])) < 2:
        missing.append("examples.min_2")
    for example in note.get("examples",[]):
        if not example.get("ja"):
            missing.append("example.ja")
        if not example.get("zh"):
            missing.append("example.zh")
    if not note.get("pitch") and not note.get("accents"):
        missing.append("pitch")
    note["needs_enrichment"] = sorted(set(missing))
    return note

def normalize_prepared(payload: Any) -> tuple[list[dict],list[dict]]:
    original = payload.get("notes") if isinstance(payload,dict) else payload
    if not isinstance(original,list) or not all(isinstance(note,dict) for note in original):
        raise ValueError("JSON必须为prepared-notes数组或包含notes数组的对象")
    notes = []
    for index, source in enumerate(original,1):
        note = copy.deepcopy(source)
        if not clean(note.get("word")):
            raise ValueError(f"第{index}条JSON记录缺word；未跳过该记录")
        note["word"],note["kana"] = clean(note["word"]),clean(note.get("kana"))
        note["meanings"] = values(note.get("meanings",note.get("meaning")))
        note["pos"] = values(note.get("pos"))
        examples = []
        translations = values(note.get("translations"))
        source_examples = note.get("examples") or []
        if not isinstance(source_examples,list):
            raise ValueError(f"第{index}条JSON的examples必须是列表")
        for j,example in enumerate(source_examples):
            if isinstance(example,str):
                examples.append({"ja":example,"zh":translations[j] if j < len(translations) else ""})
            elif isinstance(example,dict):
                examples.append(dict(example,ja=clean(example.get("ja")),zh=clean(example.get("zh"))))
            else:
                raise ValueError(f"第{index}条JSON例句格式无效")
        note["examples"] = examples
        if "pitch" not in note:
            note["pitch"] = " / ".join(values(note.get("pitches")))
        if "accents" not in note:
            note["accents"] = parse_accents(note["pitch"],note["kana"])
        # Prepared notes retain their existing identity.  Namespace is only a
        # collision-avoidance option for rows read from a new table.
        notes.append(requirements(note))
    return notes,[]

def normalize_table(headers: list[str], records: list[dict], explicit: dict, namespace: str = "") -> tuple[list[dict],list[dict]]:
    mapping = infer_mapping(headers,explicit)
    grouped,skipped,context = {},[],""
    for record in records:
        row,row_number = record["values"],record["row"]
        def get(index):
            return clean(row[index]) if index is not None and index < len(row) else ""
        word = get(mapping["word"])
        if len(row)>len(headers) and any(row[len(headers):]):
            raise ValueError(f"第{row_number}行比表头多列，可能有未转义分隔符；请修正源表或映射")
        if not any(row):
            continue
        if not word:
            skipped.append(dict(record,reason="word column is empty"))
            continue
        if word.startswith("**") and word.endswith("**") and not any(value for i,value in enumerate(row) if i!=mapping["word"]):
            context = word.strip("* ")
            skipped.append(dict(record,reason="section heading",context=context))
            continue
        if canonical(word)==canonical(headers[mapping["word"]]) and all(canonical(get(i))==canonical(headers[i]) for i in range(min(len(row),len(headers)))):
            skipped.append(dict(record,reason="repeated header"))
            continue
        word = re.sub(r"\*\*(.*?)\*\*",r"\1",word).strip()
        kana = get(mapping.get("kana"))
        key = (word,kana) if kana else (word,tuple(row))
        guid = stable_guid(word,kana,namespace) if kana else stable_guid(word,json.dumps(row,ensure_ascii=False),namespace)
        note = grouped.setdefault(key,dict(guid=guid,word=word,kana=kana,
            meanings=[],pos=[],pitch="",accents=[],examples=[],tags=[],locations=[],
            source_rows=[],source_records=[],warnings=[]))
        note["source_rows"].append(row_number)
        note["source_records"].append(dict(record,columns={
            (headers[i] if i < len(headers) else f"extra_{i+1}") or f"column_{i+1}":value
            for i,value in enumerate(row)}))
        # Keep each source cell intact: splitting punctuation can alter meaning.
        for field,mapped in (("meanings","meaning"),("pos","pos")):
            value = get(mapping.get(mapped))
            if value and value not in note[field]:
                note[field].append(value)
        pitch = get(mapping.get("pitch"))
        if not note["pitch"]:
            note["pitch"],note["accents"] = pitch,parse_accents(pitch,kana)
        elif pitch and note["pitch"]!=pitch:
            note["warnings"].append({"type":"pitch_conflict","row":row_number,"values":[note["pitch"],pitch]})
        location = get(mapping.get("location")) or context
        if location and location not in note["locations"]:
            note["locations"].append(location)
        for pair in mapping["examples"]:
            example = {"ja":get(pair["ja"]),"zh":get(pair.get("zh"))}
            if any(example.values()) and example not in note["examples"]:
                note["examples"].append(example)
    return [requirements(note) for note in grouped.values()],skipped

def normalize(path: Path,mapping: dict | None=None,sheet: str | None=None,namespace: str = "") -> dict:
    namespace = clean(namespace)
    if path.suffix.lower()==".json":
        notes,skipped = normalize_prepared(json.loads(path.read_text(encoding="utf-8-sig")))
        records = sum(len(note.get("source_rows") or note.get("rows") or [1]) for note in notes)
    else:
        headers,rows = read_table(path,sheet)
        notes,skipped = normalize_table(headers,rows,mapping or {},namespace)
        records = sum(len(note["source_rows"]) for note in notes)
    if len({note["guid"] for note in notes})!=len(notes):
        raise ValueError("输入有重复GUID；请先处理冲突，未写出覆盖旧卡的输出")
    return dict(schema_version="1.0",source=dict(path=str(path.resolve()),namespace=namespace,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),format=path.suffix.lstrip("."),sheet=sheet),
        notes=notes,skipped_rows=skipped,summary=dict(source_rows=records,unique_notes=len(notes),
        example_pairs=sum(len(note["examples"]) for note in notes),
        needs_enrichment=sum(bool(note["needs_enrichment"]) for note in notes),skipped_rows=len(skipped)))

def load_mapping(value: str | None) -> dict:
    if not value:
        return {}
    result = json.loads(value) if value.lstrip().startswith("{") else json.loads(Path(value).read_text(encoding="utf-8-sig"))
    if not isinstance(result,dict):
        raise ValueError("mapping必须是JSON对象")
    return result

def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--mapping",help="JSON映射文件或内联对象；列号从1开始")
    parser.add_argument("--sheet",help="xlsx工作表名称；多工作表文件必须指定")
    parser.add_argument("--namespace",default="",help="新表词包的稳定来源标识；仅影响新生成GUID，已有GUID保留")
    args=parser.parse_args()
    if args.input.resolve()==args.output.resolve():
        parser.error("输出不能覆盖输入单词表")
    try:
        result=normalize(args.input.resolve(),load_mapping(args.mapping),args.sheet,args.namespace)
    except (ValueError,OSError,json.JSONDecodeError) as error:
        parser.exit(2,f"输入未处理：{error}\n")
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result["summary"],ensure_ascii=False))

if __name__=="__main__":
    main()
