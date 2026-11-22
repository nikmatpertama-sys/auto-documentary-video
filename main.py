
import asyncio
import edge_tts
import re
import textwrap

TEXT = """
Imagine a world where electricity suddenly disappears.
Within seconds, lights go dark and machines fall silent.
But the real consequences would be far greater than darkness.

Hospitals would struggle to operate life-saving equipment.
Traffic systems would stop working, creating chaos on busy roads.
Communication networks could fail, disconnecting millions of people.

Electricity powers nearly every part of modern civilization.
It keeps food refrigerated, delivers clean water, and supports industries
that produce the things we depend on every day.

Yet electricity is not something we simply find waiting in nature.
It must be generated, transmitted, and carefully distributed.
Power plants convert energy from sources like sunlight, wind,
moving water, and fossil fuels into electrical energy.

That energy travels through enormous networks of power lines
before reaching homes, schools, and businesses.

So the next time you switch on a light,
remember that behind that simple action
is one of humanity's most important inventions.
"""

VOICE = "en-US-AriaNeural"
TICKS_PER_SECOND = 10_000_000


def ass_time(seconds):
    total = round(seconds * 100)
    hours = total // 360000
    minutes = (total % 360000) // 6000
    secs = (total % 6000) // 100
    centiseconds = total % 100

    return (
        f"{hours}:{minutes:02}:"
        f"{secs:02}.{centiseconds:02}"
    )


def create_subtitles(words):
    groups = []
    current = []

    for word in words:
        current.append(word)

        sentence_end = bool(
            re.search(r'[.!?]["\']?$', word["text"])
        )

        if len(current) >= 6 or sentence_end:
            groups.append(current)
            current = []

    if current:
        groups.append(current)

    with open("captions.ass", "w", encoding="utf-8") as file:
        file.write("""[Script Info]
Title: Documentary Captions
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,48,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,2,80,80,700,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""")

        for group in groups:
            start = group[0]["offset"] / TICKS_PER_SECOND

            last = group[-1]
            end = (
                last["offset"] + last["duration"]
            ) / TICKS_PER_SECOND

            end = max(end, start + 0.6)

            phrase = " ".join(
                item["text"] for item in group
            )

            phrase = "\\N".join(
                textwrap.wrap(
                    phrase,
                    width=30,
                    break_long_words=False,
                    break_on_hyphens=False
                )
            )

            phrase = phrase.replace("{", "\\{").replace("}", "\\}")

            file.write(
                f"Dialogue: 0,{ass_time(start)},"
                f"{ass_time(end)},Default,,0,0,0,,"
                f"{phrase}\n"
            )


async def main():
    communicate = edge_tts.Communicate(
        TEXT,
        VOICE,
        boundary="WordBoundary"
    )

    words = []

    with open("voiceover.mp3", "wb") as audio:
        async for message in communicate.stream():

            if message["type"] == "audio":
                audio.write(message["data"])

            elif message["type"] == "WordBoundary":
                words.append({
                    "offset": message["offset"],
                    "duration": message["duration"],
                    "text": message["text"]
                })

    if not words:
        raise RuntimeError(
            "No word timing data received from Edge TTS!"
        )

    create_subtitles(words)

    print("Voice-over successfully generated!")
    print(f"Subtitle groups created: {len(words)} word events processed.")


if __name__ == "__main__":
    asyncio.run(main())
