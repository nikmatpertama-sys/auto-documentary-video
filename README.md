Auto Documentary Video

Automated vertical mini-documentary generator using AI-assisted (or local-template fallback) story planning, stock footage, local SigLIP thumbnail ranking, API-free illustrated fallback scenes, Edge TTS narration, Pillow graphics, and FFmpeg rendering.

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

API keys are optional. Add any available keys under:

Repository → Settings → Secrets and variables → Actions

Supported optional secrets:

- "GROQ_API_KEY"
- "DEEPSEEK_API_KEY"
- "GEMINI_API_KEY"
- "OPENROUTER_API_KEY"
- "PIXABAY_API_KEY"
- "PEXELS_API_KEY"
- "COVERR_API_KEY"

Without AI text keys, a cautious local-template script is used. Without suitable footage or when AI Vision services are unavailable, a local illustrated 2D fallback scene is rendered. Footage provider keys improve the chance of finding real footage but are not required for the fallback renderer.

Then open:

Actions → Hybrid Documentary Generator → Run workflow

Enter a documentary topic and run the workflow.

When the workflow completes successfully, download the generated artifact from the workflow run.

Local Setup

Requirements:

- Python 3.11+
- FFmpeg and ffprobe
- FFmpeg subtitles/libass support
- Optional AI API keys (local-template fallback is available if providers are unavailable)

Install dependencies:

python -m pip install -r requirements.txt

Set the API keys as environment variables before running the generator.

Run:

python main.py

Visual Matching

The project uses a local SigLIP-based visual matcher to help rank footage thumbnails. It is not equivalent to full video-frame verification.

SigLIP ranks available footage thumbnails locally. If it cannot load, metadata ranking is used and the report does not claim SigLIP verification.

AI visual verification is attempted when providers are configured. If verification services are unavailable, or no suitable footage can be selected, the pipeline creates an original 2D motion-graphic scene locally with Pillow and FFmpeg. This is an illustrated fallback, not photorealistic 3D.

Rendering Pipeline

- Scene durations are allocated according to narration length.
- Footage is formatted for vertical 9:16 output.
- Visual treatments include restrained camera motion and contrast/saturation adjustments.
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
