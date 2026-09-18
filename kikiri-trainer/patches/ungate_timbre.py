#!/usr/bin/env python3
"""Decouple timbre learning from the SLM adversarial phase in train_second.py.

Upstream steps the ``style_encoder`` and ``decoder`` optimizers only once
``epoch >= joint_epoch``:

    if epoch >= joint_epoch:
        optimizer.step("style_encoder")
        optimizer.step("decoder")

Those two modules are where a speaker's timbre lives, and their gradients are
already populated on every iteration by ``g_loss`` (``lambda_mel * loss_mel``,
weight 5.0). Gating the *step* ties "learning how the speaker sounds" to the SLM
adversarial phase, which is a separate concern: that phase needs more VRAM than a
12 GB card has, and even on a 16 GB card it progresses orders of magnitude slower
(measured: no logged step in 21 minutes, versus 3.3 s/step without it).

The practical consequence upstream is a trap. Push ``joint_epoch`` beyond the run
length to make training fit, and it completes happily while producing a voice
that only ever learned the speaker's *phrasing* — the decoder stays bit-identical
to the base model. That is exactly what happened to the first two Vocarium voices.

This patch steps both optimizers every iteration and leaves ``joint_epoch``
gating only the adversarial polish it was meant to gate.
"""

from __future__ import annotations

import sys
from pathlib import Path

TARGET = Path("/workspace/StyleTTS2/train_second.py")

REPLACEMENTS = [
    # The base checkpoint ships trained discriminators (mpd 41.1M, msd 0.3M,
    # wd 1.2M parameters) but upstream throws them away on load. Randomly
    # initialised discriminators against an already-trained generator explode:
    # measured three times, `Disc Loss: nan` roughly 150 steps after the
    # adversarial losses switch on, taking the mel loss to 0.00000 and the
    # validation loss from 0.420 to 1.62. Keep them — they are calibrated for
    # exactly this generator and this language.
    (
        """                ignore_modules=[
                    "predictor_encoder",
                    "msd",
                    "mpd",
                    "wd",
                    "diffusion",
                ],""",
        """                ignore_modules=[
                    "predictor_encoder",
                    "diffusion",
                ],""",
    ),
    # A separate gate for timbre, so it no longer shares one with the WavLM phase.
    # Defaults to 1: one epoch of prosody-only warmup, then timbre. Starting the
    # discriminators cold against the base checkpoint diverges — measured twice,
    # the second time to `Disc Loss: nan` by step 150.
    (
        """    joint_epoch = loss_params.joint_epoch""",
        """    joint_epoch = loss_params.joint_epoch
    # Vocarium: timbre (discriminators + decoder/style_encoder) on its own gate.
    timbre_epoch = getattr(loss_params, "timbre_epoch", 1)""",
    ),
    # The waveform discriminators and the generator adversarial loss. The decoder
    # cannot be trained without them: on mel L1 alone it collapses from 0.396 to
    # 0.00000 within 100 steps.
    (
        """        if epoch >= joint_epoch:
            start_ds = True""",
        """        if epoch >= timbre_epoch:
            start_ds = True""",
    ),
    # The optimizer steps that actually move timbre.
    (
        """            if epoch >= joint_epoch:
                optimizer.step("style_encoder")
                optimizer.step("decoder")

                # randomly pick whether to use in-distribution text""",
        """            if epoch >= timbre_epoch:
                optimizer.step("style_encoder")
                optimizer.step("decoder")

            if epoch >= joint_epoch:
                # randomly pick whether to use in-distribution text""",
    ),
]


FINETUNE = Path("/workspace/StyleTTS2/train_finetune.py")

# The kikiri fork carries a PyTorch-2.6 shim (torch.load defaults to
# weights_only=True there, which cannot unpickle the ASR aligner checkpoint) —
# but only in train_second.py. train_finetune.py needs the same one.
FINETUNE_SHIM = (
    """import torch
""",
    """import torch

if getattr(torch, "_original_load", None) is None:
    torch._original_load = torch.load
    torch.load = lambda *args, **kwargs: torch._original_load(
        *args, **{**kwargs, "weights_only": False}
    )
""",
)


def main() -> None:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else TARGET
    source = target.read_text(encoding="utf-8")
    applied = 0
    for old, new in REPLACEMENTS:
        if new in source:
            continue
        if old not in source:
            raise SystemExit(
                f"{target}: expected block not found — upstream changed, re-check "
                "the patch before trusting a fine-tune:\n" + old
            )
        source = source.replace(old, new, 1)
        applied += 1
    target.write_text(source, encoding="utf-8")
    print(f"train_second.py: {applied} timbre gate(s) removed, WavLM phase still gated")

    finetune = FINETUNE if len(sys.argv) <= 1 else Path(sys.argv[1]).parent / "train_finetune.py"
    if finetune.is_file():
        source = finetune.read_text(encoding="utf-8")
        old, new = FINETUNE_SHIM
        if new not in source:
            if old not in source:
                raise SystemExit(f"{finetune}: import anchor for the torch.load shim not found")
            finetune.write_text(source.replace(old, new, 1), encoding="utf-8")
        print("train_finetune.py: torch.load shim in place")


if __name__ == "__main__":
    main()
