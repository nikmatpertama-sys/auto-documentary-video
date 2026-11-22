
import requests
import os
import time

os.makedirs("footage", exist_ok=True)

API = "https://commons.wikimedia.org/w/api.php"

SEARCH_TERMS = [
    "hospital",
    "traffic",
    "power station",
    "solar panels",
    "wind turbine",
    "hydroelectric dam",
    "power lines",
    "electricity",
    "light bulb"
]

HEADERS = {
    "User-Agent": "DocumentaryVideoGenerator/1.0"
}

downloaded = 0
seen = set()
MAX_VIDEOS = 10

for term in SEARCH_TERMS:
    if downloaded >= MAX_VIDEOS:
        break

    print("Searching:", term)

    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": f"{term} filetype:video",
        "gsrnamespace": 6,
        "gsrlimit": 10,
        "prop": "imageinfo",
        "iiprop": "url|extmetadata|mime",
        "format": "json"
    }

    try:
        response = requests.get(
            API, params=params, headers=HEADERS, timeout=30
        )
        response.raise_for_status()

        pages = response.json().get(
            "query", {}
        ).get("pages", {})

        print("Search results:", len(pages))

    except requests.exceptions.RequestException as error:
        print("Search error:", error)
        continue

    for page in pages.values():
        if downloaded >= MAX_VIDEOS:
            break

        title = page["title"]

        if title in seen:
            continue

        seen.add(title)

        info = page.get("imageinfo", [{}])[0]
        url = info.get("url", "")
        mime = info.get("mime", "")

        if not url or not mime.startswith("video/"):
            continue

        ext = os.path.splitext(url.split("?")[0])[1].lower()

        if ext not in [".mp4", ".webm", ".ogv", ".ogg"]:
            continue

        number = downloaded + 1
        filename = f"footage/clip_{number}{ext}"

        try:
            print("Downloading:", title)

            with requests.get(
                url, headers=HEADERS,
                stream=True, timeout=60
            ) as video:

                if video.status_code == 429:
                    print("Rate limited. Skipping.")
                    continue

                video.raise_for_status()

                with open(filename, "wb") as file:
                    for chunk in video.iter_content(1024 * 1024):
                        if chunk:
                            file.write(chunk)

            metadata = info.get("extmetadata", {})

            with open(
                f"footage/credit_{number}.txt", "w"
            ) as credit:
                credit.write("Title: " + title + "\n")
                credit.write(
                    "Artist: " +
                    metadata.get("Artist", {}).get("value", "Unknown") + "\n"
                )
                credit.write(
                    "License: " +
                    metadata.get("LicenseShortName", {}).get("value", "Check source") + "\n"
                )
                credit.write(
                    "Source: " + info.get("descriptionurl", "") + "\n"
                )

            downloaded += 1
            print("Downloaded:", downloaded)

        except requests.exceptions.RequestException as error:
            print("Download error:", error)
            if os.path.exists(filename):
                os.remove(filename)
            continue

        time.sleep(2)

print("Footage search completed!")
print("Total videos downloaded:", downloaded)

if downloaded == 0:
    raise RuntimeError("No video footage could be downloaded!")
