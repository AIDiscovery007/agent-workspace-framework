# 可选离线媒体配置

语音/视频处理运行在单独的 macOS 文件沙箱内，任务执行时禁网。源码位于 `media_input.py` / `media_worker.py`。最大 10 分钟、最多 12 个采样帧；采样不等于逐帧审阅，转写不等于人工确认。

1. 在目标 `.codex/hi-bridge/media-runtime/venv` 创建 Python 虚拟环境，通过 `pip install -r config/media-requirements.lock` 安装基线环境依赖。这是原运行环境快照，不保证所有平台均有对应 wheel；失败保留错误并将媒体能力标 pending，不修改其他模块环境。
2. 从 [模型来源](https://huggingface.co/Systran/faster-whisper-small) 取得许可允许使用的模型文件。按 `config/media-model.json` 验证文件名、字节数和 SHA256，放到目标 `.codex/hi-bridge/media-runtime/model`。仓库不包含模型权重，也不转移下载凭据。
3. 使用 `python3 scripts/media_check.py --root <工作区>` 核验依赖与模型文件。它不下载、不启动模型推理、不上传文件。
4. 在本人测试目录使用无隐私测试语音和视频，验证实际转写、帧输出、限额与目录边界。必须通过完整桥接输入路径确认原始文件和分析结果能到达指定请求人的会话。仅模型文件存在不算媒体验收。

更换模型/版本要更新本机模型清单并实际验证，不把不匹配的哈希标为通过；公开仓库的清单只对应当前基线。未安装时应报告缺媒体依赖，其他正文/文档/附件入口仍可使用。
