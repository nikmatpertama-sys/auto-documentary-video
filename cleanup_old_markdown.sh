#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
removed=0

# Delete only generated upgrade notes and validation reports. Preserve the two
# permanent project documents and every unrelated Markdown file.
while IFS= read -r -d '' file; do
  name="$(basename "$file" | tr '[:upper:]' '[:lower:]')"
  if [[ "$name" == *upgrade*.md ]] || [[ "$name" == *validation*report*.md ]]; then
    rm -f -- "$file"
    printf 'Dihapus: %s\n' "${file#"$ROOT"/}"
    ((removed+=1))
  fi
done < <(find "$ROOT" \
  -type d \( -name .git -o -name .venv -o -name venv -o -name node_modules -o -name __pycache__ \) -prune -o \
  -type f -iname '*.md' -print0)

printf 'Selesai. %d file Markdown generated dihapus. README.md dan SCRIPTWRITER_MASTER_PROMPT.md tetap aman.\n' "$removed"
