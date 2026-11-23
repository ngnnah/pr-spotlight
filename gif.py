#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "pillow"]
# ///
"""Make a spotlight's looping image.gif: gif.py <repo>#<number>

Run after `spotlight.py run ... --no-post`. It asks Codex for four consecutive frames of the spotlight's
own scene in one 2x2 sheet (same prompt, same photos), cuts the frames apart at the white gutters and
joins them into spotlights/<repo>/<pr>/image.gif. `spotlight.py post` then puts the GIF in the comment.
"""
import sys
from pathlib import Path

from PIL import Image

import spotlight as s

FRAMES = """Four frames in one image: four consecutive frames of one short looping flipbook of the scene below, \
for an animated GIF. Two across and two down, each frame 3:2 landscape, thin white gutters between them and a \
white margin around them. The camera, the framing, the setting, the style and everyone's clothes stay exactly \
the same in all four frames; only the action moves, a small step per frame: frame 1 the start, frames 2 and 3 \
the action, frame 4 a step that leads straight back into frame 1, so the loop runs smoothly. Faces stay the \
same in every frame.

The scene, as one frame would be drawn:

"""


def cut(sheet: Path) -> list[Image.Image]:
    """The four frames, cut at the white gutters (rows, then columns)."""
    img = Image.open(sheet).convert("RGB")
    g = img.convert("L")
    w, h = g.size
    px = g.load()

    def white(vals):
        return sum(v > 235 for v in vals) >= 0.97 * len(vals)

    def runs(flags, min_len=40):
        out, start = [], None
        for i, f in enumerate(flags + [True]):
            if not f and start is None:
                start = i
            elif f and start is not None:
                if i - start >= min_len:
                    out.append((start, i))
                start = None
        return out

    boxes = []
    for y0, y1 in runs([white([px[x, y] for x in range(0, w, 2)]) for y in range(h)]):
        for x0, x1 in runs([white([px[x, y] for y in range(y0, y1, 2)]) for x in range(w)]):
            boxes.append((x0, y0, x1, y1))
    if len(boxes) != 4:
        sys.exit(f"found {len(boxes)} frames, not 4: {boxes}")
    fw = min(b[2] - b[0] for b in boxes)
    fh = min(b[3] - b[1] for b in boxes)
    return [img.crop((b[0], b[1], b[0] + fw, b[1] + fh)) for b in boxes]


def main() -> None:
    team = s.load_team(s.DEFAULT_TEAM)
    repo, pr = s.resolve(sys.argv[1], team)
    d = s.spot_dir(repo, pr)
    run = s.read_run(repo, pr)
    if not run.get("prompt"):
        sys.exit("no brief yet: run `spotlight.py run <pr> --no-post` first")
    photos = [s.ROOT / ph for c in run.get("cast", []) for ph in c.get("photos", [])]
    sheet = d / "frames.png"
    ok, said = s.codex(FRAMES + run["prompt"], photos, sheet)
    if not ok:
        sys.exit(f"no frames: {said[:500]}")
    frames = [f.resize((800, round(800 * f.height / f.width)), Image.LANCZOS) for f in cut(sheet)]
    frames[0].save(d / "image.gif", save_all=True, append_images=frames[1:], duration=450, loop=0, optimize=True)
    sheet.unlink()
    print(d / "image.gif", f"{(d / 'image.gif').stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
