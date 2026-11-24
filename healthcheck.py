"""Validate the GitHub Actions runner before a paid generation run."""
import importlib
import subprocess
import sys


REQUIRED_MODULES = (
    "requests", "edge_tts", "torch", "transformers", "sentencepiece", "google.protobuf", "PIL", "safetensors"
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

    # The opening card must never fall back to an unreadable bitmap font.
    try:
        from hook_overlay import _font_path
        print(f"Opening overlay font: {_font_path(True)}")
    except Exception as exc:
        failures.append(f"opening overlay font: {exc}")

    # Eight 1080x1920 clips plus segments and the final encode need headroom;
    # running out of disk mid-render used to look like a random FFmpeg failure.
    try:
        import shutil
        free_gb = shutil.disk_usage(".").free / (1024 ** 3)
        print(f"Free disk: {free_gb:.1f} GB")
        if free_gb < 2.0:
            failures.append(f"only {free_gb:.1f} GB free disk space; at least 2 GB is required")
    except OSError as exc:
        print(f"Disk space check skipped: {type(exc).__name__}")

    if failures:
        print("Health check failed:")
        for failure in failures:
            print(f" - {failure}")
        raise SystemExit(1)
    print("Health check passed.")


if __name__ == "__main__":
    main()
