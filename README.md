# AI Interview - Auto Audio to MP3

Google Drive se audio files automatically MP3 me convert karta hai aur Google Sheet ke column J me link update karta hai.

## GitHub Actions Setup (24/7 auto run)

### 1. Repo me code push karo

```bash
git add .
git commit -m "Add auto MP3 converter"
git remote add origin https://github.com/vipinyadav-creator/Ai-interview-fix-Audio-mp3-verson.git
git push -u origin main
```

### 2. GitHub Secrets add karo

Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Secret name | Value |
|-------------|-------|
| `GOOGLE_CREDENTIALS_JSON` | Poora `credentials.json` file ka content copy-paste |
| `GOOGLE_TOKEN_JSON` | Poora `token.json` file ka content copy-paste |

### 3. Google Cloud APIs enable karo (zaroori)

Project: `fix-audio-498510`

1. **Google Drive API** – enable karo  
2. **Google Sheets API** – enable karo (ye step miss hone par workflow fail hota hai):  
   https://console.cloud.google.com/apis/api/sheets.googleapis.com/overview?project=220553136075  
   → **Enable** dabao → 2-5 minute wait karo

`token.json` me ye scopes honi chahiye:
- `https://www.googleapis.com/auth/drive`
- `https://www.googleapis.com/auth/spreadsheets`

### 4. Actions enable karo

Repo → **Actions** tab → workflow enable karo.

Workflow **har 5 minute** automatically chalega. Nayi file aate hi convert + sheet update ho jayega.

Manual run: Actions → **Auto MP3 Converter** → **Run workflow**

## Local run

```bash
pip install -r requirements.txt
python fixedaudio.py
```

`ffmpeg` install hona chahiye.
