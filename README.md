## 另一个版本：支持人物角色分离，对于中文，推荐使用large-v3模型，准确率更高

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
    image: onerahmet/openai-whisper-asr-webservice:latest
    container_name: whisper-asr-webservice6002
    ports:
      - "6002:9000"
    volumes:
      - ./huggingface/hub:/root/.cache/huggingface/hub
    environment:
      - ASR_MODEL=large-v3  # 可选 large-v3、medium、distil-large-v3(不支持中文)
      - ASR_COMPUTE_TYPE=int8
      - ASR_ENGINE=whisperx
      - HF_TOKEN=hf_your_huggingface_token_here
      - HF_ENDPOINT=https://hf-mirror.com   # https://hf-mirror.com
      - HF_HOME=/root/.cache/huggingface/
    deploy:
      resources:
        limits:
          memory: 4G
    restart: unless-stopped
    networks:
      - speakr-network

  app:
    image: learnedmachine/speakr:latest
    container_name: speakr8890
    restart: unless-stopped
    ports:
      - "8890:8899"
    environment:
      - TEXT_MODEL_BASE_URL=http://100.64.0.16:11434/v1
      - TEXT_MODEL_API_KEY=111111
      - TEXT_MODEL_NAME=qwen2.5:14b
      - USE_ASR_ENDPOINT=true

      - ASR_BASE_URL=http://100.64.0.16:6002
      - ASR_ENCODE=true
      - ASR_TASK=transcribe
      - ASR_DIARIZE=true
      - ASR_MIN_SPEAKERS=1
      - ASR_MAX_SPEAKERS=5

      - ALLOW_REGISTRATION=false
      - SUMMARY_MAX_TOKENS=8000
      - CHAT_MAX_TOKENS=5000
      - ADMIN_USERNAME=admin
      - ADMIN_EMAIL=admin@alt.org
      - ADMIN_PASSWORD=11111111

      - SQLALCHEMY_DATABASE_URI=sqlite:////data/instance/transcriptions.db
      - UPLOAD_FOLDER=/data/uploads

      # 以下三个变量用于提高默认的上传附件大小
      - MAX_CONTENT_LENGTH=2048 # 2048MB
      - UPLOAD_LIMIT=2147483648 # 字节
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
huggingface-cli download --resume-download jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn --local-dir D:/models/models--jonatasgrosman--wav2vec2-large-xlsr-53-chinese-zh-cn

# https://hf-mirror.com/jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn/tree/main


# 设置镜像环境变量（Windows PowerShell）
$env:HF_ENDPOINT = "https://hf-mirror.com"

huggingface-cli download --resume-download openai/whisper-medium --local-dir D:/models/models--openai--whisper-medium

# https://hf-mirror.com/openai/whisper-medium/tree/main


https://hf-mirror.com/Systran/faster-whisper-medium
https://hf-mirror.com/Systran/faster-whisper-medium/commit/08e178d48790749d25932bbc082711ddcfdfbc4f

ref/main里面就是 08e178d48790749d25932bbc082711ddcfdfbc4f


huggingface-cli download --resume-download Systran/faster-whisper-medium --local-dir D:/models/models--Systran-faster-whisper-medium

huggingface-cli download --resume-download pyannote/speaker-diarization-3.1 --local-dir D:/models/models--pyannote-speaker-diarization-3.1

huggingface-cli download --resume-download pyannote/segmentation-3.0 --local-dir D:/models/models--pyannote-segmentation-3.0


以下两个模型需要先在huggingface官网注册账号并获取token后才能下载，并且在huggingface模型主页的model card里填写名称和网站信息
https://huggingface.co/pyannote/speaker-diarization-3.1
https://huggingface.co/pyannote/segmentation-3.0

https://hf-mirror.com/pyannote/speaker-diarization-3.1
https://hf-mirror.com/pyannote/segmentation-3.0

huggingface-cli download --token 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 --resume-download pyannote/speaker-diarization-3.1 --local-dir D:/models/models--pyannote-speaker-diarization-3.1

huggingface-cli download --token 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 --resume-download pyannote/segmentation-3.0 --local-dir D:/models/models--pyannote-segmentation-3.0
```
## 🙌 鸣谢

- [Speakr](https://github.com/murtaza-nasir/speakr)
- [LocalAI](https://github.com/go-skynet/LocalAI)
- [Whisper](https://github.com/openai/whisper)
