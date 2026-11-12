# 脚本与数据接口

以下命令从技能目录运行；输入、配置与输出路径都可替换。`scripts/requirements.txt` 列出 `openpyxl`、`genanki`、`numpy` 等轻量依赖。读取单词表和导出可使用普通 Python 环境；直接推理使用外部 GPT-SoVITS 环境的 Python。不要假定 GPU 环境已安装 Excel 库。推理仓库、PyTorch、权重和参考音频由外部提供。

## 从单词表开始

```powershell
python scripts/prepare_input.py --input "单词表.xlsx" --output "work/normalized-notes.json"
```

支持 `.xlsx`、`.csv`、`.tsv`、Markdown 表格和既有 prepared-notes JSON。Excel 只读加载已有计算值，以首个非空行为表头；多工作表文件必须用 `--sheet "工作表名"` 选择，不猜测要处理的页。表头映射有歧义时提供 `--mapping mapping.json`；映射值可为精确列名或从 1 开始的列号。例如：

```json
{
  "word": "単語",
  "kana": "読み方",
  "pitch": "アクセント",
  "pos": "品詞",
  "meaning": "中文意思",
  "location": "課",
  "examples": [
    {"ja": "例文1", "zh": "訳1"},
    {"ja": "例文2", "zh": "訳2"}
  ]
}
```

脚本只整理已有数据，不编造词义、读音或例句。检查 `needs_enrichment` 和跳过行记录，由智能体补充获授权的缺项，并把原值、补充值及原因保存在记录中。按完整词条约定，每张笔记补足至少两条不同日文例句及其中文译文；先完成内容整理再规划生成。最小输入、全部字段和三个完整示例见 [data-contract.md](data-contract.md)。

已经整理并审阅的笔记不需要重新解析原表。沿用旧笔记数据时，规范化器保留其 GUID 和原字段。原表、旧词包及旧运行记录保持不变。

从不同书目或独立来源新建词卡时，可用 `--namespace "稳定来源或书名ID"` 区分新生成的 GUID，后续更新保持同一命名空间。它只影响从表格或 Markdown 产生的新 GUID，已有 JSON 中的 GUID 始终保留；省略时沿用默认算法。不要为旧包补音频另换命名空间。

## 生成前核对读音

先按 [试运行复盘与执行建议](operations-review.md) 检查本批实际包含的语言分支及依赖；CPU 日语前端检查不能覆盖 GPT-SoVITS 的英语分支。把缩写、数字、人名和多音词的朗读校订写入派生笔记，再冻结正式输入。

```powershell
python scripts/audit_pronunciation.py --notes "work/normalized-notes.json" --output "work/pronunciation-audit.json"
```

该检查使用 CPU 上的 `pyopenjtalk-plus`，不导入 PyTorch、不占用 GPU、不修改笔记。使用安装了这一前端的外部推理 Python 环境，或另行准备相同前端；不要仅为检查读音加载大模型。

报告 `counts` 比较展示词形的前端读音与来源假名，`effective_counts` 比较实际的 `tts_word_text` 校订输入。先核对人名、地名、多音词、词缀和生僻词，再为确有误读的条目设置 override；相应例句也需核对上下文读音。保留原词形和来源假名。来源假名本身拼错时记录内容问题，不能为了让数字全部通过而强迫模型跟随错字。这只是文字前端检查，不证明生成波形中的发音或音调正确。

## 规划与生成

把 [assets/v1.profile.example.json](../assets/v1.profile.example.json) 复制到当前任务作为 `v1.local.json`，修改其中的外部路径。示例路径都是占位值，不表示资源已经存在。`runtime.module_root` 填技能的 `scripts/runtime` 绝对目录；该目录只含轻量适配器。

配置路径规则：`runtime.module_root`、`repo_root`、参考音频和 `server_config` 相对**配置文件目录**解析；GPT/SoVITS 权重及 BERT/HuBERT 路径相对相应的 **`repo_root`** 解析。复制配置到新目录后需重新核对这些路径。两套推理环境可以使用不同的 Python 可执行文件。

```powershell
python scripts/generate_audio.py plan --notes "work/normalized-notes.json" --config "v1.local.json" --output "work/audio-run"
python scripts/generate_audio.py run --notes "work/normalized-notes.json" --config "v1.local.json" --output "work/audio-run" --kind word --transport direct --batch-size 1
python scripts/generate_audio.py run --notes "work/normalized-notes.json" --config "v1.local.json" --output "work/audio-run" --kind sentence --transport http --batch-size 4
```

先读规划结果，核对笔记数、单词任务数、全部例句数、去重例句任务数和已有缓存覆盖。`--kind` 支持 `word`、`sentence`、`all`；`--transport` 支持 `direct` 与 `http`。**直接模式拒绝 `--kind all`**：分别启动单词进程和例句进程。若不使用服务，也可将例句命令改为 `--kind sentence --transport direct`，在独立进程中执行。

`--batch-size` 支持 `1`、`2`、`4`，生成器缺省为 `1`。**单词和例句均优先批量**，执行前按后端声明与小批验证结果显式设置，不把脚本缺省值当作最佳策略。当前 V2ProPlus 可测试 `4`；随包 V5 适配器尚不支持批量，所以上方单词命令临时用 `1`。后续 V5 支持并通过验证时，相应单词命令也应改用批量。超过 150 字符的单条输入及落单项会串行回退；并发发送普通 `/tts` 请求不能代替真正的批推理。

`direct` 使用 profile 指定的本地推理适配器。两个不同上游仓库的 Python 模块可能同名；不同模型阶段应由独立进程执行，避免只更换工作目录和模块搜索路径后误用已导入的模块。当前本地环境可用 V5 直接进程生成单词，完成并退出后启动例句服务，再通过 HTTP 生成例句。

HTTP 地址从对应 profile 的 `endpoint` 读取，须同时提供指向实际服务配置的 `server_config`。HTTP 阶段会核对 `/health` 的模型版本、精度、设备、切分、权重路径以及参考音频摘要、文本和语言，并交叉检查服务配置。服务仍加载旧音色时应按目标配置重启，不能仅修改客户端路径或凭端口可连通判断。

批量 HTTP 还要求后端声明 `batch_supported` 并提供 `/tts/batch`。每条输入有独立 ID；客户端检查返回项数量、ID、索引、文本、顺序和独立 WAV 格式，全部通过后才登记结果，清单原子写入。异常或中断后核对组状态与已登记文件，不重用错序或未验证的结果。

上述直接调用示例假定显卡资源已可用。若现有服务占用同一显卡，先按其已有启动/停止方式完成协调；确认空闲后停止，结束或异常时恢复原配置。对已授权的本地任务自主执行这些步骤，不反复把相同批准事项交给用户。

运行到相同输出目录即可续跑。生成器重新验证清单和输出文件；文本相同且配置相同的例句共用任务。配置或笔记文件摘要变化时，当前生成器要求使用新输出目录，不会在原清单中混入不兼容缓存。每条音频保存原始 WAV、音量对齐副本、摘要和统计。

生成后才发现局部读音问题时，不直接改旧清单的摘要绕过检查。先结束写入进程、备份，再为派生笔记建立新规划，按 [局部校订与缓存迁移](operations-review.md#局部校订与缓存迁移) 校验并复用未变任务；仅重做受影响项。随包生成器尚无通用自动迁移命令，需要显式实施并记录迁移过程。

本次已授权的串行→例句批量执行优化单独保存在 `execution`、`sessions[].batches` 和汇总来源记录中，不改变模型/音色配置指纹；已验证的旧串行缓存继续保留。记录请求批大小、实际批大小、组成员顺序、种子和并行状态。相同种子不承诺串行与批量字节一致，报告应说明实际混合来源。此例外不允许把权重、参考、文本或基础参数的变化伪装成同一配置。

运行日志提供逐项或逐批完成事件。按阶段切换、实质异常和完成事件检查进度，不用固定短间隔反复读取清单；最终覆盖与质量检查仍需完整执行。

生成完执行完整验证：

```powershell
python scripts/generate_audio.py verify --notes "work/normalized-notes.json" --config "v1.local.json" --output "work/audio-run"
```

`verify` 不调用 GPU 推理；它核对全部计划项与实际音频。生成过程中的音频质量检查和缓存摘要检查也会用于续跑。

需要定位静音和异常输出时执行独立文件审查：

```powershell
python scripts/audit_audio.py --manifest "work/audio-run/audio-manifest.json" --output "work/audio-audit.json"
```

该命令仅用 CPU 与 NumPy 读取音频，检查 WAV 格式、截断、时长、音量、削波及首尾/内部静音，按单词和例句分组统计，列出待听审文件。默认尾静音阈值按类型区分：单词及其他类型 0.8 秒，例句 2.0 秒。可选参数为 `--max-duration 30 --max-leading-silence 0.8 --max-trailing-silence 0.8 --max-sentence-trailing-silence 2.0 --max-internal-silence 1.2`，单位为秒。阈值警告是听审线索，不自动删除或改写音频；已确认声音的自然停顿可能触发保守阈值。生成尚未结束时 `missing_jobs` 单列缺项，不能据此宣称整批完成。该命令不判断发音和音调。

## 导出

```powershell
python scripts/export_deck.py --notes "work/normalized-notes.json" --model "work/prepared-model.json" --manifest "work/audio-run/audio-manifest.json" --output "outputs/词包v1.0" --model-id 1830401001 --deck-id 1830402001
```

`--model` 指向当前已确认的样式和字段配置。旧包补音频时复用其身份和原字段顺序；背面将单词音频放入 `日语单词`，逐条例句音频放入相应 `用例` 的 `<li>`。导出器在末尾追加 `正面单词`、`正面用例` 两个派生字段，并改正面模板引用，确保正面没有声音引用或例句译文。默认模板已声明这两个字段；它们由导出器填充，不要求原表增加列。原字段不删除或重排，GUID 与模型/牌组身份继续复用。

新表可复制 [assets/card-model.v1.json](../assets/card-model.v1.json) 为 `work/prepared-model.json`。它只含已确认的模板、字段和 CSS，没有用户词汇，也没有旧包的模型 ID。为独立的新词包分配新的稳定模型/牌组整数 ID，并保存到任务配置；使用 `--model-id`、`--deck-id`、`--deck-name` 和 `--filename` 指定，后续更新复用同一组 ID。每个独立新包都必须显式选择自己的模型及牌组身份。例如下方数字仅为新任务示例，实际应选未使用且可复用的 ID：

```powershell
python scripts/export_deck.py --notes "work/normalized-notes.json" --model "work/prepared-model.json" --manifest "work/audio-run/audio-manifest.json" --output "outputs/新单词表v1.0" --model-id 1830401001 --deck-id 1830402001 --deck-name "日语::新单词表" --filename "新单词表_v1.0.apkg" --title "新单词表 · v1.0"
```

导出默认执行词包内部审查，通过才写成功报告。默认输出包括 `.apkg`、`卡片预览.html`、`audio/`、`prepared-notes-with-audio.json`、`prepared-model-with-audio.json` 和 `词包验证.json`。可按上例自定包名。

需重新审查已有包时使用 `--verify "词包.apkg"`，仍提供原 `--notes`、`--model`、`--manifest` 及原导出的 ID 参数；需要时提供 `--enriched-notes` 和 `--enriched-model`。不把已含音频的 enriched 笔记当作新一轮补音频输入。

更新已有词包且本机有 Anki 导入库时，可用 `scripts/validate_import_update.py --old-package "旧包.apkg" --new-package "新包.apkg" --guid "待核对GUID" --output "work/import-check"` 在隔离收藏中检查导入更新、笔记/卡片身份和调度保留。这不写入用户正在使用的收藏，也不是每个新表必做的操作。

## 规范化笔记

新脚本使用以下包络；音频生成器同时兼容本项目旧的 prepared-notes 列表。

```json
{
  "schema_version": "1.0",
  "source": {"path": "单词表.xlsx", "sha256": "实际文件摘要"},
  "notes": [
    {
      "guid": "稳定笔记标识",
      "word": "力士",
      "kana": "りきし",
      "tts_word_text": "りきし",
      "meanings": ["相扑手", "大力士"],
      "pos": ["名词"],
      "accents": [1, 0],
      "examples": [
        {"ja": "あれ、次の力士、日本人じゃないみたいですね…。", "zh": "咦，下一位相扑选手好像不是日本人呢……"},
        {"ja": "力士たちが土俵に上がる。", "zh": "力士们登上了相扑台。"}
      ],
      "tags": ["日语::中级"],
      "needs_enrichment": []
    }
  ]
}
```

每条笔记至少有两条不同的 `examples`，每项都有明确的日文与中文对应。复杂音调可以保留原 `pitch` 数据、原字段和校对状态，不能强行改成简单数字。来源行、位置、备注和旧模型字段保留在相应的来源或扩展字段中。

`tts_word_text` 是可选的单词发音校订输入；无该值时朗读 `word`，生成器会按需要补句号。`examples[].tts_text` 是可选的整条例句发音校订输入；无该值时朗读 `ja`。它们仅改变模型输入，卡面仍显示原词形和原句；记录原输入、校订输入及理由。两者都进入任务身份和缓存键，先冻结读音再启动正式批次。

生成器输出的清单具有以下核心关联；其他统计和校验信息可以扩展：

```json
{
  "schema_version": "1.0",
  "config_version": "1.0",
  "audio": {
    "实际任务标识": {
      "kind": "word",
      "text": "力士。",
      "file": "audio/实际文件名.wav",
      "sha256": "实际音频摘要",
      "sample_rate": 48000,
      "duration_seconds": 0.88
    }
  },
  "cards": {
    "稳定笔记标识": {
      "word": "单词任务标识",
      "examples": ["第一条例句任务标识", "第二条例句任务标识"]
    }
  }
}
```

导出的 `prepared-notes-with-audio.json` 还会在每条笔记的 `audio.word` 和 `audio.examples` 中提供实际文件、摘要、读入文本及音色信息，这是清单的派生视图；具体约定见 [data-contract.md](data-contract.md)。

实际清单文件名是 **`audio-manifest.json`**。`cards[guid].examples` 顺序与笔记的 `examples` 顺序一致，数量相同。不同笔记可以引用同一例句任务，但不能漏掉其播放器。`file` 相对清单所在目录解析；外部资源路径遵循上面的配置路径规则。

## 外部配置形状

实际 v1.0 本机配置由项目输出保存。配置包含 `profiles.word` 和 `profiles.sentence`；每个 profile 提供 `backend.factory`、`backend.options`、`voices`、`warmup_text` 及需要时的 HTTP `endpoint`。全局保存版本与种子等已确认参数。两个 profile 分别指向单词与例句的环境、权重和参考录音。

轻量适配器由 `scripts/runtime/tts_service/` 提供，配置的 factory 为 `tts_service.backends.gpt_sovits:GPTSoVITSBackend`。替换新模型时新增或指定符合 `TTSBackend` 协议的适配器和 profile，保留词卡整理、清单与导出逻辑。当前生成器的检查逻辑针对本版两个 profile；接入其他后端时同步调整模型身份验证，不能仅改 factory 后声称验证等效。

使用随包的可移植配置示例填写本机路径。占位路径不对应已安装资源，不能直接当作成功部署。安装技能时不把任何推理权重、完整模型源码、参考录音或虚拟环境复制进技能目录。
