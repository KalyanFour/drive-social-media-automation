
import os
import json
import random
import re
import time
import tempfile
from pathlib import Path

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]
IG_TOKEN = os.environ["INSTAGRAM_ACCESS_TOKEN"]
IG_ACCOUNT_ID = os.environ["INSTAGRAM_ACCOUNT_ID"]
HISTORY_FILE = Path("history.json")

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/drive.readonly",
]

def make_services():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return (
        build("drive", "v3", credentials=creds),
        build("youtube", "v3", credentials=creds),
    )

def load_history():
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    return {}

def save_history(history):
    HISTORY_FILE.write_text(
        json.dumps(history, indent=2), encoding="utf-8"
    )

def get_videos(drive):
    result = drive.files().list(
        q=f"'{FOLDER_ID}' in parents and trashed = false",
        fields="files(id,name,mimeType,createdTime,size)",
        orderBy="createdTime",
        pageSize=1000,
    ).execute()
    return sorted(
        [f for f in result.get("files", [])
         if f.get("mimeType", "").startswith("video/")],
        key=lambda f: f.get("createdTime", "")
    )

def download_video(drive, file_id, destination):
    request = drive.files().get_media(fileId=file_id)
    with open(destination, "wb") as output:
        downloader = MediaIoBaseDownload(output, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

def hashtags(filename, platform):
    text = re.sub(r"\.[^.]+$", "", filename).lower()
    words = re.findall(r"[a-z0-9]+", text)
    topic = []
    for word in words:
        tag = "#" + word
        if len(tag) > 2 and tag not in topic:
            topic.append(tag)
    topic = topic[:3]
    defaults = ["#shorts", "#viralvideos", "#trending"]
    platform_tags = (
        ["#instagramreels", "#reels"]
        if platform == "instagram"
        else ["#youtubeshorts", "#shorts"]
    )
    tags = topic + platform_tags
    for tag in defaults:
        if len(tags) >= 5:
            break
        if tag not in tags:
            tags.append(tag)
    while len(tags) < 5:
        tags.append("#video" if "#video" not in tags else "#entertainment")
    return " ".join(tags[:5])

def caption_for(filename, platform):
    base = re.sub(r"\.[^.]+$", "", filename).strip()
    return f"{base}\n\n{hashtags(filename, platform)}"

def upload_youtube(youtube, path, filename):
    body = {
        "snippet": {
            "title": re.sub(r"\.[^.]+$", "", filename)[:100],
            "description": caption_for(filename, "youtube"),
            "categoryId": "24",
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=MediaFileUpload(
            path, chunksize=-1, resumable=True
        ),
    )
    response = None
    while response is None:
        _, response = request.next_chunk()
    print("YouTube upload complete:", response["id"])

def publish_instagram(file_id, filename):
    # Instagram must be able to fetch this URL directly.
    video_url = (
        "https://drive.google.com/uc"
        f"?export=download&id={file_id}"
    )
    base_url = f"https://graph.facebook.com/v23.0/{IG_ACCOUNT_ID}"
    response = requests.post(
        f"{base_url}/media",
        data={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption_for(filename, "instagram"),
            "share_to_feed": "true",
            "access_token": IG_TOKEN,
        },
        timeout=60,
    )
    response.raise_for_status()
    container_id = response.json()["id"]

    for _ in range(30):
        status = requests.get(
            f"https://graph.facebook.com/v23.0/{container_id}",
            params={
                "fields": "status_code",
                "access_token": IG_TOKEN,
            },
            timeout=30,
        )
        status.raise_for_status()
        state = status.json().get("status_code")
        if state == "FINISHED":
            break
        if state == "ERROR":
            raise RuntimeError("Instagram reported media processing ERROR")
        time.sleep(10)
    else:
        raise TimeoutError("Instagram media processing timed out")

    result = requests.post(
        f"{base_url}/media_publish",
        data={
            "creation_id": container_id,
            "access_token": IG_TOKEN,
        },
        timeout=60,
    )
    result.raise_for_status()
    print("Instagram Reel published:", result.json().get("id"))

def main():
    drive, youtube = make_services()
    history = load_history()
    videos = get_videos(drive)
    print(f"Found {len(videos)} videos in Drive.")

    for video in videos:
        file_id = video["id"]
        filename = video["name"]
        record = history.setdefault(
            file_id, {"name": filename, "youtube": False, "instagram": False}
        )

        if record["youtube"] and record["instagram"]:
            continue

        print("Processing:", filename)
        suffix = Path(filename).suffix or ".mp4"

        with tempfile.NamedTemporaryFile(
            suffix=suffix, delete=False
        ) as temp:
            temp_path = temp.name

        try:
            download_video(drive, file_id, temp_path)

            if not record["youtube"]:
                upload_youtube(youtube, temp_path, filename)
                record["youtube"] = True
                save_history(history)

            if not record["instagram"]:
                publish_instagram(file_id, filename)
                record["instagram"] = True
                save_history(history)

            print("Finished:", filename)
        finally:
            Path(temp_path).unlink(missing_ok=True)

        # Only one video per scheduled run.
        if record["youtube"] and record["instagram"]:
            return

    print("No unpublished videos remain.")

if __name__ == "__main__":
    main()
