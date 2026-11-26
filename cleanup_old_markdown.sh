#!/usr/bin/env bash
set -euo pipefail

# Hapus changelog upgrade dan laporan validasi lama.
# README.md dan SCRIPTWRITER_MASTER_PROMPT.md tetap dipertahankan.
find . -type f \( -name 'UPGRADE_*.md' -o -name 'PASS*_VALIDATION_REPORT.md' \) -delete

echo "Selesai. README.md dan SCRIPTWRITER_MASTER_PROMPT.md tetap aman."
