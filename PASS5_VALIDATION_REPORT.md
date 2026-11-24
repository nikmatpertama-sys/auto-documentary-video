# PROJECT1 — Pass 5 Validation Report

## Scope

Pass 5 reorganizes production so that the completed, measured voice-over is the master timeline. It also repairs missing Edge TTS word-boundary events without dropping script words or aborting subtitle generation.

## Implemented production order

1. Generate and validate the script.
2. Generate the complete voice-over, including inter-sentence pauses and the ending hold.
3. Inspect the assembled audio with FFmpeg/ffprobe. The audio must have a valid stream, at least 44.1 kHz sample rate, 70–90 seconds duration, duration matching the timing manifest, audible mean level, and a valid peak level.
4. Lock the validated VO timeline and assign each scene its measured time window.
5. Generate subtitle timings against the final audio. Edge TTS timings are used where available; only missing word timings are estimated within the measured sentence audio window.
6. Retrieve footage and published reference visuals against the VO-anchored scene/narration map.
7. Render the scenes to the same VO timeline and validate the final MP4 streams, dimensions, frame rate, and duration.

## Tests performed

- Python syntax compilation: passed for the production modules.
- Unit suite: 33/33 tests passed.
- Workflow YAML parsing: passed.
- ZIP integrity test: passed.
- Clean extraction test: passed; the extracted ZIP independently passed Python compilation and 33/33 tests.
- Integrated local voice pipeline simulation: passed. eSpeak-generated test speech was supplied through a fake Edge TTS adapter, with deliberately missing word-boundary events. The pipeline assembled the VO, applied gentle duration correction, ran FFmpeg/ffprobe audio QA, created captions, and assigned all eight scene windows.
- In that simulation, measured final VO duration was 70.976 seconds; audio was 48 kHz stereo, mean level -24.2 dB, peak -3.4 dB. Subtitle timing retained all 138 script words: 122 used supplied timing events and 16 received estimates. All subtitle timings fit within the final VO duration.
- Six-second FFmpeg render smoke test: passed, producing a 1080x1920 H.264/AAC preview with title overlay and ASS subtitles.

## What these tests do not prove

The isolated build environment did not have the `edge-tts` package or access to the real Edge TTS service, and it did not have the user's GitHub Secrets or external footage/vision API access. The integrated test therefore verifies timing, orchestration, and media handling using test speech; it does not certify the real Edge voice's pronunciation, naturalness, or expressive delivery. It also does not certify the full GitHub Actions run or a finished documentary with live footage providers. Those require a run in the user's `project1` branch.

The audio QA stage verifies technical properties (stream, sample rate, duration, level, peak); it cannot replace listening to the MP3 to judge accent, pronunciation, pacing, or editorial quality.

## Review artifacts produced by a successful Actions run

- `output/voiceover.mp3`: listen to the exact VO used by the video.
- `timing.json`: inspect every subtitle word and whether its timing came from Edge TTS or estimation.
- `production_report.json`: includes the complete voice-over manifest, subtitle alignment coverage, VO scene windows, and visual-source diagnostics.
- `documentary.mp4`: final rendered video.
