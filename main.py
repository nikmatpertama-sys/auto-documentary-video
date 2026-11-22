
import asyncio
import edge_tts

async def main():
    text = """
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

    voice = "en-US-AriaNeural"

    communicate = edge_tts.Communicate(
        text,
        voice
    )

    await communicate.save("voiceover.mp3")

    print("Voice-over successfully generated!")

if __name__ == "__main__":
    asyncio.run(main())
