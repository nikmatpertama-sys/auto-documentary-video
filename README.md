Auto Documentary Video

Automated vertical mini-documentary generator using AI-powered story planning, stock footage, visual verification, Edge TTS narration, Pillow-generated explainer cards, and FFmpeg rendering.

Output

- "documentary.mp4" — 1080 × 1920, 30 FPS, H.264/AAC.
- "plan.json" — generated script, scene plan, source metadata, and render timing.
- "credits.txt" — footage source and creator attribution.
- "output/" — downloaded media, voice-over, captions, scene cards, and intermediate renders.

AI Providers

The project supports multiple AI providers with fallback routing:

- Groq
- DeepSeek
- Google Gemini
- OpenRouter

Provider availability depends on valid API keys, model availability, quotas, and service status.

GitHub Actions Setup

Add the required API keys under:

Repository → Settings → Secrets and variables → Actions

Supported secrets:

- "GROQ_API_KEY"
- "DEEPSEEK_API_KEY"
- "GEMINI_API_KEY"
- "OPENROUTER_API_KEY"
- "PIXABAY_API_KEY"
- "PEXELS_API_KEY" (optional)

Then open:

Actions → Hybrid Documentary Generator → Run workflow

Enter a documentary topic and run the workflow.

When the workflow completes successfully, download the generated artifact from the workflow run.

Local Setup

Requirements:

- Python 3.11+
- FFmpeg and ffprobe
- FFmpeg subtitles/libass support
- Required API keys

Install dependencies:

python -m pip install -r requirements.txt

Set the API keys as environment variables before running the generator.

Run:

python main.py

Visual Matching

The project uses a local SigLIP-based visual matcher to help evaluate footage relevance.

When local visual matching is unavailable, the system must report the fallback accurately rather than claiming that SigLIP verification succeeded.

AI visual verification uses the configured provider routing and fallback logic.

Rendering Pipeline

- Scene durations are allocated according to narration length.
- Footage is formatted for vertical 9:16 output.
- Visual treatments include restrained motion, contrast/saturation adjustments, and short fades.
- Diagram scenes use original Pillow-generated cards.
- Captions use Edge TTS word-boundary timings.
- FFmpeg handles final video rendering and encoding.

Important Notes

- Review factual claims and footage relevance before publishing.
- Automated topic generation is not a substitute for editorial fact-checking.
- Pixabay and Pexels asset availability varies by topic.
- NASA footage availability varies by topic and source.
- "credits.txt" records selected sources where available.
- API quotas and provider availability may affect generation.
- Always verify the generated MP4 before publishing.
