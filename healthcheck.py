
"""Validate the GitHub Actions runner before a paid generation run."""
import importlib
import subprocess
import sys


REQUIRED_MODULES = (
    "requests", "edge_tts", "torch", "transformers", "sentencepiece", "PIL", "safetensors"
)


def main():
    failures = []
    print(f"Python: {sys.version.split()[0]}")
    for name in REQUIRED_MODULES:
        try:
            module = importlib.import_module(name)
            version = getattr(module, "__version__", "installed")
            print(f"Dependency OK: {name} ({version})")
        except Exception as exc:
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    try:
        import torch
        print(f"PyTorch: {torch.__version__}; CUDA available={torch.cuda.is_available()}; CPU mode is supported.")
        torch.set_num_threads(2)
    except Exception as exc:
        failures.append(f"torch runtime: {exc}")

    for command in ("ffmpeg", "ffprobe"):
        try:
            result = subprocess.run([command, "-version"], capture_output=True, text=True, timeout=15, check=True)
            print(f"{command}: {result.stdout.splitlines()[0]}")
        except Exception as exc:
            failures.append(f"{command}: {exc}")

    try:
        result = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, timeout=20, check=True)
        if "subtitles" not in result.stdout:
            failures.append("FFmpeg subtitles/libass filter is unavailable")
        else:
            print("FFmpeg subtitles filter: available")
        encoders = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20, check=True)
        if "libx264" not in encoders.stdout:
            failures.append("FFmpeg libx264 encoder is unavailable")
        else:
            print("FFmpeg libx264 encoder: available")
    except Exception as exc:
        failures.append(f"FFmpeg filter check: {exc}")

    if failures:
        print("Health check failed:")
        for failure in failures:
            print(f" - {failure}")
        raise SystemExit(1)
    print("Health check passed.")


if __name__ == "__main__":
    main()
