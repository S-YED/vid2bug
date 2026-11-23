#!/usr/bin/env python3
"""vid2bug: turn a screen recording of a test session into a bug report.

  scan    contact sheets (timestamped thumbnail grids) + automatic signals:
          black screens, white/blank screens, frozen screens
  frames  full-resolution frames at given timestamps
  draft   optional: ask Claude to draft bugs from the sheets, signals and your notes
  report  evidence frames + report.md + bugs.csv from a bugs.json you reviewed

Needs Python 3.9+ and ffmpeg/ffprobe on PATH. No other dependencies.
"""
import argparse
import base64
import csv
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

__version__ = "0.1.0"
SEVERITIES = ["Critical", "High", "Medium", "Low"]


# ---------- time helpers ----------

def parse_time(value):
    """'83.5', '1:23', '01:02:03.5' -> seconds (float)."""
    value = str(value).strip()
    parts = value.split(":")
    if not 1 <= len(parts) <= 3 or not all(re.fullmatch(r"\d+(\.\d+)?", p) for p in parts):
        raise ValueError(f"bad timestamp: {value!r} (use seconds, mm:ss or hh:mm:ss)")
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + float(p)
    return seconds


def fmt_time(seconds):
    """seconds -> 'mm:ss' or 'h:mm:ss' (drops fractions, used in names and reports)."""
    s = int(seconds)
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ---------- ffmpeg helpers ----------

def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace")


def check_ffmpeg():
    for tool in ("ffmpeg", "ffprobe"):
        try:
            run([tool, "-version"])
        except FileNotFoundError:
            sys.exit(f"{tool} not found. Install ffmpeg and make sure it is on PATH.")


def probe(video):
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration", "-of", "json", str(video)])
    if r.returncode:
        sys.exit(f"ffprobe could not read {video}:\n{r.stderr.strip()}")
    info = json.loads(r.stdout)
    stream = info["streams"][0]
    return {"duration": float(info["format"]["duration"]),
            "width": stream["width"], "height": stream["height"]}


def contact_sheets(video, out_dir, every, cols, rows, thumb_width):
    """Write sheet_001.png... Each tile is one frame every `every` seconds, timestamp burned in."""
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"fps=1/{every},scale={thumb_width}:-2"
    label = "drawtext=text='%{pts\\:hms}':x=4:y=4:fontsize=14:fontcolor=white:box=1:boxcolor=black@0.6"
    attempts = [f"{base},{label}"]
    arial = Path("C:/Windows/Fonts/arial.ttf")
    if arial.exists():  # Windows ffmpeg builds often lack a default font
        attempts.append(f"{base},{label}:fontfile='C\\:/Windows/Fonts/arial.ttf'")
    attempts.append(base)  # last resort: no burned-in time, use sheets.json instead
    for vf in attempts:
        r = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
                 "-vf", f"{vf},tile={cols}x{rows}", str(out_dir / "sheet_%03d.png")])
        if r.returncode == 0:
            return sorted(out_dir.glob("sheet_*.png")), "drawtext" in vf
    sys.exit(f"ffmpeg failed to make contact sheets:\n{r.stderr.strip()}")


def sheet_index(sheets, every, cols, rows, duration):
    """Which timestamp each tile shows, so a reader (or an AI) can map a tile back to the video."""
    per_sheet = cols * rows
    index = []
    for i, sheet in enumerate(sheets):
        tiles = []
        for t in range(per_sheet):
            sec = (i * per_sheet + t) * every
            if sec >= duration:
                break
            tiles.append({"row": t // cols + 1, "col": t % cols + 1, "time": fmt_time(sec)})
        index.append({"sheet": sheet.name, "tiles": tiles})
    return index


def _intervals(log, start_key, end_key, duration):
    """Pair up '<start_key>N' ... '<end_key>M' values from an ffmpeg log."""
    starts = [float(x) for x in re.findall(rf"{start_key}\s*(-?[\d.]+)", log)]
    ends = [float(x) for x in re.findall(rf"{end_key}\s*(-?[\d.]+)", log)]
    ends += [duration] * (len(starts) - len(ends))  # still frozen/black when the video ended
    return [(round(s, 2), round(e, 2)) for s, e in zip(starts, ends)]


def parse_blackdetect(log, duration):
    return _intervals(log, "black_start:", "black_end:", duration)


def parse_freezedetect(log, duration):
    return _intervals(log, "freeze_start:", "freeze_end:", duration)


def detect_signals(video, duration, min_blank, min_frozen, freeze_noise=0.0003):
    """Black, white and frozen stretches. Frozen = nothing on screen changed (hang, slow load, or the tester reading)."""
    black = f"blackdetect=d={min_blank}:pix_th=0.10"
    pass1 = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(video), "-an",
                 "-vf", f"{black},freezedetect=n={freeze_noise}:d={min_frozen}",
                 "-f", "null", "-"])
    pass2 = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(video), "-an",
                 "-vf", f"negate,{black}", "-f", "null", "-"])
    blank = [{"type": "black screen", "start": s, "end": e} for s, e in parse_blackdetect(pass1.stderr, duration)]
    blank += [{"type": "white/blank screen", "start": s, "end": e} for s, e in parse_blackdetect(pass2.stderr, duration)]
    frozen = []
    for s, e in parse_freezedetect(pass1.stderr, duration):
        # a black or white screen is also "frozen"; report it once, as blank
        covered = sum(max(0, min(e, b["end"]) - max(s, b["start"])) for b in blank)
        if covered < 0.8 * (e - s):
            frozen.append({"type": "frozen screen", "start": s, "end": e})
    signals = sorted(blank + frozen, key=lambda x: x["start"])
    for sig in signals:
        sig["duration"] = round(sig["end"] - sig["start"], 2)
        sig["at"] = f"{fmt_time(sig['start'])}-{fmt_time(sig['end'])}"
    return signals


def extract_frame(video, seconds, out_path, box=None):
    """Full-resolution frame. box = [x, y, w, h] draws a red rectangle around the problem."""
    vf = ["-vf", f"drawbox=x={box[0]}:y={box[1]}:w={box[2]}:h={box[3]}:color=red:t=6"] if box else []
    r = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{seconds:.3f}",
             "-i", str(video), *vf, "-frames:v", "1", str(out_path)])
    if r.returncode or not out_path.exists():
        raise RuntimeError(f"could not extract frame at {fmt_time(seconds)}: {r.stderr.strip()}")
    return out_path


# ---------- commands ----------

def cmd_scan(a):
    check_ffmpeg()
    out = Path(a.out)
    meta = probe(a.video)
    sheets, labelled = contact_sheets(a.video, out / "sheets", a.every, a.cols, a.rows, a.thumb_width)
    signals = [] if a.no_signals else detect_signals(a.video, meta["duration"], a.min_blank, a.min_frozen, a.freeze_noise)
    scan = {"video": str(a.video), **meta, "every": a.every, "timestamps_burned_in": labelled,
            "sheets": sheet_index(sheets, a.every, a.cols, a.rows, meta["duration"]), "signals": signals}
    (out / "scan.json").write_text(json.dumps(scan, indent=2))
    print(f"{len(sheets)} contact sheet(s) in {out / 'sheets'} (one frame every {a.every}s)")
    for s in signals:
        print(f"  {s['at']}  {s['type']} for {s['duration']}s")
    print(f"wrote {out / 'scan.json'}")


def cmd_frames(a):
    check_ffmpeg()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for t in a.time:
        sec = parse_time(t)
        path = extract_frame(a.video, sec, out / f"frame_{fmt_time(sec).replace(':', '-')}.png")
        print(path)


DRAFT_PROMPT = """You are a senior QA engineer reviewing a screen recording of a test session.
You get contact sheets (thumbnail grids; each tile is labelled with its timestamp, and the
sheet index below maps every tile to a time), automatically detected signals (black, blank or
frozen stretches), and the tester's own notes.

Draft one bug per real problem. Rules:
- Only report what is visible in the frames or stated in the notes. Never invent behaviour.
- A frozen stretch can be the tester reading; only call it a bug if the frames or notes support it.
- Use the timestamps from the tiles. Give the best 1-3 evidence timestamps per bug.
- Severity is one of: Critical, High, Medium, Low.
- If you are unsure, say so in "confidence" ("high", "medium" or "low").

Reply with ONLY a JSON array, no prose, using exactly these keys:
[{{"id": "BUG-01", "title": "...", "severity": "High", "area": "screen or feature",
  "times": ["01:23"], "steps": ["...", "..."], "expected": "...", "actual": "...",
  "fix_hint": "...", "confidence": "medium"}}]

Video: {duration}s, {width}x{height}
Sheet index: {index}
Detected signals: {signals}
Tester notes:
{notes}
"""


def build_draft_request(scan, sheet_paths, notes, model):
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                            "data": base64.b64encode(p.read_bytes()).decode()}}
               for p in sheet_paths]
    prompt = DRAFT_PROMPT.format(duration=round(scan["duration"], 1), width=scan["width"], height=scan["height"],
                                 index=json.dumps(scan["sheets"]), signals=json.dumps(scan["signals"]),
                                 notes=notes.strip() or "(none)")
    content.append({"type": "text", "text": prompt})
    return {"model": model, "max_tokens": 8000, "messages": [{"role": "user", "content": content}]}


def parse_draft(text):
    """Pull the JSON array out of the model reply and mark every bug as an unreviewed AI draft."""
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        raise ValueError("model reply contained no JSON array")
    bugs = json.loads(match.group(0))
    for i, bug in enumerate(bugs, 1):
        bug.setdefault("id", f"BUG-{i:02d}")
        bug.setdefault("severity", "Medium")
        bug["times"] = bug.get("times") or []
        bug["needs_review"] = True
    return bugs


def cmd_draft(a):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("Set ANTHROPIC_API_KEY to use draft. Or skip it and write bugs.json yourself "
                 "(see examples/bugs.example.json).")
    out = Path(a.out)
    scan = json.loads((out / "scan.json").read_text())
    sheets = sorted((out / "sheets").glob("sheet_*.png"))[: a.max_sheets]
    notes = Path(a.notes).read_text(encoding="utf-8") if a.notes else ""
    body = build_draft_request(scan, sheets, notes, a.model)
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                          "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        reply = json.loads(resp.read())
    text = "".join(block.get("text", "") for block in reply.get("content", []))
    bugs = parse_draft(text)
    (out / "bugs.draft.json").write_text(json.dumps(bugs, indent=2))
    print(f"{len(bugs)} draft bug(s) in {out / 'bugs.draft.json'}")
    print("Review every one against the recording, fix or delete, set needs_review to false, then run report.")


def cmd_report(a):
    check_ffmpeg()
    out = Path(a.out)
    ev_dir = out / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)
    bugs = json.loads(Path(a.bugs).read_text(encoding="utf-8"))
    scan_path = out / "scan.json"
    signals = json.loads(scan_path.read_text())["signals"] if scan_path.exists() else []
    order = {s: i for i, s in enumerate(SEVERITIES)}
    bugs.sort(key=lambda b: (order.get(b.get("severity"), len(order)), b["id"]))

    for bug in bugs:
        bug["evidence"] = []
        for t in bug.get("times", []):
            sec = parse_time(t)
            name = f"{bug['id']}_{fmt_time(sec).replace(':', '-')}.png"
            extract_frame(a.video, sec, ev_dir / name, bug.get("box"))
            bug["evidence"].append(f"evidence/{name}")

    title = a.title or f"Bug report: {Path(a.video).name}"
    counts = {s: sum(b.get("severity") == s for b in bugs) for s in SEVERITIES}
    lines = [f"# {title}", "",
             f"{len(bugs)} bugs · " + " · ".join(f"{n} {s}" for s, n in counts.items() if n), ""]
    if any(b.get("needs_review") for b in bugs):
        lines += ["> Bugs marked **DRAFT** were drafted by AI and not yet checked against the recording.", ""]
    lines += ["| ID | Severity | Title | Time |", "|---|---|---|---|"]
    lines += [f"| [{b['id']}](#{b['id'].lower()}) | {b.get('severity', '')} | {b['title']}"
              f"{' (DRAFT)' if b.get('needs_review') else ''} | {', '.join(b.get('times', []))} |" for b in bugs]
    for b in bugs:
        lines += ["", f"## {b['id']}", "", f"**{b['title']}**" + (" · DRAFT, verify" if b.get("needs_review") else ""), "",
                  f"- **Severity:** {b.get('severity', '')}"]
        if b.get("area"):
            lines.append(f"- **Area:** {b['area']}")
        lines.append(f"- **Seen at:** {', '.join(b.get('times', [])) or 'n/a'}")
        if b.get("steps"):
            lines += ["", "**Steps to reproduce**", ""] + [f"{i}. {s}" for i, s in enumerate(b["steps"], 1)]
        for key, label in (("expected", "Expected"), ("actual", "Actual"), ("fix_hint", "Fix hint")):
            if b.get(key):
                lines += ["", f"**{label}:** {b[key]}"]
        for path in b["evidence"]:
            lines += ["", f"![{b['id']} evidence]({path})"]
    if signals:
        lines += ["", "## Automatic signals", "",
                  "Stretches where the screen was black, blank or did not change. Not every one is a bug "
                  "(the tester may have been reading), but long ones are worth a look.", "",
                  "| Time | Signal | Seconds |", "|---|---|---|"]
        lines += [f"| {s['at']} | {s['type']} | {s['duration']} |" for s in signals]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    with open(out / "bugs.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ID", "Title", "Severity", "Area", "Time", "Steps to reproduce", "Expected", "Actual",
                    "Fix hint", "Evidence", "Status"])
        for b in bugs:
            w.writerow([b["id"], b["title"], b.get("severity", ""), b.get("area", ""), ", ".join(b.get("times", [])),
                        "\n".join(f"{i}. {s}" for i, s in enumerate(b.get("steps", []), 1)),
                        b.get("expected", ""), b.get("actual", ""), b.get("fix_hint", ""),
                        ", ".join(b["evidence"]), "Draft" if b.get("needs_review") else "Open"])
    print(f"wrote {out / 'report.md'}, {out / 'bugs.csv'} and {sum(len(b['evidence']) for b in bugs)} evidence frame(s)")


def main(argv=None):
    p = argparse.ArgumentParser(prog="vid2bug", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="contact sheets + blank/frozen screen signals")
    s.add_argument("video")
    s.add_argument("-o", "--out", default="vid2bug-out")
    s.add_argument("--every", type=float, default=3, help="seconds between thumbnails (default 3)")
    s.add_argument("--cols", type=int, default=8)
    s.add_argument("--rows", type=int, default=2)
    s.add_argument("--thumb-width", type=int, default=200)
    s.add_argument("--min-blank", type=float, default=1.5, help="shortest black/blank stretch to report, seconds")
    s.add_argument("--min-frozen", type=float, default=4, help="shortest frozen stretch to report, seconds")
    s.add_argument("--freeze-noise", type=float, default=0.0003,
                   help="how much change still counts as frozen (0-1, default 0.0003); raise it for noisy recordings")
    s.add_argument("--no-signals", action="store_true", help="skip blank/frozen detection (faster)")
    s.set_defaults(func=cmd_scan)

    f = sub.add_parser("frames", help="full-resolution frames at timestamps")
    f.add_argument("video")
    f.add_argument("-t", "--time", action="append", required=True, help="seconds, mm:ss or hh:mm:ss; repeatable")
    f.add_argument("-o", "--out", default="vid2bug-out/frames")
    f.set_defaults(func=cmd_frames)

    d = sub.add_parser("draft", help="optional: draft bugs.json with Claude (needs ANTHROPIC_API_KEY)")
    d.add_argument("-o", "--out", default="vid2bug-out", help="folder that scan wrote to")
    d.add_argument("--notes", help="text file with what you saw while testing")
    d.add_argument("--model", default=os.environ.get("VID2BUG_MODEL", "claude-sonnet-5-5"))
    d.add_argument("--max-sheets", type=int, default=40)
    d.set_defaults(func=cmd_draft)

    r = sub.add_parser("report", help="report.md + bugs.csv + evidence frames from bugs.json")
    r.add_argument("video")
    r.add_argument("bugs", help="bugs.json (reviewed)")
    r.add_argument("-o", "--out", default="vid2bug-out")
    r.add_argument("--title")
    r.set_defaults(func=cmd_report)

    a = p.parse_args(argv)
    if hasattr(a, "video"):
        a.video = Path(a.video)
        if not a.video.exists():
            sys.exit(f"video not found: {a.video}")
    try:
        a.func(a)
    except (ValueError, RuntimeError, OSError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
