# 模型和 TTS 服务

## 外部依赖

工作流曾在 Windows、Intel Arc A750、PyTorch XPU、FP32 上使用 GPT-SoVITS V5 标准版生成单词和 V2ProPlus 生成例句。这里不提供推理环境、权重、参考音频或现成声音；设备、驱动、PyTorch、模型组件、语言前端和可合法使用的 3–10 秒参考录音须自行准备。参考文本须与实际录音逐字对应。

本次测试所用上游仓库是 [RVC-Boss/GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)：V5 为 `e91506f1fba265e1841617c3042a1573206a7351`，V2ProPlus 为 `d7c2210da8c013e81a94bfc7b811a477c99fd506`。两个版本分别置于独立目录和 Python 环境；不要在一个进程内切换导入两个同名上游模块。

测试环境的本地源码还调整了中文分词依赖及 V2Pro 的一个导入；`patches/` 保存按上述提交生成的补丁和上游许可证。需要时先检查补丁及上游版本，再在对应仓库运行 `git apply --check <补丁路径>`、`git apply <补丁路径>`；不要对其他版本盲目套用。环境还需要正常安装上游依赖、BERT、HuBERT、V2ProPlus 说话人编码器和相应权重。PyTorch XPU、驱动与上游依赖的安装方法以各项目官方文档为准；本仓库不承诺任意版本可直接运行。

## 配置

1. 将 `examples/v1.profile.example.json` 复制到仓库根目录并命名 `v1.local.json`。修改 `runtime.module_root`、两个 `repo_root`、模型文件、参考音频、准确的 `reference_text`、`device` 和 `sample_rate`。`xpu` 需要可用 Intel XPU，其他设备自行设置和验证。
2. 工作流配置中 `repo_root`、参考录音、`runtime.module_root` 和 `server_config` 相对配置文件目录；权重和 BERT/HuBERT 相对对应模型仓库目录（也可使用绝对路径）。示例路径均是占位符。`plan` 会校验两个模型配置中的权重和参考录音，即便暂时只运行一种声音，也需先准备两套。
3. 如需 HTTP 服务，将 `tts-service/config.word.example.json` 或 `config.sentence.example.json` 复制为对应的 `config.word.local.json` / `config.sentence.local.json`，填写相同模型和参考信息。服务配置的相对路径相对服务配置文件目录，权重相对模型仓库。两份配置要指向同一实际资源。
4. 两种模型分阶段执行，显存不足时不要同时加载。服务启动时加载和预热模型，同一进程内连续请求复用；停止进程时释放。更换配置后必须重启服务，再检查 `/health` 的设备、权重及参考摘要。

`reference_text` 示范文字仅演示字段，不能用于不匹配的录音。权重文件应只从可信来源取得；某些检查点格式可能涉及 Python 对象反序列化。

## 启动本地服务

在对应模型的 Python 环境中安装服务依赖：

```powershell
python -m pip install -r tts-service/requirements.txt
python tts-service/run_server.py --config tts-service/config.word.local.json
```

也可在 PowerShell 传入具体解释器：`./tts-service/start.ps1 -Python <模型环境的python.exe> -Config tts-service/config.word.local.json`。访问 `http://127.0.0.1:17862/` 使用简易界面，或运行：

```powershell
python tts-service/client.py "こんにちは。" --url http://127.0.0.1:17862 --output hello.wav
```

`GET /health`、`GET /voices`、`POST /tts` 和 `POST /tts/batch` 也可用于集成。批量接口只在后端声明支持时使用，最多 4 条，每条最多 150 字符；同时发起多个普通请求只会排队。更换到例句模型时停止单词服务，按例句配置和对应 Python 环境重新启动。服务无身份认证，应保持本机监听。

## 替换模型

服务通过 `backend.factory` 加载 `模块名:类名`。适配器实现 `tts_service/backend.py` 的 `TTSBackend`：`load` 一次加载、`synthesize` 返回完整 WAV、`info` 返回可序列化的状态、`close` 释放；方法都在服务的同一个推理线程中调用。可选 `synthesize_batch` 须返回与输入一一对应且顺序一致的独立 WAV。详情看协议源码及 `tests/` 中的假后端。

制卡生成器的身份检查目前仍针对 GPT-SoVITS 的配置字段和 `/health` 元数据。新后端接入 HTTP 框架后，需相应扩展 `generate_audio.py` 的配置身份与服务检查，并测试缓存隔离及端到端导出；只改 factory 不代表制卡流水线已兼容。
