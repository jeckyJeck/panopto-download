"""
panopto_dl.py - download every stream (camera, screen...) of one Panopto lecture
and combine them into a single phone-friendly video.

Usage:
    python panopto_dl.py "<URL>"                         # one lecture
    python panopto_dl.py "<URL1>" "<URL2>" ...           # several
    python panopto_dl.py -f lectures.txt --drive "G:\\My Drive\\Lectures"
    python panopto_dl.py "<URL>" --list                  # only show the available streams

Already-downloaded lectures are skipped (tracked in <out>\\.downloaded.txt).
Lectures are pipelined: the next one downloads while the current one encodes.

Login is automatic (see panopto_auth.py): a browser window opens only when
the university login has expired.

Files that must sit in the same folder:
    panopto_dl.py, panopto_auth.py, video_combine.py

Requirements (one-time):
    pip install -U yt-dlp playwright tqdm
    ffmpeg + ffprobe on PATH
"""

import argparse
import copy
import queue
import re
import shutil
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

import yt_dlp
from tqdm import tqdm
from yt_dlp.utils import DownloadError

from panopto_auth import get_cookie_file
from video_combine import combine


# ---------------------------------------------------------------- helpers

def safe_filename(name):
    """Strip characters Windows does not allow in file names."""
    return re.sub(r'[<>:"/\\|?*]+', "_", name).strip()[:150] or "lecture"


def pick_streams(info):
    """
    A Panopto session can contain several video streams (camera, screen...).
    yt-dlp lists all their qualities together in info['formats']; group them
    by stream and keep the best quality of each.
    """
    groups = {}
    for f in info.get("formats", []):
        if f.get("vcodec") in (None, "none"):
            continue  # audio-only format
        # format_note holds Panopto's stream tag (e.g. DV = camera, SCREEN...)
        key = f.get("format_note") or "main"
        groups.setdefault(key, []).append(f)

    best = []
    for key, fmts in groups.items():
        # Highest resolution first, then highest bitrate
        fmts.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
        best.append((key, fmts[-1]))
    return best


# ---------------------------------------------------------------- download

class DownloadBar:
    """
    yt-dlp progress hook that drives one tqdm bar per stream.
    yt-dlp calls it repeatedly with a dict: status, downloaded_bytes, and
    total_bytes (or total_bytes_estimate for HLS, which is refined as it goes).
    position = which screen row the bar uses, so it doesn't clash with the
    encoding bar running at the same time.
    """
    def __init__(self, desc, position=0):
        self.bar = tqdm(desc=desc, unit="B", unit_scale=True, unit_divisor=1024,
                        position=position, leave=False)

    def __call__(self, d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                self.bar.total = int(total)
            self.bar.n = d.get("downloaded_bytes", 0)
            self.bar.refresh()
        elif d["status"] == "finished":
            self.bar.total = self.bar.n
            self.bar.refresh()

    def close(self):
        self.bar.close()


class LoginError(Exception):
    pass


def fetch_info(url, cookie_file):
    """Fetch the lecture metadata and list of formats (no download)."""
    try:
        with yt_dlp.YoutubeDL({"cookiefile": str(cookie_file), "quiet": True}) as ydl:
            return ydl.extract_info(url, download=False)
    except DownloadError as e:
        if "registered users" in str(e) or "login" in str(e).lower():
            raise LoginError() from e
        raise


def download_streams(info, cookie_file, workdir, label):
    """Download the best version of every stream. Returns a list of files."""
    streams = pick_streams(info)
    if not streams:
        raise RuntimeError("no video streams found")

    files = []
    for i, (key, f) in enumerate(streams):
        opts = {
            "cookiefile": str(cookie_file),
            "format": f["format_id"],
            "outtmpl": str(workdir / f"stream{i}_{safe_filename(key)}.%(ext)s"),
            "concurrent_fragment_downloads": 4,   # parallel HLS segments = faster
            "quiet": True, "noprogress": True,    # our own tqdm bar instead
        }
        # Panopto's servers sometimes stall on a fragment. Wait longer, retry
        # each fragment many times, and never silently skip one (a skipped
        # fragment = a hole in the video, and in yt-dlp it can also crash).
        opts.update({
            "socket_timeout": 60,
            "retries": 10,
            "fragment_retries": 30,
            "file_access_retries": 10,
            "skip_unavailable_fragments": False,
        })
        for attempt in range(1, 4):
            hook = DownloadBar(f"{label} download {key} ({i + 1}/{len(streams)})", position=0)
            opts["progress_hooks"] = [hook]
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    # process_ie_result reuses the metadata we already have,
                    # so the page isn't fetched again for every stream.
                    # On a retry yt-dlp resumes from the fragments it already has.
                    result = ydl.process_ie_result(copy.deepcopy(info), download=True)
                    files.append(Path(ydl.prepare_filename(result)))
                break
            except (DownloadError, OSError) as e:
                if attempt == 3:
                    raise
                tqdm.write(f"{label} {key}: download interrupted ({e}) - resuming, try {attempt + 1}/3")
            finally:
                hook.close()
    return files


# ---------------------------------------------------------------- one lecture

@dataclass
class Job:
    """A lecture whose streams are downloaded and waiting to be encoded."""
    url: str
    label: str        # e.g. "[2/5]"
    title: str
    files: list
    workdir: Path


def session_id(url):
    """The lecture's unique id (the ?id=... part of the viewer URL)."""
    m = re.search(r"[?&]id=([0-9a-fA-F-]{36})", url)
    return m.group(1).lower() if m else url


def download_lecture(url, label, cookie_file):
    """Download all streams of one lecture into a temp folder."""
    info = fetch_info(url, cookie_file)
    title = info.get("title") or "lecture"
    workdir = Path(tempfile.mkdtemp(prefix="panopto_"))
    try:
        files = download_streams(info, cookie_file, workdir, label)
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    tqdm.write(f"{label} downloaded: {title}")
    return Job(url, label, title, files, workdir)


def encode_lecture(job, out_dir, args):
    """Combine a downloaded lecture and move the result to out_dir."""
    final_path = out_dir / f"{safe_filename(job.title)}.mp4"
    try:
        # Encode locally first, then move: writing straight into a synced
        # Drive folder would make it upload a half-finished file.
        local_out = job.workdir / "combined.mp4"
        combine(job.files, local_out, layout=args.layout, height=args.height,
                quality=args.crf, hw=not args.cpu,
                desc=f"{job.label} encode", position=1)
        shutil.move(str(local_out), final_path)
        if args.drive:
            # Separate copy for Drive: deleting one later doesn't touch the other
            drive_dir = Path(args.drive)
            drive_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(final_path, drive_dir / final_path.name)
    finally:
        if args.keep:
            tqdm.write(f"Separate streams kept in: {job.workdir}")
        else:
            shutil.rmtree(job.workdir, ignore_errors=True)
    tqdm.write(f"{job.label} saved: {final_path}" + (" (+ copied to Drive)" if args.drive else ""))
    return final_path


def read_url_file(path):
    """One URL per line; blank lines and lines starting with # are ignored."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Download and combine Panopto lectures.")
    ap.add_argument("urls", nargs="*", help="One or more Panopto viewer URLs")
    ap.add_argument("-f", "--file", help="Text file with one lecture URL per line")
    ap.add_argument("--out", default=str(Path.home() / "Lectures"),
                    help="Local folder for the finished videos (default: ~/Lectures)")
    ap.add_argument("--drive", help=r'Also copy each video here, e.g. "G:\My Drive\Lectures"')
    ap.add_argument("--layout", choices=["pip", "side"], default="pip",
                    help="pip = camera in the corner (default), side = side by side")
    ap.add_argument("--height", type=int, default=720, help="Output height (default 720)")
    ap.add_argument("--crf", type=int, default=26,
                    help="Quality: lower = better and bigger (default 26)")
    ap.add_argument("--cpu", action="store_true",
                    help="Encode on the CPU instead of Intel Quick Sync")
    ap.add_argument("--cookies", help="Use this cookies.txt instead of logging in automatically")
    ap.add_argument("--list", action="store_true", help="Only list streams/formats of the first URL")
    ap.add_argument("--keep", action="store_true", help="Keep the separate stream files")
    ap.add_argument("--redo", action="store_true", help="Download again even if already done")
    args = ap.parse_args()

    urls = list(args.urls) + (read_url_file(args.file) if args.file else [])
    urls = list(dict.fromkeys(urls))   # drop duplicates, keep order
    if not urls:
        ap.error("give at least one URL, or --file with a list of URLs")

    def login(url):
        return Path(args.cookies) if args.cookies else get_cookie_file(url)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Record of finished lectures (by id), kept next to the videos
    done_file = out_dir / ".downloaded.txt"
    done = set(done_file.read_text().split()) if done_file.exists() else set()

    if not args.list and not args.redo and all(session_id(u) in done for u in urls):
        print("All lectures already downloaded - nothing to do.")
        return

    cookie_file = login(urls[0])

    if args.list:
        info = fetch_info(urls[0], cookie_file)
        with yt_dlp.YoutubeDL({"cookiefile": str(cookie_file)}) as ydl:
            ydl.list_formats(info)
        return

    pending = [u for u in urls if args.redo or session_id(u) not in done]
    status = {u: "skipped (already downloaded)" for u in urls if u not in pending}
    total = len(pending)

    # Pipeline: a background thread downloads lecture N+1 while the main
    # thread encodes lecture N. Drive for Desktop uploads finished files
    # in the background on its own, so all three overlap.
    # maxsize=1 -> at most one downloaded lecture waits, which bounds disk use.
    jobs = queue.Queue(maxsize=1)

    def downloader():
        nonlocal cookie_file
        try:
            for n, url in enumerate(pending, 1):
                label = f"[{n}/{total}]"
                for attempt in (1, 2):
                    try:
                        jobs.put(download_lecture(url, label, cookie_file))
                        break
                    except LoginError:
                        if attempt == 1 and not args.cookies:
                            tqdm.write(f"{label} login expired - refreshing...")
                            cookie_file = login(url)
                            continue
                        status[url] = "FAILED: login refused"
                    except Exception as e:   # one bad lecture shouldn't stop the batch
                        status[url] = f"FAILED (download): {e}"
                        break
        finally:
            jobs.put(None)   # tell the encoder there's nothing more

    threading.Thread(target=downloader, daemon=True).start()

    while (job := jobs.get()) is not None:
        try:
            path = encode_lecture(job, out_dir, args)
            with open(done_file, "a") as f:
                f.write(session_id(job.url) + "\n")
            status[job.url] = f"OK -> {path.name}"
        except Exception as e:
            status[job.url] = f"FAILED (encode): {e}"

    print("\n" + "=" * 60)
    for url in urls:
        print(f"{status.get(url, '?'):45.45s}  {session_id(url)}")
    if any(s.startswith("FAILED") for s in status.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()