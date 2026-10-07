"""Download the minimal official CosyVoice3 inference snapshot.

The revision and file allow-list are deliberately pinned so the deployment is
reproducible and does not download TensorRT, RL, or batch-tokenizer artifacts
that are unused by the local PyTorch inference path.
"""

from __future__ import annotations

import os
from pathlib import Path

from huggingface_hub import snapshot_download


REPO_ID = "FunAudioLLM/Fun-CosyVoice3-0.5B-2512"
REVISION = "29e01c4e8d000f4bcd70751be16fa94bf3d85a18"
ALLOW_PATTERNS = (
    "CosyVoice-BlankEN/*",
    "campplus.onnx",
    "configuration.json",
    "cosyvoice3.yaml",
    "flow.pt",
    "hift.pt",
    "llm.pt",
    "speech_tokenizer_v3.onnx",
)


def main() -> None:
    workspace = Path(__file__).resolve().parents[1]
    model_dir = workspace / "models" / "Fun-CosyVoice3-0.5B-2512"
    cache_dir = workspace / "cache" / "huggingface"
    model_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(cache_dir))

    resolved = snapshot_download(
        repo_id=REPO_ID,
        revision=REVISION,
        local_dir=model_dir,
        allow_patterns=list(ALLOW_PATTERNS),
        max_workers=4,
    )
    print(f"MODEL_DIR={resolved}")
    print(f"REVISION={REVISION}")


if __name__ == "__main__":
    main()
