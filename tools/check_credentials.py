#!/usr/bin/env python3
"""Report provider credential wiring as SET / MISSING only.

This tool never prints, hashes, logs or persists a credential value. It exists
because "the key is missing" and "the key exists but never reached the runtime"
look identical to the pipeline: both end in "no provider available". The report
below separates the two by listing the exact environment names that main.py
reads, which alias groups resolved to a value, and - with --probe - whether the
provider itself accepts the credential.

Usage:
    python tools/check_credentials.py                     # presence only
    python tools/check_credentials.py --probe             # presence + live probe
    python tools/check_credentials.py --json-out out.json  # machine-readable

Exit codes:
    0  every required group has at least one credential
    1  a required group has no credential (or --probe found a rejected key)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable

TIMEOUT_SECONDS = 20


def _present(name: str) -> bool:
    return bool(os.getenv(name, "").strip())


def _numbered(base: str) -> list[str]:
    """Return numbered variants such as GROQ_API_KEY_1 that are set."""
    pattern = re.compile(r"^" + re.escape(base) + r"(?:_?(\d+))$", re.I)
    found: list[tuple[int, str]] = []
    for env_name in os.environ:
        match = pattern.match(env_name)
        if match and _present(env_name):
            found.append((int(match.group(1)), env_name))
    return [name for _, name in sorted(found)]


def _group(*bases: str) -> dict:
    """Describe one logical credential group without exposing any value."""
    names: list[str] = []
    for base in bases:
        names.append(base)
        names.extend(_numbered(base))
    seen: list[str] = []
    values = 0
    for name in names:
        if not _present(name):
            continue
        if name not in seen:
            seen.append(name)
        values += 1
    return {
        "bases": list(bases),
        "present_names": seen,
        "credential_count": len({os.getenv(n, "").strip() for n in seen}),
        "set": bool(seen),
    }


def _cloudflare_pairs() -> dict:
    """Mirror main.py's paired token/account discovery."""
    suffixes = [""]
    for number in range(1, 21):
        suffixes.extend((str(number), f"_{number}"))
    pairs = [(f"CLOUDFLARE_API_TOKEN{s}", f"CLOUDFLARE_ACCOUNT_ID{s}") for s in suffixes]
    pairs += [(f"CF_API_TK{n}", f"CF_ID{n}") for n in range(1, 21)]
    pairs += [
        ("CF_API_TOKEN", "CF_ACCOUNT_ID"),
        ("CLOUDFLARE_TOKEN", "CLOUDFLARE_ACCOUNT_ID"),
    ]
    complete: list[str] = []
    partial: list[str] = []
    for token_name, account_name in pairs:
        token, account = _present(token_name), _present(account_name)
        if token and account:
            complete.append(token_name)
        elif token or account:
            partial.append(f"{token_name}+{account_name}")
    return {
        "bases": ["CLOUDFLARE_API_TOKEN+CLOUDFLARE_ACCOUNT_ID", "CF_API_TK1+CF_ID1"],
        "present_names": complete,
        "partial_pairs": partial,
        "credential_count": len(complete),
        "set": bool(complete),
    }


def collect_report() -> dict:
    groups = {
        "groq": _group("GROQ_API_KEY"),
        "openrouter": _group("OPENROUTER_API_KEY"),
        "gemini": _group("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "cloudflare": _cloudflare_pairs(),
        "speechify": _group("SPVO_API", "SPEECHIFY_API_KEY"),
        "pixabay": _group("PIXABAY_API_KEY"),
        "pexels": _group("PEXELS_API_KEY"),
        "coverr": _group("COVERR_API_KEY"),
        "deepseek": _group("DEEPSEEK_API_KEY"),
    }
    text_providers = [name for name in ("groq", "openrouter", "gemini", "cloudflare") if groups[name]["set"]]
    vision_providers = [name for name in ("gemini", "groq", "openrouter", "cloudflare") if groups[name]["set"]]
    return {
        "groups": groups,
        "text_providers_available": text_providers,
        "vision_providers_available": vision_providers,
        "speechify_available": groups["speechify"]["set"],
        "ci": os.getenv("GITHUB_ACTIONS", "").strip().lower() == "true",
        "ref": os.getenv("GITHUB_REF_NAME", "").strip(),
        "repository": os.getenv("GITHUB_REPOSITORY", "").strip(),
    }


def _classify(status: int) -> str:
    """Map a probe status to a verdict.

    Only 401 means the credential itself is wrong. 403 means the credential was
    accepted but that endpoint/plan is not permitted, which is a warning, not a
    proof that the secret is absent or invalid.
    """
    if 200 <= status < 300:
        return "OK"
    if status == 401:
        return "INVALID"
    if status == 403:
        return "FORBIDDEN"
    return "ERROR"


def _status_line(name: str, group: dict) -> str:
    if group["set"]:
        detail = ", ".join(group["present_names"])
        return f"{name.upper():<11} SET      ({group['credential_count']} credential(s): {detail})"
    hint = ""
    if group.get("partial_pairs"):
        hint = " - incomplete pair: " + ", ".join(group["partial_pairs"])
    return f"{name.upper():<11} MISSING  (looked for: {', '.join(group['bases'])}){hint}"


def _request(url: str, headers: dict[str, str]) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={**headers, "User-Agent": "credential-check/1"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, ""
    except urllib.error.HTTPError as error:  # pragma: no cover - network dependent
        return error.code, error.reason or ""
    except (urllib.error.URLError, TimeoutError, OSError) as error:  # pragma: no cover
        return 0, str(getattr(error, "reason", error))[:120]


def probe(group_name: str, limit: int = 3) -> list[dict]:
    """Call a read-only endpoint per credential and report only HTTP status."""
    results: list[dict] = []
    keys: Iterable[str]
    if group_name == "groq":
        for name in ("GROQ_API_KEY", *_numbered("GROQ_API_KEY"))[:limit]:
            if not _present(name):
                continue
            code, note = _request(
                "https://api.groq.com/openai/v1/models",
                {"Authorization": f"Bearer {os.environ[name].strip()}"},
            )
            results.append({"provider": "groq", "env": name, "http_status": code, "note": note})
    elif group_name == "openrouter":
        for name in ("OPENROUTER_API_KEY", *_numbered("OPENROUTER_API_KEY"))[:limit]:
            if not _present(name):
                continue
            code, note = _request(
                "https://openrouter.ai/api/v1/key",
                {"Authorization": f"Bearer {os.environ[name].strip()}"},
            )
            results.append({"provider": "openrouter", "env": name, "http_status": code, "note": note})
    elif group_name == "gemini":
        for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            if not _present(name):
                continue
            code, note = _request(
                "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
                {"x-goog-api-key": os.environ[name].strip()},
            )
            results.append({"provider": "gemini", "env": name, "http_status": code, "note": note})
    elif group_name == "speechify":
        # Mirror exactly what main.py sends, so a probe result means something
        # about the pipeline instead of about a different request shape.
        query = urllib.parse.urlencode(
            {
                "type": "shared",
                "locale": "en-US",
                "gender": "male",
                "model": os.getenv("SPEECHIFY_MODEL", "simba-3.5").strip() or "simba-3.5",
                "limit": 1,
            }
        )
        for name in ("SPVO_API", "SPEECHIFY_API_KEY"):
            if not _present(name):
                continue
            code, note = _request(
                f"https://api.speechify.ai/v1/voices?{query}",
                {"Authorization": f"Bearer {os.environ[name].strip()}"},
            )
            results.append({"provider": "speechify", "env": name, "http_status": code, "note": note})
    elif group_name == "cloudflare":
        for token_name in collect_report()["groups"]["cloudflare"]["present_names"][:limit]:
            code, note = _request(
                "https://api.cloudflare.com/client/v4/user/tokens/verify",
                {"Authorization": f"Bearer {os.environ[token_name].strip()}"},
            )
            results.append({"provider": "cloudflare", "env": token_name, "http_status": code, "note": note})
    elif group_name == "pixabay":
        if _present("PIXABAY_API_KEY"):
            code, note = _request(
                "https://pixabay.com/api/videos/?q=test&per_page=3&key="
                + os.environ["PIXABAY_API_KEY"].strip(),
                {},
            )
            results.append({"provider": "pixabay", "env": "PIXABAY_API_KEY", "http_status": code, "note": note})
    elif group_name == "pexels":
        if _present("PEXELS_API_KEY"):
            code, note = _request(
                "https://api.pexels.com/v1/videos/search?query=test&per_page=1",
                {"Authorization": os.environ["PEXELS_API_KEY"].strip()},
            )
            results.append({"provider": "pexels", "env": "PEXELS_API_KEY", "http_status": code, "note": note})
    elif group_name == "coverr":
        if _present("COVERR_API_KEY"):
            code, note = _request(
                "https://api.coverr.co/videos?query=test&page_size=1&urls=true",
                {"Authorization": f"Bearer {os.environ['COVERR_API_KEY'].strip()}"},
            )
            results.append({"provider": "coverr", "env": "COVERR_API_KEY", "http_status": code, "note": note})
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report credential presence as SET/MISSING only.")
    parser.add_argument("--probe", action="store_true", help="also verify each credential against its provider")
    parser.add_argument("--json-out", default="", help="write the report as JSON (never contains values)")
    parser.add_argument(
        "--require-speechify",
        action="store_true",
        help="treat a missing Speechify credential as a failure",
    )
    args = parser.parse_args(argv)

    report = collect_report()
    groups = report["groups"]

    print("Credential presence (values are never read out):")
    for name, group in groups.items():
        print("  " + _status_line(name, group))

    if report["ci"]:
        print(f"  context: GitHub Actions on {report['repository']}@{report['ref']}")
    else:
        print("  context: local process (not GitHub Actions)")

    failures: list[str] = []
    if not report["text_providers_available"]:
        failures.append("no text provider credential is present")
    if args.require_speechify and not report["speechify_available"]:
        failures.append("Speechify credential is not present")

    if args.probe:
        print("Live credential probe (HTTP status only):")
        probes: list[dict] = []
        for name, group in groups.items():
            if not group["set"] or name == "deepseek":
                continue
            probes.extend(probe(name))
        warnings: list[str] = []
        for item in probes:
            status = item["http_status"]
            verdict = _classify(status)
            note = f" ({item['note']})" if item["note"] else ""
            print(f"  {item['provider']:<11} {item['env']:<26} HTTP {status} {verdict}{note}")
            item["verdict"] = verdict
            if verdict == "FORBIDDEN":
                warnings.append(f"{item['provider']}:{item['env']} reachable but forbidden (plan or scope)")
        invalid = [item["env"] for item in probes if item["verdict"] == "INVALID"]
        if invalid:
            failures.append("credential rejected by provider: " + ", ".join(sorted(set(invalid))))
        for warning in warnings:
            print(f"WARN: {warning}", file=sys.stderr)
        report["warnings"] = warnings
        report["probe_results"] = probes

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
        print(f"Report written to {args.json_out} (names and booleans only).")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print("OK: at least one text provider credential reached this process.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
