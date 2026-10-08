"""Standalone adapter executed by PrettyPitch's Python, not the application's Python."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace


def main() -> None:
    parser = argparse.ArgumentParser()
    for name in ("root", "leapsinger-root", "score", "mora-table", "output", "device"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.root)))
    import numpy as np
    import onnxruntime as ort
    import torch
    from svs import render
    from svs.score_io import load_score

    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)

    def load_vocoder(path, sr=44100):
        options = ort.SessionOptions()
        options.intra_op_num_threads = args.threads
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        return SimpleNamespace(
            session=ort.InferenceSession(
                str(path), sess_options=options, providers=["CPUExecutionProvider"],
            ), sr=sr,
        )

    original_loader = render._load_infer_module

    def load_module(name, path):
        module = original_loader(name, path)
        if name == "ls_infer":
            module.load_vocoder = load_vocoder
        return module

    render._load_infer_module = load_module
    context = render.load_models(
        spk_id=2, device=args.device, leapsinger_root=Path(args.leapsinger_root),
    )
    if context.sample_rate != 44100:
        raise RuntimeError("Expected a 44100 Hz PrettyPitch model")
    output = np.zeros(round(args.duration * context.sample_rate), dtype=np.float32)
    phrases = load_score(
        Path(args.score), mora_table=Path(args.mora_table), phoneset=context.phoneset,
    )
    if not phrases:
        raise RuntimeError("PrettyPitch score contains no sung phrases")
    for phrase in phrases:
        result = render.render_phrase(context, phrase, style_id=0, seed=0)
        samples = np.asarray(result["wav"], dtype=np.float32)
        if samples.ndim != 1 or not len(samples) or not np.all(np.isfinite(samples)):
            raise RuntimeError("PrettyPitch returned invalid audio")
        start = round(result["start_sec"] * context.sample_rate)
        # Keep absolute score time: a leading consonant before t=0 is clipped,
        # never compensated by shifting the whole track.
        if start < 0:
            samples, start = samples[-start:], 0
        end = min(len(output), start + len(samples))
        if end > start:
            output[start:end] += samples[:end - start]
    # Keep float samples until the final blend so overlaps are not clipped early.
    np.save(args.output, output, allow_pickle=False)


if __name__ == "__main__":
    main()
