"""
video_combine.py - combine Panopto streams (screen + camera) into one video,
using the GPU encoder when one is available.

Used by panopto_dl.py:
    from video_combine import combine
    combine(files, out_path, layout="pip", height=720, hw=True)
"""

import json
import subprocess
import tempfile

from tqdm import tqdm
from functools import lru_cache

# Hardware encoders to try, in order, with roughly equivalent quality settings.
# Each entry: (encoder, extra ffmpeg args, pixel format it wants)
HW_ENCODERS = [
    # NVIDIA: constant-quality VBR; p4 = balanced speed/quality preset
    ("h264_nvenc", ["-preset", "p4", "-rc", "vbr", "-cq", "{q}", "-b:v", "0"], "yuv420p"),
    # Intel Quick Sync: ICQ mode (global_quality ~ crf)
    ("h264_qsv", ["-preset", "faster", "-global_quality", "{q}"], "nv12"),
    # AMD: constant QP, speed-oriented
    ("h264_amf", ["-quality", "speed", "-rc", "cqp", "-qp_i", "{q}", "-qp_p", "{q}"], "nv12"),
]
CPU_ENCODER = ("libx264", ["-preset", "veryfast", "-crf", "{q}"], "yuv420p")

# The encoder this machine uses (chosen by benchmark): Intel Quick Sync
ENCODER = HW_ENCODERS[1]


def probe(path):
    """Return (width, height, has_audio) of a media file via ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    streams = json.loads(out)["streams"]
    video = next((s for s in streams if s["codec_type"] == "video"), None)
    has_audio = any(s["codec_type"] == "audio" for s in streams)
    if video is None:
        return 0, 0, has_audio
    return int(video["width"]), int(video["height"]), has_audio


def duration(path):
    """Length of a media file in seconds."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    ).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def run_ffmpeg(cmd, total_seconds, desc="Combining", position=0):
    """
    Run ffmpeg with a single tqdm progress bar instead of its per-frame output.
    -progress pipe:1 makes ffmpeg write machine-readable 'key=value' lines to
    stdout (out_time_us = how far into the output it has encoded).
    -nostats turns off the usual status line on stderr.
    """
    cmd = cmd[:1] + ["-nostats", "-progress", "pipe:1"] + cmd[1:]
    # stderr goes to a temp file, shown only if ffmpeg fails
    with tempfile.TemporaryFile(mode="w+") as err, \
         tqdm(total=round(total_seconds), unit="s", desc=desc, position=position, leave=False,
              bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt}s "
                         "[{elapsed}<{remaining}]") as bar:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True)
        for line in proc.stdout:
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and value.isdigit():
                bar.update(min(round(int(value) / 1e6), bar.total) - bar.n)
            elif key == "speed":
                bar.set_postfix_str(f"speed={value}")
            elif key == "progress" and value == "end":
                bar.update(bar.total - bar.n)
        proc.wait()
        if proc.returncode != 0:
            err.seek(0)
            raise RuntimeError(f"ffmpeg failed:\n{err.read()[-2000:]}")


@lru_cache(maxsize=1)
def pick_encoder(hw=True):
    """
    Return the first encoder that actually works on this machine.
    An encoder can be compiled into ffmpeg without the matching GPU being present,
    so each one is tested with a 1-second dummy encode (lavfi testsrc = generated video).
    """
    if hw:
        for enc, args, pix in HW_ENCODERS:
            test = ["ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30", "-t", "1",
                    "-pix_fmt", pix, "-c:v", enc,
                    *[a.replace("{q}", "28") for a in args],
                    "-f", "null", "-"]
            if subprocess.run(test, capture_output=True).returncode == 0:
                return enc, args, pix
    return CPU_ENCODER


def _even(x):
    return int(x) // 2 * 2


def combine(files, out_path, layout="pip", height=720, quality=26, hw=True,
            desc="Combining", position=0):
    """
    Merge downloaded streams into one video.
      layout  : 'pip' = camera in the bottom-right corner, 'side' = side by side
      height  : output height (720 is plenty for a phone; never upscales)
      quality : lower = better/bigger (crf-like scale for every encoder)
      hw      : use the GPU encoder (Intel Quick Sync); False = CPU
      desc, position : label and screen row of the progress bar
    """
    # Fixed after benchmarking on this machine: Intel Quick Sync works and
    # keeps the CPU free. hw=False forces the CPU encoder (e.g. for debugging).
    enc, enc_args, pix = ENCODER if hw else CPU_ENCODER
    enc_args = [a.replace("{q}", str(quality)) for a in enc_args]

    probed = [(f, *probe(f)) for f in files]  # (file, w, h, has_audio)

    if len(probed) == 1:
        f, w, h, _ = probed[0]
        H = _even(min(height, h))
        filt = f"[0:v]scale=-2:{H},setsar=1[v]"
        inputs, audio_map = ["-i", str(f)], "0:a?"
    else:
        # The camera is the stream with the lecturer's audio;
        # if that's ambiguous, take the smaller-resolution stream.
        # Which stream is the camera? Panopto tags its primary stream "DV"
        # (the camera, which also carries the audio). Otherwise: the only
        # stream with audio, or failing that the smaller one.
        tagged = [p for p in probed if "_DV" in p[0].name]
        with_audio = [p for p in probed if p[3]]
        if len(tagged) == 1:
            cam = tagged[0]
        elif len(with_audio) == 1:
            cam = with_audio[0]
        else:
            cam = min(probed, key=lambda p: p[1] * p[2])
        main = max((p for p in probed if p is not cam), key=lambda p: p[1] * p[2])
        if len(probed) > 2:
            tqdm.write(f"Note: {len(probed)} streams; combining {main[0].name} + {cam[0].name} only.")

        H = _even(min(height, main[2]))
        if layout == "pip":
            main_w = _even(main[1] * H / main[2])
            cam_w = _even(main_w / 4)
            # eof_action=pass: if the camera ends early, the screen keeps playing
            filt = (f"[0:v]scale={main_w}:{H}[m];[1:v]scale={cam_w}:-2[c];"
                    f"[m][c]overlay=W-w-20:H-h-20:eof_action=pass,setsar=1[v]")
        else:
            filt = (f"[0:v]scale=-2:{H}[m];[1:v]scale=-2:{H}[c];"
                    f"[m][c]hstack=inputs=2:shortest=0,setsar=1[v]")
        inputs = ["-i", str(main[0]), "-i", str(cam[0])]
        audio_map = "1:a?" if cam[3] else "0:a?"

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
           *inputs, "-filter_complex", filt,
           "-map", "[v]", "-map", audio_map,
           "-pix_fmt", pix, "-c:v", enc, *enc_args,
           "-c:a", "aac", "-b:a", "96k",
           "-movflags", "+faststart",   # playable before fully loaded
           str(out_path)]
    # Output is as long as the longest input (screen keeps going if camera ends)
    run_ffmpeg(cmd, max(duration(p[0]) for p in probed),
               desc=f"{desc} [{enc}]", position=position)