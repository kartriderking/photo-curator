# -*- coding: utf-8 -*-
"""环境自检：CUDA / exiftool / Ollama / CLIP / HEIC / 逆地理。"""
import subprocess
import sys

from common import CONFIG, EXIFTOOL, setup_logging

log = setup_logging("check")


def main():
    ok = True

    print("== 1. PyTorch CUDA ==")
    try:
        import torch
        if torch.cuda.is_available():
            print(f"  [OK] {torch.cuda.get_device_name(0)}, "
                  f"{torch.cuda.get_device_properties(0).total_memory/2**30:.1f} GB, torch {torch.__version__}")
        else:
            print("  [X] CUDA 不可用，CLIP 将退化为 CPU（很慢）")
            ok = False
    except ImportError as e:
        print(f"  [X] torch 未安装: {e}")
        ok = False

    print("== 2. exiftool ==")
    try:
        r = subprocess.run([EXIFTOOL, "-ver"], capture_output=True, timeout=30)
        print(f"  [OK] v{r.stdout.decode().strip()}")
    except Exception as e:
        print(f"  [X] exiftool 不可用: {e}")
        ok = False

    print("== 3. HEIC 解码 ==")
    try:
        import pillow_heif
        print(f"  [OK] pillow-heif {pillow_heif.__version__}")
    except ImportError:
        print("  [X] pillow-heif 未装，iPhone HEIC 图将读不了")
        ok = False

    print("== 4. 逆地理库 ==")
    try:
        import reverse_geocoder
        print("  [OK] reverse_geocoder 可用（首次运行会初始化数据）")
    except ImportError:
        print("  [X] reverse_geocoder 未装，GPS 城市列将留空")

    print("== 5. Ollama / VLM ==")
    try:
        import requests
        r = requests.get(f"{CONFIG['ollama_url']}/api/tags", timeout=5)
        names = [m["name"] for m in r.json().get("models", [])]
        if any(n.startswith(CONFIG["vlm_model"]) for n in names):
            print(f"  [OK] 模型就绪: {CONFIG['vlm_model']}")
        else:
            print(f"  [!] Ollama 在跑但缺模型，现有: {names}。执行: ollama pull {CONFIG['vlm_model']}")
            ok = False
    except Exception:
        print(f"  [!] Ollama 未启动（{CONFIG['ollama_url']}）。启动 Ollama 后重试；"
              f"首次拉模型: ollama pull {CONFIG['vlm_model']}")

    print("== 6. OpenCLIP ==")
    try:
        import open_clip
        print(f"  [OK] open_clip {open_clip.__version__}（权重首次使用时自动下载）")
    except ImportError:
        print("  [X] open_clip 未安装")
        ok = False

    print("\n结论:", "环境就绪 ✔" if ok else "存在缺失项，见上方 [X]/[!] 提示")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
