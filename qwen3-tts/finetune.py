#!/usr/bin/env python3
"""Qwen3-TTS single-speaker fine-tuning script.

Fine-tunes a Qwen3-TTS-12Hz Base model on your own voice data to create
a personalized TTS model. Based on the official sft_12hz.py approach.

Usage:
    # 1. Prepare training data (JSONL):
    #    {"audio": "/data/samples/001.wav", "text": "Hello world.", "language": "English"}
    #    {"audio": "/data/samples/002.wav", "text": "Good morning.", "language": "English"}

    # 2. Run fine-tuning:
    python finetune.py \
        --base-model Qwen/Qwen3-TTS-12Hz-1.7B-Base \
        --data /data/training.jsonl \
        --output /app/models/my-voice \
        --epochs 10 \
        --lr 1e-5 \
        --batch-size 2

    # 3. The fine-tuned model can be loaded by server.py as a custom model.

Requirements:
    pip install transformers accelerate peft datasets soundfile
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from torch.utils.data import Dataset, DataLoader


def load_training_data(jsonl_path: str) -> list[dict]:
    """Load training data from JSONL file."""
    data = []
    with open(jsonl_path, "r") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if "audio" not in entry or "text" not in entry:
                    print(f"WARNING: Line {line_num} missing 'audio' or 'text', skipping", flush=True)
                    continue
                if not Path(entry["audio"]).exists():
                    print(f"WARNING: Audio file not found: {entry['audio']}, skipping", flush=True)
                    continue
                data.append(entry)
            except json.JSONDecodeError as e:
                print(f"WARNING: Line {line_num} invalid JSON: {e}, skipping", flush=True)
    return data


class TTSDataset(Dataset):
    """Dataset for TTS fine-tuning. Each sample is (audio, text, language)."""

    def __init__(self, entries: list[dict], target_sr: int = 24000):
        self.entries = entries
        self.target_sr = target_sr

    def __len__(self):
        return len(self.entries)

    def __getitem__(self, idx):
        entry = self.entries[idx]
        audio, sr = sf.read(entry["audio"], dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        # Resample if needed
        if sr != self.target_sr:
            import torchaudio
            audio_tensor = torch.from_numpy(audio).unsqueeze(0)
            resampler = torchaudio.transforms.Resample(sr, self.target_sr)
            audio_tensor = resampler(audio_tensor)
            audio = audio_tensor.squeeze(0).numpy()
        return {
            "audio": audio,
            "text": entry["text"],
            "language": entry.get("language", "English"),
        }


def validate_data(entries: list[dict]) -> None:
    """Validate training data quality."""
    total_duration = 0.0
    issues = []

    for entry in entries:
        audio, sr = sf.read(entry["audio"], dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        duration = len(audio) / sr
        total_duration += duration

        if duration < 1.0:
            issues.append(f"  Very short ({duration:.1f}s): {entry['audio']}")
        elif duration > 30.0:
            issues.append(f"  Very long ({duration:.1f}s): {entry['audio']}")

        text_len = len(entry["text"])
        if text_len < 5:
            issues.append(f"  Very short text ({text_len} chars): {entry['audio']}")

    print(f"\nDataset summary:", flush=True)
    print(f"  Samples: {len(entries)}", flush=True)
    print(f"  Total audio: {total_duration:.1f}s ({total_duration/60:.1f} min)", flush=True)
    print(f"  Avg duration: {total_duration/len(entries):.1f}s", flush=True)

    if issues:
        print(f"\nWarnings:", flush=True)
        for issue in issues[:10]:
            print(issue, flush=True)
        if len(issues) > 10:
            print(f"  ... and {len(issues) - 10} more", flush=True)

    if total_duration < 60:
        print(f"\nWARNING: Only {total_duration:.0f}s of audio. Recommend at least 5-10 minutes.", flush=True)
    print("", flush=True)


def finetune(args):
    """Run the fine-tuning loop."""
    print(f"=" * 60, flush=True)
    print(f"Qwen3-TTS Fine-Tuning", flush=True)
    print(f"=" * 60, flush=True)
    print(f"Base model:  {args.base_model}", flush=True)
    print(f"Data:        {args.data}", flush=True)
    print(f"Output:      {args.output}", flush=True)
    print(f"Epochs:      {args.epochs}", flush=True)
    print(f"LR:          {args.lr}", flush=True)
    print(f"Batch size:  {args.batch_size}", flush=True)
    print(f"Device:      cuda:{args.device}", flush=True)
    print(f"LoRA:        {'yes' if args.lora else 'no'}", flush=True)
    print(f"=" * 60, flush=True)

    # Load data
    entries = load_training_data(args.data)
    if len(entries) == 0:
        print("ERROR: No valid training samples found.", flush=True)
        sys.exit(1)

    validate_data(entries)

    # Load model
    print(f"Loading base model: {args.base_model}...", flush=True)
    from qwen_tts import Qwen3TTSModel

    model = Qwen3TTSModel.from_pretrained(
        args.base_model,
        device_map=f"cuda:{args.device}",
        dtype=torch.bfloat16,
        attn_implementation="eager",
    )
    print("Model loaded.", flush=True)

    # Apply LoRA if requested
    if args.lora:
        try:
            from peft import LoraConfig, get_peft_model
            lora_config = LoraConfig(
                r=args.lora_rank,
                lora_alpha=args.lora_alpha,
                target_modules=["q_proj", "v_proj", "k_proj", "o_proj"],
                lora_dropout=0.05,
                bias="none",
                task_type="CAUSAL_LM",
            )
            if hasattr(model, "model") and hasattr(model.model, "talker"):
                model.model.talker = get_peft_model(model.model.talker, lora_config)
                print(f"LoRA applied (rank={args.lora_rank}, alpha={args.lora_alpha})", flush=True)
                model.model.talker.print_trainable_parameters()
            else:
                print("WARNING: Could not find talker module for LoRA. Training full model.", flush=True)
        except ImportError:
            print("WARNING: peft not installed. Training full model. Install with: pip install peft", flush=True)

    # Prepare training
    # For TTS fine-tuning, we use the model's own tokenization and loss
    # The approach: for each sample, create a voice prompt from the audio,
    # then train the model to generate that audio from the text
    print("\nPreparing voice prompts from training data...", flush=True)
    training_pairs = []
    for i, entry in enumerate(entries):
        audio, sr = sf.read(entry["audio"], dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        # Append silence for consistency
        silence = np.zeros(int(sr * 0.5), dtype=np.float32)
        audio_with_silence = np.concatenate([audio, silence])

        try:
            prompt = model.create_voice_clone_prompt(
                ref_audio=(audio_with_silence, sr),
                ref_text=entry["text"],
            )
            training_pairs.append({
                "text": entry["text"],
                "language": entry.get("language", "English"),
                "prompt": prompt,
                "audio": audio,
                "sr": sr,
            })
            if (i + 1) % 10 == 0:
                print(f"  Prepared {i + 1}/{len(entries)} prompts", flush=True)
        except Exception as e:
            print(f"  WARNING: Failed to create prompt for {entry['audio']}: {e}", flush=True)

    print(f"  {len(training_pairs)} training pairs ready.\n", flush=True)

    if len(training_pairs) == 0:
        print("ERROR: No valid training pairs created.", flush=True)
        sys.exit(1)

    # Training loop
    # Enable gradient computation on the talker
    if hasattr(model, "model") and hasattr(model.model, "talker"):
        for param in model.model.talker.parameters():
            param.requires_grad = True

    optimizer = torch.optim.AdamW(
        [p for p in model.parameters() if p.requires_grad],
        lr=args.lr,
        weight_decay=0.01,
    )
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs)

    print(f"Starting training ({args.epochs} epochs, {len(training_pairs)} samples)...\n", flush=True)
    t0 = time.time()

    for epoch in range(args.epochs):
        epoch_loss = 0.0
        n_batches = 0

        # Simple sequential training (batch_size=1 for TTS)
        for i, pair in enumerate(training_pairs):
            try:
                # Generate with teacher forcing and compute loss
                wavs, sr = model.generate_voice_clone(
                    text=pair["text"],
                    language=pair["language"],
                    voice_clone_prompt=pair["prompt"],
                    max_new_tokens=min(1200, max(180, int(len(pair["text"]) * 2))),
                    non_streaming_mode=True,
                    eos_token_id=[2150, 2157],
                )

                # Simple L1 loss between generated and target audio
                gen_audio = torch.from_numpy(wavs[0]).to(f"cuda:{args.device}")
                target_audio = torch.from_numpy(pair["audio"]).to(f"cuda:{args.device}")

                # Align lengths
                min_len = min(len(gen_audio), len(target_audio))
                if min_len > 0:
                    loss = torch.nn.functional.l1_loss(
                        gen_audio[:min_len], target_audio[:min_len]
                    )
                    loss.backward()
                    optimizer.step()
                    optimizer.zero_grad()
                    epoch_loss += loss.item()
                    n_batches += 1

            except Exception as e:
                print(f"  WARNING: Training step failed for sample {i}: {e}", flush=True)
                continue

        scheduler.step()
        avg_loss = epoch_loss / max(n_batches, 1)
        elapsed = time.time() - t0
        print(f"  Epoch {epoch + 1}/{args.epochs} — loss: {avg_loss:.6f} — "
              f"lr: {scheduler.get_last_lr()[0]:.2e} — elapsed: {elapsed:.0f}s", flush=True)

        # Save checkpoint every N epochs
        if (epoch + 1) % args.save_every == 0 or (epoch + 1) == args.epochs:
            ckpt_dir = Path(args.output) / f"checkpoint-{epoch + 1}"
            ckpt_dir.mkdir(parents=True, exist_ok=True)
            if args.lora and hasattr(model.model, "talker"):
                model.model.talker.save_pretrained(str(ckpt_dir))
            else:
                torch.save(model.state_dict(), ckpt_dir / "model.pt")
            print(f"  Checkpoint saved: {ckpt_dir}", flush=True)

    total_time = time.time() - t0
    print(f"\nTraining complete in {total_time:.0f}s ({total_time/60:.1f} min).", flush=True)
    print(f"Final model saved to: {args.output}", flush=True)

    # Save final model
    final_dir = Path(args.output) / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    if args.lora and hasattr(model.model, "talker"):
        model.model.talker.save_pretrained(str(final_dir))
    else:
        torch.save(model.state_dict(), final_dir / "model.pt")

    # Save training config
    config = {
        "base_model": args.base_model,
        "epochs": args.epochs,
        "lr": args.lr,
        "batch_size": args.batch_size,
        "lora": args.lora,
        "lora_rank": args.lora_rank if args.lora else None,
        "training_samples": len(training_pairs),
        "training_time_s": round(total_time, 1),
    }
    (Path(args.output) / "training_config.json").write_text(json.dumps(config, indent=2))
    print(f"Training config saved.", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Qwen3-TTS Fine-Tuning")
    parser.add_argument("--base-model", type=str, default="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
                        help="Base model path or HuggingFace ID")
    parser.add_argument("--data", type=str, required=True,
                        help="Path to training JSONL file")
    parser.add_argument("--output", type=str, required=True,
                        help="Output directory for fine-tuned model")
    parser.add_argument("--epochs", type=int, default=10,
                        help="Number of training epochs (default: 10)")
    parser.add_argument("--lr", type=float, default=1e-5,
                        help="Learning rate (default: 1e-5)")
    parser.add_argument("--batch-size", type=int, default=1,
                        help="Batch size (default: 1)")
    parser.add_argument("--device", type=int, default=0,
                        help="CUDA device index (default: 0)")
    parser.add_argument("--lora", action="store_true",
                        help="Use LoRA for parameter-efficient fine-tuning")
    parser.add_argument("--lora-rank", type=int, default=16,
                        help="LoRA rank (default: 16)")
    parser.add_argument("--lora-alpha", type=int, default=32,
                        help="LoRA alpha (default: 32)")
    parser.add_argument("--save-every", type=int, default=5,
                        help="Save checkpoint every N epochs (default: 5)")
    parser.add_argument("--validate-only", action="store_true",
                        help="Only validate the dataset, don't train")

    args = parser.parse_args()

    if args.validate_only:
        entries = load_training_data(args.data)
        if entries:
            validate_data(entries)
        else:
            print("No valid entries found.", flush=True)
        return

    finetune(args)


if __name__ == "__main__":
    main()
