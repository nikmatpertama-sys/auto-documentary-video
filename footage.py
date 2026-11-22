
import requests
import os
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

TOPIC = "electricity"
os.makedirs("footage", exist_ok=True)

API = "https://commons.wikimedia.org/w/api.php"

HEADERS = {
    "User-Agent": "DocumentaryVideoGenerator/1.0 (https://github.com/nikmatpertama-sys/auto-documentary-video)"
}

params = {
    "action": "query",
    "generator": "search",
    "gsrsearch": f"{TOPIC} filetype:video",
    "gsrnamespace": 6,
    "gsrlimit": 5,
    "prop": "imageinfo",
    "iiprop": "url|extmetadata",
    "format": "json"
}

response = requests.get(
    API, params=params, headers=HEADERS, timeout=30
)
response.raise_for_status()

pages = response.json().get("query", {}).get("pages", {})

downloaded = 0

for index, page in enumerate(pages.values(), 1):
    info = page.get("imageinfo", [{}])[0]
    url = info.get("url", "")

    if not url:
        continue

    ext = os.path.splitext(url.split("?")[0])[1].lower()

    if ext not in [".mp4", ".webm", ".ogv", ".ogg"]:
        continue

    filename = f"footage/clip_{index}{ext}"

    print("Downloading:", page["title"])

    try:
        with requests.get(
            url, headers=HEADERS, stream=True, timeout=60
        ) as video:

            if video.status_code == 429:
                print("Rate limited. Skipping this clip.")

                retry = video.headers.get("Retry-After")
                if retry:
                    try:
                        wait = int(retry)
                    except ValueError:
                        try:
                            target = parsedate_to_datetime(retry)
                            wait = max(
                                0,
                                int((target - datetime.now(timezone.utc)).total_seconds())
                            )
                        except Exception:
                            wait = 0

                    print("Server requested wait:", wait, "seconds")

                continue

            video.raise_for_status()

            with open(filename, "wb") as file:
                for chunk in video.iter_content(1024 * 1024):
                    if chunk:
                        file.write(chunk)

        metadata = info.get("extmetadata", {})

        with open(f"footage/credit_{index}.txt", "w") as credit:
            credit.write("Title: " + page["title"] + "\n")
            credit.write("Artist: " + metadata.get("Artist", {}).get("value", "Unknown") + "\n")
            credit.write("License: " + metadata.get("LicenseShortName", {}).get("value", "Check source") + "\n")
            credit.write("Source: " + info.get("descriptionurl", "") + "\n")

        downloaded += 1

    except requests.exceptions.RequestException as error:
        print("Could not download clip:", error)
        continue

print("Footage download finished.")
print("Successfully downloaded:", downloaded)
