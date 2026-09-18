#!/usr/bin/env python3
"""Turn a StyleTTS2 second-stage checkpoint into a servable Kikiri model directory.

The training checkpoint carries optimizer state and the modules that only matter
while training (discriminators, aligner, pitch extractor, diffusion). The CPU
serving runtime loads five submodules, so the export keeps exactly those and
drops the rest — roughly 1 GB down to 330 MB.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import torch

# The submodules kokoro.KModel expects to find in a checkpoint.
SERVING_MODULES = ("bert", "bert_encoder", "predictor", "text_encoder", "decoder")
ASSETS = Path(__file__).resolve().parent.parent / "assets"


def jsonable(value: object) -> object:
    """Checkpoint bookkeeping arrives as tensors; meta.json needs plain numbers."""
    if isinstance(value, torch.Tensor):
        return value.item() if value.numel() == 1 else value.tolist()
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return str(value)


def strip_checkpoint(checkpoint: Path, destination: Path) -> dict[str, object]:
    payload = torch.load(checkpoint, map_location="cpu", weights_only=False)
    net = payload.get("net", payload)
    missing = [name for name in SERVING_MODULES if name not in net]
    if missing:
        raise SystemExit(f"Checkpoint {checkpoint} is missing modules: {', '.join(missing)}")
    torch.save({name: net[name] for name in SERVING_MODULES}, destination)
    return {
        key: jsonable(payload.get(key))
        for key in ("epoch", "iters", "val_loss")
    }


def extract_voicepack(
    checkpoint: Path,
    style_checkpoint: Path | None,
    audio_dir: Path,
    destination: Path,
    workspace: Path,
) -> None:
    script = workspace / "scripts" / "extract_voicepack.py"
    if not script.is_file():
        raise SystemExit(f"extract_voicepack.py not found at {script}")
    command = [
        sys.executable,
        str(script),
        "--model",
        str(checkpoint),
        "--audio-dir",
        str(audio_dir),
        "--output",
        str(destination),
        "--device",
        "cpu",
    ]
    if style_checkpoint is not None:
        command += ["--style-encoder-model", str(style_checkpoint)]
    subprocess.run(command, check=True, cwd=str(workspace))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("output", type=Path, help="Model directory to create.")
    parser.add_argument("--audio-dir", type=Path, required=True)
    parser.add_argument("--style-encoder-model", type=Path)
    parser.add_argument("--workspace", type=Path, default=Path("/workspace"))
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--display-name")
    args = parser.parse_args()

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    stats = strip_checkpoint(args.checkpoint, output / "model.pth")
    shutil.copyfile(ASSETS / "kokoro_config.json", output / "config.json")
    extract_voicepack(
        args.checkpoint,
        args.style_encoder_model,
        args.audio_dir,
        output / "default.pt",
        args.workspace,
    )

    voicepack = torch.load(output / "default.pt", map_location="cpu", weights_only=True)
    if tuple(voicepack.shape) != (510, 1, 256):
        raise SystemExit(f"Unexpected voicepack shape {tuple(voicepack.shape)}")

    meta = {
        "model_id": args.model_id,
        "display_name": args.display_name or args.model_id,
        "source_checkpoint": str(args.checkpoint),
        "model_bytes": (output / "model.pth").stat().st_size,
        **{key: value for key, value in stats.items() if value is not None},
    }
    (output / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
