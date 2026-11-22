
import asyncio
import edge_tts

async def main():
    text = (
        "What if electricity suddenly disappeared? "
        "The world as we know it would change forever."
    )

    voice = "en-US-AriaNeural"

    communicate = edge_tts.Communicate(
        text,
        voice
    )

    await communicate.save("voiceover.mp3")

    print("Voice-over successfully generated!")

if __name__ == "__main__":
    asyncio.run(main())
