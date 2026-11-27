"""Local end-to-end production check (zero cost, no API key required).

This harness exercises the parts of the production line that can run offline
with free tools only:

    script (fixture) -> edge-tts voice-over -> captions -> editing/rendering
    -> music bed + sound design -> audio mix -> final QC

It deliberately uses synthetic vertical clips instead of live footage so the
editing pipeline can be verified without stock-footage API limits or AI vision
keys. Research/fact-check/footage-search are covered by the main pipeline and
by unit tests; this harness proves the render, VO, timing, caption, audio-mix
and QC stages on a real 1080x1920 file.

Usage: python tools/local_e2e.py [output_dir]
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

SCRIPT = (
    "Bumi memiliki lautan yang jauh lebih luas daripada daratannya, dan sebagian "
    "besar belum pernah kita lihat langsung. "
    "Para ilmuwan baru memetakan sebagian kecil dari dasar samudra yang sangat "
    "luas ini. "
    "Di bawah permukaan, cahaya matahari menghilang perlahan sampai menjadi gelap "
    "total di kedalaman tertentu. "
    "Meski begitu, kehidupan tetap ada di sana dengan bentuk yang tidak biasa dan "
    "sulit dibayangkan. "
    "Beberapa hewan menghasilkan cahaya sendiri melalui reaksi kimia di dalam "
    "tubuhnya, dan cahaya itu dipakai untuk berkomunikasi. "
    "Cahaya yang mereka keluarkan sering berwarna biru atau hijau yang lembut di "
    "dalam air gelap. "
    "Hewan lain bertahan dengan memakan sisa organik yang jatuh dari lapisan air "
    "yang lebih terang di atasnya. "
    "Tekanan air di kedalaman ekstrem bisa berkali-kali lipat tekanan di "
    "permukaan, sehingga tubuh mereka berevolusi dengan cara yang berbeda. "
    "Beberapa spesies bahkan tidak memiliki mata karena hidup di kegelapan total "
    "sepanjang waktu. "
    "Suhu air juga turun drastis, membuat metabolisme berjalan jauh lebih lambat "
    "daripada hewan di permukaan. "
    "Setiap penemuan baru dari kedalaman mengingatkan kita bahwa laut masih "
    "menyimpan banyak rahasia yang belum terbaca. "
    "Setiap ekspedisi membawa pulang data yang belum pernah ada sebelumnya, dan "
    "sebagian besar dasar laut masih menunggu untuk dijelajahi."
)

_SENTENCES = [sentence.strip() + "." for sentence in SCRIPT.split(".") if sentence.strip()]
_GROUP_SIZES = [2, 2, 2, 2, 1, 1, 1, 1]  # 12 sentences -> exactly 8 scenes
_QUERIES = [
    ("ocean surface from above", ["aerial blue ocean water", "sea horizon wide shot"]),
    ("deep sea sunlight fading", ["underwater light rays", "dark ocean water column"]),
    ("deep sea creature swimming", ["deep ocean animal close up", "underwater creature silhouette"]),
    ("bioluminescent plankton glowing", ["glowing jellyfish in dark water", "blue light underwater"]),
    ("organic particles sinking underwater", ["marine snow underwater", "tiny particles in deep water"]),
    ("deep sea fish with pressure", ["deep sea anglerfish", "strange deep ocean fish"]),
    ("cold deep ocean water", ["cold dark underwater scene", "deep ocean darkness"]),
    ("ocean research expedition", ["research ship at sea", "scientific ocean exploration"]),
]
SCENES = []
_cursor = 0
for _index, _size in enumerate(_GROUP_SIZES):
    _chunk = _SENTENCES[_cursor:_cursor + _size]
    _cursor += _size
    _query, _alternatives = _QUERIES[_index]
    SCENES.append({
        "narration": " ".join(_chunk),
        "visual_query": _query,
        "visual_queries": _alternatives,
    })


def build_clips(footage_dir: Path, count=8, seconds=12):
    """Generate distinct synthetic vertical clips (no third-party footage)."""
    footage_dir.mkdir(parents=True, exist_ok=True)
    patterns = [
        ("testsrc2=size=1080x1920:rate=30", "0x0e2a47"),
        ("smptebars=size=1080x1920:rate=30", "0x11324f"),
        ("testsrc=size=1080x1920:rate=30", "0x0b2239"),
        ("rgbtestsrc=size=1080x1920:rate=30", "0x123a5c"),
    ]
    clips = []
    for index in range(count):
        pattern, tint = patterns[index % len(patterns)]
        output = footage_dir / f"clip_{index + 1:02d}.mp4"
        if output.is_file() and output.stat().st_size > 100_000:
            clips.append(output)
            continue
        command = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"{pattern}:d={seconds}",
            "-t", str(seconds),
            "-vf",
            (
                f"hue=s=0.35:h={index * 47},colorbalance=bs=0.25:bm=0.15,"
                f"drawbox=x=0:y=1600:w=1080:h=120:color={tint}@0.65:t=fill,"
                "fps=30,format=yuv420p"
            ),
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
            str(output),
        ]
        subprocess.run(command, check=True)
        clips.append(output)
    return clips


def main():
    workdir = Path(sys.argv[1] if len(sys.argv) > 1 else "e2e_run").resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    os.chdir(workdir)
    os.environ.setdefault("SCRIPT_LANGUAGE", "id")
    os.environ.setdefault("MUSIC_BED", "true")
    os.environ.setdefault("SFX_ENABLED", "true")

    import main as pipeline
    import qc as qc_module

    print(f"Workdir: {workdir}")
    clips = build_clips(Path("output") / "footage")

    plan = pipeline.validate_plan_candidate({
        "title": "Kedalaman Laut",
        "hook": "Sebagian besar laut belum pernah kita lihat.",
        "script": SCRIPT,
        "scenes": [dict(scene) for scene in SCENES],
    })
    for index, scene in enumerate(plan["scenes"]):
        scene["media_file"] = str(clips[index])
        scene["source"] = "Synthetic test clip (local FFmpeg)"
        scene["creator"] = "Auto Documentary Video harness"
        scene["page_url"] = ""
        scene["license"] = "Generated locally; no third-party rights"
        scene["visual_type"] = "footage"

    Path("plan.json").write_text(
        json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Plan ready: {len(plan['script'].split())} words, {len(plan['scenes'])} scenes.")

    sentence_events, total_duration = pipeline.build_voice_and_subs(
        plan["script"], enforce_minimum=False
    )
    print(f"VO measured: {total_duration:.2f}s")
    if total_duration < pipeline.MIN_VIDEO_DURATION:
        raise SystemExit(
            f"VO is {total_duration:.2f}s, below 70s. In production this triggers "
            "script extension or a slower natural pace; set TTS_RATE=-8% for this "
            "harness if the fixture is short."
        )

    pipeline.render(plan, sentence_events, total_duration)
    report = qc_module.run_qc("documentary.mp4", plan, total_duration)
    qc_module.write_qc_report(report)
    Path("timing.json").write_text(
        json.dumps(
            {"total_duration": total_duration, "sentences": sentence_events},
            indent=2,
        ),
        encoding="utf-8",
    )
    print("QC status:", report["status"])
    print(json.dumps({c["check"]: c["status"] for c in report["checks"]}, indent=2))


if __name__ == "__main__":
    main()
