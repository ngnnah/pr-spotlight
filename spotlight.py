#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""A spotlight for a merged PR in a team's repos: an image of the people behind it and a short caption
of lessons from the diff, posted as a PR comment and in Slack. Each team is teams/<team>.yaml.

    ./spotlight.py run <pr> [--dry-run] [--no-post] [--rebrief]
    ./spotlight.py post <pr>          publish a spotlight already rendered with --no-post
    ./spotlight.py recaption <pr>     a new caption in the current format; image and title stay
    ./spotlight.py update <pr>        rewrite the posted PR comment with the run's current caption
    ./spotlight.py poll               PRs merged since the team's start without a spotlight, three at most
    ./spotlight.py install            the launchd job: poll every 15 minutes, at login, and once on wake
    ./spotlight.py usage              the Codex limits from the newest session log

<pr> is <repo>#<number> (web-app#12) or a bare number for the team's first repo. --team picks the
team file (default: the first in teams/).

Steps, each kept in spotlights/<repo>/<pr>/ so a rerun picks up where it stopped:
gather (gh) -> cast (people/*.yaml) -> seed (styles.yaml, map.yaml, log.jsonl) -> brief (claude -p:
prompt.md, caption.md) -> render (codex exec: image.jpg) -> publish (git push, gh comment, Slack).
--dry-run stops after the brief; --no-post stops after the render. Never reads ~/.codex/auth.json.
"""

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent


def _self_repo() -> str:
    """This repo as owner/name, from its origin remote: comments link to images stored here."""
    url = subprocess.run(["git", "-C", str(ROOT), "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
    return re.sub(r"(\.git)?$", "", re.split(r"github\.com[:/]", url)[-1]) if url else "owner/pr-spotlight"


SELF = _self_repo()
# The team when --team isn't given: the only one, or the first file in teams/.
DEFAULT_TEAM = next((f.stem for f in sorted((ROOT / "teams").glob("*.yaml"))), "example")
MAX_PER_POLL = 3  # spotlights per poll, so a night's catch-up doesn't spend the Codex window at once
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


def render(repo: str, pr: int, b: dict, people: list[dict]) -> Path:
    d = spot_dir(repo, pr)
    png, jpg = d / "image.png", d / "image.jpg"
    photos = [ph for p in people for ph in p["photos"]]
    st = subprocess.run(["codex", "login", "status"], capture_output=True, text=True)
    if "ChatGPT" not in st.stdout + st.stderr:
        raise RuntimeError("Codex isn't signed in with ChatGPT; run: codex login")
    ok, said = codex(b["prompt"], photos, png)
    if not ok and re.search(r"similar|policy|third.party|blocked", said, re.I):
        retry = b["prompt"] + ("\n\nDraw every referenced character, place and object in this image's own "
                               "style rather than as it looks in the original work.")
        ok, said = codex(retry, photos, png)
    if not ok:
        raise RuntimeError(f"render: {said[:500]}")
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
    sh(["git", "-C", str(ROOT), "add", str(spot_dir(repo, pr).relative_to(ROOT)), "log.jsonl"])
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
    git_push(repo, pr, f"Spotlight {short(repo, pr)}: posted")
    return run


# ---------- commands ----------

def about_of(repo: str, team: dict) -> str:
    return next((r.get("about", "") for r in team["repos"] if r["repo"] == repo), "")


def run_one(repo: str, pr: int, team: dict, dry: bool = False, no_post: bool = False, rebrief: bool = False) -> dict:
    run = read_run(repo, pr)
    tag = short(repo, pr)
    if run.get("posted"):
        print(f"{tag}: posted already: {run['posted'].get('comment')}")
        return run
    d = spot_dir(repo, pr)
    try:
        if rebrief or not run.get("prompt"):
            (d / "image.jpg").unlink(missing_ok=True)  # a new brief needs a new image
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
            render(repo, pr, run, people)
            run["render_s"] = round(time.time() - start)
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
    """Every PR merged into a team repo's default branch or dev since the team's start, without a spotlight,
    oldest first, at most MAX_PER_POLL per call."""
    print(f"--- poll {team['id']} {dt.datetime.now():%Y-%m-%d %H:%M}")
    auto = bool(team.get("auto_post"))
    start = str(team.get("start") or dt.date.today()).replace(" ", "T").replace("+00:00", "Z")
    todo = []
    for r in team["repos"]:
        repo = r["repo"]
        default = gh_json(["repo", "view", repo, "--json", "defaultBranchRef"])["defaultBranchRef"]["name"]
        prs = gh_json(["pr", "list", "-R", repo, "--state", "merged", "--limit", "50",
                       "--search", f"merged:>={start}", "--json", "number,baseRefName,mergedAt"])
        todo += [(p["mergedAt"], repo, p["number"]) for p in prs if p["baseRefName"] in (default, "dev")]
    done = 0
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
        r = run_one(repo, n, team, no_post=not auto)
        if r.get("error"):
            r["tries"] = r.get("tries", 0) + 1
            write_run(repo, n, r)
            notify(f"{tag} failed ({r['tries']} of {TRIES}): {r['error'][:80]}")
        elif not auto:
            notify(f"{tag} rendered: {r['title']}. Post it with ./spotlight.py post {tag} --team {team['id']}")


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
    ap.add_argument("cmd", choices=["run", "post", "recaption", "update", "poll", "install", "usage"])
    ap.add_argument("pr", nargs="?", help="<repo>#<number>, or a bare number for the team's first repo")
    ap.add_argument("--team", default=DEFAULT_TEAM, help=f"teams/<team>.yaml (default: {DEFAULT_TEAM})")
    ap.add_argument("--dry-run", action="store_true", help="stop after the brief")
    ap.add_argument("--no-post", action="store_true", help="stop after the render")
    ap.add_argument("--rebrief", action="store_true", help="write the brief again")
    a = ap.parse_args()
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
            sys.exit(1 if run_one(repo, pr, team, a.dry_run, a.no_post, a.rebrief).get("error") else 0)
        else:
            run = read_run(repo, pr)
            if not run.get("prompt"):
                sys.exit(f"{short(repo, pr)}: no brief yet; run it first")
            run = publish(repo, pr, run, team)
            print(run["posted"])


if __name__ == "__main__":
    main()
