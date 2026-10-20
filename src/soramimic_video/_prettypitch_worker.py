"""Standalone adapter executed by PrettyPitch's Python, not the application's Python."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

GPU_RETRY_EXIT_CODE = 75
MIN_CUDA_FREE_BYTES = 2 * 1024**3


def cuda_has_capacity(torch, device: str) -> bool:
    try:
        return (
            torch.cuda.is_available()
            and torch.cuda.mem_get_info(device)[0] >= MIN_CUDA_FREE_BYTES
        )
    except RuntimeError:
        return False


def native_vocoder(root: Path, device: str, sr: int):
    # NHVSing's modules have generic names; keep its search path local to loading.
    original_path = list(sys.path)
    sys.path.insert(0, str(root))
    try:
        from nhv_vocoder import NHVVocoder

        native = NHVVocoder(
            str(root / "exported_models/v3_2_1/nhv_v3_2_1.pth"),
            str(root / "config_v3_2.yaml"), device=device,
        )
    finally:
        sys.path[:] = original_path
    if native.sample_rate != sr or native.hop_size != 256:
        raise RuntimeError("Expected a 44100 Hz / hop256 NHVSing model")

    class Session:
        def run(self, _names, inputs):
            samples = native.infer(
                inputs["mel"][0], inputs["f0"].reshape(-1), inputs["uv"].reshape(-1),
            )
            return [samples.reshape(1, 1, -1)]

    return SimpleNamespace(session=Session(), sr=sr)


def render_audio(args) -> None:
    sys.path.insert(0, str(Path(args.root)))
    import numpy as np
    import torch
    from svs import render
    from svs.score_io import load_score

    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)
    if args.device.startswith("cuda") and not cuda_has_capacity(torch, args.device):
        sys.exit(GPU_RETRY_EXIT_CODE)

    def load_vocoder(path, sr=44100):
        if args.nhvsing_root:
            return native_vocoder(Path(args.nhvsing_root), args.device, sr)
        import onnxruntime as ort

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
        spk_id=args.speaker_id, device=args.device, leapsinger_root=Path(args.leapsinger_root),
    )
    if context.sample_rate != 44100:
        raise RuntimeError("Expected a 44100 Hz PrettyPitch model")
    if args.output_format == "wav":
        import soundfile as sf

        samples, rate = render.render_score(
            Path(args.score), ctx=context, mora_table=Path(args.mora_table), seed=0,
        )
        if not len(samples) or not np.all(np.isfinite(samples)):
            raise RuntimeError("PrettyPitch returned invalid audio")
        sf.write(args.output, samples, rate, subtype="PCM_16")
        return
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


def main() -> None:
    parser = argparse.ArgumentParser()
    for name in ("root", "leapsinger-root", "score", "mora-table", "output", "device"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--speaker-id", type=int, default=2)
    parser.add_argument("--nhvsing-root")
    parser.add_argument("--output-format", choices=("npy", "wav"), default="npy")
    args = parser.parse_args()
    if args.output_format == "npy" and (args.duration is None or args.duration <= 0):
        parser.error("--duration must be positive for npy output")
    try:
        render_audio(args)
    except Exception as exc:
        import torch

        if args.device.startswith("cuda") and isinstance(exc, torch.cuda.OutOfMemoryError):
            print("PrettyPitch CUDA memory exhausted; retry on CPU", file=sys.stderr)
            sys.exit(GPU_RETRY_EXIT_CODE)
        raise


if __name__ == "__main__":
    main()
