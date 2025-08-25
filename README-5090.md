# 🎙️ Speakr + ASR Whisper（rtx 5090 support） 本地离线语音转写系统部署指南

本项目集成了 [Speakr](https://github.com/learnedmachine/speakr) 和 [Asr-webservice](https://github.com/ahmetoner/whisper-asr-webservice)，实现了基于 Whisper 的本地离线音频转写功能，支持中文和英文录音的转写，并提供网页端界面，同时asr服务额外添加了对于RTX 5090显卡的支持（torch2.7+cuda128）。

## 支持5090显卡，支持人物角色分离，对于中文，推荐使用large-v3模型，准确率更高

## 📦 项目结构

├─speakr_whisperx
│  ├─huggingface
│  │  └─hub
│  │      ├─models--jonatasgrosman--wav2vec2-large-xlsr-53-chinese-zh-cn
│  │      │  ├─.no_exist
│  │      │  │  └─99ccb2737be22b8bb50dcfcc39ad4d567fb90cfd
│  │      │  ├─blobs
│  │      │  ├─refs
│  │      │  └─snapshots
│  │      │      └─99ccb2737be22b8bb50dcfcc39ad4d567fb90cfd
│  │      │          └─.cache
│  │      │              └─huggingface
│  │      │                  └─download
│  │      ├─models--pyannote--segmentation-3.0
│  │      │  └─.cache
│  │      │      └─huggingface
│  │      │          └─download
│  │      ├─models--pyannote--speaker-diarization-3.1
│  │      │  ├─.cache
│  │      │  │  └─huggingface
│  │      │  │      └─download
│  │      │  │          ├─.github
│  │      │  │          │  └─workflows
│  │      │  │          └─reproducible_research
│  │      │  ├─.github
│  │      │  │  └─workflows
│  │      │  └─reproducible_research
│  │      ├─models--Systran--faster-whisper-large-v3
│  │      │  ├─blobs
│  │      │  ├─refs
│  │      │  └─snapshots
│  │      │      └─edaa852ec7e145841d8ffdb056a99866b5f0a478
│  │      └─models--Systran--faster-whisper-medium
│  │          ├─blobs
│  │          ├─refs
│  │          └─snapshots
│  │              └─08e178d48790749d25932bbc082711ddcfdfbc4f
│  ├─instance
│  ├─models
│  └─uploads
│  └─docker-compose.yml

```yml
services:
  whisper-asr-webservice6002:    
    image: crpi-n9jif4z5nex2rnkd.cn-hangzhou.personal.cr.aliyuncs.com/docker_2025-images/whisper-asr-webservice_for_5090:latest
    container_name: whisper-asr-webservice6002
    ports:
      - "6002:9000"
    volumes:
      - ./huggingface/hub:/root/.cache/huggingface/hub
    environment:
      - ASR_MODEL=large-v3  # 可选 large-v3、medium、distil-large-v3(不支持中文)
      - ASR_COMPUTE_TYPE=fp16 # 可选 fp16、int8
      - ASR_ENGINE=whisperx
      - HF_TOKEN=hf_your_huggingface_token_here
      - HF_ENDPOINT=https://hf-mirror.com
      - HF_HOME=/root/.cache/huggingface/
    deploy:
      resources:
        limits:
          memory: 12G
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    runtime: nvidia
    restart: always
    networks:
      - speakr-network

  app:
    image: learnedmachine/speakr:latest
    container_name: speakr8890
    restart: always
    ports:
      - "8890:8899"
    environment:
      - TEXT_MODEL_BASE_URL=http://100.64.0.16:11434/v1
      - TEXT_MODEL_API_KEY=your_api_key_here # ollama的话这里随意填写
      - TEXT_MODEL_NAME=qwen2.5:14b
      - USE_ASR_ENDPOINT=true

      - ASR_BASE_URL=http://100.64.0.16:6002
      # - ASR_BASE_URL=http://whisper-asr-webservice6002:9000 # Speakr作者推荐这样使用，但我本地实际测试这样和asr无法通信，不知道为什么
      # 项目运行后，记得在Speakr前台admin界面修改timeout超时时间，默认为30分钟，我设置为180分钟
      - ASR_ENCODE=true
      - ASR_TASK=transcribe
      - ASR_DIARIZE=true
      - ASR_MIN_SPEAKERS=1
      - ASR_MAX_SPEAKERS=5

      - ALLOW_REGISTRATION=false
      - SUMMARY_MAX_TOKENS=8000
      - CHAT_MAX_TOKENS=5000

      # Large File Chunking Configuration (for endpoints with file size limits like OpenAI)
      # Enable automatic chunking for large files that exceed API limits
      - ENABLE_CHUNKING=true
      - CHUNK_SIZE_MB=20

      # Overlap between chunks in seconds to ensure no speech is lost at boundaries
      # Recommended: 3-5 seconds for natural speech
      - CHUNK_OVERLAP_SECONDS=3

      - ADMIN_USERNAME=admin
      - ADMIN_EMAIL=admin@alt.org
      - ADMIN_PASSWORD=11111111

      # --- Automated File Processing (Black Hole Directory) ---
      # Set to "true" to enable automated file processing
      - ENABLE_AUTO_PROCESSING=false

      # Processing mode: admin_only, user_directories, or single_user
      - AUTO_PROCESS_MODE=admin_only

      # Directory to watch for new audio files
      - AUTO_PROCESS_WATCH_DIR=/auto-process

      # How often to check for new files (seconds)
      - AUTO_PROCESS_CHECK_INTERVAL=60

      # Default username for single_user mode (only used if AUTO_PROCESS_MODE=single_user)
      # AUTO_PROCESS_DEFAULT_USERNAME=admin

      - SQLALCHEMY_DATABASE_URI=sqlite:////data/instance/transcriptions.db
      - UPLOAD_FOLDER=/data/uploads
      
      - MAX_CONTENT_LENGTH=2621440000  # 2500*1024*1024
      - UPLOAD_LIMIT=2147483648
      - MAX_UPLOAD_SIZE=2147483648
    volumes:
      - ./uploads:/data/uploads
      - ./instance:/data/instance
    depends_on:
      - whisper-asr-webservice6002
    networks:
      - speakr-network

networks:
  speakr-network:
    driver: bridge
```
### 模型下载代码
```
# 设置镜像环境变量（Windows PowerShell）
$env:HF_ENDPOINT = "https://hf-mirror.com"

# 在本地电脑上直接使用 huggingface-cli 下载
huggingface-cli download --resume-download jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn --cache-dir D:/models/models--jonatasgrosman--wav2vec2-large-xlsr-53-chinese-zh-cn

# https://hf-mirror.com/jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn/tree/main


# 设置镜像环境变量（Windows PowerShell）
$env:HF_ENDPOINT = "https://hf-mirror.com"

huggingface-cli download --resume-download openai/whisper-medium --cache-dir D:/models/models--openai--whisper-medium
# https://hf-mirror.com/openai/whisper-medium/tree/main

huggingface-cli download --resume-download Systran/faster-whisper-medium --cache-dir D:/models/models--Systran-faster-whisper-medium
# https://hf-mirror.com/Systran/faster-whisper-medium


huggingface-cli download --resume-download Systran/faster-whisper-large-v3 --cache-dir D:/models/models--Systran--faster-whisper-large-v3

# https://hf-mirror.com/Systran/faster-whisper-large-v3

以下两个模型需要先在huggingface官网注册账号并获取token后才能下载，并且在huggingface模型主页的model card里填写名称和网站信息
https://huggingface.co/pyannote/speaker-diarization-3.1
https://huggingface.co/pyannote/segmentation-3.0


huggingface-cli download --token hf_your_huggingface_token_here --resume-download pyannote/speaker-diarization-3.1 --cache-dir D:/models/models--pyannote-speaker-diarization-3.1

huggingface-cli download --token hf_your_huggingface_token_here --resume-download pyannote/segmentation-3.0 --cache-dir D:/models/models--pyannote-segmentation-3.0
```
## 🙌 鸣谢

- [Speakr](https://github.com/murtaza-nasir/speakr)
- [Asr-webservice](https://github.com/ahmetoner/whisper-asr-webservice)
- [Whisper](https://github.com/openai/whisper)
