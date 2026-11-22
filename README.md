Auto Documentary Video
Automated vertical mini-documentary generator using Gemini for the story plan, Pixabay with NASA fallback for footage, Edge TTS for narration, Pillow for original explainer cards, and FFmpeg for rendering.
Output
documentary.mp4 — 1080 × 1920, 30 FPS, H.264/AAC
plan.json — generated script, scene plan, source metadata, and render timing
credits.txt — footage source/creator attribution
output/ — downloaded media, voice-over, captions, scene cards, and intermediate renders
GitHub Actions setup
Add repository Actions secrets GEMINI_API_KEY and PIXABAY_API_KEY.
Open Actions → Hybrid Documentary Generator → Run workflow.
Enter a documentary topic and run it.
Download the hybrid-documentary-* artifact when the workflow completes.
Local setup
Requires Python 3.11+, FFmpeg (including ffprobe and the subtitles/libass filter), and the two API keys.
python -m pip install -r requirements.txt
export GEMINI_API_KEY="..."
export PIXABAY_API_KEY="..."
export INPUT_TOPIC="Why do fireflies glow?"
python main.py
Editing pipeline
Scene durations are allocated according to narration length. Each shot is formatted for vertical 9:16, receives restrained motion, contrast/saturation treatment and short fades. Diagram scenes use original Pillow-generated cards. Captions are generated from Edge TTS word-boundary timings. A quiet, original tonal ambience bed is synthesized during rendering so the project does not depend on an unlicensed music download.
Notes
Review factual claims and stock-footage relevance before publishing. Automated topic generation is not a substitute for editorial fact-checking.
Pixabay/NASA asset availability varies by topic. credits.txt records the selected source where available.
The synthetic ambience is intentionally subtle and is not a substitute for a properly licensed music track or bespoke sound design.
