"""Render a 36-second fake mobile-app test session with planted bugs, for the demo.

Needs Pillow and ffmpeg:  python examples/make_demo_video.py examples/demo.mp4
Timeline: login 0-6s, home 6-12s, game 12-20s, results 20-24s (shows the wrong game's score),
white screen 24-27s, spinner stuck 27-33s, home 33-36s.
"""
import subprocess
import sys
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 360, 640, 10
try:
    FONT = ImageFont.truetype("DejaVuSans.ttf", 22)
    SMALL = ImageFont.truetype("DejaVuSans.ttf", 16)
except OSError:
    FONT = SMALL = ImageFont.load_default()


def screen(title, body, color, t, cursor=None):
    im = Image.new("RGB", (W, H), "#f4f1ea")
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, W, 64], fill=color)
    d.text((16, 20), title, font=FONT, fill="white")
    y = 100
    for line in body:
        d.text((24, y), line, font=SMALL, fill="#222")
        y += 34
    if cursor:  # a "finger" moving around, so normal screens are never static
        x, yy = cursor
        d.ellipse([x - 14, yy - 14, x + 14, yy + 14], outline="#e33", width=3)
    d.text((W - 70, H - 30), f"{t:4.1f}s", font=SMALL, fill="#999")
    return im


def frame(t):
    wobble = (t * 37) % 200
    if t < 6:
        return screen("Sign in", ["Email: tester@example.com", "Password: ********", "", "[ Sign in ]"], "#3b5bdb", t,
                      (80 + wobble, 240))
    if t < 12:
        return screen("Home", ["Hi Asha!", "XP: 120", "Today's games:", "  - Snack Catch", "  - Hide and Seek"],
                      "#2b8a3e", t, (120, 220 + wobble))
    if t < 20:
        im = screen("Snack Catch", ["Catch the apples!", f"Caught: {int((t - 12) * 2)}"], "#e8590c", t)
        d = ImageDraw.Draw(im)
        x = 40 + (t * 90) % 260
        d.ellipse([x, 300 + (t * 50) % 200, x + 30, 330 + (t * 50) % 200], fill="#c92a2a")
        return im
    if t < 24:  # bug: results show the previous game's name and score
        return screen("Mission Complete", ["Game: Hide and Seek", "Score: 7 / 10", "+80 XP", "", "[ Continue ]"],
                      "#7048e8", t, (180, 300 + wobble / 4))
    if t < 27:  # bug: blank white screen after Continue
        return Image.new("RGB", (W, H), "white")
    if t < 33:  # bug: loading spinner never finishes (screen does not change)
        im = screen("Home", ["Loading your progress..."], "#2b8a3e", 0)
        d = ImageDraw.Draw(im)
        d.arc([150, 300, 210, 360], 0, 270, fill="#2b8a3e", width=6)
        return im
    return screen("Home", ["Hi Asha!", "XP: 120", "(XP did not go up)"], "#2b8a3e", t, (100 + wobble, 260))


def main(out):
    ff = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                           "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-pix_fmt", "yuv420p", out],
                          stdin=subprocess.PIPE)
    for i in range(36 * FPS):
        ff.stdin.write(frame(i / FPS).tobytes())
    ff.stdin.close()
    sys.exit(ff.wait())


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo.mp4")
