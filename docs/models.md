# 模型与外部工具安装

仓库不包含模型权重、安装包、照片或任何整理记录。根据需要安装外部工具和模型；只做 pHash / 模糊筛选时不需要下载 AI 模型。

## 基础外部工具

- **ExifTool**：GPS 和拍摄时间读取需要。从 [ExifTool 官网](https://exiftool.org/) 下载 Windows 版本，解压到项目的 `tools/`。将 `exiftool(-k).exe` 重命名为 `exiftool.exe`，并保留下载包内随附的 `exiftool_files/` 文件夹。也可以放入 PATH 或用 `PHOTO_CURATOR_EXIFTOOL` 指定完整路径。
- **FFmpeg**：视频抽帧和浏览器播放转换需要。通过 [FFmpeg 官网](https://ffmpeg.org/download.html) 提供的安装渠道安装，并把 `ffmpeg` 加入 PATH。

不要把可执行文件提交到仓库。安装与权重目录都已加入 `.gitignore`。

## CLIP：分类与近似照片

先安装 AI 依赖：

```bat
.venv\Scripts\python -m pip install -r requirements-ai.txt
```

有 NVIDIA 显卡时，先按 [PyTorch 官网](https://pytorch.org/get-started/locally/) 为实际 CUDA 环境安装匹配的 PyTorch / torchvision，再安装其余依赖。也可以使用 CPU，但分析较慢。

默认模型为 `ViT-B-32`，预训练权重为 `laion2b_s34b_b79k`。首次分类或近似查找时，OpenCLIP 自动下载权重到：

```text
models/open_clip/
```

这个目录不上传 GitHub。使用 [OpenCLIP 官方仓库](https://github.com/mlfoundations/open_clip) 对应模型的权重进行手动下载时，可把文件保存到 `models/open_clip/`，再通过环境变量指定：

```bat
set PHOTO_CURATOR_CLIP_CHECKPOINT=%CD%\models\open_clip\open_clip_pytorch_model.bin
start_web.bat
```

文件名可以不同，但必须与 `ViT-B-32` 架构兼容。更换本地权重文件后，缓存身份会按文件路径、大小和修改时间区分。

可以通过 `PHOTO_CURATOR_MODELS_DIR` 改变整个模型缓存根目录。需要镜像时由用户自行设置 `HF_ENDPOINT`；项目不会默认指定镜像、代理或个人目录。

## Ollama：景点识别

1. 从 [Ollama 官网](https://ollama.com/download) 安装 Ollama。
2. 如需把 Ollama 模型存入项目目录，在启动 Ollama 服务之前设置环境变量 `OLLAMA_MODELS` 为项目中 `models/ollama` 的**绝对路径**，然后重启 Ollama。只给当前命令行设置变量，不会改变已运行服务的存储位置。
3. 启动服务后下载模型：

```bat
ollama pull qwen3-vl:8b
```

Ollama 自己管理模型文件，不能把普通权重文件随意复制到此目录代替 `ollama pull`。详见 [Ollama FAQ](https://docs.ollama.com/faq)。

默认服务地址是 `http://127.0.0.1:11434`。可通过 `OLLAMA_HOST` 更改；网页高级设置可以选择已安装的视觉模型名称。景点识别需要模型支持图片输入。

## 隐私与网络

图片内容分析在本机进行。模型初次下载需要网络；选择联网中文地名查询时，会把 GPS 坐标发送到 Nominatim。可选代理通过 `PHOTO_CURATOR_PROXY` 或标准代理环境变量设置。使用各模型与外部工具时，请遵守其各自许可证与使用条款。
