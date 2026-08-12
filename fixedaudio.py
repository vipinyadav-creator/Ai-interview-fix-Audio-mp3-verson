"""
Google Drive audio converter + Google Sheets sync:
Source folder se audio MP3 me convert karo, destination folder me upload karo,
aur Spreadsheet ke Interviews sheet me column J me MP3 link likho.

FIX NOTES:
- Pehle har row ke liye alag write API call hoti thi (skip ke case me bhi,
  aur phir end me saari links ka dobara full resync) -> 60 writes/min quota
  turant exceed ho jaati thi (429 RATE_LIMIT_EXCEEDED).
- Ab sab pending row-updates ek hi batchUpdate() call me jaate hain (chahe
  100+ rows ho, Sheets API isse ek hi write request count karta hai).
- synced.json cache rakha hai taaki jo row already sahi value ke saath
  likhi ja chuki hai use dobara na likha jaaye.
- Har Google API call ko exponential-backoff retry wrapper me wrap kiya
  hai taaki koi bhi 429 / temporary 5xx error crash na kare.
"""

import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

# --- Config ---
SOURCE_FOLDER_ID = "1nOJjVKbuSeaCzYvzD3BYJ2RhCoJd_1ch"
DEST_FOLDER_ID = "1p4fZ5y7mDCRIJcSefywPCMH2CSuzLSbc"

SPREADSHEET_ID = "1cl2wmwgBABsrpQWAkHP5suMZicgQFXepoDmhykFQuqE"
SHEET_NAME = "Interviews"
SOURCE_COL = "I"   # original audio link
MP3_COL = "J"      # converted mp3 link

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]
SCRIPT_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = SCRIPT_DIR / "credentials.json"
TOKEN_FILE = SCRIPT_DIR / "token.json"
PROCESSED_FILE = SCRIPT_DIR / "processed.json"
MP3_LINKS_FILE = SCRIPT_DIR / "mp3_links.json"
SYNCED_FILE = SCRIPT_DIR / "synced.json"   # row -> already sheet me likha hua mp3_id

AUDIO_MIME_PREFIXES = ("audio/",)
AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac", ".wma",
    ".webm", ".opus", ".amr", ".3gp", ".mp4", ".mkv",
}

MAX_RETRIES = 6
BASE_DELAY = 2  # seconds, exponential backoff ka base
BATCH_CHUNK_SIZE = 300  # ek batchUpdate call me itni ranges (safe limit)


def is_ci() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("CI") == "true"


def write_secret_files_from_env() -> None:
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    token_json = os.environ.get("GOOGLE_TOKEN_JSON")
    if creds_json and not CREDENTIALS_FILE.exists():
        CREDENTIALS_FILE.write_text(creds_json, encoding="utf-8")
    if token_json and not TOKEN_FILE.exists():
        TOKEN_FILE.write_text(token_json, encoding="utf-8")


def get_credentials() -> Credentials:
    write_secret_files_from_env()

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if is_ci():
                print("CI me token invalid hai. GitHub Secrets me GOOGLE_TOKEN_JSON update karo.")
                sys.exit(1)
            if not CREDENTIALS_FILE.exists():
                print("credentials.json nahi mili. Google Cloud Console se OAuth credentials download karo.")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")

    return creds


def get_drive_service(creds: Credentials):
    return build("drive", "v3", credentials=creds)


def get_sheets_service(creds: Credentials):
    return build("sheets", "v4", credentials=creds)


# --- Local state helpers ---

def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def load_processed() -> set[str]:
    return set(load_json(PROCESSED_FILE, []))


def save_processed(processed: set[str]) -> None:
    PROCESSED_FILE.write_text(json.dumps(sorted(processed), indent=2), encoding="utf-8")


def load_mp3_links() -> dict[str, str]:
    return load_json(MP3_LINKS_FILE, {})


def save_mp3_link(source_id: str, mp3_id: str) -> None:
    links = load_mp3_links()
    links[source_id] = mp3_id
    MP3_LINKS_FILE.write_text(json.dumps(links, indent=2), encoding="utf-8")


def load_synced() -> dict[str, str]:
    """row (string key) -> mp3_id jo already sheet me likha ja chuka hai."""
    return load_json(SYNCED_FILE, {})


def save_synced(synced: dict[str, str]) -> None:
    SYNCED_FILE.write_text(json.dumps(synced, indent=2), encoding="utf-8")


def drive_file_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


def extract_drive_file_id(value: str) -> str | None:
    if not value or not str(value).strip():
        return None

    text = str(value).strip()

    if text.startswith("=HYPERLINK"):
        match = re.search(r'https?://[^",\s]+', text, re.IGNORECASE)
        if match:
            text = match.group(0)

    patterns = [
        r"/file/d/([a-zA-Z0-9_-]+)",
        r"[?&]id=([a-zA-Z0-9_-]+)",
        r"/folders/([a-zA-Z0-9_-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


# --- Retry wrapper ---

def with_retry(request_factory, description: str = "API call"):
    """
    Google API request ko exponential backoff + jitter ke saath retry karta hai.
    429 (rate limit) aur 5xx (server) errors par retry karega; baaki errors turant raise honge.
    request_factory: 0-arg function jo naya request object return kare (retry par fresh call banta hai).
    """
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            request = request_factory()
            return request.execute()
        except HttpError as exc:
            last_exc = exc
            status = exc.resp.status if exc.resp is not None else None
            retryable = status == 429 or (status is not None and 500 <= status < 600)
            if not retryable or attempt == MAX_RETRIES:
                raise
            delay = BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 1)
            print(f"  {description}: HTTP {status}, retry {attempt}/{MAX_RETRIES} in {delay:.1f}s...")
            time.sleep(delay)
    raise last_exc  # pragma: no cover


# --- Sheets helpers ---

def read_sheet_source_rows(sheets_service) -> dict[str, int]:
    """Column I se source file id -> row number (1-based) map."""
    range_name = f"{SHEET_NAME}!{SOURCE_COL}:{SOURCE_COL}"
    result = with_retry(
        lambda: sheets_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=range_name,
            valueRenderOption="FORMULA",
        ),
        description="Read column I",
    )

    source_to_row: dict[str, int] = {}
    values = result.get("values", [])
    for idx, row in enumerate(values):
        if not row:
            continue
        file_id = extract_drive_file_id(row[0])
        if file_id:
            source_to_row[file_id] = idx + 1
    return source_to_row


def batch_update_sheet(sheets_service, updates: dict[int, str]) -> int:
    """
    Multiple rows ek hi batchUpdate API call me update karta hai, taaki
    'Write requests per minute per user' quota par pressure na pade.
    updates: {row_number: mp3_file_id}
    """
    if not updates:
        return 0

    data = [
        {
            "range": f"{SHEET_NAME}!{MP3_COL}{row}",
            "values": [[drive_file_url(mp3_id)]],
        }
        for row, mp3_id in updates.items()
    ]

    total_updated = 0
    for i in range(0, len(data), BATCH_CHUNK_SIZE):
        chunk = data[i:i + BATCH_CHUNK_SIZE]
        body = {"valueInputOption": "USER_ENTERED", "data": chunk}
        with_retry(
            lambda: sheets_service.spreadsheets().values().batchUpdate(
                spreadsheetId=SPREADSHEET_ID,
                body=body,
            ),
            description=f"Batch sheet update ({len(chunk)} rows)",
        )
        total_updated += len(chunk)
        print(f"  Sheet batch updated: {len(chunk)} row(s) (col {MP3_COL})")
    return total_updated


# --- Drive helpers ---

def is_audio_file(file_meta: dict) -> bool:
    mime = file_meta.get("mimeType", "")
    name = file_meta.get("name", "").lower()

    if mime.startswith(AUDIO_MIME_PREFIXES):
        return True
    if mime.startswith("video/") and any(name.endswith(ext) for ext in AUDIO_EXTENSIONS):
        return True
    return any(name.endswith(ext) for ext in AUDIO_EXTENSIONS)


def list_audio_files(service, folder_id: str) -> list[dict]:
    files = []
    page_token = None
    query = f"'{folder_id}' in parents and trashed = false"

    while True:
        response = with_retry(
            lambda: service.files().list(
                q=query,
                spaces="drive",
                fields="nextPageToken, files(id, name, mimeType)",
                pageToken=page_token,
                pageSize=100,
            ),
            description="List Drive files",
        )
        for item in response.get("files", []):
            if is_audio_file(item):
                files.append(item)
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return files


def download_file(service, file_id: str, dest_path: Path) -> None:
    request = service.files().get_media(fileId=file_id)
    with open(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def convert_to_mp3(input_path: Path, output_path: Path) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(input_path),
        "-vn",
        "-acodec", "libmp3lame",
        "-q:a", "2",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg error:\n{result.stderr}")


def upload_mp3(service, local_path: Path, dest_folder_id: str, filename: str) -> str:
    if not filename.lower().endswith(".mp3"):
        filename = Path(filename).stem + ".mp3"

    metadata = {
        "name": filename,
        "parents": [dest_folder_id],
    }
    media = MediaFileUpload(str(local_path), mimetype="audio/mpeg", resumable=True)
    uploaded = with_retry(
        lambda: service.files().create(body=metadata, media_body=media, fields="id"),
        description=f"Upload {filename}",
    )
    return uploaded["id"]


def mp3_name(original_name: str) -> str:
    return Path(original_name).stem + ".mp3"


def process_file(
    drive_service,
    file_meta: dict,
    processed: set[str],
) -> str | None:
    """
    File convert + upload karta hai. Return: naya mp3_id (agar convert hua), warna None.
    NOTE: Yaha se ab DIRECTLY sheet update nahi hota — sab pending sheet updates
    main() me collect hoke ek single batch call me jaate hain.
    """
    file_id = file_meta["id"]
    file_name = file_meta["name"]

    if file_id in processed:
        print(f"Skip (already done): {file_name}")
        return None

    print(f"Processing: {file_name}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        input_path = tmp_dir / file_name
        output_path = tmp_dir / mp3_name(file_name)

        download_file(drive_service, file_id, input_path)

        if file_name.lower().endswith(".mp3"):
            output_path = input_path
        else:
            convert_to_mp3(input_path, output_path)

        mp3_id = upload_mp3(drive_service, output_path, DEST_FOLDER_ID, mp3_name(file_name))

    save_mp3_link(file_id, mp3_id)
    processed.add(file_id)
    save_processed(processed)
    print(f"Done: {mp3_name(file_name)}")
    return mp3_id


def backfill_mp3_links_from_dest(drive_service, audio_files: list[dict]) -> int:
    """Destination folder ki MP3 files se source id -> mp3 id map banao."""
    dest_files = list_audio_files(drive_service, DEST_FOLDER_ID)
    mp3_by_stem = {
        Path(f["name"]).stem.lower(): f["id"]
        for f in dest_files
        if f["name"].lower().endswith(".mp3")
    }

    links = load_mp3_links()
    added = 0
    for src in audio_files:
        stem = Path(src["name"]).stem.lower()
        mp3_id = mp3_by_stem.get(stem)
        if mp3_id and src["id"] not in links:
            links[src["id"]] = mp3_id
            added += 1

    if added:
        MP3_LINKS_FILE.write_text(json.dumps(links, indent=2), encoding="utf-8")
    return added


def check_ffmpeg() -> None:
    try:
        subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("ffmpeg install karo aur PATH me add karo.")
        print("Download: https://ffmpeg.org/download.html")
        sys.exit(1)


def print_sheets_setup_help(exc: HttpError | None = None) -> None:
    print("\nGoogle Sheets access nahi mil raha. Ye steps follow karo:")
    print("1. Google Cloud Console me Google Sheets API enable karo:")
    print("   https://console.cloud.google.com/apis/api/sheets.googleapis.com/overview?project=220553136075")
    print("2. 2-5 minute wait karo, phir workflow dubara chalao.")
    print("3. token.json me spreadsheets scope honi chahiye (local par naya login).")
    print("4. Spreadsheet usi Google account se share ho jisse login kiya hai.")
    if exc is not None:
        print(f"\nError detail: {exc}")


def init_sheet_sync(sheets_service) -> tuple[dict[str, int], bool]:
    try:
        with_retry(
            lambda: sheets_service.spreadsheets().get(
                spreadsheetId=SPREADSHEET_ID,
                fields="properties.title",
            ),
            description="Check spreadsheet access",
        )
    except HttpError as exc:
        print_sheets_setup_help(exc)
        print("\nSheet sync skip ho raha hai. MP3 convert/upload phir bhi chalega.")
        return {}, False

    print("Spreadsheet se column I links padh rahe hain...")
    source_to_row = read_sheet_source_rows(sheets_service)
    print(f"Sheet me {len(source_to_row)} source link(s) mili.")
    return source_to_row, True


def main() -> None:
    check_ffmpeg()
    creds = get_credentials()
    drive_service = get_drive_service(creds)
    sheets_service = get_sheets_service(creds)
    processed = load_processed()
    synced = load_synced()

    source_to_row, sheets_ok = init_sheet_sync(sheets_service)

    print("Source folder se audio files dhoondh rahe hain...")
    audio_files = list_audio_files(drive_service, SOURCE_FOLDER_ID)

    if not audio_files:
        print("Koi audio file nahi mili.")
    else:
        backfilled = backfill_mp3_links_from_dest(drive_service, audio_files)
        if backfilled:
            print(f"Pehle ki {backfilled} file(s) ke MP3 links map ho gaye.")
        print(f"Total audio files: {len(audio_files)}")
        print(f"Pehle se processed: {len(processed)}")

        converted = 0
        for file_meta in audio_files:
            try:
                mp3_id = process_file(drive_service, file_meta, processed)
                if mp3_id:
                    converted += 1
            except Exception as exc:
                print(f"Error ({file_meta['name']}): {exc}")

        print(f"\nIs run me {converted} nayi file(s) convert/upload hui.")

    # Sab source files ke liye check karo: sheet me row already sahi value
    # ke saath sync hai ya nahi. Sirf jo naya/change hua hai wahi likhenge,
    # aur wo bhi ek hi batchUpdate call me (quota-safe).
    if sheets_ok:
        mp3_links = load_mp3_links()
        pending_updates: dict[int, str] = {}

        for source_id, mp3_id in mp3_links.items():
            row = source_to_row.get(source_id)
            if not row:
                continue
            if synced.get(str(row)) == mp3_id:
                continue  # already sahi value likhi hui hai, skip
            pending_updates[row] = mp3_id

        if pending_updates:
            print(f"\nSheet sync: {len(pending_updates)} row(s) update karni hain (batch me)...")
            batch_update_sheet(sheets_service, pending_updates)
            for row, mp3_id in pending_updates.items():
                synced[str(row)] = mp3_id
            save_synced(synced)
        else:
            print("\nSheet already up-to-date hai, kuch likhne ki zaroorat nahi.")
    else:
        print("\nSheet sync skip (Sheets API enable / access fix karo).")

    print("Complete.")


if __name__ == "__main__":
    main()
