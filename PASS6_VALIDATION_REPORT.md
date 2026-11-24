# Pass 6 validation report

## Change

Addressed the failure seen in run `122661536185`: all configured Vision providers became unavailable while Scene 7 had candidates in the source pool, but the previous fallback evaluated only the narrow search-query score. A camera/source-specific query could therefore reject a clip whose metadata was relevant to the actual narration.

Pass 6 now:
- checks up to 8 ranked candidates by default (hard cap remains 8);
- evaluates narration-context metadata when Vision is unavailable;
- accepts only strong metadata matches under explicit thresholds;
- keeps weak candidates rejected;
- records metadata-only status and states that pixel verification was not completed.

## Validation performed locally

- Python compileall: passed.
- Unit tests: 37/37 passed.
- FFmpeg vertical-video fallback tests: passed as part of the suite.
- ZIP integrity and clean extraction: verified after packaging.

## Limitations

- This environment cannot reproduce the exact GitHub Actions run with the user's live API credentials, source-provider rate limits, and Edge TTS endpoint.
- The actual final video has not been rendered or visually reviewed in GitHub Actions.
- Metadata-only fallback is explicitly weaker than AI pixel verification; it is restricted to strong subject/context matches and is reported in the log.
