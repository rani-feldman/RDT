"""Narrate each slide's notes with Kokoro (voice am_michael), cut in the GUI clips, burn in captions,
and write one MP4.  python tools/media/deck_video.py OUT.mp4
Needs render.py's slides and capture.py's clips in WORK."""
import json, subprocess, sys
from pathlib import Path

import numpy as np
import soundfile as sf
from common import SR, VOICE, WORK, kokoro, say, sentences, ts

R = WORK / "render"
W = WORK / "video"
FPS = 30
INTRO = 3.0          # seconds the slide shows before its clip starts
CLIPS = {"watch": "connect", "structures": "structures", "write": "write", "chart": "chart",
         "memory": "memory", "console": "console"}


def silence(sec):
    return np.zeros(int(sec * SR), dtype=np.float32)


def clip_list(name, start, total, out):
    """ffmpeg concat list: the slide for INTRO s, then the clip frames at their real timing, then
    the last frame held to fill ``total``."""
    d = WORK / "cap/clips" / name
    times = [float(x) for x in (d / "times.txt").read_text().split()]
    frames = sorted(d.glob("f*.jpg"))[: len(times)]
    lines = [f"file '{R / 'png' / start}.png'", f"duration {INTRO:.3f}"]
    used = INTRO
    for i, f in enumerate(frames):
        dur = (times[i + 1] - times[i]) if i + 1 < len(times) else 0.1
        lines += [f"file '{f}'", f"duration {dur:.3f}"]
        used += dur
    lines += [f"file '{frames[-1]}'", f"duration {max(total - used, 0.1):.3f}", f"file '{frames[-1]}'"]
    out.write_text("\n".join(lines) + "\n")
    return used


def main():
    out_mp4 = Path(sys.argv[1])
    data = json.loads((R / "notes.json").read_text())
    W.mkdir(parents=True, exist_ok=True)
    k = kokoro()
    segs, srt, t0 = [], [], 0.0
    for n, sid in enumerate(data["order"]):
        audio, t = [silence(0.5)], 0.5
        for sent in sentences(data["notes"][sid]):
            samples, sr = k.create(say(sent), voice=VOICE, speed=1.0, lang="en-us")
            assert sr == SR
            dur = len(samples) / SR
            srt.append((t0 + t, t0 + t + dur, sent))
            audio += [samples.astype(np.float32), silence(0.3)]
            t += dur + 0.3
        audio.append(silence(0.6))
        total = t + 0.6
        listf = W / f"{n:02d}-{sid}.txt"
        if sid in CLIPS:
            used = clip_list(CLIPS[sid], sid, total, listf)
            if used > total:          # let the clip finish
                audio.append(silence(used - total + 0.3))
                total = used + 0.3
                clip_list(CLIPS[sid], sid, total, listf)
        else:
            listf.write_text(f"file '{R / 'png' / sid}.png'\nduration {total:.3f}\nfile '{R / 'png' / sid}.png'\n")
        wav = W / f"{n:02d}-{sid}.wav"
        sf.write(wav, np.concatenate(audio), SR)
        mp4 = W / f"{n:02d}-{sid}.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listf),
                        "-i", str(wav), "-vf", f"scale=1920:1080:flags=lanczos,fps={FPS},format=yuv420p",
                        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-b:a", "128k",
                        "-ar", "48000", "-t", f"{total:.3f}", str(mp4)], check=True)
        real = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                     "csv=p=0", str(mp4)], capture_output=True, text=True).stdout)
        segs.append(mp4)
        t0 += real
        print(f"{sid:14s} {real:6.1f}s  total {t0/60:5.2f} min", flush=True)
    (W / "all.txt").write_text("".join(f"file '{p}'\n" for p in segs))
    srt_path = W / "captions.srt"
    srt_path.write_text("".join(f"{i+1}\n{ts(a)} --> {ts(b)}\n{txt}\n\n" for i, (a, b, txt) in enumerate(srt)))
    joined = W / "joined.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(W / "all.txt"),
                    "-c", "copy", str(joined)], check=True)
    style = ("FontName=IBM Plex Sans,FontSize=15,PrimaryColour=&H00FFFFFF,BackColour=&H99000000,"
             "BorderStyle=4,Outline=0,Shadow=0,MarginV=18,MarginL=60,MarginR=60")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(joined), "-vf",
                    f"subtitles={srt_path}:force_style='{style}'", "-c:v", "libx264", "-preset", "medium",
                    "-crf", "21", "-c:a", "copy", "-movflags", "+faststart", str(out_mp4)], check=True)
    print(out_mp4)


if __name__ == "__main__":
    main()
