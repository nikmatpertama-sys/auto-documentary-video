import os, re, json, asyncio, subprocess, textwrap, time, hashlib
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont
import edge_tts

ROOT = Path(".")
OUT = Path("output")
FOOT = OUT / "footage"
VIS = OUT / "visuals"
OUT.mkdir(exist_ok=True)
FOOT.mkdir(exist_ok=True)
VIS.mkdir(exist_ok=True)

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
PIXABAY_KEY = os.getenv("PIXABAY_API_KEY", "").strip()
TOPIC = os.getenv("INPUT_TOPIC", "").strip()
VOICE = os.getenv("TTS_VOICE", "en-US-GuyNeural")
TTS_RATE = os.getenv("TTS_RATE", "-5%")
TTS_PITCH = os.getenv("TTS_PITCH", "-4Hz")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

TARGET_SECONDS = 70
W, H, FPS = 1080, 1920, 30

if not GEMINI_KEY:
    raise RuntimeError("Missing GEMINI_API_KEY GitHub Secret.")
if not PIXABAY_KEY:
    raise RuntimeError("Missing PIXABAY_API_KEY GitHub Secret.")
if not TOPIC:
    raise RuntimeError("Enter a topic in the Run workflow form.")


def run(cmd):
    subprocess.run(cmd, check=True)


def gemini_plan(topic):
    prompt = f"""Create a fact-conscious, original English mini-documentary plan for vertical cinematic documentary video.
TOPIC: {topic}
Target voiceover duration: 60-75 seconds (roughly 145-165 spoken words).
Return ONLY valid JSON with this exact structure:
{{"title":"...","hook":"one punchy spoken opening sentence, max 12 words","script":"...","scenes":[{{"narration":"exact sentence(s) from script","visual_query":"specific searchable real-world footage phrase, 3-7 words","visual_queries":["alternate specific search phrase","another visual angle"],"visual_type":"footage or diagram","diagram_kind":"process, map, comparison, timeline, or data","diagram_title":"short title","diagram_text":"one concise explanatory line","diagram_steps":["short step 1","short step 2","short step 3"],"source_label":"reputable institution name only when genuinely certain","source_url":"official source URL only when genuinely certain"}}]}}
Requirements: 8 scenes; scene 1 MUST be real footage (never a diagram) and its narration is a curiosity-driven hook suitable for the first 3 seconds. Open with a surprising consequence, paradox, vivid question, or stakes; avoid generic introductions. Script must be one coherent story with a reveal and satisfying payoff. Every scene narration appears verbatim in script and scenes cover it in order. Vary sentence length, cadence, pauses, and emotional intensity; write for a warm, deep male documentary narrator, not an announcer. Use 2-3 animated explanatory diagram scenes, choosing process/map/comparison/timeline/data layouts; never write a generic info-card paragraph. Other scenes use highly specific real-world stock-footage queries with distinct camera angles and subjects. Ask for 2 alternate footage queries per scene. No fabricated statistics, unsupported precise numbers, fake citations, or invented source URLs. Only name an institution/source when the fact and official URL are confidently known; otherwise leave source fields empty. Avoid repetitive structures, list-like narration, and calls to action."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.55,
        },
    }

    # Retry the configured model only. Do not assume an unconfigured fallback model exists.
    model_candidates = [MODEL]

    response = None
    last_error = None

    for model_index, active_model in enumerate(model_candidates):
        active_url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{active_model}:generateContent"
        )
        for attempt in range(1, 4):
            try:
                response = requests.post(
                    active_url,
                    params={"key": GEMINI_KEY},
                    json=payload,
                    timeout=(20, 180),
                )

                if response.status_code in (408, 429, 500, 502, 503, 504):
                    last_error = (
                        f"HTTP {response.status_code}: {response.text[:500]}"
                    )
                    if attempt < 3:
                        wait_seconds = 15 * (2 ** (attempt - 1))
                        print(
                            f"{active_model} temporarily unavailable "
                            f"({last_error}); retrying in {wait_seconds}s "
                            f"({attempt}/3)..."
                        )
                        time.sleep(wait_seconds)
                        continue
                    print(
                        f"{active_model} still unavailable after 3 attempts."
                    )
                    response = None
                    break

                # Non-transient errors (e.g. invalid key or bad request)
                # should be shown immediately rather than retried.
                if response.status_code >= 400:
                    raise RuntimeError(
                        f"Gemini API error {response.status_code}: "
                        f"{response.text[:1200]}"
                    )

                print(f"Gemini response received using {active_model}.")
                break

            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError) as exc:
                last_error = str(exc)
                response = None
                if attempt < 3:
                    wait_seconds = 15 * (2 ** (attempt - 1))
                    print(
                        f"{active_model} connection timed out; retrying in "
                        f"{wait_seconds}s ({attempt}/3)..."
                    )
                    time.sleep(wait_seconds)
                else:
                    print(
                        f"{active_model} failed after 3 connection attempts."
                    )

        if response is not None:
            break

        if model_index < len(model_candidates) - 1:
            print(
                f"Switching automatically from {active_model} "
                f"to {model_candidates[model_index + 1]}..."
            )

    if response is None:
        raise RuntimeError(
            "Gemini models are temporarily unavailable after retries. "
            f"Last error: {last_error}. Try the workflow again later."
        )
    data = response.json()
    try:
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
        plan = json.loads(raw)
    except Exception as exc:
        raise RuntimeError(
            f"Could not parse Gemini JSON response: {exc}; "
            f"response={str(data)[:1000]}"
        )

    script = re.sub(r"\s+", " ", plan.get("script", "")).strip()
    scenes = plan.get("scenes", [])
    if not script or len(scenes) < 6:
        raise RuntimeError(
            "Gemini returned an incomplete script/scene plan. Run again."
        )

    plan["script"] = script
    plan["scenes"] = scenes[:8]
    Path("plan.json").write_text(
        json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Script ready: {len(script.split())} words; {len(plan['scenes'])} scenes")
    return plan


def download_stream(url, dest):
    with requests.get(
        url,
        stream=True,
        timeout=(25, 180),
        headers={"User-Agent": "HybridDocumentaryPipeline/1.0"},
    ) as response:
        response.raise_for_status()
        total = 0
        with open(dest, "wb") as file:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    total += len(chunk)
                    if total > 180 * 1024 * 1024:
                        raise RuntimeError("Clip exceeded 180 MB safety limit")
                    file.write(chunk)

    if Path(dest).stat().st_size < 50000:
        raise RuntimeError("Downloaded media file is unexpectedly small")


def pixabay_search(query):
    cache_dir = Path(".api-cache")
    cache_dir.mkdir(exist_ok=True)
    cache_file = cache_dir / (
        hashlib.sha256(query.lower().encode()).hexdigest() + ".json"
    )
    if cache_file.exists() and time.time() - cache_file.stat().st_mtime < 86400:
        try:
            return json.loads(cache_file.read_text(encoding="utf-8")).get("hits", [])
        except Exception:
            pass

    response = requests.get(
        "https://pixabay.com/api/videos/",
        params={
            "key": PIXABAY_KEY,
            "q": query,
            "per_page": 12,
            "safesearch": "true",
            "video_type": "film",
            "order": "popular",
        },
        timeout=35,
    )
    if response.status_code == 429:
        raise RuntimeError("Pixabay rate limit reached; wait before retrying.")
    response.raise_for_status()
    data = response.json()
    cache_file.write_text(json.dumps(data), encoding="utf-8")
    return data.get("hits", [])


def nasa_search(query):
    response = requests.get(
        "https://images-api.nasa.gov/search",
        params={"q": query, "media_type": "video", "page_size": 8},
        timeout=35,
    )
    response.raise_for_status()
    return response.json().get("collection", {}).get("items", [])


def fetch_footage(plan):
    credits = []
    used = set()
    count = 0
    for i, scene in enumerate(plan["scenes"], 1):
        if scene.get("visual_type") == "diagram":
            print(f"Scene {i}: animated explanatory graphic planned.")
            continue
        queries = [str(scene.get("visual_query") or plan["title"])] + [str(q) for q in scene.get("visual_queries", [])[:2]]
        selected_files = []
        # Two distinct shots per scene where the stock library has them; alternate queries improve visual variety.
        for query in queries:
            try:
                hits = pixabay_search(query[:95])
            except Exception as exc:
                print(f"Pixabay search issue for scene {i}: {exc}")
                continue
            for hit in hits:
                media_id = hit.get("id")
                if not media_id or media_id in used:
                    continue
                renditions = hit.get("videos", {})
                rendition = renditions.get("large") or renditions.get("medium") or renditions.get("small") or {}
                candidate = rendition.get("url")
                if not candidate or rendition.get("width", 0) < 640:
                    continue
                destination = FOOT / f"scene_{i:02d}_take_{len(selected_files)+1}.mp4"
                try:
                    download_stream(candidate, destination)
                except Exception as exc:
                    print(f"Scene {i}: clip download failed: {exc}")
                    continue
                used.add(media_id); count += 1; selected_files.append(str(destination))
                credits.append(f"Scene {i} | Pixabay | Creator: {hit.get('user','Unknown')} | Source: {hit.get('pageURL','')} | License: Pixabay Content License")
                if len(selected_files) >= 2:
                    break
            if len(selected_files) >= 2:
                break
        # NASA is a fallback only when Pixabay did not supply a shot.
        if not selected_files:
            for query in queries:
                try:
                    for item in nasa_search(query):
                        meta = item.get("data", [{}])[0]
                        nasa_id = meta.get("nasa_id")
                        if not nasa_id or nasa_id in used:
                            continue
                        asset = requests.get(f"https://images-api.nasa.gov/asset/{nasa_id}", timeout=30)
                        asset.raise_for_status()
                        links = asset.json().get("collection", {}).get("items", [])
                        candidates = [entry.get("href", "") for entry in links if entry.get("href", "").lower().endswith(".mp4")]
                        if candidates:
                            destination = FOOT / f"scene_{i:02d}_take_1.mp4"
                            download_stream(candidates[0], destination)
                            used.add(nasa_id); count += 1; selected_files.append(str(destination))
                            credits.append(f"Scene {i} | NASA | Center: {meta.get('center','NASA')} | Source: https://images.nasa.gov/details/{nasa_id}")
                            break
                except Exception as exc:
                    print(f"NASA fallback issue for scene {i}: {exc}")
                if selected_files:
                    break
        if selected_files:
            scene["media_files"] = selected_files
            scene["media_file"] = selected_files[0]
            print(f"Scene {i}: secured {len(selected_files)} distinct footage shot(s).")
        else:
            print(f"Scene {i}: no relevant footage found; animated explainer fallback will be used.")
    # Keep only source labels/URLs explicitly returned by the planning model; never invent references.
    for i, scene in enumerate(plan["scenes"], 1):
        label, url = str(scene.get("source_label") or "").strip(), str(scene.get("source_url") or "").strip()
        if label and url.startswith("https://"):
            credits.append(f"Explainer source for scene {i} | {label} | {url}")
    Path("credits.txt").write_text("\n".join(credits) if credits else "No external footage/source references were verified; explanatory graphics are illustrative.\n", encoding="utf-8")
    Path("plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    return count


def font(size, bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def make_diagram(scene, idx):
    """Create a short animated editorial graphic, never a static info card."""
    from PIL import Image, ImageDraw
    import math
    width, height, fps, frames = 720, 1280, 24, 72
    kind = str(scene.get("diagram_kind") or "process").lower()
    if kind not in {"process", "map", "comparison", "timeline", "data"}:
        kind = "process"
    title = str(scene.get("diagram_title") or "The hidden connection")[:70]
    body = str(scene.get("diagram_text") or "Follow the chain of cause and effect")[:120]
    steps = [str(x)[:34] for x in (scene.get("diagram_steps") or [])[:4]]
    if len(steps) < 2:
        steps = ["The trigger", "The process", "The result"]
    accent = (79, 205, 216)
    bg = (7, 18, 29)
    frames_dir = VIS / f"diagram_frames_{idx:02d}"
    frames_dir.mkdir(parents=True, exist_ok=True)
    f_title = font(39, True); f_body = font(25); f_small = font(19, True)
    for frame in range(frames):
        t = frame / (frames - 1)
        im = Image.new("RGB", (width, height), bg)
        d = ImageDraw.Draw(im)
        # Subtle moving editorial grid and animated accent sweep.
        for x in range(-80, width + 80, 60):
            xx = int((x + (frame * 1.4)) % (width + 120) - 60)
            d.line((xx, 0, xx, height), fill=(14, 34, 48), width=1)
        d.text((48, 70), f"FIELD EXPLAINER  /  {idx:02d}", font=f_small, fill=accent)
        d.multiline_text((48, 135), textwrap.fill(title, 25), font=f_title, fill=(245,248,250), spacing=9)
        d.line((48, 270, 672, 270), fill=(43,82,101), width=2)
        if kind == "comparison":
            labels = steps[:2]
            for j, label in enumerate(labels):
                x0 = 55 + j*320
                d.rounded_rectangle((x0, 390, x0+280, 800), radius=22, outline=accent if t > j*.22 else (45,75,90), width=3, fill=(13,35,49))
                d.text((x0+22, 430), "A" if j == 0 else "B", font=f_title, fill=accent)
                d.multiline_text((x0+22, 520), textwrap.fill(label, 15), font=f_body, fill=(235,242,246), spacing=8)
                bar_w = int(210 * max(0, min(1, (t-j*.18)/.65)))
                d.rounded_rectangle((x0+22, 730, x0+22+bar_w, 746), radius=8, fill=accent)
        elif kind == "timeline":
            y = 570
            d.line((85, y, 635, y), fill=(55,100,120), width=6)
            for j, label in enumerate(steps[:4]):
                x = 95 + j * (530/max(1,len(steps)-1))
                active = max(0, min(1, (t*len(steps))-j))
                r = 12 + int(8*active)
                d.ellipse((x-r,y-r,x+r,y+r), fill=accent if active > 0 else (45,75,90))
                d.multiline_text((x-58, y+55), textwrap.fill(label, 12), font=f_small, fill=(235,242,246), spacing=6, align="center")
        elif kind == "map":
            # Schematic route/network map; deliberately labelled as illustrative.
            d.rounded_rectangle((65, 360, 655, 880), radius=28, fill=(10,35,47), outline=(38,89,105), width=2)
            path = [(125,760),(235,650),(330,700),(420,530),(565,440)]
            for a,b in zip(path,path[1:]):
                d.line((a[0],a[1],b[0],b[1]), fill=(45,105,120), width=9)
            upto = min(len(path)-1, int(t*(len(path)-1))+1)
            for j,(x,y0) in enumerate(path):
                r=13 if j <= upto else 8
                d.ellipse((x-r,y0-r,x+r,y0+r), fill=accent if j <= upto else (60,92,104))
            d.text((90, 910), "Illustrative pathway — not to scale", font=f_small, fill=(150,180,190))
        elif kind == "data":
            for j, label in enumerate(steps[:4]):
                y=380+j*125
                d.text((65,y), textwrap.fill(label, 30), font=f_body, fill=(235,242,246))
                w=int(500*max(0,min(1,t*1.45-j*.22)))
                d.rounded_rectangle((65,y+54,65+max(4,w),y+68), radius=7, fill=accent)
        else:
            # Sequential cause-and-effect nodes with an animated connecting pulse.
            n=min(4,len(steps)); gap=28; boxw=(600-gap*(n-1))//n; y=470
            for j,label in enumerate(steps[:n]):
                x=60+j*(boxw+gap)
                active=max(0,min(1,(t*n)-j))
                d.rounded_rectangle((x,y,x+boxw,y+250), radius=18, fill=(13,37,51), outline=accent if active>0 else (43,76,91), width=3)
                d.text((x+16,y+22), f"0{j+1}", font=f_small, fill=accent)
                d.multiline_text((x+16,y+85), textwrap.fill(label, 12), font=f_body, fill=(235,242,246), spacing=7)
                if j<n-1:
                    cy=y+125; cx=x+boxw+gap//2
                    d.polygon([(cx-8,cy-12),(cx+8,cy),(cx-8,cy+12)], fill=accent if t*n>j+1 else (60,90,100))
        d.multiline_text((50, 1030), textwrap.fill(body, 42), font=f_body, fill=(195,215,224), spacing=9)
        source = str(scene.get("source_label") or "")
        if source:
            d.text((50, 1195), f"SOURCE: {source[:55]}", font=f_small, fill=(125,165,178))
        d.text((50, 1235), "EXPLANATORY GRAPHIC", font=f_small, fill=(90,125,140))
        im.save(frames_dir / f"frame_{frame:03d}.png")
    return frames_dir


def ass_time(seconds):
    ticks = max(0, int(round(seconds * 100)))
    return (
        f"{ticks // 360000}:"
        f"{(ticks // 6000) % 60:02}:"
        f"{(ticks // 100) % 60:02}."
        f"{ticks % 100:02}"
    )


def build_voice_and_subs(script):
    """Render sentence-sized TTS takes with subtle delivery variation and real pauses."""
    import shutil
    sentence_list = re.findall(r"[^.!?]+[.!?]+|[^.!?]+$", script)
    sentence_list = [x.strip() for x in sentence_list if x.strip()]
    if not sentence_list:
        raise RuntimeError("The script contains no speakable sentences.")
    async def generate():
        all_words = []
        concat_lines = []
        cursor = 0.0
        for index, sentence in enumerate(sentence_list):
            # Small variation only: keep the same recognizable male voice, avoid robotic over-acting.
            rate_steps = ["-3%", "-5%", "-2%", "-4%"]
            pitch_steps = ["-3Hz", "-1Hz", "-4Hz", "-2Hz"]
            rate = rate_steps[index % len(rate_steps)]
            pitch = pitch_steps[index % len(pitch_steps)]
            path = OUT / f"voice_{index:02d}.mp3"
            words = []
            communication = edge_tts.Communicate(
                sentence, VOICE, rate=rate, pitch=pitch, boundary="WordBoundary"
            )
            with open(path, "wb") as audio_file:
                async for message in communication.stream():
                    if message["type"] == "audio":
                        audio_file.write(message["data"])
                    elif message["type"] == "WordBoundary":
                        words.append({
                            "text": message["text"],
                            "offset": message["offset"] / 10_000_000,
                            "duration": message["duration"] / 10_000_000,
                        })
            if not words or not path.exists() or path.stat().st_size < 1000:
                raise RuntimeError(f"TTS failed on sentence {index+1}.")
            # Pause longer after full stops; short breath after commas is already in TTS punctuation.
            seg_duration = media_duration(path)
            for word in words:
                all_words.append({**word, "offset": cursor + word["offset"]})
            concat_lines.append(f"file '{path.resolve()}'")
            cursor += seg_duration
            pause = 0.24 if sentence.endswith(("?", "!")) else 0.18
            silence = OUT / f"pause_{index:02d}.mp3"
            run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", str(pause), "-q:a", "9", str(silence)])
            concat_lines.append(f"file '{silence.resolve()}'")
            cursor += pause
        concat_file = OUT / "voice_concat.txt"
        concat_file.write_text("\n".join(concat_lines), encoding="utf-8")
        run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c:a", "libmp3lame", "-q:a", "2", str(OUT / "voiceover.mp3")])
        if not all_words:
            raise RuntimeError("Edge TTS returned no word timings.")
        groups=[]; current=[]
        for word in all_words:
            current.append(word)
            endmark=bool(re.search(r'[.!?]["\']?$', word["text"]))
            if len(current)>=5 or endmark:
                groups.append(current); current=[]
        if current: groups.append(current)
        lines=["[Script Info]","Title: Cinematic Documentary Captions","ScriptType: v4.00+","WrapStyle: 2","ScaledBorderAndShadow: yes","PlayResX: 1080","PlayResY: 1920","[V4+ Styles]","Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding","Style: Default,DejaVu Sans,52,&H00FFFFFF,&H000000FF,&H00101A25,&H90000000,-1,0,0,0,100,100,0,0,1,4,1,2,75,75,560,1","[Events]","Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
        for group in groups:
            st=group[0]["offset"]; en=max(group[-1]["offset"]+group[-1]["duration"],st+.55)
            phrase=" ".join(x["text"] for x in group)
            phrase=r"\N".join(textwrap.wrap(phrase,width=28,break_long_words=False))
            phrase=phrase.replace("{",r"\{").replace("}",r"\}")
            lines.append(f"Dialogue: 0,{ass_time(st)},{ass_time(en)},Default,,0,0,0,,{phrase}")
        (OUT / "captions.ass").write_text("\n".join(lines),encoding="utf-8")
    asyncio.run(generate())


def media_duration(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def render(plan):
    voice = OUT / "voiceover.mp3"
    duration = media_duration(voice)
    scenes = plan["scenes"]
    segment_duration = duration / len(scenes)
    segment_files = []
    for i, scene in enumerate(scenes, 1):
        media_files = scene.get("media_files") or ([scene.get("media_file")] if scene.get("media_file") else [])
        media_files = [x for x in media_files if x and Path(x).exists()]
        part_duration = segment_duration / max(1, len(media_files)) if media_files else segment_duration
        parts = []
        if scene.get("visual_type") == "diagram" or not media_files:
            frames_dir = make_diagram(scene, i)
            output = OUT / f"segment_{i:02d}.mp4"
            run(["ffmpeg","-y","-framerate","24","-i",str(frames_dir / "frame_%03d.png"),"-t",f"{segment_duration:.3f}","-vf",f"scale={W}:{H}:flags=lanczos,fps={FPS},format=yuv420p","-an","-c:v","libx264","-preset","veryfast","-crf","21",str(output)])
            parts.append(output)
        else:
            for take, source in enumerate(media_files, 1):
                output = OUT / f"segment_{i:02d}_take_{take}.mp4"
                zoom = "min(zoom+0.00055,1.06)" if (i+take) % 2 else "max(1.0,1.06-on*0.000018)"
                run(["ffmpeg","-y","-stream_loop","-1","-i",str(source),"-t",f"{part_duration:.3f}","-vf",f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)+sin(on/75)*10':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS},setsar=1,format=yuv420p","-an","-c:v","libx264","-preset","veryfast","-crf","21",str(output)])
                parts.append(output)
        if i == 1 and parts:
            hook = str(plan.get("hook") or plan.get("title") or "A hidden force is changing this world")
            hook = re.sub(r"[^A-Za-z0-9 ,.'?!-]", "", hook)[:90].replace(":", "")
            hook_file = OUT / "hook.txt"; hook_file.write_text(hook, encoding="utf-8")
            hooked = OUT / "segment_01_hook.mp4"
            run(["ffmpeg","-y","-i",str(parts[0]),"-vf",f"drawbox=x=0:y=180:w=1080:h=390:color=black@0.40:t=fill:enable='between(t,0,3.2)',drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:textfile={hook_file}:fontcolor=white:fontsize=66:line_spacing=12:borderw=2:bordercolor=black@0.6:x=70:y=250:alpha='if(lt(t,0.35),t/0.35,if(gt(t,2.8),(3.2-t)/0.4,1))':enable='between(t,0,3.2)'","-c:v","libx264","-preset","veryfast","-crf","21","-pix_fmt","yuv420p","-an",str(hooked)])
            parts[0] = hooked
        segment_files.extend(parts)
    concat = OUT / "concat.txt"
    concat.write_text("".join(f"file '{path.resolve()}'\n" for path in segment_files), encoding="utf-8")
    base = OUT / "base.mp4"
    run(["ffmpeg","-y","-f","concat","-safe","0","-i",str(concat),"-c","copy",str(base)])
    output = Path("documentary.mp4")
    run(["ffmpeg","-y","-i",str(base),"-i",str(voice),"-filter_complex",("[0:v]subtitles=output/captions.ass[v];""[1:a]highpass=f=65,lowpass=f=15500,acompressor=threshold=-20dB:ratio=2.2:attack=18:release=220:makeup=1.3,alimiter=limit=0.84,loudnorm=I=-16:TP=-1.5:LRA=8,aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a]"),"-map","[v]","-map","[a]","-t",f"{duration:.3f}","-r",str(FPS),"-c:v","libx264","-preset","veryfast","-crf","20","-pix_fmt","yuv420p","-c:a","aac","-b:a","192k","-ar","48000","-ac","2","-movflags","+faststart",str(output)])
    print(f"Final video: {output}; duration {duration:.1f}s")


def main():
    print("1/4 Generating original documentary plan...")
    plan = gemini_plan(TOPIC)
    print("2/4 Finding stock footage...")
    fetch_footage(plan)
    print("3/4 Generating voice-over and word-timed subtitles...")
    build_voice_and_subs(plan["script"])
    print("4/4 Rendering vertical documentary...")
    render(plan)
    print("SUCCESS: documentary.mp4, plan.json, credits.txt")


if __name__ == "__main__":
    main()
