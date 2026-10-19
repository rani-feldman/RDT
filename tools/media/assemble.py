"""Put the full live walkthrough together: a narrated title card, the screen recording with each step's
narration at the moment it was spoken and a smooth zoom on what the pointer is doing, a closing card,
captions and chapter labels.
python tools/media/assemble.py OUT.mp4 [--reuse]  (reads WORK/live from walkthrough.py, cards from render.py)"""
import bisect, json, subprocess, sys
from pathlib import Path

import cv2
import numpy as np
import soundfile as sf
from common import SR, VOICE, WORK, kokoro, say as spoken, sentences, ts

LIVE, OUT = WORK / "live", Path(sys.argv[1]).resolve()
W = LIVE / "work"
INTRO = ("This is RDT, the Remote Debugger Tool. It reads, watches and changes the variables and memory of "
         "running firmware, over the link your product already has, using each MCU’s ELF file. "
         "Let’s watch someone use it, against the simulator.")
OUTRO = ("That’s RDT. The user manual covers every feature in detail, with troubleshooting and where each "
         "file lives. Start with the simulator, then point RDT at your own board.")


def run(*a):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *a], check=True)


def card(png, text, k, name, caps, t0):
    parts, t = [np.zeros(int(0.6 * SR), np.float32)], 0.6
    for s in sentences(text):
        a, _ = k.create(spoken(s), voice=VOICE, speed=1.0, lang="en-us")
        caps.append((t0 + t, t0 + t + len(a) / SR, s))
        parts += [a.astype(np.float32), np.zeros(int(0.3 * SR), np.float32)]
        t += len(a) / SR + 0.3
    parts.append(np.zeros(int(0.8 * SR), np.float32))
    total = t + 0.8
    sf.write(W / f"{name}.wav", np.concatenate(parts), SR)
    run("-loop", "1", "-framerate", "30", "-i", str(png), "-i", str(W / f"{name}.wav"), "-t", f"{total:.3f}",
        "-vf", "scale=1920:1080,format=yuv420p", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", str(W / f"{name}.mp4"))
    return total


FPS, WIDTH, HEIGHT = 30, 1920, 1080


def zoom_video(steps, focus, t0, dur, wav, out):
    """Re-encode the recording from wall-clock time t0 for dur seconds, zoomed per step on the
    latest pointer or focus point, both eased so the camera glides."""
    times = [f[0] for f in focus]
    starts = [s["t"] for s in steps]
    src = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-ss", f"{t0 - START:.3f}", "-i", str(LIVE / "screen.mkv"),
                            "-t", f"{dur:.3f}", "-vf", f"fps={FPS}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                           stdout=subprocess.PIPE)
    dst = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                            "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS), "-i", "-", "-i", str(wav), "-map", "0:v",
                            "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
                            "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-shortest", str(out)], stdin=subprocess.PIPE)
    z, cx, cy = 1.0, WIDTH / 2, HEIGHT / 2
    k, size = 0, WIDTH * HEIGHT * 3
    a_z, a_c = 1 - np.exp(-1 / (FPS * 0.45)), 1 - np.exp(-1 / (FPS * 0.35))
    while True:
        buf = src.stdout.read(size)
        if len(buf) < size:
            break
        frame = np.frombuffer(buf, np.uint8).reshape(HEIGHT, WIDTH, 3)
        t = t0 + k / FPS
        i = bisect.bisect_right(starts, t) - 1
        tz = steps[i]["zoom"] if i >= 0 else 1.0
        j = bisect.bisect_right(times, t) - 1
        fx, fy = (focus[j][1], focus[j][2]) if j >= 0 else (WIDTH / 2, HEIGHT / 2)
        z += (tz - z) * a_z
        cx += (fx - cx) * a_c
        cy += (fy - cy) * a_c
        if z > 1.01:
            w, h = WIDTH / z, HEIGHT / z
            x0 = min(max(cx - w / 2, 0), WIDTH - w)
            y0 = min(max(cy - h / 2, 0), HEIGHT - h)
            m = np.float32([[z, 0, -z * x0], [0, z, -z * y0]])
            frame = cv2.warpAffine(frame, m, (WIDTH, HEIGHT), flags=cv2.INTER_CUBIC)
        dst.stdin.write(frame.tobytes())
        k += 1
    dst.stdin.close()
    dst.wait()
    src.wait()


def ass_time(t):
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def main():
    W.mkdir(exist_ok=True)
    k = kokoro()
    steps = json.loads((LIVE / "steps.json").read_text())
    global START
    start = START = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=start_time", "-of", "csv=p=0",
                                  str(LIVE / "screen.mkv")], capture_output=True, text=True).stdout)
    caps, chapters = [], []
    t_intro = card(WORK / "render/png/cover.png", INTRO, k, "intro", caps, 0.0)

    # the live part: from 1 s before the first step to the end of the recording
    begin = steps[0]["t"] - start - 1.0
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(LIVE / "screen.mkv")], capture_output=True, text=True).stdout) - start - begin - 0.2
    audio = np.zeros(int((dur + 1) * SR), np.float32)
    for s in steps:
        a, _ = sf.read(LIVE / "say" / f"{s['i']:03d}.wav", dtype="float32")
        at = int((s["t"] - start - begin) * SR)
        audio[at:at + len(a)] += a[: len(audio) - at]
        rel = t_intro + s["t"] - start - begin
        caps.append((rel, rel + s["dur"], s["text"]))
        if s["chapter"]:
            chapters.append((rel, s["chapter"]))
    sf.write(W / "live.wav", audio[: int(dur * SR)], SR)
    if "--reuse" not in sys.argv or not (W / "live.mp4").exists():
        zoom_video(steps, json.loads((LIVE / "focus.json").read_text()), start + begin, dur, W / "live.wav",
                   W / "live.mp4")
    t_live = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                   str(W / "live.mp4")], capture_output=True, text=True).stdout)
    chapters.append((t_intro + t_live, None))
    card(WORK / "render/png/closing.png", OUTRO, k, "outro", caps, t_intro + t_live)

    (W / "all.txt").write_text("".join(f"file '{W / n}.mp4'\n" for n in ("intro", "live", "outro")))
    run("-f", "concat", "-safe", "0", "-i", str(W / "all.txt"), "-c", "copy", str(W / "joined.mp4"))

    lines = ["[Script Info]", "ScriptType: v4.00+", "PlayResX: 1920", "PlayResY: 1080", "",
             "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
             "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
             "MarginL, MarginR, MarginV, Encoding",
             "Style: Cap,IBM Plex Sans,44,&H00FFFFFF,&H00FFFFFF,&H00000000,&H66000000,0,0,0,0,100,100,0,0,4,14,0,2,"
             "160,160,70,1",
             "Style: Chap,IBM Plex Sans,34,&H00FFFFFF,&H00FFFFFF,&H00000000,&H405F8A1F,1,0,0,0,100,100,1,0,4,12,0,9,"
             "40,40,70,1", "",
             "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for a, b, text in caps:
        lines.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Cap,,0,0,0,,{text}")
    for (a, name), (b, _) in zip(chapters, chapters[1:]):
        lines.append(f"Dialogue: 1,{ass_time(a)},{ass_time(b)},Chap,,0,0,0,,{name}")
    ass = W / "captions.ass"
    ass.write_text("\n".join(lines) + "\n")
    srt = "".join(f"{i+1}\n{ts(a)} --> {ts(b)}\n{t}\n\n" for i, (a, b, t) in enumerate(caps))
    OUT.with_suffix(".srt").write_text(srt)
    run("-i", str(W / "joined.mp4"), "-vf", f"ass={ass}", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "copy", "-movflags", "+faststart", str(OUT))
    print(OUT, t_intro + t_live)


if __name__ == "__main__":
    main()
