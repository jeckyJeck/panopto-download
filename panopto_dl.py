"""
panopto_dl.py - download every stream (camera, screen...) of one Panopto lecture
and combine them into a single phone-friendly video.

Usage:
    python panopto_dl.py "<URL>"                         # one lecture
    python panopto_dl.py "<URL1>" "<URL2>" ...           # several
    python panopto_dl.py -f lectures.txt --drive "G:\\My Drive\\Lectures"
    python panopto_dl.py "<URL>" --list                  # only show the available streams

Which stream is the presentation is chosen automatically (SCREEN tag, else the
stream that compresses best = static slides). Check with --list, override with
--screen N.

Offline use: --download-only fetches everything into <out>\\_raw now;
--encode-only encodes it later without internet. A normal run does both at
once, with downloads never waiting for the encoder.

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
import json
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


@dataclass
class Stream:
    """One video stream of a Panopto session (best quality of it)."""
    tag: str        # Panopto's tag: DV = primary camera, OBJECT / SCREEN = other sources
    fmt: dict       # best-quality variant (used to classify the stream)
    variants: list  # all quality variants of this stream, low to high

    def smallest(self, min_w=0, min_h=0):
        """
        The cheapest variant that is still at least min_w x min_h.
        No point downloading a 1080p camera that ends up 320px wide in a corner.
        """
        ok = [f for f in self.variants
              if (f.get("width") or 0) >= min_w and (f.get("height") or 0) >= min_h]
        return min(ok, key=lambda f: f.get("tbr") or 0) if ok else self.fmt

    @property
    def kbps_per_mpx(self):
        """
        Bitrate per megapixel. Slides/screen recordings are mostly static and
        compress ~10x better than a camera filming a room, so a low value
        means 'this is the presentation'.
        """
        w, h, tbr = self.fmt.get("width"), self.fmt.get("height"), self.fmt.get("tbr")
        if not (w and h and tbr):
            return float("inf")
        return tbr / (w * h / 1e6)

    def describe(self):
        f = self.fmt
        tbr = f"{f['tbr']:.0f} kbps" if f.get("tbr") else "? kbps"
        return f"{self.tag:8s} {f.get('width')}x{f.get('height')}  {tbr:>10s}  ({self.kbps_per_mpx:.0f} kbps/MPx)"


def list_streams(info):
    """
    All separate video streams of the session.
    Formats are grouped by their source playlist (manifest_url), NOT by tag:
    two cameras can share the same tag, and grouping by tag would merge them.
    The PODCAST format (Panopto's own pre-mixed version) is dropped when the
    real streams are available.
    """
    groups = {}
    for f in info.get("formats", []):
        if f.get("vcodec") == "none":
            continue  # audio-only, or the slide images ('mhtml')
        tag = f.get("format_note") or "main"
        source = f.get("manifest_url") or f.get("url")
        groups.setdefault((tag, source), []).append(f)

    streams = []
    for (tag, _), fmts in groups.items():
        # Highest resolution first, then highest bitrate
        fmts.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
        streams.append(Stream(tag, fmts[-1], fmts))

    real = [s for s in streams if s.tag != "PODCAST"]
    return real or streams


def choose_streams(streams, screen_choice=None):
    """
    Decide which stream is the presentation (screen) and which is the camera.
      screen_choice: None = automatic, a number = stream # from --list (1-based),
                     or a tag such as SCREEN / OBJECT.
    Returns (screen, camera); camera may be None.
    """
    if len(streams) == 1:
        return streams[0], None

    # Camera = the primary 'DV' stream (it carries the lecturer's audio)
    dv = [s for s in streams if s.tag == "DV"]
    camera = dv[0] if dv else streams[0]

    if screen_choice:
        if screen_choice.isdigit():
            screen = streams[int(screen_choice) - 1]
        else:
            matches = [s for s in streams if s.tag.upper() == screen_choice.upper()]
            if not matches:
                raise RuntimeError(f"no stream tagged {screen_choice}")
            screen = min(matches, key=lambda s: s.kbps_per_mpx)
        if screen is camera:   # user picked the DV stream as screen: use another as camera
            camera = next(s for s in streams if s is not screen)
        return screen, camera

    others = [s for s in streams if s is not camera]
    tagged_screen = [s for s in others if s.tag == "SCREEN"]
    # A SCREEN-tagged stream wins; otherwise the one that compresses best
    screen = tagged_screen[0] if tagged_screen else min(others, key=lambda s: s.kbps_per_mpx)
    return screen, camera


def has_slides(info):
    """True if the session has PowerPoint slides as images (not a video stream)."""
    return any(f.get("format_id") == "slides" for f in info.get("formats", []))


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


def download_stream(info, stream, fmt, cookie_file, out_base, label):
    """Download one variant of a stream (with retries/resume). Returns the file path."""
    opts = {
        "cookiefile": str(cookie_file),
        "format": fmt["format_id"],
        "outtmpl": str(out_base) + ".%(ext)s",
        "concurrent_fragment_downloads": 4,   # parallel HLS segments = faster
        "quiet": True, "noprogress": True,    # our own tqdm bar instead
        # Panopto's servers sometimes stall on a fragment. Wait longer, retry
        # each fragment many times, and never silently skip one (a skipped
        # fragment = a hole in the video, and in yt-dlp it can also crash).
        "socket_timeout": 60,
        "retries": 10,
        "fragment_retries": 30,
        "file_access_retries": 10,
        "skip_unavailable_fragments": False,
    }
    for attempt in range(1, 4):
        hook = DownloadBar(f"{label} download {out_base.name} ({stream.tag} {fmt.get('height')}p)", position=0)
        opts["progress_hooks"] = [hook]
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                # process_ie_result reuses the metadata we already have,
                # so the page isn't fetched again for every stream.
                # On a retry yt-dlp resumes from the fragments it already has.
                result = ydl.process_ie_result(copy.deepcopy(info), download=True)
                return Path(ydl.prepare_filename(result))
        except (DownloadError, OSError) as e:
            if attempt == 3:
                raise
            tqdm.write(f"{label} {stream.tag}: download interrupted ({e}) - resuming, try {attempt + 1}/3")
        finally:
            hook.close()


# ---------------------------------------------------------------- one lecture

@dataclass
class Job:
    """A lecture whose streams are downloaded and waiting to be encoded."""
    url: str
    label: str        # e.g. "[2/5]"
    title: str
    screen: Path
    camera: Path      # None if the session has a single stream
    workdir: Path


def session_id(url):
    """The lecture's unique id (the ?id=... part of the viewer URL)."""
    m = re.search(r"[?&]id=([0-9a-fA-F-]{36})", url)
    return m.group(1).lower() if m else url


def load_raw(raw_dir, label=""):
    """A fully downloaded lecture from its raw folder, or None if incomplete."""
    meta_file = raw_dir / "meta.json"
    if not meta_file.exists():
        return None
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    camera = raw_dir / meta["camera"] if meta["camera"] else None
    return Job(meta["url"], label, meta["title"], raw_dir / meta["screen"], camera, raw_dir)


def download_lecture(url, label, cookie_file, args, raw_root):
    """
    Download the presentation + camera of one lecture into raw_root/<id>/.
    The folder persists: an interrupted download resumes on the next run,
    and a finished one can be encoded later, even offline (--encode-only).
    meta.json is written last, so its presence means 'download complete'.
    """
    raw_dir = raw_root / session_id(url)
    if (job := load_raw(raw_dir, label)) is not None:
        tqdm.write(f"{label} already downloaded: {job.title}")
        return job

    info = fetch_info(url, cookie_file)
    title = info.get("title") or "lecture"
    streams = list_streams(info)
    if not streams:
        raise RuntimeError("no video streams found")
    screen, camera = choose_streams(streams, args.screen)

    # Download only the quality the output actually needs
    screen_fmt = screen.smallest(min_h=min(args.height, screen.fmt.get("height") or 0))
    camera_fmt = None
    if camera:
        if args.layout == "pip":
            # The camera ends up 1/4 of the output width
            out_w = (screen.fmt.get("width") or 1920) * args.height / (screen.fmt.get("height") or 1080)
            camera_fmt = camera.smallest(min_w=int(out_w / 4))
        else:
            camera_fmt = camera.smallest(min_h=min(args.height, camera.fmt.get("height") or 0))

    skipped = len(streams) - (2 if camera else 1)
    tqdm.write(f"{label} {title}: screen={screen.tag} {screen_fmt.get('height')}p"
               + (f", camera={camera.tag} {camera_fmt.get('height')}p" if camera else "")
               + (f" (ignoring {skipped} other stream(s); see --list)" if skipped else ""))

    raw_dir.mkdir(parents=True, exist_ok=True)
    screen_file = download_stream(info, screen, screen_fmt, cookie_file, raw_dir / "screen", label)
    camera_file = (download_stream(info, camera, camera_fmt, cookie_file, raw_dir / "camera", label)
                   if camera else None)
    (raw_dir / "meta.json").write_text(json.dumps({
        "url": url, "title": title, "layout": args.layout,
        "screen": screen_file.name, "camera": camera_file.name if camera_file else None,
    }, ensure_ascii=False), encoding="utf-8")
    tqdm.write(f"{label} downloaded: {title}")
    return Job(url, label, title, screen_file, camera_file, raw_dir)


def encode_lecture(job, out_dir, args):
    """Combine a downloaded lecture and move the result to out_dir."""
    final_path = out_dir / f"{safe_filename(job.title)}.mp4"
    local_out = job.workdir / "combined.mp4"
    try:
        # Encode locally first, then move: writing straight into a synced
        # Drive folder would make it upload a half-finished file.
        combine(job.screen, job.camera, local_out, layout=args.layout, height=args.height,
                quality=args.crf, hw=not args.cpu,
                desc=f"{job.label} encode", position=1)
        shutil.move(str(local_out), final_path)
        if args.drive:
            # Separate copy for Drive: deleting one later doesn't touch the other
            drive_dir = Path(args.drive)
            drive_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(final_path, drive_dir / final_path.name)
    except BaseException:
        local_out.unlink(missing_ok=True)   # raw files stay, so it can be retried
        raise
    # Success: the raw streams are no longer needed
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
    ap.add_argument("--list", action="store_true",
                    help="Only show the streams of each URL and which would be used")
    ap.add_argument("--screen",
                    help="Which stream is the presentation: its number from --list, "
                         "or a tag (SCREEN, OBJECT...). Default: automatic")
    ap.add_argument("--download-only", action="store_true",
                    help="Only download (to <out>/_raw); encode later with --encode-only")
    ap.add_argument("--encode-only", action="store_true",
                    help="Encode everything already in <out>/_raw - no internet needed")
    ap.add_argument("--keep", action="store_true", help="Keep the raw stream files after encoding")
    ap.add_argument("--redo", action="store_true", help="Process again even if already done")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_root = out_dir / "_raw"   # downloaded, not yet encoded lectures
    # Record of finished lectures (by id), kept next to the videos
    done_file = out_dir / ".downloaded.txt"
    done = set(done_file.read_text().split()) if done_file.exists() else set()

    status = {}

    def encode_and_record(job, key):
        try:
            path = encode_lecture(job, out_dir, args)
            with open(done_file, "a") as f:
                f.write(session_id(job.url) + "\n")
            status[key] = f"OK -> {path.name}"
        except Exception as e:
            status[key] = f"FAILED (encode): {e}"

    def summary(keys):
        print("\n" + "=" * 60)
        for k in keys:
            print(f"{status.get(k, '?'):45.45s}  {session_id(k)}")
        if any(s.startswith("FAILED") for s in status.values()):
            sys.exit(1)

    # ---- offline: encode whatever was downloaded earlier
    if args.encode_only:
        raws = [j for d_ in sorted(raw_root.glob("*")) if (j := load_raw(d_)) is not None]
        if not raws:
            print(f"Nothing downloaded in {raw_root}.")
            return
        for n, job in enumerate(raws, 1):
            job.label = f"[{n}/{len(raws)}]"
            encode_and_record(job, job.url)
        summary([j.url for j in raws])
        return

    urls = list(args.urls) + (read_url_file(args.file) if args.file else [])
    urls = list(dict.fromkeys(urls))   # drop duplicates, keep order
    if not urls:
        ap.error("give at least one URL, or --file with a list of URLs")

    def login(url):
        return Path(args.cookies) if args.cookies else get_cookie_file(url)

    pending = [u for u in urls if args.redo or session_id(u) not in done]
    # Lectures already sitting in _raw don't need the network
    need_net = [u for u in pending if load_raw(raw_root / session_id(u)) is None]

    if not args.list and not pending:
        print("All lectures already done - nothing to do.")
        return

    cookie_file = login(urls[0]) if (args.list or need_net) else None

    if args.list:
        for url in urls:
            info = fetch_info(url, cookie_file)
            streams = list_streams(info)
            screen, camera = choose_streams(streams, args.screen)
            print(f"\n{info.get('title')}")
            for i, s in enumerate(streams, 1):
                role = "<- screen" if s is screen else "<- camera" if s is camera else ""
                print(f"  {i}. {s.describe()}  {role}")
            if has_slides(info):
                print("  (also has PowerPoint slides as images - not downloaded)")
        return

    status.update({u: "skipped (already done)" for u in urls if u not in pending})
    total = len(pending)

    # Pipeline: a background thread downloads everything as fast as it can,
    # while the main thread encodes whatever has finished downloading.
    # The queue is unbounded, so downloads never wait for the encoder -
    # when you're about to lose the internet, the downloads are what matter.
    jobs = queue.Queue()

    def downloader():
        nonlocal cookie_file
        try:
            for n, url in enumerate(pending, 1):
                label = f"[{n}/{total}]"
                for attempt in (1, 2):
                    try:
                        jobs.put(download_lecture(url, label, cookie_file, args, raw_root))
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
        if args.download_only:
            status[job.url] = "downloaded (encode later: --encode-only)"
        else:
            encode_and_record(job, job.url)

    if args.download_only:
        print(f"\nAll downloads done. Encode them any time, even offline:\n"
              f'  python panopto_dl.py --encode-only --out "{out_dir}"')
    summary(urls)


if __name__ == "__main__":
    main()