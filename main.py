
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


def format_srt_time(seconds):
    milliseconds = round(seconds * 1000)

    hours = milliseconds // 3_600_000
    milliseconds %= 3_600_000

    minutes = milliseconds // 60_000
    milliseconds %= 60_000

    secs = milliseconds // 1000
    milliseconds %= 1000

    return (
        f"{hours:02}:"
        f"{minutes:02}:"
        f"{secs:02},"
        f"{milliseconds:03}"
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

    subtitles = []

    for index, group in enumerate(groups, start=1):
        start = group[0]["offset"] / TICKS_PER_SECOND

        last_word = group[-1]
        end = (
            last_word["offset"] + last_word["duration"]
        ) / TICKS_PER_SECOND

        end = max(end, start + 0.6)

        phrase = " ".join(item["text"] for item in group)
        phrase = "\n".join(
            textwrap.wrap(
                phrase,
                width=34,
                break_long_words=False,
                break_on_hyphens=False
            )
        )

        subtitles.append(
            f"{index}\n"
            f"{format_srt_time(start)} --> "
            f"{format_srt_time(end)}\n"
            f"{phrase}\n"
        )

    with open("captions.srt", "w", encoding="utf-8") as file:
        file.write("\n".join(subtitles))


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
    print(f"Subtitle cues generated: {len(words)} word events grouped into captions.")


if __name__ == "__main__":
    asyncio.run(main())
