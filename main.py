import os, re, json, math, asyncio, random, subprocess, textwrap, time, hashlib
from pathlib import Path
import requests
from PIL import Image, ImageDraw, ImageFont
import edge_tts

ROOT = Path('.')
OUT = Path('output')
FOOT = OUT / 'footage'
VIS = OUT / 'visuals'
OUT.mkdir(exist_ok=True); FOOT.mkdir(exist_ok=True); VIS.mkdir(exist_ok=True)
GEMINI_KEY = os.getenv('GEMINI_API_KEY', '').strip()
PIXABAY_KEY = os.getenv('PIXABAY_API_KEY', '').strip()
TOPIC = os.getenv('INPUT_TOPIC', '').strip()
VOICE = os.getenv('TTS_VOICE', 'en-US-AriaNeural')
MODEL = os.getenv('GEMINI_MODEL', 'gemini-3.7-flash')
TARGET_SECONDS = 70
W, H, FPS = 1080, 1920, 30

if not GEMINI_KEY: raise RuntimeError('Missing GEMINI_API_KEY GitHub Secret.')
if not PIXABAY_KEY: raise RuntimeError('Missing PIXABAY_API_KEY GitHub Secret.')
if not TOPIC: raise RuntimeError('Enter a topic in the Run workflow form.')


def run(cmd):
    subprocess.run(cmd, check=True)


def gemini_plan(topic):
    prompt = f'''Create a fact-conscious, original English mini-documentary plan for Facebook Reels.
TOPIC: {topic}
Target voiceover duration: about {TARGET_SECONDS} seconds (roughly 145-165 spoken words).
Return ONLY valid JSON with this exact structure:
{{"title":"...","script":"...","scenes":[{{"narration":"exact sentence(s) from script","visual_query":"specific real-world stock-video search phrase, 3-7 words","diagram_title":"short title","diagram_text":"short explanatory text, max 18 words","visual_type":"footage or diagram"}}]}}
Requirements: 8 scenes; script is one coherent narration with a 3-second curiosity hook, accurate cautious wording, no fabricated statistics, no unsupported precise numbers, no call to action, natural documentary English. Each scene narration must appear verbatim in script and scenes must cover the script in order. Use diagram for 2 or 3 scenes where a process, comparison, timeline, or mechanism is better explained visually; other scenes use real footage. Avoid asking for AI-generated footage. Do not invent citations or claim you browsed sources. Keep claims conservative and distinguish established facts from hypotheses.'''
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent'
    r = requests.post(url, params={'key': GEMINI_KEY}, json={
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {'responseMimeType': 'application/json', 'temperature': 0.55}
    }, timeout=90)
    if r.status_code >= 400:
        raise RuntimeError(f'Gemini API error {r.status_code}: {r.text[:1200]}')
    data = r.json()
    try:
        raw = data['candidates'][0]['content']['parts'][0]['text']
        plan = json.loads(raw)
    except Exception as e:
        raise RuntimeError(f'Could not parse Gemini JSON response: {e}; response={str(data)[:1000]}')
    script = re.sub(r'\s+', ' ', plan.get('script', '')).strip()
    scenes = plan.get('scenes', [])
    if not script or len(scenes) < 6: raise RuntimeError('Gemini returned an incomplete script/scene plan. Run again.')
    plan['script'] = script
    plan['scenes'] = scenes[:8]
    Path('plan.json').write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f"Script ready: {len(script.split())} words; {len(plan['scenes'])} scenes")
    return plan


def safe_name(s): return re.sub(r'[^a-zA-Z0-9_-]+', '_', s)[:40]


def download_stream(url, dest):
    with requests.get(url, stream=True, timeout=(25, 180), headers={'User-Agent':'HybridDocumentaryPipeline/1.0'}) as r:
        r.raise_for_status()
        total = 0
        with open(dest, 'wb') as f:
            for chunk in r.iter_content(1024*1024):
                if chunk:
                    total += len(chunk)
                    if total > 180*1024*1024: raise RuntimeError('Clip exceeded 180 MB safety limit')
                    f.write(chunk)
    if Path(dest).stat().st_size < 50000: raise RuntimeError('Downloaded media file is unexpectedly small')


def pixabay_search(query):
    # Persist search responses for 24 hours; the workflow caches .api-cache between runs.
    cache_dir = Path('.api-cache'); cache_dir.mkdir(exist_ok=True)
    cache_file = cache_dir / (hashlib.sha256(query.lower().encode()).hexdigest() + '.json')
    if cache_file.exists() and time.time() - cache_file.stat().st_mtime < 86400:
        try: return json.loads(cache_file.read_text(encoding='utf-8')).get('hits', [])
        except Exception: pass
    url = 'https://pixabay.com/api/videos/'
    r = requests.get(url, params={'key':PIXABAY_KEY,'q':query,'per_page':12,'safesearch':'true','video_type':'film','order':'popular'}, timeout=35)
    if r.status_code == 429: raise RuntimeError('Pixabay rate limit reached; wait before retrying.')
    r.raise_for_status()
    data = r.json()
    cache_file.write_text(json.dumps(data), encoding='utf-8')
    return data.get('hits', [])


def nasa_search(query):
    r = requests.get('https://images-api.nasa.gov/search', params={'q':query,'media_type':'video','page_size':8}, timeout=35)
    r.raise_for_status()
    return r.json().get('collection',{}).get('items',[])


def fetch_footage(plan):
    credits=[]; used=set(); count=0
    for i, scene in enumerate(plan['scenes'], 1):
        if scene.get('visual_type') == 'diagram':
            print(f'Scene {i}: planned original diagram; no stock clip needed.')
            continue
        q = str(scene.get('visual_query') or plan['title'])[:95]
        chosen=None; provider=''; source=''; creator=''
        try:
            for hit in pixabay_search(q):
                if hit.get('id') in used: continue
                rend=hit.get('videos',{})
                candidate=(rend.get('large') or {}).get('url') or (rend.get('medium') or {}).get('url') or (rend.get('small') or {}).get('url')
                width=(rend.get('large') or rend.get('medium') or rend.get('small') or {}).get('width',0)
                if candidate and width >= 640:
                    chosen=candidate; provider='Pixabay'; source=hit.get('pageURL',''); creator=hit.get('user','Unknown'); used.add(hit.get('id')); break
        except Exception as e: print(f'Pixabay search issue for scene {i}: {e}')
        if not chosen:
            try:
                for item in nasa_search(q):
                    meta=item.get('data',[{}])[0]
                    nasa_id=meta.get('nasa_id')
                    if not nasa_id or nasa_id in used: continue
                    asset=requests.get(f'https://images-api.nasa.gov/asset/{nasa_id}',timeout=30)
                    asset.raise_for_status()
                    links=asset.json().get('collection',{}).get('items',[])
                    candidates=[x.get('href','') for x in links if x.get('href','').lower().endswith('.mp4')]
                    if candidates:
                        chosen=candidates[0]; provider='NASA'; source=f'https://images.nasa.gov/details/{nasa_id}'; creator=meta.get('center','NASA'); used.add(nasa_id); break
            except Exception as e: print(f'NASA fallback issue for scene {i}: {e}')
        if chosen:
            dest=FOOT/f'scene_{i:02d}.mp4'
            try:
                download_stream(chosen,dest); count+=1
                credits.append(f"Scene {i} | {provider} | Creator/Center: {creator} | Source: {source}")
                scene['media_file']=str(dest); scene['media_source']=provider
                print(f'Scene {i}: downloaded from {provider}')
            except Exception as e:
                print(f'Scene {i}: download failed: {e}')
        else:
            print(f'Scene {i}: no suitable footage found; will use an original scene graphic.')
    Path('credits.txt').write_text('\n'.join(credits) if credits else 'No external footage downloaded; all scenes use generated graphics.\n',encoding='utf-8')
    Path('plan.json').write_text(json.dumps(plan,indent=2,ensure_ascii=False),encoding='utf-8')
    return count


def font(size, bold=False):
    candidates=['/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', '/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf']
    for p in candidates:
        if Path(p).exists(): return ImageFont.truetype(p,size)
    return ImageFont.load_default()


def make_diagram(scene, idx):
    im=Image.new('RGB',(W,H),(8,17,31)); d=ImageDraw.Draw(im)
    # restrained editorial infographic, original from scene text (not fabricated data)
    d.rounded_rectangle((70,220,1010,1690),radius=42,fill=(14,31,49),outline=(55,100,130),width=3)
    d.text((120,310),f'FIELD NOTES  /  {idx:02d}',font=font(30,True),fill=(92,204,219))
    title=str(scene.get('diagram_title') or scene.get('visual_query') or 'How it works')
    lines=textwrap.wrap(title,26)
    y=500
    for line in lines[:3]:
        d.text((120,y),line,font=font(66,True),fill=(245,248,250)); y+=88
    d.rounded_rectangle((120,820,960,824),radius=2,fill=(74,170,190))
    body=str(scene.get('diagram_text') or scene.get('narration') or '')
    y=920
    for line in textwrap.wrap(body,34)[:6]:
        d.text((125,y),line,font=font(43),fill=(200,218,229)); y+=72
    d.text((120,1510),'THE BIGGER PICTURE',font=font(28,True),fill=(92,204,219))
    p=VIS/f'diagram_{idx:02d}.png'; im.save(p); return p


def ass_time(s):
    t=max(0,int(round(s*100))); return f'{t//360000}:{(t//6000)%60:02}:{(t//100)%60:02}.{t%100:02}'


def build_voice_and_subs(script):
    async def go():
        words=[]
        comm=edge_tts.Communicate(script,VOICE,boundary='WordBoundary')
        with open(OUT/'voiceover.mp3','wb') as f:
            async for m in comm.stream():
                if m['type']=='audio': f.write(m['data'])
                elif m['type']=='WordBoundary': words.append({'text':m['text'],'offset':m['offset']/10_000_000,'duration':m['duration']/10_000_000})
        if not words: raise RuntimeError('Edge TTS returned no word timings.')
        groups=[]; cur=[]
        for w in words:
            cur.append(w)
            endmark=bool(re.search(r'[.!?][\"\']?$',w['text']))
            if len(cur)>=5 or endmark:
                groups.append(cur); cur=[]
        if cur: groups.append(cur)
        lines=['[Script Info]','Title: Original Documentary Captions','ScriptType: v4.00+','WrapStyle: 2','ScaledBorderAndShadow: yes','PlayResX: 1080','PlayResY: 1920','[V4+ Styles]','Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding','Style: Default,DejaVu Sans, fifty, &H00FFFFFF,&H000000FF,&H00101A25,&H90000000,-1,0,0,0,100,100,0,0,1,4,1,2,75,75,560,1'.replace('fifty','52'),'[Events]','Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text']
        for g in groups:
            start=g[0]['offset']; end=max(g[-1]['offset']+g[-1]['duration'],start+0.55)
            phrase=' '.join(x['text'] for x in g)
            phrase='\\N'.join(textwrap.wrap(phrase,width=28,break_long_words=False))
            phrase=phrase.replace('{','\\{').replace('}','\\}')
            lines.append(f'Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{phrase}')
        Path(OUT/'captions.ass').write_text('\n'.join(lines),encoding='utf-8')
    asyncio.run(go())


def media_duration(path):
    p=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(path)],capture_output=True,text=True,check=True)
    return float(p.stdout.strip())


def render(plan):
    voice=OUT/'voiceover.mp3'; duration=media_duration(voice)
    scenes=plan['scenes']; seg=duration/len(scenes)
    # build individual visual segments to keep FFmpeg graph simple and robust
    segment_files=[]
    for i,scene in enumerate(scenes,1):
        src=scene.get('media_file')
        if scene.get('visual_type')=='diagram' or not src or not Path(src).exists():
            src=make_diagram(scene,i)
            out=OUT/f'segment_{i:02d}.mp4'
            run(['ffmpeg','-y','-loop','1','-i',str(src),'-t',f'{seg:.3f}','-vf',f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},zoompan=z='min(zoom+0.0005,1.04)':d=1:s={W}x{H}:fps={FPS},format=yuv420p",'-an','-c:v','libx264','-preset','veryfast','-crf','24',str(out)])
        else:
            out=OUT/f'segment_{i:02d}.mp4'
            run(['ffmpeg','-y','-stream_loop','-1','-i',str(src),'-t',f'{seg:.3f}','-vf',f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1,format=yuv420p",'-an','-c:v','libx264','-preset','veryfast','-crf','24',str(out)])
        segment_files.append(out)
    concat=OUT/'concat.txt'
    concat.write_text(''.join(f"file '{p.resolve()}'\n" for p in segment_files),encoding='utf-8')
    base=OUT/'base.mp4'
    run(['ffmpeg','-y','-f','concat','-safe','0','-i',str(concat),'-c','copy',str(base)])
    # Original low-volume tonal ambience synthesized locally; no third-party music asset.
    out=Path('documentary.mp4')
    subtitle_filter=f"subtitles={str((OUT/'captions.ass').resolve()).replace(':','\\:')}"
    run(['ffmpeg','-y','-i',str(base),'-i',str(voice),'-f','lavfi','-i',f"aevalsrc=0.018*sin(2*PI*110*t)+0.012*sin(2*PI*164.81*t)+0.008*sin(2*PI*220*t):s=48000:d={duration:.3f}",
         '-filter_complex',f"[0:v]{subtitle_filter}[v];[2:a]lowpass=f=420,volume=0.65,afade=t=in:st=0:d=2,afade=t=out:st={max(0,duration-3):.2f}:d=3[bed];[1:a]volume=1.0[vo];[vo][bed]amix=inputs=2:duration=first:dropout_transition=2[a]",
         '-map','[v]','-map','[a]','-t',f'{duration:.3f}','-r',str(FPS),'-c:v','libx264','-preset','veryfast','-crf','21','-pix_fmt','yuv420p','-c:a','aac','-b:a','192k','-movflags','+faststart',str(out)])
    print(f'Final video: {out}; duration {duration:.1f}s')


def main():
    print('1/4 Generating original documentary plan...')
    plan=gemini_plan(TOPIC)
    print('2/4 Finding licensed footage...')
    fetch_footage(plan)
    print('3/4 Generating voice-over and word-timed subtitles...')
    build_voice_and_subs(plan['script'])
    print('4/4 Rendering vertical documentary...')
    render(plan)
    print('SUCCESS: documentary.mp4, plan.json, credits.txt')

if __name__=='__main__': main()
