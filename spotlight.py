#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "pillow"]
# ///
"""A spotlight for a merged PR in a team's repos: an image of the people behind it and a short caption
of lessons from the diff, posted as a PR comment and in Slack. Each team is teams/<team>.yaml.

    ./spotlight.py run <pr> [--dry-run] [--no-post] [--rebrief] [--gif]
    ./spotlight.py post <pr>          publish a spotlight already rendered with --no-post
    ./spotlight.py recaption <pr>     a new caption in the current format; image and title stay
    ./spotlight.py update <pr>        rewrite the posted PR comment with the run's current caption
    ./spotlight.py poll               PRs merged since the team's start without a spotlight, three at most
    ./spotlight.py install            the launchd job: poll every 15 minutes, at login, and once on wake
    ./spotlight.py usage              the Codex limits from the newest session log
    ./spotlight.py page               gallery-team.html: the posted ones, for a team's claude.ai page; gallery.html: every
                                      spotlight, held or posted, while no team has that page; GALLERY.md, for GitHub

<pr> is <repo>#<number> (web-app#12) or a bare number for the team's first repo. --team picks the
team file (default: the first in teams/).

Steps, each kept in spotlights/<repo>/<pr>/ so a rerun picks up where it stopped:
gather (gh) -> cast (people/*.yaml) -> seed (styles.yaml, map.yaml, log.jsonl) -> brief (claude -p:
prompt.md, caption.md) -> render (codex exec: image.jpg) -> publish (git push, gh comment, Slack).
--dry-run stops after the brief; --no-post stops after the render. Never reads ~/.codex/auth.json.

A team's `gif_every: <n>` makes every n-th spotlight it renders a looping GIF; --gif makes this one a GIF.
A GIF is one render: eight frames of the scene in a 2x4 sheet, cut apart into image.gif, with frame 5
as image.jpg.
"""

import argparse
import datetime as dt
import hashlib
import html
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml
from PIL import Image

ROOT = Path(__file__).resolve().parent


def _self_repo() -> str:
    """This repo as owner/name, from its origin remote: comments link to images stored here."""
    url = subprocess.run(["git", "-C", str(ROOT), "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
    return re.sub(r"(\.git)?$", "", re.split(r"github\.com[:/]", url)[-1]) if url else "owner/pr-spotlight"


SELF = _self_repo()
# The team when --team isn't given: the only one, or the first file in teams/.
DEFAULT_TEAM = next((f.stem for f in sorted((ROOT / "teams").glob("*.yaml"))), "example")
MAX_PER_POLL = 6  # spotlights per poll, so a night's catch-up doesn't spend the Codex window at once (3 until 2026-11-25)
MIN_LINES = 30  # feature PRs with fewer changed lines get no spotlight
TRIES = 2  # a PR that fails this many times waits for a person
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
REVIEWERS_PCT = 50  # share of feature PRs where the reviewers share the picture with the opener
MAX_REVIEWERS = 3  # on a feature PR
MAX_CAST = 7  # on a promotion: its reviewers first, then the authors whose work it ships
MAX_PHOTOS = 5  # Codex's image tool takes at most five reference images
DIFF_CHARS = 60_000
SESSIONS = Path.home() / ".codex" / "sessions"


# ---------- helpers ----------

def sh(cmd: list[str], **kw) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}… failed: {r.stderr.strip()[:500]}")
    return r.stdout


def gh_json(args: list[str]):
    return json.loads(sh(["gh", *args]))


def load_yaml(name: str):
    p = ROOT / name
    return yaml.safe_load(p.read_text()) if p.exists() else []


def load_people() -> dict[str, dict]:
    """Everyone with a people/<login>.yaml, on any team; nobody else is ever drawn."""
    people = {}
    for p in sorted((ROOT / "people").glob("*.yaml")):
        t = yaml.safe_load(p.read_text()) or {}
        if t.get("login"):
            people[t["login"].lower()] = t
    return people


def load_team(team: str) -> dict:
    p = ROOT / "teams" / f"{team}.yaml"
    if not p.exists():
        sys.exit(f"no team file {p.relative_to(ROOT)}")
    t = yaml.safe_load(p.read_text()) or {}
    t["id"] = team
    t["repos"] = [r if isinstance(r, dict) else {"repo": r} for r in t.get("repos") or []]
    return t


def resolve(ref: str, team: dict) -> tuple[str, int]:
    """'web-app#12', 'owner/web-app#12' or '12' (the team's first repo) as (owner/repo, 12)."""
    name, _, num = ref.rpartition("#")
    if not num.isdigit():
        sys.exit(f"not a PR: {ref}")
    if not name:
        return team["repos"][0]["repo"], int(num)
    for r in team["repos"]:
        if name in (r["repo"], r["repo"].split("/")[-1]):
            return r["repo"], int(num)
    owner = team["repos"][0]["repo"].split("/")[0]  # a bare repo name is in the team's first repo's owner
    return (name if "/" in name else f"{owner}/{name}"), int(num)


def short(repo: str, pr: int) -> str:
    return f"{repo.split('/')[-1]}#{pr}"


def as_text(x) -> str:
    """A YAML list item as text: an unquoted "Title: words" item parses as a dict."""
    return x if isinstance(x, str) else "; ".join(f"{k}: {v}" for k, v in x.items()) if isinstance(x, dict) else str(x)


def spot_dir(repo: str, pr: int) -> Path:
    return ROOT / "spotlights" / repo.split("/")[-1] / str(pr)


def read_run(repo: str, pr: int) -> dict:
    p = spot_dir(repo, pr) / "run.json"
    return json.loads(p.read_text()) if p.exists() else {}


def write_run(repo: str, pr: int, run: dict) -> None:
    d = spot_dir(repo, pr)
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.json").write_text(json.dumps(run, indent=2, ensure_ascii=False) + "\n")


def season(d: dt.date) -> str:
    return {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
            6: "summer", 7: "summer", 8: "summer"}.get(d.month, "autumn")


# ---------- 1. gather ----------

def gather(repo: str, pr: int, about: str = "") -> dict:
    fields = "number,title,body,url,author,baseRefName,headRefName,mergedAt,state,files,reviews,commits,additions,deletions"
    info = gh_json(["pr", "view", str(pr), "-R", repo, "--json", fields])
    try:
        diff = sh(["gh", "pr", "diff", str(pr), "-R", repo], timeout=120)
    except Exception:
        diff = ""  # too large to render; the file list still tells the story
    # Review effort, the point of the app: each reviewer's reviews and inline comments, with a few of
    # their own words so the caption can say what they caught. Bots and the opener's own notes don't count.
    opener = info["author"]["login"].lower()
    effort: dict[str, dict] = {}
    for r in info.get("reviews", []):
        l = (r.get("author") or {}).get("login", "").lower()
        if l and l != opener and r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED"):
            e = effort.setdefault(l, {"reviews": 0, "comments": 0, "said": []})
            e["reviews"] += 1
            if (r.get("body") or "").strip():
                e["said"].append(r["body"].strip()[:300])
    try:
        inline = gh_json(["api", "--paginate", f"repos/{repo}/pulls/{pr}/comments", "--jq",
                          "[.[] | {l: .user.login, b: .body}]"])
    except Exception:
        inline = []
    for c in inline if isinstance(inline, list) else []:
        l = (c.get("l") or "").lower()
        if l and l != opener and not l.endswith("[bot]"):
            e = effort.setdefault(l, {"reviews": 0, "comments": 0, "said": []})
            e["comments"] += 1
            e["said"].append((c.get("b") or "").strip()[:300])
    for e in effort.values():
        e["said"] = e["said"][:5]
    commit_authors: dict[str, int] = {}
    for c in info.get("commits", []):
        for a in c.get("authors", []):
            if a.get("login"):
                commit_authors[a["login"].lower()] = commit_authors.get(a["login"].lower(), 0) + 1
    if not about:
        about = (gh_json(["repo", "view", repo, "--json", "description"]).get("description") or "").strip()
    return {
        "repo": repo,
        "about": about,
        "number": info["number"],
        "title": info["title"],
        "body": (info.get("body") or "")[:3000],
        "url": info["url"],
        "author": info["author"]["login"].lower(),
        "base": info["baseRefName"],
        "head": info["headRefName"],
        "merged_at": info.get("mergedAt"),
        "state": info["state"],
        "files": [f["path"] for f in info.get("files", [])],
        "additions": info.get("additions"),
        "deletions": info.get("deletions"),
        "approvers": sorted({r["author"]["login"].lower() for r in info.get("reviews", [])
                             if r.get("state") == "APPROVED" and r.get("author")}),
        "reviewers": sorted({r["author"]["login"].lower() for r in info.get("reviews", [])
                             if r.get("author") and r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED")}),
        "commit_authors": commit_authors,
        "effort": effort,
        "promotion": info["baseRefName"] == "main" and info["headRefName"] == "dev",
        "diff": diff[:DIFF_CHARS],
        "diff_cut": len(diff) > DIFF_CHARS,
    }


# ---------- 2. cast ----------

def cast(info: dict, team: dict) -> list[dict]:
    """Who's in the picture, in order of importance; each with their photos or their totem."""
    def effort(l: str) -> str:
        e = info["effort"].get(l, {})
        return f"{e.get('reviews', 0)} reviews, {e.get('comments', 0)} inline comments"

    if info["promotion"]:
        # A promotion spotlights its reviewers: they check everyone's work before it reaches
        # production, and that time goes unnoticed (Nhat, 2026-11-20). Then the authors it ships.
        revs = sorted((l for l in info["approvers"] + info["reviewers"] if l != info["author"] and l in team),
                      key=lambda l: (l not in info["approvers"], -info["effort"].get(l, {}).get("comments", 0)))
        revs = list(dict.fromkeys(revs))
        authors = sorted(info["commit_authors"], key=lambda l: -info["commit_authors"][l])
        logins = list(dict.fromkeys(revs + authors + [info["author"]]))
        logins = [l for l in logins if l in team][:MAX_CAST]
        roles = {l: (f"hero: reviewed the promotion ({effort(l)})" if l in revs else
                     "ensemble: opened the promotion" if l == info["author"] else
                     "ensemble: their work ships in this promotion") for l in logins}
    else:
        # The PR's opener is the spotlight. In half of PRs its reviewers join them in the picture,
        # approvers first, drawn from the PR number so a rerun casts the same people
        # (Nhat, 2026-11-20). Bots and people outside the team never count.
        revs = [l for l in info["approvers"] + [r for r in info["reviewers"] if r not in info["approvers"]]
                if l != info["author"] and l in team][:MAX_REVIEWERS]
        h = int(hashlib.sha256(f"cameo-{info['number']}".encode()).hexdigest(), 16)
        logins = [info["author"]] + (revs if h % 100 < REVIEWERS_PCT else [])
        roles = {l: "hero: opened the PR" if l == info["author"] else
                 f"co-star: reviewed the PR ({effort(l)})" for l in logins}
    people = []
    for l in logins:
        t = team.get(l)
        if not t:
            continue  # bots and people outside the team stay out of the picture
        photos = [ROOT / p for p in (t.get("photos") or []) if (ROOT / p).is_file()]
        likeness = str(t.get("likeness", "no")).lower() in ("yes", "true")
        people.append({"login": l, "name": t.get("name", l), "role": roles[l],
                       "said": info["effort"].get(l, {}).get("said", []) if "review" in roles[l] else [],
                       "likeness": likeness and bool(photos), "photos": photos,
                       "totem": t.get("totem", ""), "emoji": t.get("emoji", ""),
                       "known_for": [as_text(x) for x in t.get("known_for") or []],
                       "jokes": [as_text(x) for x in t.get("jokes") or []]})
    # photo budget: one each in cast order, then the rest to the first person
    budget, slots = MAX_PHOTOS, {}
    for p in people:
        if p["likeness"] and budget:
            slots[p["login"]] = 1
            budget -= 1
    for p in people:
        if p["likeness"]:
            extra = min(budget, len(p["photos"]) - 1)
            slots[p["login"]] += extra
            budget -= extra
    for p in people:
        p["photos"] = p["photos"][: slots.get(p["login"], 0)]
        p["likeness"] = bool(p["photos"])
    return people


# ---------- 3. seed ----------

def recent_log(n: int = 30) -> list[dict]:
    """The last n posted spotlights, plus any briefed but not yet posted, so a batch doesn't repeat itself."""
    p = ROOT / "log.jsonl"
    posted = [json.loads(l) for l in p.read_text().splitlines()[-n:] if l.strip()] if p.exists() else []
    pending = []
    for r in (ROOT / "spotlights").glob("*/*/run.json"):
        run = json.loads(r.read_text())
        if run.get("prompt") and not run.get("posted"):
            pending.append({"repo": run.get("repo"), "pr": run["pr"], "style": run.get("style"),
                            "reference": run.get("reference")})
    return posted + pending


def seed(info: dict, people: list[dict], today: dt.date) -> dict:
    styles = load_yaml("styles.yaml") or []
    recent = recent_log()
    group = sum(p["likeness"] for p in people) > 1
    # Every style is in: a clean picture comes from the prompt's story, not the style (Nhat, 2026-11-23).
    # log.jsonl is the usage record. Least used first, so the whole shelf gets a turn before any style
    # repeats; and never a style from the last half-shelf of spotlights, so a repeat is never recent.
    log = recent_log(100_000)
    count = {}
    for r in log:
        count[r.get("style")] = count.get(r.get("style"), 0) + 1
    fits = [s for s in styles if s.get("group_ok", True) or not group] or styles
    recent_styles = {r.get("style") for r in log[-max(1, len(fits) // 2):]}
    pool = [s for s in fits if s["id"] not in recent_styles] or fits
    if pool:
        low = min(count.get(s["id"], 0) for s in pool)
        pool = [s for s in pool if count.get(s["id"], 0) == low]
    h = int(hashlib.sha256(f"{info['number']}-{today}".encode()).hexdigest(), 16)
    style = pool[h % len(pool)] if pool else {"id": "free", "name": "your choice", "prompt": "any illustrated style"}

    text = " ".join([info["title"], info["body"], " ".join(info["files"]), info["diff"]]).lower()
    hits = []
    for m in load_yaml("map.yaml") or []:
        n = sum(text.count(s.lower()) for s in m.get("match", []))
        if n:
            hits.append((n, m))
    hits.sort(key=lambda x: -x[0])
    used_refs = {r.get("reference") for r in recent}
    terms = []
    for _, m in hits[:4]:
        refs = [r for r in m.get("refs", []) if r not in used_refs] or m.get("refs", [])
        terms.append({"term": m["term"], "refs": refs, "lesson_hint": m.get("lesson_hint", "")})
    avoid = [r["reference"] for r in recent
             if r.get("reference") and (r.get("repo"), r.get("pr")) != (info["repo"], info["number"])]
    return {"style": style, "terms": terms, "date": today.isoformat(), "weekday": today.strftime("%A"),
            "season": season(today), "avoid": avoid}


# ---------- 4. brief ----------

BRIEF = """You write the brief for a "PR spotlight": one image and a short caption posted on a merged pull request
in the {repo} repo{about}. It's a fun, kind team ritual for the {team_name}, and the caption doubles as
a short lesson for teammates. Reply with ONLY a JSON object:

{{"reference": "the one reference you chose, in at most six words (e.g. The Godfather, as ducks)",
  "title": "3 to 6 words naming the image",
  "prompt": "the full image prompt",
  "summary": "what the PR does, one plain sentence, at most 18 words",
  "thanks": "for the reviewers in the cast: their names and what their review did, at most 15 words; empty string when the cast has no reviewer",
  "lesson": "one concrete lesson from this diff, at most 22 words",
  "tip": "one practice or tool note, at most 18 words"}}

## The PR
#{number} {title} ({kind}), +{additions}/-{deletions}, merged {merged_at}
Body:
{body}

Files ({nfiles}):
{files}

Diff{cut}:
{diff}

## Who is in the picture
{cast}

## Seeds
Date: {date} ({weekday}), {season}. Use the season or a holiday within a week only as a light color cue,
never as extra objects (no scattered leaves, pumpkins or decorations).
Style (use this one): {style_name}: {style_prompt}
Picture references matched from the diff (pick ONE that fits what the PR actually does; never combine two):
{terms}
Used recently, so pick something clearly different from these:
{avoid}

## The image prompt: rules
- Clean and clear above all (Nhat, 2026-11-20: "aim for clean images with clear visual messages"). The image
  has ONE visual message a teammate gets in two seconds at thumbnail size: one metaphor, one action, the
  change made visible. Write that message to yourself in one line first, then cut everything that doesn't
  serve it.
- Open with the form, the style and who is in it: one landscape image, 3:2, one single scene, never panels.
- Composition: the main figure or two large and central; at least a third of the image calm, empty space;
  a plain or softly graded background, or one simple setting element; at most three props, each part of the
  message; a limited palette of three or four colors. No crowds, no background figures, no busy patterns,
  no ornament, no small scattered details.
- {photo_rule}
- People without photos appear only as their totem, a character, never as a real person's face. Give each
  totem its emoji's spirit.
- Eyes open in every face; no big cartoon eyes.
- Reviewers are why this app exists: their time checking others' work goes unnoticed. Whoever the cast
  marks as reviewing gets a prominent, admiring place in the scene, doing the reviewing in the story's terms
  (the lookout who spots the reef, the inspector who catches the crack). On a promotion, the reviewers are
  the large figures; everyone else is small, simple and grouped as one shape.
- At most one inside joke from the team notes, and only one that's there; don't invent jokes about people.
- No text, letters, numbers, logos, watermarks, borders or captions anywhere in the image.
- Only the cast above appears, no one else.
- Kind and work-safe: nobody looks foolish, nothing about looks, age, body, gender, ethnicity or religion.
- Name films, myths and artists outright; ask for the image's own take on them, not a copy of any frame.

## The caption fields: rules
They are laid out as a bold line and three short bullets, so each is read at a glance (Nhat, 2026-11-20:
"more concise"). Every field stands alone; no field repeats another. Markdown allowed for `code` and links.
- summary: what the PR does, in words a teammate outside it gets. No file names here.
- thanks: when the cast has reviewers, their first names and what their review did: what they caught or
  asked, from their own words above, or else "for the review" (with the comment count if it's more than a
  few). Empty when nobody reviewed.
- lesson: the one thing a teammate should take from this diff, naming the file, function or pattern.
- tip: one practice or tool note about a tool this PR uses. A claim about something new in a tool (a
  release, a feature) only with a link to its source that you checked with web search; otherwise a timeless
  practice.
- Plain, short words; no hype words (powerful, robust, seamless, game-changer), no exclamation marks, no emoji.
"""


def brief(info: dict, people: list[dict], sd: dict, team_name: str = "team") -> dict:
    n = 0
    lines = []
    for p in people:
        if p["likeness"]:
            nums = list(range(n + 1, n + 1 + len(p["photos"])))
            n = nums[-1]
            who = f"photo{'s' if len(nums) > 1 else ''} {', '.join(map(str, nums))}"
            look = f"drawn from {who} (their likeness)"
        else:
            look = f"appears as their totem: {p['totem']} {p['emoji']}"
        notes = f" Known for: {', '.join(p['known_for'])}." if p["known_for"] else ""
        jokes = f" Team notes: {' / '.join(p['jokes'])}" if p["jokes"] else ""
        said = (" Their review said: " + " / ".join(" ".join(x.split())[:200] for x in p["said"] if x)) if p.get("said") else ""
        lines.append(f"- {p['name']}, {p['role']}, {look}.{notes}{jokes}{said}")
    if n:
        photo_rule = (f"Photos 1 to {n} are attached in the order listed in the cast. Say what each photo is for, "
                      "use them only for that person's likeness and leave out everything else in them "
                      "(background, clothes, other people, lighting). Take each face feature by feature: eyes, "
                      "nose and lips, face shape, hair, glasses or facial hair as the photo shows them. Keep "
                      "faces large enough to read.")
    else:
        photo_rule = "No photos are attached: nobody's real face appears."
    terms = "\n".join(f"- {t['term']}: " + " | ".join(t["refs"]) + (f" (lesson hint: {t['lesson_hint']})" if t["lesson_hint"] else "")
                      for t in sd["terms"]) or "- none matched: pick a reference that fits the PR"
    text = BRIEF.format(
        repo=info["repo"].split("/")[-1], about=f" ({info['about']})" if info.get("about") else "",
        team_name=team_name, number=info["number"], title=info["title"],
        kind="dev to main promotion" if info["promotion"] else f"{info['head']} into {info['base']}",
        additions=info["additions"], deletions=info["deletions"], merged_at=info["merged_at"] or "not yet",
        body=info["body"] or "(none)", nfiles=len(info["files"]), files="\n".join(info["files"][:120]),
        cut=" (cut short)" if info["diff_cut"] else "", diff=info["diff"] or "(unavailable)",
        cast="\n".join(lines) or "- nobody from the team: show the work itself",
        date=sd["date"], weekday=sd["weekday"], season=sd["season"],
        style_name=sd["style"]["name"], style_prompt=sd["style"]["prompt"], terms=terms, photo_rule=photo_rule,
        avoid="\n".join(f"- {a}" for a in sd["avoid"]) or "- nothing yet")
    out = sh(["claude", "-p", "--output-format", "json", "--allowedTools", "WebSearch", "WebFetch"],
             input=text, timeout=900)
    result = json.loads(out).get("result", "")
    m = re.search(r"\{.*\}", result, re.S)
    if not m:
        raise RuntimeError(f"brief: no JSON in Claude's reply: {result[:300]}")
    b = json.loads(m.group(0))
    for k in ("reference", "title", "prompt", "summary", "lesson", "tip"):
        if not b.get(k):
            raise RuntimeError(f"brief: missing {k}")
    b["caption"] = caption_md(b)
    return b


def caption_md(b: dict) -> str:
    """The caption as the comment shows it: the summary in bold, then thanks, lesson and tip as bullets."""
    rows = ([f"- 🙏 **Thanks** {b['thanks'].strip()}"] if (b.get("thanks") or "").strip() else []) + [
        f"- 💡 **Lesson** {b['lesson'].strip()}", f"- 🛠️ **Tip** {b['tip'].strip()}"]
    return f"**{b['summary'].strip()}**\n\n" + "\n".join(rows)


# ---------- 5. render ----------

INSTRUCTION = """Use your built-in image generation tool to make exactly one image, and nothing else.

- Pass the prompt between the two marker lines to the image tool word for word: don't reword, shorten, summarize or add to it.
- {refs}
- Save the one generated image as a PNG at {out}.
- Generate one image only; don't retry or make variations.
- Then reply with two lines: the saved file's path, and its pixel size.

=== PROMPT START ===
{prompt}
=== PROMPT END ===
"""


def codex(prompt: str, photos: list[Path], out: Path) -> tuple[bool, str]:
    refs = (f"Give the tool the {len(photos)} attached images as reference images, in the order attached; "
            f"the prompt calls them photo 1 to photo {len(photos)} in that order." if photos
            else "There are no reference images.")
    tmp = Path(tempfile.mkdtemp(prefix="spotlight-"))
    cmd = ["codex", "exec", INSTRUCTION.format(refs=refs, out=out, prompt=prompt), "--skip-git-repo-check",
           "-s", "workspace-write", "-C", str(out.parent)]
    for p in photos:
        cmd += ["-i", str(p)]
    cmd += ["--json", "-o", str(tmp / "last.txt")]
    with (tmp / "events.jsonl").open("w") as ev, (tmp / "stderr.txt").open("w") as err:
        # stdin must be closed: with it open, codex exec waits for more input forever.
        subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=ev, stderr=err, timeout=900)
    said = (tmp / "last.txt").read_text().strip() if (tmp / "last.txt").exists() else ""
    errors = []
    for line in (tmp / "events.jsonl").read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") in ("error", "turn.failed"):
            errors.append(e.get("message") or (e.get("error") or {}).get("message") or "")
    return out.exists(), "; ".join(filter(None, errors)) or said or f"no image; logs in {tmp}"


N_FRAMES = 8  # four looked too still (Nhat, 2026-11-25)
FRAMES = """Eight frames in one tall portrait image: eight consecutive frames of one short looping flipbook of \
the scene below, for an animated GIF. Two across and four down, read left to right then top to bottom, each frame \
3:2 landscape, thin white gutters between them and a white margin around them. The camera, the framing, the \
setting, the style and everyone's clothes stay exactly the same in all eight frames; only the action moves, a \
small, clear step per frame: frame 1 the start, frames 2 to 7 the action building and peaking, frame 8 a step \
that leads straight back into frame 1, so the loop runs smoothly. Faces stay the same in every frame.

The scene, as one frame would be drawn:

"""


def wants_gif(team: dict) -> bool:
    """Every `gif_every`-th render of the team is a GIF (Nhat, 2026-11-25: "a gif for every 5 image gens")."""
    every = int(team.get("gif_every") or 0)
    if not every:
        return False
    done = sum("render_s" in json.loads(f.read_text())
               for r in team["repos"] for f in (ROOT / "spotlights" / r["repo"].split("/")[-1]).glob("*/run.json"))
    return (done + 1) % every == 0


def cut(sheet: Path) -> list[Image.Image]:
    """The frames of a sheet in reading order, cut at the white gutters (rows, then columns)."""
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
    if len(boxes) != N_FRAMES:
        raise ValueError(f"found {len(boxes)} frames, not {N_FRAMES}")
    fw = min(b[2] - b[0] for b in boxes)
    fh = min(b[3] - b[1] for b in boxes)
    return [img.crop((b[0], b[1], b[0] + fw, b[1] + fh)) for b in boxes]


def render(repo: str, pr: int, b: dict, people: list[dict], gif: bool = False) -> Path:
    """image.jpg; with gif, one frames sheet instead of the still: image.gif from its frames and
    frame 5 as image.jpg. A sheet that doesn't cut into N_FRAMES frames falls back to a still."""
    d = spot_dir(repo, pr)
    png, jpg = d / "image.png", d / "image.jpg"
    photos = [ph for p in people for ph in p["photos"]]
    st = subprocess.run(["codex", "login", "status"], capture_output=True, text=True)
    if "ChatGPT" not in st.stdout + st.stderr:
        raise RuntimeError("Codex isn't signed in with ChatGPT; run: codex login")

    def draw(prompt: str) -> None:
        ok, said = codex(prompt, photos, png)
        if not ok and re.search(r"similar|policy|third.party|blocked", said, re.I):
            ok, said = codex(prompt + ("\n\nDraw every referenced character, place and object in this image's "
                                       "own style rather than as it looks in the original work."), photos, png)
        if not ok:
            raise RuntimeError(f"render: {said[:500]}")

    if gif:
        draw(FRAMES + b["prompt"])
        try:
            frames = cut(png)
        except ValueError as e:
            print(f"  gif: {e}; rendering a still instead", file=sys.stderr)
            png.unlink()
        else:
            frames[N_FRAMES // 2].save(jpg, quality=85)  # the still: mid-action, where the scene says most
            w = min(800, frames[0].width)  # never upscale: it only softens the frames
            frames = [f.resize((w, round(w * f.height / f.width)), Image.LANCZOS) for f in frames]
            frames[0].save(d / "image.gif", save_all=True, append_images=frames[1:], duration=300, loop=0,
                           optimize=True)
            png.unlink()
            return jpg
    draw(b["prompt"])
    sh(["sips", "-Z", "1600", "-s", "format", "jpeg", "-s", "formatOptions", "85", str(png), "--out", str(jpg)])
    png.unlink()
    return jpg


# ---------- 6. publish ----------

def comment_body(repo: str, pr: int, run: dict) -> str:
    # a looping image.gif, when one was made for the spotlight, goes in place of the still (Nhat, 2026-11-23)
    name = "image.gif" if (spot_dir(repo, pr) / "image.gif").exists() else "image.jpg"
    url = f"https://github.com/{SELF}/blob/main/{spot_dir(repo, pr).relative_to(ROOT)}/{name}?raw=true"
    return (f"### 🔦 PR spotlight: {run['title']}\n\n![{run['title']}]({url})\n\n{run['caption']}\n\n"
            f"<sub>{run['style_name']} · {run['reference']} · {load_team(run.get('team') or DEFAULT_TEAM).get('footer', 'Posted by pr-spotlight')} · "
            f"[Add your photo or a joke](https://github.com/{SELF}/tree/main/people)</sub>")


def git_push(repo: str, pr: int, msg: str) -> None:
    # Commit first, then rebase onto origin: a pull refuses while run.json has unstaged changes.
    sh(["git", "-C", str(ROOT), "add", str(spot_dir(repo, pr).relative_to(ROOT)), "log.jsonl"]
       + (["GALLERY.md"] if (ROOT / "GALLERY.md").exists() else []))
    if subprocess.run(["git", "-C", str(ROOT), "diff", "--cached", "--quiet"]).returncode:
        sh(["git", "-C", str(ROOT), "commit", "-q", "-m",
            f"{msg}\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"])
    # --autostash: other spotlights rendered and held leave uncommitted files, which would block the rebase.
    sh(["git", "-C", str(ROOT), "pull", "-q", "--rebase", "--autostash", "origin", "main"])
    sh(["git", "-C", str(ROOT), "push", "-q", "origin", "main"])


def slack(run: dict, comment_url: str, team: dict) -> str:
    """One line: the spotlight's title linked to the PR comment, and Slack tags for the cast. Only the
    team's slack_tag logins are tagged for now (Nhat, 2026-11-20)."""
    people = load_people()
    tag = {l.lower() for l in team.get("slack_tag") or []}
    tags = " ".join(f"<@{people[c['login']]['slack_id']}>" for c in run["cast"]
                    if c["login"] in tag and people.get(c["login"], {}).get("slack_id"))
    text = (f"🔦 PR spotlight on {short(run['repo'], run['pr'])}: <{comment_url}|{run['title']}>"
            + (f" {tags}" if tags else ""))
    ask = (f"Send exactly this message to the Slack channel {team['slack_channel']} with slack_send_message, unchanged, "
           f"then reply with only the message's permalink.\n\n=== MESSAGE ===\n{text}\n=== END ===")
    out = sh(["claude", "-p", "--output-format", "json", "--allowedTools", "mcp__claude_ai_Slack__slack_send_message"],
             input=ask, timeout=300)
    return json.loads(out).get("result", "").strip()


def publish(repo: str, pr: int, run: dict, team: dict) -> dict:
    if not (spot_dir(repo, pr) / "image.jpg").exists():
        raise RuntimeError("publish: no image.jpg; render first")
    logged = any((r.get("repo"), r.get("pr")) == (repo, pr) for r in recent_log(10_000) if r.get("date"))
    with (ROOT / "log.jsonl").open("a") as f:
        if not logged:  # a retried post logs once
            f.write(json.dumps({"repo": repo, "pr": pr, "date": run["date"], "style": run["style"], "reference": run["reference"]},
                               ensure_ascii=False) + "\n")
    run["posted"] = {"at": dt.datetime.now().isoformat(timespec="seconds")}
    write_run(repo, pr, run)
    git_push(repo, pr, f"Spotlight for {short(repo, pr)}: {run['title']}")
    url = sh(["gh", "pr", "comment", str(pr), "-R", repo, "--body", comment_body(repo, pr, run)]).strip()
    run["posted"]["comment"] = url
    try:
        if team.get("slack_channel"):  # empty: no Slack post
            run["posted"]["slack"] = slack(run, url, team)
    except Exception as e:  # the PR comment is the main post; Slack is a bonus
        run["posted"]["slack_error"] = str(e)[:300]
    write_run(repo, pr, run)
    page()  # gallery.html and GALLERY.md show it as posted
    git_push(repo, pr, f"Spotlight {short(repo, pr)}: posted")
    return run


# ---------- commands ----------

def about_of(repo: str, team: dict) -> str:
    return next((r.get("about", "") for r in team["repos"] if r["repo"] == repo), "")


def run_one(repo: str, pr: int, team: dict, dry: bool = False, no_post: bool = False, rebrief: bool = False,
            gif: bool = False) -> dict:
    run = read_run(repo, pr)
    tag = short(repo, pr)
    if run.get("posted"):
        print(f"{tag}: posted already: {run['posted'].get('comment')}")
        return run
    d = spot_dir(repo, pr)
    try:
        if gif and not (d / "image.gif").exists():
            (d / "image.jpg").unlink(missing_ok=True)  # --gif on a held still: render it again as a GIF
        if rebrief or not run.get("prompt"):
            (d / "image.jpg").unlink(missing_ok=True)  # a new brief needs a new image
            (d / "image.gif").unlink(missing_ok=True)
            info = gather(repo, pr, about_of(repo, team))
            people = cast(info, load_people())
            today = dt.date.today()
            sd = seed(info, people, today)
            b = brief(info, people, sd, team.get("name", "team"))
            run = {"repo": repo, "pr": pr, "pr_title": info["title"], "pr_url": info["url"],
                   "promotion": info["promotion"], "team": team["id"],
                   "date": today.isoformat(), "style": sd["style"]["id"], "style_name": sd["style"]["name"],
                   "terms": [t["term"] for t in sd["terms"]], "reference": b["reference"], "title": b["title"],
                   "cast": [{"login": p["login"], "name": p["name"], "role": p["role"], "likeness": p["likeness"],
                             "photos": [str(ph.relative_to(ROOT)) for ph in p["photos"]]} for p in people],
                   "prompt": b["prompt"], "caption": b["caption"],
                   "caption_fields": {k: b.get(k, "") for k in ("summary", "thanks", "lesson", "tip")}}
            write_run(repo, pr, run)
            (d / "prompt.md").write_text(b["prompt"] + "\n")
            (d / "caption.md").write_text(b["caption"] + "\n")
        print(f"{tag} {run['pr_title']}\n  style: {run['style_name']}\n  reference: {run['reference']}\n"
              f"  cast: {', '.join(c['name'] + (' (photo)' if c['likeness'] else '') for c in run['cast'])}\n"
              f"  caption: {run['caption']}\n  prompt: {d / 'prompt.md'}")
        if dry:
            return run
        if not (d / "image.jpg").exists():
            people = [{"photos": [ROOT / p for p in c["photos"]]} for c in run["cast"]]
            start = time.time()
            render(repo, pr, run, people, gif or wants_gif(team))
            run["render_s"] = round(time.time() - start)
            run["gif"] = (d / "image.gif").exists()
            run.pop("error", None)
            write_run(repo, pr, run)
        print(f"  image: {d / 'image.jpg'}")
        if no_post:
            return run
        run = publish(repo, pr, run, team)
        print(f"  comment: {run['posted']['comment']}\n  slack: {run['posted'].get('slack') or run['posted'].get('slack_error')}")
        return run
    except Exception as e:
        run["error"] = str(e)[:1000]
        write_run(repo, pr, run)
        print(f"{tag} failed: {e}", file=sys.stderr)
        return run


def recaption(repo: str, pr: int, team: dict) -> dict:
    """A new caption in the current format for a spotlight already briefed; title, prompt and image stay."""
    run = read_run(repo, pr)
    info = gather(repo, pr, about_of(repo, team))
    people = cast(info, load_people())
    b = brief(info, people, seed(info, people, dt.date.today()), team.get("name", "team"))
    run["caption"] = b["caption"]
    run["caption_fields"] = {k: b.get(k, "") for k in ("summary", "thanks", "lesson", "tip")}
    write_run(repo, pr, run)
    (spot_dir(repo, pr) / "caption.md").write_text(b["caption"] + "\n")
    print(f"{short(repo, pr)}\n{comment_body(repo, pr, run)}\n")
    return run


def update_comment(repo: str, pr: int) -> str:
    """Rewrite a posted spotlight comment with the run's current title, caption, image link and footer."""
    run = read_run(repo, pr)
    url = run.get("posted", {}).get("comment", "")
    cid = url.rsplit("issuecomment-", 1)[-1]
    if not cid.isdigit():
        raise RuntimeError(f"{short(repo, pr)}: no posted comment to update")
    sh(["gh", "api", "-X", "PATCH", f"repos/{repo}/issues/comments/{cid}", "-f", f"body={comment_body(repo, pr, run)}"])
    run["posted"]["updated"] = dt.datetime.now().isoformat(timespec="seconds")
    write_run(repo, pr, run)
    return url


def skip_reason(repo: str, pr: int) -> str:
    """Why a merged PR gets no spotlight, or '' when it gets one. Promotions (dev into main) always get one."""
    v = gh_json(["pr", "view", str(pr), "-R", repo, "--json", "baseRefName,headRefName,additions,deletions,files"])
    if v["baseRefName"] == "main" and v["headRefName"] == "dev":
        return ""
    if v["additions"] + v["deletions"] < MIN_LINES:
        return f"under {MIN_LINES} changed lines"
    if all(f["path"].startswith(".github/") or f["path"].endswith(".md") for f in v["files"]):
        return "CI or docs only"
    return ""


def notify(text: str) -> None:
    subprocess.run(["osascript", "-e", f"display notification {json.dumps(text)} with title \"PR spotlight\""])


def poll(team: dict) -> None:
    """Every PR merged into a team repo's default branch, dev or main since the team's start, without a spotlight,
    oldest first, at most MAX_PER_POLL per call."""
    sys.stdout.reconfigure(line_buffering=True)  # launchd's log file: each line as it happens, not at exit
    print(f"--- poll {team['id']} {dt.datetime.now():%Y-%m-%d %H:%M}")
    auto = bool(team.get("auto_post"))
    start = str(team.get("start") or dt.date.today()).replace(" ", "T").replace("+00:00", "Z")
    todo = []
    for r in team["repos"]:
        repo = r["repo"]
        default = gh_json(["repo", "view", repo, "--json", "defaultBranchRef"])["defaultBranchRef"]["name"]
        prs = gh_json(["pr", "list", "-R", repo, "--state", "merged", "--limit", "50",
                       "--search", f"merged:>={start}", "--json", "number,baseRefName,mergedAt"])
        # main too: in a repo whose default branch is dev, promotions into main were missed (Nhat, 2026-11-25)
        todo += [(p["mergedAt"], repo, p["number"]) for p in prs if p["baseRefName"] in (default, "dev", "main")]
    done = posted = 0
    for _, repo, n in sorted(todo):
        if done >= MAX_PER_POLL:
            break
        r = read_run(repo, n)
        tag = short(repo, n)
        if r.get("posted") or r.get("tries", 0) >= TRIES:
            continue
        if not auto and (spot_dir(repo, n) / "image.jpg").exists():
            continue  # rendered and held for a person to post
        if not r and (why := skip_reason(repo, n)):
            print(f"{tag}: skipped, {why}")
            continue
        done += 1
        r = run_one(repo, n, team, no_post=not auto)  # posts when auto_post is on
        if r.get("error"):
            r["tries"] = r.get("tries", 0) + 1
            write_run(repo, n, r)
            notify(f"{tag} failed ({r['tries']} of {TRIES}): {r['error'][:80]}")
        elif not auto:
            notify(f"{tag} rendered: {r['title']}. Post it with ./spotlight.py post {tag} --team {team['id']}")
        if (r.get("posted") or {}).get("comment"):
            posted += 1
    page()
    if posted:
        print_share(team)


def print_share(team: dict) -> None:
    """The team's claude.ai page gets the new spotlights; a failure there never stops a post."""
    try:
        if said := share_gallery(team):
            print(f"gallery page: {said[:300]}")
    except Exception as e:
        print(f"gallery page failed: {str(e)[:300]}")


def _md(s: str) -> str:
    """The caption's markdown, as the PR comment shows it: bold, code and bullets."""
    out, items = [], []
    for line in html.escape(s).splitlines():
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"`(.+?)`", r"<code>\1</code>", line)
        if line.startswith("- "):
            items.append(f"<li>{line[2:]}</li>")
            continue
        if items:
            out.append("<ul>" + "".join(items) + "</ul>")
            items = []
        if line.strip():
            out.append(f"<p>{line}</p>")
    if items:
        out.append("<ul>" + "".join(items) + "</ul>")
    return "".join(out)


def gallery_md(runs: list) -> None:
    """GALLERY.md, for GitHub: the posted spotlights, newest first, three across. Held pictures aren't pushed yet."""
    posted = [(d, r) for d, r in runs if (r.get("posted") or {}).get("comment")]
    cells = []
    for d, r in posted:
        img = next((d / n for n in ("image.gif", "image.jpg") if (d / n).exists()), None)
        if not img:
            continue
        title = html.escape(r.get("title", ""))
        cells.append(f'<td width="33%" valign="top"><a href="{r["posted"]["comment"]}"><img src="{img.relative_to(ROOT)}" '
                     f'alt="{title}"></a><br><b>{short(r["repo"], r["pr"])} · {title}</b><br>'
                     f'<sub>{html.escape(r.get("style_name", ""))} · {r.get("date", "")}</sub></td>')
    rows = ["<tr>\n" + "\n".join(cells[i:i + 3]) + "\n</tr>" for i in range(0, len(cells), 3)]
    (ROOT / "GALLERY.md").write_text(
        f"# 🔦 Posted spotlights\n\n{len(cells)} so far, newest first. Click a picture for its PR comment.\n\n"
        "<table>\n" + "\n".join(rows) + "\n</table>\n")


def page() -> Path:
    """gallery-team.html (posted spotlights, for the team's claude.ai page) and, while no team has one,
    gallery.html (every spotlight, held or posted); filters, views, grouping and sorting run in the page.
    poll and each post rewrite them, and GALLERY.md with them."""
    runs = []
    for f in (ROOT / "spotlights").glob("*/*/run.json"):
        r = json.loads(f.read_text())
        if r.get("prompt"):
            runs.append((f.parent, r))
    runs.sort(key=lambda x: (x[1].get("posted", {}).get("at") or x[1].get("date") or "", x[1].get("pr", 0)), reverse=True)
    items = []
    for d, r in runs:
        img = next((d / n for n in ("image.gif", "image.jpg") if (d / n).exists()), None)
        posted = r.get("posted") or {}
        state = "posted" if posted.get("comment") else "failed" if r.get("error") and not img else "held" if img else ""
        if not state:
            continue
        items.append({
            "repo": r["repo"].split("/")[-1], "team": r.get("team") or "", "pr": r["pr"], "tag": short(r["repo"], r["pr"]), "state": state,
            "date": r.get("date", ""), "at": posted.get("at") or r.get("date", ""),
            "title": r.get("title", ""), "prTitle": r.get("pr_title", ""), "prUrl": r.get("pr_url", ""),
            "caption": _md(r.get("caption", "")), "style": r.get("style_name", ""), "reference": r.get("reference", ""),
            "cast": ", ".join(c["name"] + (" 📷" if c.get("likeness") else "") for c in r.get("cast", [])),
            "img": str(img.relative_to(ROOT)) if img else "", "gif": bool(img and img.suffix == ".gif"),
            "promotion": bool(r.get("promotion")), "comment": posted.get("comment", ""), "slack": posted.get("slack", ""),
            "error": (r.get("error") or "")[:200]})
    gallery_md(runs)
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    def fill(its: list, shared: bool) -> str:
        data = json.dumps(its, ensure_ascii=False).replace("</", "<\\/")
        return (PAGE_HTML.replace("__NOW__", now).replace("__HOW__", how_html())
                .replace("__SHARED__", "true" if shared else "false").replace("__DATA__", data))

    # the team's copy on claude.ai: posted spotlights only; the Artifact tool wraps it in the document skeleton
    team_page = ROOT / "gallery-team.html"
    team_page.write_text(fill([i for i in items if i["state"] == "posted"], True))
    # gallery.html, with held and failed ones and their commands, only while no team has a claude.ai page
    # (Nhat, 2026-11-25); failures still show in the poll log and a Mac notification
    out = ROOT / "gallery.html"
    if any(load_team(f.stem).get("gallery_url") for f in (ROOT / "teams").glob("*.yaml")):
        out.unlink(missing_ok=True)
        return team_page
    out.write_text('<!doctype html><html lang="en"><meta charset="utf-8">'
                   '<meta name="viewport" content="width=device-width, initial-scale=1">\n' + fill(items, False))
    return out


def share_gallery(team: dict) -> str:
    """Republish gallery-team.html to the team's claude.ai page (gallery_url) with the images it doesn't have yet,
    through claude -p and its Artifact tool. gallery-team.files lists the images already there."""
    url = team.get("gallery_url")
    if not url:
        return ""
    page()
    sent_f = ROOT / "gallery-team.files"
    sent = set(sent_f.read_text().split()) if sent_f.exists() else set()
    need = re.findall(r'"img": ?"([^"]+)"', (ROOT / "gallery-team.html").read_text())
    new = [p for p in need if p not in sent]
    ask = (f"Update the claude.ai artifact {url}. First call the Artifact tool with action read and url {url}. "
           f"Then call it with action publish, url {url}, file_path {ROOT / 'gallery-team.html'}"
           + (f", and files {json.dumps([{'path': p} for p in new])}" if new else "")
           + ". Change nothing else. Reply with only the word ok, or the error.")
    out = sh(["claude", "-p", "--output-format", "json", "--allowedTools", "Artifact"], input=ask, cwd=str(ROOT), timeout=600)
    said = json.loads(out).get("result", "").strip()
    if said.lower().startswith("ok"):
        sent_f.write_text("\n".join(sorted(sent | set(new))) + "\n")
    return said


def how_html() -> str:
    """The page's folded "How it works" panel, read from teams/*.yaml and the rules above, so it can't drift."""
    e = html.escape
    rows = []
    for f in sorted((ROOT / "teams").glob("*.yaml")):
        t = load_team(f.stem)
        repos = ", ".join(f'<a href="https://github.com/{e(r["repo"])}">{e(r["repo"].split("/")[-1])}</a>' for r in t["repos"])
        every = int(t.get("gif_every") or 0)
        bits = [f"repos {repos}", f"PRs merged since {e(str(t.get('start', ''))[:10])}",
                f"{e(t.get('runner') or 'a Mac')} checks every 15 minutes and makes at most {MAX_PER_POLL} spotlights "
                f"each time, oldest merge first",
                "posts on its own" if t.get("auto_post") else "each spotlight waits for a person to post it",
                f"one spotlight in {every} is a looping GIF" if every else "no GIFs",
                "a line in Slack for each" if t.get("slack_channel") else "no Slack line",
                f'<a href="https://github.com/{SELF}/blob/main/teams/{e(f.name)}">settings</a>']
        rows.append(f"<li><b>{e(t.get('name', f.stem))}</b>: {'; '.join(bits)}.</li>")
    return (f"<ul>{''.join(rows)}</ul><ul>"
            f"<li>Every merged PR into dev, main or the default branch gets one, except PRs under {MIN_LINES} changed lines "
            f"and PRs that only touch CI or docs. Promotions from dev to main always get one.</li>"
            f"<li>A promotion stars its reviewers. A feature PR stars its opener; in about {REVIEWERS_PCT}% of them, "
            f"up to {MAX_REVIEWERS} reviewers join the picture.</li>"
            f"<li>Faces only for people who said yes; everyone else appears as their totem. "
            f'<a href="https://github.com/{SELF}/tree/main/people">Add your photo, totem or a joke</a>.</li></ul>')


PAGE_HTML = r"""<title>PR spotlights</title>
<style>
:root{--bg:#f7f8f4;--card:#fff;--ink:#22302a;--dim:#5d6b64;--edge:#dfe5dc;--held:#fdf1c7;--held-ink:#7a5d00;--posted:#dcefe2;--posted-ink:#256a3e;--failed:#fbe0e0;--failed-ink:#9a2e2e;--gif:#e3ecfb;--gif-ink:#2d5a9e;--link:#2f6db5;--code:#eef2f7}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#151a18;--card:#1e2522;--ink:#e6ece8;--dim:#9fb0a7;--edge:#2c3631;--held:#4a3f17;--held-ink:#f5dc8a;--posted:#1f3d2b;--posted-ink:#9fe0b4;--failed:#4a2222;--failed-ink:#f3b0b0;--gif:#1f2f4a;--gif-ink:#a9c6f2;--link:#8cb8ee;--code:#26302d;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#151a18;--card:#1e2522;--ink:#e6ece8;--dim:#9fb0a7;--edge:#2c3631;--held:#4a3f17;--held-ink:#f5dc8a;--posted:#1f3d2b;--posted-ink:#9fe0b4;--failed:#4a2222;--failed-ink:#f3b0b0;--gif:#1f2f4a;--gif-ink:#a9c6f2;--link:#8cb8ee;--code:#26302d;color-scheme:dark}
.how{max-width:1200px;margin:32px auto 0;border-top:1px solid var(--edge)}.how ul{margin:6px 0 10px;padding-left:20px;max-width:75ch}.how li{margin:4px 0}
*{box-sizing:border-box}[hidden]{display:none!important}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,system-ui,sans-serif;padding:24px max(16px,env(safe-area-inset-left))}
header,main{max-width:1200px;margin:0 auto}h1{font-size:24px;margin:0 0 6px}.sub{color:var(--dim);margin:0 0 12px}
.bar{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;margin-bottom:18px}.grp{display:flex;gap:6px;align-items:center;flex-wrap:wrap}.grp>span{color:var(--dim);font-size:13px}
.bar button{font:inherit;font-size:14px;border:1px solid var(--edge);background:var(--card);color:var(--ink);border-radius:999px;padding:5px 12px;cursor:pointer}
.bar button[aria-pressed=true]{background:var(--ink);color:var(--bg)}#q{flex:1 1 100%;font:inherit;font-size:16px;padding:9px 14px;border:1px solid var(--edge);border-radius:10px;background:var(--card);color:var(--ink)}mark{background:var(--held);color:inherit;border-radius:3px}.bar select{font:inherit;font-size:14px;border:1px solid var(--edge);background:var(--card);color:var(--ink);border-radius:8px;padding:5px 8px}
.gh{font-size:16px;margin:22px 0 10px;color:var(--dim);cursor:pointer;list-style:none;display:flex;align-items:center;gap:8px}.gh::-webkit-details-marker{display:none}.gh::before{content:'▸';color:var(--dim);transition:transform .15s}details[open]>.gh::before{transform:rotate(90deg)}.gh b{color:var(--ink)}.gd>.grid,.gd>.list{margin-bottom:8px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:18px}
.card{background:var(--card);border:1px solid var(--edge);border-radius:14px;overflow:hidden;display:flex;flex-direction:column;min-width:0;position:relative}
.card .pic{position:relative}.card img{width:100%;display:block;aspect-ratio:3/2;object-fit:cover;cursor:zoom-in}.card img.big{aspect-ratio:auto;object-fit:contain;cursor:zoom-out}
.gtag{position:absolute;left:10px;top:10px;background:var(--gif);color:var(--gif-ink);font-weight:800;font-size:12px;border-radius:999px;padding:2px 9px}
.body{padding:14px 16px 16px;display:flex;flex-direction:column;gap:6px;flex:1;min-width:0}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:13px}.date{color:var(--dim);margin-left:auto}
.pill{border-radius:999px;padding:1px 9px;font-weight:700;font-size:12px}.pill.held{background:var(--held);color:var(--held-ink)}.pill.posted{background:var(--posted);color:var(--posted-ink)}.pill.failed{background:var(--failed);color:var(--failed-ink)}.pill.promo{background:var(--gif);color:var(--gif-ink)}
a{color:var(--link)}.pr{font-weight:600}h2{font-size:18px;margin:2px 0 0}.prt{color:var(--dim);margin:0;font-size:13px}
.cap p{margin:4px 0}.cap ul{margin:4px 0;padding-left:18px}.cap li{margin:2px 0}code{background:var(--code);border-radius:4px;padding:0 4px;font-size:13px;overflow-wrap:anywhere}
.meta{color:var(--dim);font-size:12.5px;margin:6px 0 4px}.links{display:flex;gap:14px;flex-wrap:wrap;font-weight:600;font-size:14px}.cmd{cursor:copy;font-weight:400}.err{color:var(--failed-ink);font-size:13px}
.list .card{flex-direction:row;margin-bottom:12px}.list .card .pic{flex:0 0 240px}.list .card img{height:100%;min-height:160px}
.list .cap{display:none}.list .card.open .cap{display:block}.list .more{display:inline}.more{display:none;font-size:13px;color:var(--link);cursor:pointer;background:none;border:0;padding:0;font:inherit;font-size:13px;align-self:flex-start}
@media (max-width:640px){.list .card{flex-direction:column}.list .card .pic{flex:none}}
.empty{color:var(--dim);padding:40px 0;text-align:center}
.count{margin:-6px 0 14px;font-size:14px;font-weight:600;color:var(--dim);font-variant-numeric:tabular-nums}
</style>
<header><h1>🔦 PR spotlights</h1><p class="sub" id="sub"></p>
<div class="bar">
 <input id="q" type="search" placeholder="Search PR, title, caption, style, people…  ( / )" autocomplete="off">
 <div class="grp" id="state"><button data-v="all">All</button><button data-v="held">Held</button><button data-v="posted">Posted</button><button data-v="failed">Failed</button></div>
 <div class="grp"><button id="gif" aria-pressed="false">▶ GIFs</button><button id="promo" aria-pressed="false" title="Promotions from dev to main, where the reviewers star">dev → main</button></div>
 <label class="grp" id="teamw"><span>Team</span><select id="team"></select></label>
 <label class="grp"><span>Repo</span><select id="repo"></select></label>
 <div class="grp" id="view"><span>View</span><button data-v="grid">Grid</button><button data-v="list">List</button></div>
 <label class="grp"><span>Group by</span><select id="group"><option value="">None</option><option value="team">Team</option><option value="repo">Repo</option><option value="state">State</option><option value="style">Style</option><option value="date">Day</option><option value="cast">Cast</option></select></label>
 <label class="grp"><span>Sort</span><select id="sort"><option value="new">Newest first</option><option value="old">Oldest first</option><option value="prd">PR number, high to low</option><option value="pra">PR number, low to high</option><option value="title">Title A to Z</option></select></label>
</div><p class="count" id="count" aria-live="polite"></p></header>
<main id="out"></main>
<details class="how"><summary class="gh"><b>How it works</b></summary>__HOW__</details>
<script>
const DATA=__DATA__, SHARED=__SHARED__;
if(SHARED)document.getElementById('state').hidden=true;
const S=(()=>{try{return JSON.parse(localStorage.getItem('spot')||'{}')}catch(e){return {}}})();
const st=Object.assign({q:'',state:'all',gif:false,promo:false,team:'',repo:'',view:'grid',group:'',sort:'new'},S);
const save=()=>{try{localStorage.setItem('spot',JSON.stringify(st))}catch(e){}};
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const counts=DATA.reduce((a,d)=>(a[d.state]=(a[d.state]||0)+1,a),{});
document.getElementById('sub').innerHTML=SHARED?`${DATA.length} spotlights · ${(n=>n+(n===1?' GIF':' GIFs'))(DATA.filter(d=>d.gif).length)} · updated __NOW__. Click a picture to see it whole.`:`${counts.held||0} held · ${counts.posted||0} posted${counts.failed?` · ${counts.failed} failed`:''} · ${(n=>n+(n===1?' GIF':' GIFs'))(DATA.filter(d=>d.gif).length)} · updated __NOW__. Click a command to copy it, a picture to see it whole.`;
if(!counts.failed)document.querySelector('#state [data-v=failed]').hidden=true;
const opts=(id,vals,all)=>{document.getElementById(id).innerHTML=`<option value="">${all}</option>`+[...new Set(vals)].sort().map(v=>`<option>${esc(v)}</option>`).join('')};
opts('team',DATA.map(d=>d.team).filter(Boolean),'All teams');opts('repo',DATA.map(d=>d.repo),'All repos');
function card(d){
 const act=d.state==='posted'?`<a href="${esc(d.comment)}">PR comment</a>${d.slack?`<a href="${esc(d.slack)}">#jenkins</a>`:''}`:d.state==='held'?`<code class="cmd">./spotlight.py post ${esc(d.tag)}</code>`:`<span class="err">${esc(d.error)}</span>`;
 return `<article class="card"><div class="pic">${d.img?`<img loading="lazy" src="${esc(d.img)}" alt="${esc(d.title)}">`:''}${d.gif?'<span class="gtag">▶ GIF</span>':''}</div>
 <div class="body"><div class="row"><span class="pill ${d.state}">${d.state}</span>${d.promotion?'<span class="pill promo">dev → main</span>':''}<a class="pr" href="${esc(d.prUrl)}">${esc(d.tag)}</a><span class="date">${esc(d.date)}</span></div>
 <h2>${esc(d.title)}</h2><p class="prt">${esc(d.prTitle)}</p><button class="more">Caption</button><div class="cap">${d.caption}</div>
 <p class="meta">${esc(d.style)} · ${esc(d.reference)} · ${esc(d.cast)}</p><div class="links">${act}</div></div></article>`}
const hay=d=>[d.tag,'#'+d.pr,d.repo,d.team,d.state,d.title,d.prTitle,d.caption.replace(/<[^>]+>/g,' '),d.style,d.reference,d.cast,d.date,d.gif?'gif':'',d.promotion?'dev main promotion release':''].join(' ').toLowerCase();
DATA.forEach(d=>d._h=hay(d));
const keyOf={team:d=>d.team||'—',repo:d=>d.repo,state:d=>d.state,style:d=>d.style,date:d=>d.date,cast:d=>d.cast.split(', ')[0]||'—'};
const sorts={new:(a,b)=>b.at.localeCompare(a.at)||b.pr-a.pr,old:(a,b)=>a.at.localeCompare(b.at)||a.pr-b.pr,prd:(a,b)=>b.pr-a.pr,pra:(a,b)=>a.pr-b.pr,title:(a,b)=>a.title.localeCompare(b.title)};
function render(){
 document.querySelectorAll('#state button').forEach(b=>b.setAttribute('aria-pressed',b.dataset.v===st.state));
 document.querySelectorAll('#view button').forEach(b=>b.setAttribute('aria-pressed',b.dataset.v===st.view));
 document.getElementById('gif').setAttribute('aria-pressed',st.gif);document.getElementById('promo').setAttribute('aria-pressed',st.promo);
 document.getElementById('q').value=st.q;document.getElementById('group').value=st.group;document.getElementById('team').value=st.team;document.getElementById('repo').value=st.repo;document.getElementById('sort').value=st.sort;
 let ds=DATA.filter(d=>(st.state==='all'||d.state===st.state)&&(!st.gif||d.gif)&&(!st.promo||d.promotion)&&(!st.team||d.team===st.team)&&(!st.repo||d.repo===st.repo)&&st.q.toLowerCase().split(/\s+/).filter(Boolean).every(w=>d._h.includes(w))).sort(sorts[st.sort]);
 const cls=st.view==='list'?'list':'grid', out=document.getElementById('out');
 document.getElementById('sub').dataset.n=ds.length;
 const filt=[st.q&&`“${st.q}”`,st.state!=='all'&&st.state,st.gif&&'GIFs',st.promo&&'dev → main',st.team&&st.team,st.repo&&st.repo].filter(Boolean);
 document.getElementById('count').textContent=filt.length?`Showing ${ds.length} of ${DATA.length} · ${filt.join(' · ')}`:`Showing all ${DATA.length}`;
 if(!ds.length){out.innerHTML='<p class="empty">Nothing matches.</p>';return}
 if(!st.group){out.innerHTML=`<div class="${cls}">${ds.map(card).join('')}</div>`}
 else{const g=new Map();ds.forEach(d=>{const k=keyOf[st.group](d);g.set(k,[...(g.get(k)||[]),d])});
  out.innerHTML=[...g].map(([k,v])=>`<details class="gd"${v.length<=12?' open':''}><summary class="gh"><b>${esc(k)}</b> · ${v.length}</summary><div class="${cls}">${v.map(card).join('')}</div></details>`).join('')}
}
document.getElementById('state').onclick=e=>{if(e.target.dataset.v){st.state=e.target.dataset.v;save();render()}};
document.getElementById('view').onclick=e=>{if(e.target.dataset.v){st.view=e.target.dataset.v;save();render()}};
document.getElementById('gif').onclick=()=>{st.gif=!st.gif;save();render()};
document.getElementById('promo').onclick=()=>{st.promo=!st.promo;save();render()};
document.getElementById('group').onchange=e=>{st.group=e.target.value;save();render()};
document.getElementById('sort').onchange=e=>{st.sort=e.target.value;save();render()};
let qt;document.getElementById('q').oninput=e=>{clearTimeout(qt);qt=setTimeout(()=>{st.q=e.target.value;save();render();e.target.focus()},120)};
document.addEventListener('keydown',e=>{if(e.key==='/'&&document.activeElement.id!=='q'){e.preventDefault();document.getElementById('q').focus()}if(e.key==='Escape'&&document.activeElement.id==='q'){st.q='';e.target.value='';save();render()}});
document.getElementById('team').onchange=e=>{st.team=e.target.value;save();render()};
document.getElementById('repo').onchange=e=>{st.repo=e.target.value;save();render()};
document.getElementById('out').onclick=e=>{const t=e.target;
 if(t.matches('.card img'))t.classList.toggle('big');
 if(t.matches('.more'))t.closest('.card').classList.toggle('open');
 if(t.matches('.cmd')&&navigator.clipboard){const v=t.textContent;navigator.clipboard.writeText(v).then(()=>{t.textContent='copied';setTimeout(()=>t.textContent=v,900)},()=>{})}};
render();
</script>
"""


def install(team: dict) -> None:
    """The team's launchd job: poll every 15 minutes and at login; a sleeping Mac runs it once when it wakes."""
    label = f"local.pr-spotlight.{team['id']}"
    plist = LAUNCH_AGENTS / f"{label}.plist"
    log = ROOT / f"poll-{team['id']}.log"
    plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array><string>{ROOT / 'spotlight.py'}</string><string>poll</string><string>--team</string><string>{team['id']}</string></array>
  <key>WorkingDirectory</key><string>{ROOT}</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>{Path.home() / '.local/bin'}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>900</integer>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
""")
    target = f"gui/{subprocess.run(['id', '-u'], capture_output=True, text=True).stdout.strip()}"
    subprocess.run(["launchctl", "bootout", f"{target}/{label}"], capture_output=True)
    sh(["launchctl", "bootstrap", target, str(plist)])
    print(f"installed {plist}; log: {log}")


def usage() -> str:
    logs = sorted(SESSIONS.glob("*/*/*/rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for log in logs[:20]:
        for line in reversed(log.read_text().splitlines()):
            if '"rate_limits"' in line:
                lim = json.loads(line).get("payload", {}).get("rate_limits") or {}
                return f"codex 5h {round((lim.get('primary') or {}).get('used_percent', 0))}% · week {round((lim.get('secondary') or {}).get('used_percent', 0))}%"
    return "codex usage: unknown"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "post", "recaption", "update", "poll", "install", "usage", "page"])
    ap.add_argument("pr", nargs="?", help="<repo>#<number>, or a bare number for the team's first repo")
    ap.add_argument("--team", default=DEFAULT_TEAM, help=f"teams/<team>.yaml (default: {DEFAULT_TEAM})")
    ap.add_argument("--dry-run", action="store_true", help="stop after the brief")
    ap.add_argument("--no-post", action="store_true", help="stop after the render")
    ap.add_argument("--rebrief", action="store_true", help="write the brief again")
    ap.add_argument("--gif", action="store_true", help="render this spotlight as a looping GIF")
    a = ap.parse_args()
    if a.cmd == "page":
        print(page())
        return
    if a.cmd == "usage":
        print(usage())
        return
    team = load_team(a.team)
    if a.cmd == "poll":
        poll(team)
    elif a.cmd == "install":
        install(team)
    elif not a.pr:
        ap.error(f"{a.cmd} needs a PR")
    else:
        repo, pr = resolve(a.pr, team)
        if a.cmd == "recaption":
            recaption(repo, pr, team)
        elif a.cmd == "update":
            print(update_comment(repo, pr))
        elif a.cmd == "run":
            sys.exit(1 if run_one(repo, pr, team, a.dry_run, a.no_post, a.rebrief, a.gif).get("error") else 0)
        else:
            run = read_run(repo, pr)
            if not run.get("prompt"):
                sys.exit(f"{short(repo, pr)}: no brief yet; run it first")
            run = publish(repo, pr, run, team)
            print(run["posted"])
            print_share(team)


if __name__ == "__main__":
    main()
