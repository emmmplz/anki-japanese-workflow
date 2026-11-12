# Anki 日语词卡工作流

从日语单词表制作带中文释义、假名音调、例句译文和离线语音的 Anki 词包。仓库包含可供 Codex 等智能体使用的技能、制卡脚本和一个可替换后端的本地 TTS 服务。

## 示例产物

[下载示例词包：综合日语第三册第 1–5 课（615 张，含音频）](https://github.com/emmmplz/anki-japanese-workflow/releases/tag/v1.0.0)

## 鸣谢

### 参与人员

开发：[emmmplz](https://github.com/emmmplz)

测试：[TTTTTwilight](https://github.com/TTTTTwilight) [zhiwei](https://github.com/zhiwei1217-coder)

### 第三方项目和资源

- 本项目依赖 [Anki](https://github.com/ankitects/anki)。参考了[egg rolls JLPT词包](https://ankiweb.net/shared/info/832276382)与《综合日语》。
- TTS 依赖 [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)。`patches/` 中的补丁包含少量该项目的上下文代码；随附上游许可证 `patches/GPT-SoVITS-LICENSE`。
- 单词示读使用 GPT-SoVITS v5 默认音色，例句示读使用 GPT-SoVITS v2proplus 经 b站up主 [神秘捏果女](https://www.bilibili.com/video/BV17TxizyENV) 微调训练后的艾玛音色。本仓库不包含其模型权重或完整源码。
- 项目工作流曾参考 [ankicli](https://github.com/Takhoffman/ankicli) 和 [anki-card-skill](https://github.com/Feathersgh/anki-card-skill)；本仓库不包含这两个项目的完整代码或技能，也不要求安装它们。
- Python 依赖和可选的 Anki 官方库分别遵循各自许可证。模型权重、训练材料、声音录音、文字数据和生成内容的权利不由本项目的代码许可证授予。

发布者应在公开前检查贡献来源、模型及参考声音的许可；示例中的路径和文本不代表已提供或授权任何录音。

## 功能

- 输入 Excel、CSV、TSV、Markdown 表格或已整理的 JSON。最少提供日文词语和中文意思；智能体补全读音、音调、词性、例句及译文，脚本不会自动调用语言模型。
- 正面仅展示日文词语和例句；背面展示音调、词性、中文释义、例句译文，以及词语和各例句的音频。
- 音频可断点续跑、去重复并校验；优先使用后端支持的真正批量推理。可分别为单词和例句配置声音。
- 导出 `.apkg` 与离线 HTML 预览；更新旧包时沿用稳定身份，详见[更新旧词包](docs/updating-decks.md)。

本仓库不包含模型权重、参考录音、训练数据。音色由使用者提供的模型、录音和文本决定；请自行确认这些资源的使用许可。

## 安装技能

将 `skills/anki-japanese-v1` 目录复制到自己的 Codex 技能目录（通常是 `~/.codex/skills/anki-japanese-v1`），然后重启或重新加载技能。也可以在 Codex 中使用技能安装器从本仓库的 `skills/anki-japanese-v1` 路径安装。

示例请求：

> 使用 $anki-japanese-v1，把我的日语单词表整理为包含双语例句和音频的 Anki 词包。请使用我配置的本地 TTS。

技能会读取 [SKILL.md](skills/anki-japanese-v1/SKILL.md) 并按需使用其参考文档。`examples/words.csv` 是最小输入示例；`examples/prepared-notes.json` 是三个已整理的文本示例，尚无音频。导出前仍需审核内容与配置音频。

## 快速开始

以下命令在仓库根目录执行，要求 Python 3.10+；模型环境应按各上游项目要求单独准备。示例在 Windows PowerShell 测试，其他系统可替换路径语法。

```powershell
python -m pip install -r skills/anki-japanese-v1/scripts/requirements.txt
python skills/anki-japanese-v1/scripts/prepare_input.py --input examples/words.csv --output work/normalized-notes.json --namespace my-vocabulary
```

打开 `work/normalized-notes.json` 检查 `needs_enrichment`，由智能体或人工补全并审核。直接运行脚本不会自动生成释义、注音或例句。可以参考 [数据约定](skills/anki-japanese-v1/references/data-contract.md) 和 [三条例子](examples/prepared-notes.json)。

配置模型、参考录音与参考文本后，按 [模型与服务配置](docs/setup-tts.md) 启动或使用直接推理，再按 [脚本使用指南](skills/anki-japanese-v1/references/running.md) 规划、生成、验证和导出。首用新声音时，建议先生成少量样卡试听。

## 运行测试

安装上面的制卡依赖和 `tts-service/requirements.txt` 后，在仓库根目录执行：

```powershell
python -B -m unittest discover -s tests -v
Push-Location tts-service
python -B -m unittest discover -s tests -v
node tests/test_web.cjs
Pop-Location
python tools/check_release.py
```

测试使用模拟后端及合成测试波形，不加载 GPU 模型，也不证明实际语音质量。前端测试需要 Node.js。

## 目录

| 路径 | 内容 |
| --- | --- |
| `skills/anki-japanese-v1/` | 技能说明、数据约定、模板和制卡脚本 |
| `tts-service/` | 本地常驻服务、GPT-SoVITS 适配器与单元测试 |
| `examples/` | 独立编写的少量词条与配置示例 |
| `docs/` | 部署、模型兼容性、更新旧包及发布说明 |
| `patches/` | 对指定 GPT-SoVITS 版本的可选兼容补丁 |

## 限制

支持的模型需另行安装并下载组件。当前适配器以 GPT-SoVITS V5 标准版和 V2ProPlus 为起点；模型版本及依赖更新后需要重新验证。V2Pro/Plus 支持 1–4 条真正批量；当前 V5 适配器逐条生成。音频文件检查不能替代对发音和声调的听审。服务默认只监听本机，不提供鉴权，不要直接暴露到公网。

仓库代码按 [MIT License](LICENSE) 发布。

## TODO

- [ ] 寻找更好的单词示读TTS
- [ ] 更便捷的更换音色
- [ ] 图片格式的前端适配
