def probe_voiceover_quality(path, expected_duration):
    """Validate the completed narration file before any visual API work begins."""
    import statistics

    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_streams", "-show_format",
                "-of", "json", str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        metadata = json.loads(probe.stdout)
    except Exception as exc:
        raise RuntimeError(f"Voice-over validation failed: ffprobe could not inspect audio ({type(exc).__name__}).") from exc

    audio_streams = [s for s in metadata.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio_streams:
        raise RuntimeError("Voice-over validation failed: the generated file has no audio stream.")
    audio_stream = audio_streams[0]
    sample_rate = int(audio_stream.get("sample_rate") or 0)
    channels = int(audio_stream.get("channels") or 0)
    measured_duration = float((metadata.get("format") or {}).get("duration") or 0.0)

    if sample_rate < 44100 or channels < 1:
        raise RuntimeError(
            f"Voice-over audio format is below production requirements: {sample_rate} Hz, {channels} channel(s)."
        )
    if not 70.0 <= measured_duration <= 90.0:
        raise RuntimeError(
            f"Validated voice-over duration is {measured_duration:.2f}s; expected 70-90s."
        )
    if abs(measured_duration - float(expected_duration)) > 0.35:
        raise RuntimeError(
            "Voice-over file duration does not match the timing manifest "
            f"({measured_duration:.2f}s vs {float(expected_duration):.2f}s)."
        )

    volume = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-i", str(path),
            "-af", "volumedetect", "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    diagnostics = (volume.stderr or "") + "\n" + (volume.stdout or "")
    mean_match = re.search(r"mean_volume:\s*(-?inf|-?[0-9.]+)\s*dB", diagnostics, re.I)
    peak_match = re.search(r"max_volume:\s*(-?inf|-?[0-9.]+)\s*dB", diagnostics, re.I)
    if volume.returncode != 0 or not mean_match or not peak_match:
        raise RuntimeError("Voice-over loudness analysis could not complete; audio was not approved for production.")

    def parse_db(match):
        value = match.group(1).lower()
        return float("-inf") if value == "-inf" else float(value)

    mean_db = parse_db(mean_match)
    peak_db = parse_db(peak_match)
    if not math.isfinite(mean_db) or mean_db < -42.0:
        raise RuntimeError(
            f"Voice-over is silent or unusually quiet (mean level {mean_db:.1f} dB); "
            "visual production was not started."
        )
    if not math.isfinite(peak_db) or peak_db > 0.2:
        raise RuntimeError(
            f"Voice-over peak level is invalid ({peak_db:.1f} dB); possible clipping or corrupt audio."
        )

    return {
        "status": "passed",
        "duration_seconds": round(measured_duration, 3),
        "sample_rate_hz": sample_rate,
        "channels": channels,
        "codec": audio_stream.get("codec_name"),
        "mean_volume_db": round(mean_db, 2),
        "peak_volume_db": round(peak_db, 2),
        "checks": [
            "audio stream present",
            "sample rate at least 44.1 kHz",
            "duration between 70 and 90 seconds",
            "duration matches timing manifest",
            "non-silent audio level",
            "no detected digital clipping above +0.2 dBFS",
        ],
    }


def build_voiceover(script):
    """Build, assemble, normalize and validate the complete VO, without captions.

    Crucially, a missing Edge TTS WordBoundary event never invalidates audio.
    Caption alignment is a separate stage after the final audio file passes QA.
    """
    sentences = split_sentences(script)
    if not sentences:
        raise RuntimeError("The script contains no speakable sentences.")

    async def generate():
        wav_parts = []
        sentence_records = []
        cursor = 0.0

        opening_silence = OUT / "opening_silence.wav"
        run([
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "anullsrc=r=48000:cl=stereo",
            "-t", str(OPENING_LEAD), "-c:a", "pcm_s16le", str(opening_silence),
        ])
        wav_parts.append(opening_silence)
        cursor += OPENING_LEAD

        for index, sentence in enumerate(sentences):
            mp3_path = OUT / f"tts_{index:02d}.mp3"
            wav_path = OUT / f"tts_{index:02d}.wav"
            boundaries = []
            last_tts_error = None
            generated = False

            for tts_attempt in range(3):
                boundaries = []
                mp3_path.unlink(missing_ok=True)
                communicate = edge_tts.Communicate(
                    sentence,
                    VOICE,
                    rate=BASE_RATE,
                    pitch=BASE_PITCH,
                    boundary="WordBoundary",
                )
                try:
                    with open(mp3_path, "wb") as audio_file:
                        async for message in communicate.stream():
                            if message["type"] == "audio":
                                audio_file.write(message["data"])
                            elif message["type"] == "WordBoundary":
                                boundaries.append({
                                    "text": message["text"],
                                    "offset": message["offset"] / 10_000_000,
                                    "duration": message["duration"] / 10_000_000,
                                })
                    # Audio existence, not word-boundary count, determines TTS
                    # success. Timing gaps are repaired after the VO is final.
                    if mp3_path.exists() and mp3_path.stat().st_size >= 1000:
                        generated = True
                        break
                    raise RuntimeError("TTS returned an empty or incomplete audio file")
                except Exception as exc:
                    last_tts_error = exc
                    print(
                        f"TTS attempt {tts_attempt + 1}/3 failed for sentence "
                        f"{index + 1}; retrying."
                    )
                    await asyncio.sleep(2 * (tts_attempt + 1))

            if not generated:
                raise RuntimeError(
                    f"Edge TTS failed on sentence {index + 1} after 3 attempts: "
                    f"{type(last_tts_error).__name__ if last_tts_error else 'unknown error'}"
                )

            run([
                "ffmpeg", "-y", "-i", str(mp3_path),
                "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(wav_path),
            ])
            clip_duration = ffprobe_duration(wav_path)
            if clip_duration <= 0.05:
                raise RuntimeError(f"TTS sentence {index + 1} produced an invalid audio duration.")

            sentence_start = cursor
            sentence_end = sentence_start + clip_duration
            sentence_records.append({
                "sentence_index": index,
                "text": sentence,
                "start_unscaled": sentence_start,
                "end_unscaled": sentence_end,
                "duration_unscaled": clip_duration,
                "boundaries": boundaries,
                "boundary_count": len(boundaries),
            })
            wav_parts.append(wav_path)
            cursor = sentence_end

            gap = HOOK_GAP if index == 0 else SENTENCE_GAP
            silence = OUT / f"gap_{index:02d}.wav"
            run([
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", "anullsrc=r=48000:cl=stereo",
                "-t", str(gap), "-c:a", "pcm_s16le", str(silence),
            ])
            wav_parts.append(silence)
            cursor += gap

        # Remove only the final inter-sentence pause; retain the deliberate
        # ending hold and all pauses between spoken sentences.
        last_gap = wav_parts.pop()
        last_gap.unlink(missing_ok=True)
        ending = OUT / "ending_hold.wav"
        run([
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "anullsrc=r=48000:cl=stereo",
            "-t", str(ENDING_HOLD), "-c:a", "pcm_s16le", str(ending),
        ])
        wav_parts.append(ending)

        concat = OUT / "voice_concat.txt"
        concat.write_text(
            "".join(f"file '{part.resolve()}'\\n" for part in wav_parts),
            encoding="utf-8",
        )
        voice_path = OUT / "voiceover.wav"
        run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
            "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", str(voice_path),
        ])

        original_duration = ffprobe_duration(voice_path)
        correction = duration_correction(original_duration)
        time_scale = 1.0
        if correction is not None:
            paced_path = OUT / "voiceover_paced.wav"
            run([
                "ffmpeg", "-y", "-i", str(voice_path),
                "-af", f"atempo={correction['atempo']:.8f}",
                "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(paced_path),
            ])
            paced_duration = ffprobe_duration(paced_path)
            if not 70.0 <= paced_duration <= 90.0:
                raise RuntimeError(
                    f"Tempo correction produced {paced_duration:.2f}s; expected 70-90s."
                )
            time_scale = paced_duration / original_duration
            paced_path.replace(voice_path)
            total_duration = paced_duration
            print(
                f"VO timing normalized: {original_duration:.2f}s -> "
                f"{total_duration:.2f}s with pitch-preserving tempo correction."
            )
        else:
            total_duration = original_duration
            print(f"VO duration is within target: {total_duration:.2f}s.")

        sentence_events = []
        for record in sentence_records:
            start = record["start_unscaled"] * time_scale
            end = record["end_unscaled"] * time_scale
            duration = record["duration_unscaled"] * time_scale
            sentence_events.append({
                "start": start,
                "end": end,
                "duration": duration,
            })
            record["start"] = start
            record["end"] = end
            record["duration"] = duration

        mp3_output = OUT / "voiceover.mp3"
        run([
            "ffmpeg", "-y", "-i", str(voice_path),
            "-c:a", "libmp3lame", "-b:a", "192k", str(mp3_output),
        ])
        return {
            "audio_wav": str(voice_path),
            "audio_mp3": str(mp3_output),
            "sentences": sentence_records,
            "sentence_events": sentence_events,
            "original_duration_seconds": original_duration,
            "time_scale": time_scale,
            "total_duration": total_duration,
            "voice": VOICE,
            "rate": BASE_RATE,
            "pitch": BASE_PITCH,
        }

    voice = asyncio.run(generate())
    # This occurs after the whole VO has been generated and before visual
    # searches, so bad narration cannot consume visual API calls or create a
    # video with an unapproved voice track.
    voice["audio_quality"] = probe_voiceover_quality(
        voice["audio_wav"], voice["total_duration"]
    )
    Path("voiceover_manifest.json").write_text(
        json.dumps({
            "status": "validated",
            "script": script,
            "voice": voice["voice"],
            "rate": voice["rate"],
            "pitch": voice["pitch"],
            "duration_seconds": round(voice["total_duration"], 3),
            "time_scale": round(voice["time_scale"], 6),
            "audio_file": voice["audio_mp3"],
            "audio_quality": voice["audio_quality"],
            "sentence_count": len(voice["sentences"]),
            "edge_word_boundary_count": sum(x["boundary_count"] for x in voice["sentences"]),
            "stage": "VO completed and validated before subtitles or visual retrieval",
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(
        "VO quality gate passed: "
        f"{voice['total_duration']:.2f}s, "
        f"{voice['audio_quality']['sample_rate_hz']} Hz, "
        f"mean {voice['audio_quality']['mean_volume_db']:.1f} dB, "
        f"peak {voice['audio_quality']['peak_volume_db']:.1f} dB."
    )
    return voice


def build_subtitles_from_voice(voice):
    """Build subtitle timing only after the complete VO passed quality checks."""
    word_events = []
    sentence_events = voice["sentence_events"]
    time_scale = float(voice.get("time_scale", 1.0))
    edge_word_count = 0
    estimated_word_count = 0
    full_sentence_estimate_count = 0

    for record in voice["sentences"]:
        sentence_index = int(record["sentence_index"])
        local_events = align_boundary_text(
            record["text"],
            record.get("boundaries", []),
            audio_duration=record["duration_unscaled"],
        )
        for event in local_events:
            source = event.get("timing_source", "estimated")
            if source == "edge_tts":
                edge_word_count += 1
            else:
                estimated_word_count += 1
                if source == "estimated_full_sentence":
                    full_sentence_estimate_count += 1
            word_events.append({
                **event,
                "offset": record["start"] + float(event["offset"]) * time_scale,
                "duration": float(event["duration"]) * time_scale,
                "sentence_index": sentence_index,
            })

    if not word_events:
        raise RuntimeError("VO completed, but no subtitle words could be recovered from the script.")

    groups = build_phrase_groups(word_events)
    if not groups:
        raise RuntimeError("Subtitle phrase grouping produced no readable groups.")

    total_duration = float(voice["total_duration"])
    lines = [
        "[Script Info]",
        "Title: VO-aligned documentary captions",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "PlayResX: 1080",
        "PlayResY: 1920",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,DejaVu Sans,48,&H00FFFFFF,&H0000D7FF,&H00101A25,&H9A07121D,-1,0,0,0,100,100,0,0,1,3.2,1.6,2,80,80,355,1",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    for group_index, group in enumerate(groups):
        start, end = group_bounds(group)
        if group_index + 1 < len(groups):
            next_start, _ = group_bounds(groups[group_index + 1])
            end = min(end + 0.10, max(start + 0.35, next_start - 0.02), total_duration)
        else:
            end = min(end + 0.10, total_duration)
        phrase = karaoke_phrase(group)
        if phrase:
            lines.append(
                f"Dialogue: 0,{ass_time(start)},{ass_time(end)},"
                f"Default,,0,0,0,,{phrase}"
            )

    (OUT / "captions.ass").write_text("\n".join(lines), encoding="utf-8")
    alignment = {
        "total_words": len(word_events),
        "edge_timed_words": edge_word_count,
        "estimated_words": estimated_word_count,
        "full_sentence_estimate_words": full_sentence_estimate_count,
        "edge_timing_coverage": round(edge_word_count / max(1, len(word_events)), 4),
        "caption_group_count": len(groups),
        "alignment_status": "complete_with_estimates" if estimated_word_count else "complete_edge_timing",
    }
    Path("timing.json").write_text(
        json.dumps({
            "sentences": sentence_events,
            "word_events": word_events,
            "caption_groups": [
                {
                    "start": group_bounds(group)[0],
                    "end": group_bounds(group)[1],
                    "text": " ".join(word["text"] for word in group),
                }
                for group in groups
            ],
            "alignment": alignment,
            "total_duration": total_duration,
            "timing_master": "validated_voiceover",
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(
        f"Subtitles created from final VO: {len(word_events)} words; "
        f"{edge_word_count} measured by Edge TTS, {estimated_word_count} estimated; "
        f"{len(groups)} natural phrase groups."
    )
    return {
        "sentence_events": sentence_events,
        "word_events": word_events,
        "total_duration": total_duration,
        "alignment": alignment,
    }


def attach_voice_timing_to_scenes(plan, voice):
    """Bind each planned scene to the measured final narration timeline."""
    events = voice["sentence_events"]
    for scene_index, scene in enumerate(plan["scenes"], 1):
        first = int(scene["sentence_start"])
        last = int(scene["sentence_end"])
        if first < 0 or last >= len(events) or last < first:
            raise RuntimeError(f"Scene {scene_index} has an invalid voice-over sentence range.")
        voice_start = float(events[first]["start"])
        spoken_end = float(events[last]["end"])
        visual_end = (
            float(events[int(plan["scenes"][scene_index]["sentence_start"])]["start"])
            if scene_index < len(plan["scenes"])
            else float(voice["total_duration"])
        )
        if scene_index == 1:
            voice_start = 0.0
        if visual_end <= voice_start:
            raise RuntimeError(f"Scene {scene_index} has a non-positive VO-anchored duration.")
        scene["voice_timing"] = {
            "start_seconds": round(voice_start, 3),
            "spoken_end_seconds": round(spoken_end, 3),
            "visual_end_seconds": round(visual_end, 3),
            "duration_seconds": round(visual_end - voice_start, 3),
            "first_sentence_index": first,
            "last_sentence_index": last,
        }
    Path("plan.json").write_text(
        json.dumps(plan, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    voice_manifest_path = Path("voiceover_manifest.json")
    manifest = json.loads(voice_manifest_path.read_text(encoding="utf-8"))
    manifest["scene_timing"] = [
        {"scene": i, **scene["voice_timing"]}
        for i, scene in enumerate(plan["scenes"], 1)
    ]
    voice_manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


