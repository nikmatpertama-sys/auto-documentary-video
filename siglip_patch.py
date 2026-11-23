
"""Deprecated compatibility shim for older workflow files.

SigLIP now runs directly through visual_matcher.py. This module intentionally
never edits main.py or any project source at runtime. The repaired workflow does
not call it; retaining this harmless shim lets an old explicit reference fail
safely instead of reintroducing source mutation.
"""


def main() -> None:
    print(
        "siglip_patch.py is deprecated and disabled. "
        "SigLIP is integrated directly in visual_matcher.py; "
        "no source files were modified."
    )


if __name__ == "__main__":
    main()
