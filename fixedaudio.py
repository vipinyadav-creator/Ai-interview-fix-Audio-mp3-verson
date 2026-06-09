"""
Google Drive audio converter + Google Sheets sync:
Source folder se audio MP3 me convert karo, destination folder me upload karo,
aur Spreadsheet ke Interviews sheet me column J me MP3 link likho.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
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

AUDIO_MIME_PREFIXES = ("audio/",)
AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac", ".wma",
    ".webm", ".opus", ".amr", ".3gp", ".mp4", ".mkv",
}


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


def load_processed() -> set[str]:
    if PROCESSED_FILE.exists():
        return set(json.loads(PROCESSED_FILE.read_text(encoding="utf-8")))
    return set()


def save_processed(processed: set[str]) -> None:
    PROCESSED_FILE.write_text(
        json.dumps(sorted(processed), indent=2),
        encoding="utf-8",
    )


def load_mp3_links() -> dict[str, str]:
    if MP3_LINKS_FILE.exists():
        return json.loads(MP3_LINKS_FILE.read_text(encoding="utf-8"))
    return {}


def save_mp3_link(source_id: str, mp3_id: str) -> None:
    links = load_mp3_links()
    links[source_id] = mp3_id
    MP3_LINKS_FILE.write_text(json.dumps(links, indent=2), encoding="utf-8")


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


def read_sheet_source_rows(sheets_service) -> dict[str, int]:
    """Column I se source file id -> row number (1-based) map."""
    range_name = f"{SHEET_NAME}!{SOURCE_COL}:{SOURCE_COL}"
    result = (
        sheets_service.spreadsheets()
        .values()
        .get(
            spreadsheetId=SPREADSHEET_ID,
            range=range_name,
            valueRenderOption="FORMULA",
        )
        .execute()
    )

    source_to_row: dict[str, int] = {}
    values = result.get("values", [])
    for idx, row in enumerate(values):
        if not row:
            continue
        cell = row[0]
        file_id = extract_drive_file_id(cell)
        if file_id:
            source_to_row[file_id] = idx + 1
    return source_to_row


def update_sheet_mp3_link(sheets_service, row: int, mp3_file_id: str) -> None:
    mp3_url = drive_file_url(mp3_file_id)
    range_name = f"{SHEET_NAME}!{MP3_COL}{row}"
    (
        sheets_service.spreadsheets()
        .values()
        .update(
            spreadsheetId=SPREADSHEET_ID,
            range=range_name,
            valueInputOption="USER_ENTERED",
            body={"values": [[mp3_url]]},
        )
        .execute()
    )
    print(f"  Sheet updated: row {row}, col {MP3_COL}")


def sync_sheet_for_source(sheets_service, source_to_row: dict[str, int], source_id: str, mp3_id: str) -> None:
    row = source_to_row.get(source_id)
    if row:
        update_sheet_mp3_link(sheets_service, row, mp3_id)
    else:
        print(f"  Sheet me row nahi mili (col {SOURCE_COL}): {source_id}")


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
        response = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                fields="nextPageToken, files(id, name, mimeType)",
                pageToken=page_token,
                pageSize=100,
            )
            .execute()
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
    uploaded = (
        service.files()
        .create(body=metadata, media_body=media, fields="id")
        .execute()
    )
    return uploaded["id"]


def mp3_name(original_name: str) -> str:
    return Path(original_name).stem + ".mp3"


def process_file(
    drive_service,
    sheets_service,
    source_to_row: dict[str, int],
    file_meta: dict,
    processed: set[str],
) -> bool:
    file_id = file_meta["id"]
    file_name = file_meta["name"]

    if file_id in processed:
        mp3_links = load_mp3_links()
        if file_id in mp3_links and file_id in source_to_row:
            sync_sheet_for_source(sheets_service, source_to_row, file_id, mp3_links[file_id])
        print(f"Skip (already done): {file_name}")
        return False

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
    sync_sheet_for_source(sheets_service, source_to_row, file_id, mp3_id)

    processed.add(file_id)
    save_processed(processed)
    print(f"Done: {mp3_name(file_name)}")
    return True


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


def sync_existing_to_sheet(sheets_service, source_to_row: dict[str, int]) -> int:
    """Pehle se convert ho chuki files ke liye sheet col J update karo."""
    mp3_links = load_mp3_links()
    updated = 0
    for source_id, mp3_id in mp3_links.items():
        row = source_to_row.get(source_id)
        if row:
            update_sheet_mp3_link(sheets_service, row, mp3_id)
            updated += 1
    return updated


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
        sheets_service.spreadsheets().get(
            spreadsheetId=SPREADSHEET_ID,
            fields="properties.title",
        ).execute()
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
                if process_file(drive_service, sheets_service, source_to_row, file_meta, processed):
                    converted += 1
            except Exception as exc:
                print(f"Error ({file_meta['name']}): {exc}")

        print(f"\nIs run me {converted} nayi file(s) convert/upload hui.")

    if sheets_ok:
        print("\nSheet sync: pehle se convert files ke MP3 links col J me likh rahe hain...")
        synced = sync_existing_to_sheet(sheets_service, source_to_row)
        print(f"Sheet me {synced} row(s) update hui.")
    else:
        print("\nSheet sync skip (Sheets API enable / access fix karo).")

    print("Complete.")


if __name__ == "__main__":
    main()
