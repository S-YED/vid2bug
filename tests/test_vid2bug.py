"""Run with: python -m unittest discover tests"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vid2bug  # noqa: E402

HAS_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


def make_video(path):
    """20s clip: moving 0-4s, black 4-7s, moving 7-10s, white 10-13s, frozen bars 13-17s, moving 17-20s."""
    src = ["testsrc=d=4", "color=black:d=3", "testsrc=d=3", "color=white:d=3", "smptebars=d=4", "testsrc=d=3"]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for s in src:
        cmd += ["-f", "lavfi", "-i", f"{s}:s=360x640:r=10"]
    cmd += ["-filter_complex", "".join(f"[{i}]" for i in range(len(src))) + f"concat=n={len(src)}:v=1:a=0[v]",
            "-map", "[v]", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True)


class Pure(unittest.TestCase):
    def test_parse_time(self):
        self.assertEqual(vid2bug.parse_time("83.5"), 83.5)
        self.assertEqual(vid2bug.parse_time("1:23"), 83)
        self.assertEqual(vid2bug.parse_time("01:02:03"), 3723)
        for bad in ("", "1:2:3:4", "abc", "-5"):
            with self.assertRaises(ValueError):
                vid2bug.parse_time(bad)

    def test_fmt_time(self):
        self.assertEqual(vid2bug.fmt_time(83.9), "01:23")
        self.assertEqual(vid2bug.fmt_time(3723), "1:02:03")

    def test_parse_logs(self):
        log = ("[blackdetect @ 0x1] black_start:4 black_end:7 black_duration:3\n"
               "[blackdetect @ 0x1] black_start:18.5\n")
        self.assertEqual(vid2bug.parse_blackdetect(log, 20), [(4.0, 7.0), (18.5, 20)])
        log = ("lavfi.freezedetect.freeze_start: 13\nlavfi.freezedetect.freeze_duration: 4\n"
               "lavfi.freezedetect.freeze_end: 17\n")
        self.assertEqual(vid2bug.parse_freezedetect(log, 20), [(13.0, 17.0)])

    def test_sheet_index(self):
        idx = vid2bug.sheet_index([Path("sheet_001.png"), Path("sheet_002.png")], 3, 2, 2, 20)
        self.assertEqual([t["time"] for t in idx[0]["tiles"]], ["00:00", "00:03", "00:06", "00:09"])
        self.assertEqual([t["time"] for t in idx[1]["tiles"]], ["00:12", "00:15", "00:18"])

    def test_parse_draft_marks_review(self):
        reply = 'Here you go:\n```json\n[{"title": "Blank screen after game", "times": ["00:05"]}]\n```'
        bugs = vid2bug.parse_draft(reply)
        self.assertEqual(bugs[0]["id"], "BUG-01")
        self.assertTrue(bugs[0]["needs_review"])
        with self.assertRaises(ValueError):
            vid2bug.parse_draft("no json here")

    def test_draft_request_has_images_and_prompt(self):
        with tempfile.TemporaryDirectory() as d:
            sheet = Path(d) / "sheet_001.png"
            sheet.write_bytes(b"\x89PNG fake")
            scan = {"duration": 20, "width": 360, "height": 640, "sheets": [], "signals": []}
            body = vid2bug.build_draft_request(scan, [sheet], "login button did nothing", "m")
        content = body["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "image")
        self.assertIn("login button did nothing", content[-1]["text"])
        self.assertIn('"id": "BUG-01"', content[-1]["text"])

    def test_draft_command_with_fake_api(self):
        class FakeResp:
            def __init__(self, body): self.body = body
            def read(self): return self.body
            def __enter__(self): return self
            def __exit__(self, *a): pass
        reply = {"content": [{"type": "text", "text": '[{"title": "White screen", "times": ["00:25"]}]'}]}
        sent = {}

        def fake_urlopen(req, timeout):
            sent["body"] = json.loads(req.data)
            return FakeResp(json.dumps(reply).encode())

        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            (out / "sheets").mkdir()
            (out / "sheets" / "sheet_001.png").write_bytes(b"png")
            (out / "scan.json").write_text(json.dumps(
                {"duration": 30, "width": 360, "height": 640, "sheets": [], "signals": []}))
            with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test"}), \
                    mock.patch("urllib.request.urlopen", fake_urlopen):
                vid2bug.main(["draft", "-o", d, "--model", "test-model"])
            bugs = json.loads((out / "bugs.draft.json").read_text())
        self.assertEqual(sent["body"]["model"], "test-model")
        self.assertTrue(bugs[0]["needs_review"])


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.video = cls.tmp / "session.mp4"
        make_video(cls.video)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_scan_finds_blank_and_frozen(self):
        out = self.tmp / "out"
        vid2bug.main(["scan", str(self.video), "-o", str(out), "--min-frozen", "3"])
        scan = json.loads((out / "scan.json").read_text())
        found = {(s["type"], round(s["start"]), round(s["end"])) for s in scan["signals"]}
        self.assertIn(("black screen", 4, 7), found)
        self.assertIn(("white/blank screen", 10, 13), found)
        self.assertIn(("frozen screen", 13, 17), found)
        self.assertEqual(len(found), 3, "black/white stretches must not be reported again as frozen")
        self.assertTrue((out / "sheets" / "sheet_001.png").exists())

    def test_report(self):
        out = self.tmp / "rep"
        bugs = [{"id": "BUG-02", "title": "Low thing", "severity": "Low", "times": ["00:18"]},
                {"id": "BUG-01", "title": "Screen goes black", "severity": "High", "times": ["00:05"],
                 "steps": ["Finish a game"], "expected": "Results screen", "actual": "Black screen for 3s",
                 "box": [10, 10, 100, 100], "needs_review": True}]
        bugs_file = self.tmp / "bugs.json"
        bugs_file.write_text(json.dumps(bugs))
        vid2bug.main(["report", str(self.video), str(bugs_file), "-o", str(out)])
        md = (out / "report.md").read_text()
        self.assertLess(md.index("## BUG-01"), md.index("## BUG-02"), "sorted by severity")
        self.assertIn("DRAFT", md)
        self.assertTrue((out / "evidence" / "BUG-01_00-05.png").exists())
        self.assertIn("Black screen for 3s", (out / "bugs.csv").read_text())


if __name__ == "__main__":
    unittest.main()
