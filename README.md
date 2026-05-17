# Manga Downloader

A manga downloader for [atsu.moe](https://atsu.moe) built as a school assignment in vibe coding. Downloads chapters with full JavaScript rendering support, a web-based GUI, and parallel image downloading.

---

## Installation

### Requirements
- **Python 3.11 or newer**
  - Linux: `sudo apt install python3` / `sudo pacman -S python`
  - Windows: download from [python.org/downloads](https://www.python.org/downloads/) — tick **"Add Python to PATH"** during install

---

### Linux / macOS

1. Download or clone this repository
2. Open a terminal in the folder
3. Run the installer:
   ```bash
   chmod +x install.sh
   ./install.sh
   ```
4. When it finishes, start the app by double-clicking **"Start Manga Downloader.sh"**

---

### Windows

1. Download or clone this repository
2. Double-click **`install.bat`**
   *(If Windows blocks it, right-click → "Run anyway")*
3. When it finishes, double-click **"Start Manga Downloader.bat"**
   The browser opens automatically

---

### What the installer does

1. Checks Python 3.11+ is installed and gives a clear message if not
2. Creates a `.venv` virtual environment
3. Installs all Python packages (`playwright`, `flask`, `requests`)
4. Downloads the Chromium browser Playwright needs (~200 MB, one-time)
5. Creates a launcher so you can start the app without a terminal next time

---

## Usage

### Web GUI (recommended)

Start the app with the launcher, then open **http://localhost:7337**.

1. Paste a series URL (`https://atsu.moe/manga/...`)
2. Hit **Fetch** — cover, title, and chapter list load automatically
3. Click **↓ Download** next to any chapter, or **↓ Download All**

### Command line

```bash
# Activate virtual environment first
source .venv/bin/activate        # Linux/macOS
.venv\Scripts\activate.bat       # Windows

python downloader.py "https://atsu.moe/manga/GTyxf"          # all chapters
python downloader.py "https://atsu.moe/manga/GTyxf" --select  # choose chapters
python downloader.py "https://atsu.moe/read/GTyxf/PBvnfXlp"  # single chapter
```

**Chapter selection formats:** `all` · `1-10` · `5-` · `1,3,5` · `1-5,10,15-20`

---

## Output

```
downloads/
  Manga Title/
    001 - Chapter 1/
      001.webp
    002 - Chapter 2/
      ...
```

---

## Files

| File | Purpose |
|---|---|
| `install.sh` | Installer for Linux / macOS |
| `install.bat` | Installer for Windows |
| `app.py` | Flask web server and GUI |
| `downloader.py` | All scraping and download logic |
| `requirements.txt` | Python dependencies |
| `library.json` | Saved manga series (auto-created) |

---

## Troubleshooting

**Windows: "Python was not found"** — Reinstall Python and tick **"Add Python to PATH"**.

**Windows: install.bat blocked** — Right-click → Properties → tick "Unblock" → OK.

**Linux: permission denied** — Run `chmod +x install.sh` first.

---

## Notes

- Built for educational purposes as a school assignment in vibe coding
- Only works with atsu.moe
- Downloaded files are saved locally in `downloads/`
