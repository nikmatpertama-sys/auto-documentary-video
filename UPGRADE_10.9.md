# Project 1 — Version 10.9 upgrade notes

## Included changes

- `SCRIPTWRITER_MASTER_PROMPT.md` is now the single source of truth for the professional documentary scriptwriter role.
- The prompt enforces a factual-claim self-audit, an 8-scene structure, a 145–165-word narration target, and concrete footage queries.
- The generator reads the prompt file at runtime. It does not add a second LLM/API request.
- Repeated words are removed from generated footage queries.
- If a scene's initial candidate pool is sparse, the generator adds a small set of subject-preserving fallback searches.
- If a later scene still has no unique accepted clip, the generator may re-check an already accepted clip against the new scene. It reuses the clip only when the visual verifier approves it again.
- Reuse is recorded in `plan.json` and `credits.txt`.
- Visual verification remains enabled. Unrelated footage is never silently accepted, and a scene still fails when neither a suitable unique clip nor a re-verified related clip exists.

## Fact-checking boundary

The prompt performs a silent claim-by-claim accuracy review and prohibits invented sources or claims of research that was not performed. This is not an independent web-source verification pass: this release does not add a separate research API or a second fact-check API call.

## Permanent installation on branch `main`

1. Extract this ZIP.
2. Upload the files and folders *inside* `auto-documentary-video-main/` to the repository root on branch `main` (do not upload only the ZIP archive).
3. Commit the changes to `main`.
4. Run the GitHub Actions workflow after the files are present in the branch.

The workflow's bundled-upgrade overlay is temporary for that run only. Uploading only the ZIP as a workflow input does not permanently update branch `main`.
