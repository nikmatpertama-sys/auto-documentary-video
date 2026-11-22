
import os
import requests
import shutil

API_KEY = os.getenv("PIXABAY_API_KEY")
API_URL = "https://pixabay.com/api/videos/"

SEARCH_TERMS = [
    "electricity power station",
    "electrical power lines",
    "solar panels",
    "wind turbines",
    "hydroelectric dam"
]

OUTPUT_DIR = "footage"

if not API_KEY:
    raise RuntimeError("PIXABAY_API_KEY secret is missing!")

if os.path.exists(OUTPUT_DIR):
    shutil.rmtree(OUTPUT_DIR)

os.makedirs(OUTPUT_DIR)

downloaded_ids = set()
download_count = 0

for term in SEARCH_TERMS:
    print(f"\nSearching Pixabay: {term}")

    try:
        response = requests.get(
            API_URL,
            params={
                "key": API_KEY,
                "q": term,
                "per_page": 5,
                "safesearch": "true"
            },
            timeout=30
        )
        response.raise_for_status()

        videos = response.json().get("hits", [])
        selected = None

        for video in videos:
            if video["id"] in downloaded_ids:
                continue

            renditions = video.get("videos", {})
            video_url = (
                renditions.get("medium", {}).get("url")
                or renditions.get("small", {}).get("url")
                or renditions.get("tiny", {}).get("url")
            )

            if video_url:
                selected = video
                break

        if not selected:
            print("No suitable video found.")
            continue

        video_url = (
            selected["videos"].get("medium", {}).get("url")
            or selected["videos"].get("small", {}).get("url")
            or selected["videos"].get("tiny", {}).get("url")
        )

        filename = f"clip_{download_count + 1}.mp4"
        filepath = os.path.join(OUTPUT_DIR, filename)

        print(f"Downloading {filename}...")

        with requests.get(
            video_url,
            stream=True,
            timeout=(30, 180)
        ) as download:
            download.raise_for_status()

            with open(filepath, "wb") as file:
                for chunk in download.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        file.write(chunk)

        with open(filepath, "rb") as file:
            if file.read(8)[:2] != b"\x00\x00":
                pass

        credit = (
            f"Creator: {selected.get('user', 'Unknown')}\n"
            f"Tags: {selected.get('tags', '')}\n"
            f"Source: {selected.get('pageURL', '')}\n"
            f"License: Pixabay Content License\n"
        )

        with open(
            os.path.join(OUTPUT_DIR, f"clip_{download_count + 1}_credit.txt"),
            "w",
            encoding="utf-8"
        ) as file:
            file.write(credit)

        downloaded_ids.add(selected["id"])
        download_count += 1

        print(f"Downloaded: {filename}")

    except Exception as error:
        print(f"Failed for '{term}': {error}")

print(f"\nTotal videos downloaded: {download_count}")

if download_count == 0:
    raise RuntimeError("No footage downloaded from Pixabay!")
