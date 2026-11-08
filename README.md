# panopto_download

Download Panopto lectures (e.g. HUJI) and combine every stream (screen + camera) into a single phone-friendly MP4.

## Features

- **Automatic login** – uses a dedicated Playwright browser profile. A window opens only when your university SSO has expired; otherwise it runs silently.
- **All streams** – downloads the best quality of every stream (camera, screen, ...) with yt-dlp.
- **Combine** – merges streams into one video, either picture-in-picture (`pip`) or side by side (`side`).
- **Hardware encoding** – Intel Quick Sync by default, with automatic CPU (libx264) fallback via `--cpu`.
- **Pipelined** – the next lecture downloads while the current one encodes.
- **Resumable** – finished lectures are recorded in `<out>/.downloaded.txt` and skipped next time.
- **Optional Drive copy** – copy each finished video to a synced folder such as Google Drive.

## Files

| File | Purpose |
| --- | --- |
| `panopto_dl.py` | Main script / CLI |
| `panopto_auth.py` | Browser login, writes `cookies.txt` for yt-dlp |
| `video_combine.py` | ffmpeg stream combining and encoder selection |
| `lectures.txt` | Example list of lecture URLs |
| `requirements` | Pinned Python dependencies |

All three `.py` files must stay in the same folder.

## Requirements

- Python 3.10+
- `ffmpeg` and `ffprobe` on your `PATH`
- Microsoft Edge (ships with Windows). If unavailable: `python -m playwright install chromium`

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements
```

## Usage

```
# One lecture
python panopto_dl.py "<URL>"

# Several lectures
python panopto_dl.py "<URL1>" "<URL2>"

# From a file, also copying to Google Drive
python panopto_dl.py -f lectures.txt --drive "G:\My Drive\Lectures"

# Only list the available streams
python panopto_dl.py "<URL>" --list
```

### Lecture list file

One URL per line. Blank lines and lines starting with `#` are ignored, so you can use them as course headings:

```
# graphics
https://huji.cloud.panopto.eu/Panopto/Pages/Viewer.aspx?id=...&instance=HUJI26
```

### Options

| Option | Description |
| --- | --- |
| `-f, --file` | Text file with one URL per line |
| `--out DIR` | Output folder (default `~/Lectures`) |
| `--drive DIR` | Also copy each video to this folder |
| `--layout {pip,side}` | Camera in the corner (default) or side by side |
| `--height N` | Output height, never upscaled (default 720) |
| `--crf N` | Quality; lower = better and bigger (default 26) |
| `--cpu` | Encode on the CPU instead of Intel Quick Sync |
| `--cookies FILE` | Use an existing `cookies.txt` instead of automatic login |
| `--list` | List streams of the first URL and exit |
| `--keep` | Keep the separate stream files |
| `--redo` | Download again even if already done |

## Login only

To log in or refresh cookies without downloading:

```
python panopto_auth.py "<lecture URL>"
```

The browser profile and cookies are stored in `%LOCALAPPDATA%\panopto_dl\` (outside the repo).

## Notes

- The encoder is hard-coded to Intel Quick Sync in `video_combine.py` (`ENCODER = HW_ENCODERS[1]`). Change the index for NVIDIA (`0`) or AMD (`2`), or use `--cpu`.
- If a lecture has more than two streams, only the main (largest) stream and the camera are combined.
- Downloads are for personal use; respect your institution's terms.
