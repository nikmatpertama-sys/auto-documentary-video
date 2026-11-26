"""Legacy compatibility shim.

Active, scene-specific footage discovery is implemented in main.py.
This former hard-coded Pixabay demo is intentionally disabled: the project
keeps Pixabay/Pexels off unless the operator explicitly opts in through the
main pipeline's environment flags.
"""

if __name__ == "__main__":
    print(
        "Legacy footage.py is inactive. Run main.py for scene-aware footage "
        "discovery; Pixabay/Pexels remain disabled by default."
    )
