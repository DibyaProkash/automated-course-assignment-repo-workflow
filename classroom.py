#!/usr/bin/env python3
"""Keep student assignment repos in line with course.yml and the roster.

Every run checks every student for every released assignment and fixes only
what's missing or out of date:

  * creates the repo from the template if it doesn't exist yet
  * invites the student, or re-sends the invitation if it expired (7 days)
  * gives write access before the deadline and read-only access after it
    (honouring per-student extensions)
  * tags the repo with the course and assignment topics

So the same run works for a new assignment, a late enrolment, students who
never accepted their invite, and deadline locking. Running it twice in a row
changes nothing the second time.

Settings: course.yml. Environment: GH_ADMIN_TOKEN (required), DRY_RUN=true to
preview, ONLY_ASSIGNMENT=<template> to limit a run to one assignment, and
NOW="2026-10-20 12:00" to pretend it's a different time (for previews).
"""

import csv
import io
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

import yaml
from github import Auth, Github, GithubException, UnknownObjectException

CONFIG_FILE = os.getenv("CONFIG_FILE", "course.yml")

# What we ask for -> what the API reports back once it's granted.
PERMISSION_LEVEL = {"push": "write", "pull": "read"}


# ---------------------------------------------------------------- config ----

def parse_time(value, tz, end_of_day):
    """Accept 2026-10-20, 2026-10-20 23:59 or a YAML timestamp."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz)
    if isinstance(value, date):
        hh, mm = (23, 59) if end_of_day else (0, 0)
        return datetime(value.year, value.month, value.day, hh, mm, tzinfo=tz)
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=tz)
        except ValueError:
            pass
    try:
        return parse_time(date.fromisoformat(text), tz, end_of_day)
    except ValueError:
        sys.exit(f"{CONFIG_FILE}: can't read the date '{text}'. Use e.g. 2026-10-20 23:59")


def topic(text):
    """GitHub topics: lowercase letters, digits and hyphens, max 50 chars."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]", "-", str(text).lower())).strip("-")[:50]


@dataclass
class Assignment:
    template: str
    prefix: str
    private: bool
    release: datetime | None
    deadline: datetime | None
    lock: bool
    extensions: dict = field(default_factory=dict)

    def deadline_for(self, username):
        return self.extensions.get(username.lower(), self.deadline)


@dataclass
class Course:
    org: str
    tz: ZoneInfo
    roster: str
    topics: list
    assignments: list


def load_course():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except FileNotFoundError:
        sys.exit(f"Can't find {CONFIG_FILE}")

    for key in ("organization", "assignments"):
        if not raw.get(key):
            sys.exit(f"{CONFIG_FILE} needs an '{key}' entry")
    tz = ZoneInfo(raw.get("timezone", "America/Vancouver"))

    assignments = []
    for i, a in enumerate(raw["assignments"], start=1):
        if not isinstance(a, dict) or not a.get("template"):
            sys.exit(f"{CONFIG_FILE}: assignment #{i} needs a 'template'")
        deadline = parse_time(a.get("deadline"), tz, end_of_day=True)
        extensions = {str(u).lower().lstrip("@"): parse_time(d, tz, end_of_day=True)
                      for u, d in (a.get("extensions") or {}).items()}
        visibility = str(a.get("visibility", "private")).lower()
        if visibility not in ("private", "public"):
            sys.exit(f"{CONFIG_FILE}: visibility for {a['template']} must be private or public")
        assignments.append(Assignment(
            template=a["template"],
            prefix=a.get("prefix") or a["template"],
            private=visibility == "private",
            release=parse_time(a.get("release"), tz, end_of_day=False),
            deadline=deadline,
            lock=bool(a.get("lock_after_deadline", True)) and deadline is not None,
            extensions=extensions,
        ))

    topics = raw.get("course_topic") or []
    topics = [topics] if isinstance(topics, str) else list(topics)
    return Course(
        org=raw["organization"],
        tz=tz,
        roster=raw.get("roster", "class_roster.csv"),
        topics=[topic(t) for t in topics if topic(t)],
        assignments=assignments,
    )


# ---------------------------------------------------------------- roster ----

@dataclass
class Student:
    line: int
    username: str
    name: str


def decode(raw):
    """Handle what Excel and Google Sheets typically export."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def read_roster(path):
    """Return (students, problems). Bad rows are reported, never silently dropped."""
    try:
        with open(path, "rb") as f:
            text = decode(f.read())
    except FileNotFoundError:
        sys.exit(f"Roster file not found: {path}")

    reader = csv.DictReader(io.StringIO(text))
    headers = [(h or "").strip().lower() for h in (reader.fieldnames or [])]
    if "github_username" not in headers:
        sys.exit(f"{path} needs a 'github_username' column (found: {', '.join(headers)})")

    students, problems, seen = [], [], set()
    for line, row in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "").strip()
               for k, v in row.items() if isinstance(v, str)}
        if not any(row.values()):
            continue
        username = row.get("github_username", "").lstrip("@")
        name = row.get("first_name") or row.get("name") or "student"
        if not username:
            problems.append(f"line {line}: no GitHub username for {name}")
        elif username.lower() in seen:
            problems.append(f"line {line}: {username} is listed twice")
        else:
            seen.add(username.lower())
            students.append(Student(line, username, name))
    return students, problems


# ---------------------------------------------------------------- github ----

def get_repo_or_none(org, name):
    try:
        return org.get_repo(name)
    except UnknownObjectException:
        return None


def wait_for_repo(org, name, attempts=10):
    """Template generation is asynchronous; wait until the new repo answers."""
    for _ in range(attempts):
        repo = get_repo_or_none(org, name)
        if repo is not None:
            return repo
        time.sleep(2)
    raise RuntimeError("repo was created but isn't reachable yet; the next run will finish it")


def ensure_topics(repo, wanted, dry_run):
    current = repo.get_topics()
    missing = [t for t in wanted if t not in current]
    if not missing:
        return None
    if not dry_run:
        repo.replace_topics(current + missing)
    return ("would tag " if dry_run else "tagged ") + ", ".join(missing)


def ensure_access(repo, username, permission, dry_run):
    """Make sure the student has `permission`. Returns (status, note)."""
    wanted = PERMISSION_LEVEL[permission]
    would = "would " if dry_run else ""

    if repo.has_in_collaborators(username):
        current = repo.get_collaborator_permission(username)
        if current == "admin" or current == wanted:
            return "ok", None
        if not dry_run:
            repo.add_to_collaborators(username, permission)
        label = "locked (read-only)" if permission == "pull" else "unlocked (write)"
        return ("locked" if permission == "pull" else "unlocked"), would + label

    for invite in repo.get_pending_invitations():
        if not invite.invitee or invite.invitee.login.lower() != username.lower():
            continue
        if not invite.expired and invite.permissions == wanted:
            return "pending", f"invited {invite.created_at:%b %d}, not accepted yet"
        if not dry_run:
            repo.remove_invitation(invite.id)
            repo.add_to_collaborators(username, permission)
        reason = "expired" if invite.expired else "permission changed"
        return "re-invited", f"{would}re-send invitation ({reason})"

    if not dry_run:
        repo.add_to_collaborators(username, permission)
    return "invited", f"{would}invite ({'read-only' if permission == 'pull' else 'write'})"


# ------------------------------------------------------------------- run ----

class Runner:
    def __init__(self, g, org, course, now, dry_run):
        self.g, self.org, self.course = g, org, course
        self.now, self.dry_run = now, dry_run
        self.users = {}  # username -> canonical login, or None if not found

    def login_for(self, username):
        key = username.lower()
        if key not in self.users:
            try:
                self.users[key] = self.g.get_user(username).login
            except UnknownObjectException:
                self.users[key] = None
        return self.users[key]

    def one(self, a, template, student):
        """Return (status, repo_name, note) for one student on one assignment."""
        login = self.login_for(student.username)
        if login is None:
            return "failed", None, f"no GitHub account named '{student.username}' (roster line {student.line})"

        repo_name = f"{a.prefix}-{login}"
        deadline = a.deadline_for(login)
        locked = a.lock and deadline is not None and self.now > deadline
        permission = "pull" if locked else "push"
        notes = []

        repo = get_repo_or_none(self.org, repo_name)
        if repo is None:
            if self.dry_run:
                return "created", repo_name, "would create repo and invite"
            description = (f"{a.prefix} repository for {student.name}" if a.private
                           else f"{a.prefix} repository")  # no names on public repos
            self.org.create_repo_from_template(
                repo_name, template, description=description,
                private=a.private, include_all_branches=False)
            repo = wait_for_repo(self.org, repo_name)
            notes.append("repo created")
            time.sleep(1)  # gentle on GitHub's secondary rate limits

        status, note = ensure_access(repo, login, permission, self.dry_run)
        notes.append(note)
        notes.append(ensure_topics(repo, self.course.topics + [topic(a.prefix)], self.dry_run))
        if "repo created" in notes:
            status = "created"
        elif status == "ok" and notes[-1]:
            status = "tagged"
        return status, repo_name, "; ".join(n for n in notes if n)

    def assignment(self, a, students):
        """Process one assignment. Returns (heading, rows, counts)."""
        if a.release and self.now < a.release:
            return f"{a.prefix}: releases {a.release:%a %b %d, %H:%M}", [], {}
        try:
            template = self.org.get_repo(a.template)
        except UnknownObjectException:
            return a.prefix, [("-", "failed", None, f"template '{a.template}' not found in {self.course.org}")], {"failed": 1}
        if not template.is_template:
            return a.prefix, [("-", "failed", None, f"'{a.template}' isn't marked as a template repository")], {"failed": 1}

        rows, counts = [], {}
        for i, s in enumerate(students, start=1):
            print(f"  [{i}/{len(students)}] {s.username}", flush=True)
            try:
                status, repo_name, note = self.one(a, template, s)
            except GithubException as e:
                msg = e.data.get("message", str(e)) if isinstance(e.data, dict) else str(e)
                status, repo_name, note = "failed", None, f"GitHub API {e.status}: {msg}"
            except Exception as e:  # keep going for the rest of the class
                status, repo_name, note = "failed", None, str(e)
            counts[status] = counts.get(status, 0) + 1
            if status != "ok":
                rows.append((s.username, status, repo_name, note))

        heading = a.prefix
        if a.deadline:
            state = "locked" if a.lock and self.now > a.deadline else "due"
            heading += f": {state} {a.deadline:%a %b %d, %H:%M}"
        return heading, rows, counts


ICONS = {"failed": "❌", "pending": "⏳", "ok": "✅", "locked": "🔒", "unlocked": "🔓"}


def write_report(course, sections, roster_problems, dry_run, now):
    out = [f"## {'DRY RUN: ' if dry_run else ''}{course.org}",
           f"_{now:%a %b %d %Y, %H:%M %Z}_", ""]
    if roster_problems:
        out += ["**Roster problems**", ""] + [f"- ❌ {p}" for p in roster_problems] + [""]
    for heading, rows, counts in sections:
        out.append(f"### {heading}")
        if counts:
            out.append(" · ".join(f"{ICONS.get(s, '✅')} {n} {s}" for s, n in sorted(counts.items())))
        if rows:
            out += ["", "| Student | Status | Repository | Details |", "|---|---|---|---|"]
            for user, status, repo_name, note in rows:
                link = f"[{repo_name}](https://github.com/{course.org}/{repo_name})" if repo_name else "-"
                out.append(f"| {user} | {ICONS.get(status, '✅')} {status} | {link} | {note or ''} |")
        out.append("")
    text = "\n".join(out) + "\n"
    print(text)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text)


def main():
    course = load_course()
    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    only = os.getenv("ONLY_ASSIGNMENT", "").strip()
    now = (parse_time(os.getenv("NOW"), course.tz, end_of_day=False)
           if os.getenv("NOW") else datetime.now(course.tz))

    token = os.getenv("GH_ADMIN_TOKEN")
    if not token:
        sys.exit("GH_ADMIN_TOKEN isn't set. Add it under Settings → Secrets and variables → Actions.")

    students, roster_problems = read_roster(course.roster)
    print(f"{len(students)} students in {course.roster}"
          f"{'  (DRY RUN: nothing will change)' if dry_run else ''}")

    g = Github(auth=Auth.Token(token))
    try:
        org = g.get_organization(course.org)
    except UnknownObjectException:
        sys.exit(f"Can't find the organization '{course.org}', or the token can't see it.")

    assignments = [a for a in course.assignments if not only or only in (a.template, a.prefix)]
    if only and not assignments:
        sys.exit(f"No assignment named '{only}' in {CONFIG_FILE}")

    runner = Runner(g, org, course, now, dry_run)
    sections = []
    for a in assignments:
        print(f"\n{a.prefix}")
        sections.append(runner.assignment(a, students))

    write_report(course, sections, roster_problems, dry_run, now)

    failed = bool(roster_problems) or any(c.get("failed") for _, _, c in sections)
    # Scheduled runs only report problems; runs you start (or roster edits) go red,
    # so you hear about a bad username when you add it, not every hour afterwards.
    if failed and os.getenv("GITHUB_EVENT_NAME") != "schedule":
        sys.exit(1)


if __name__ == "__main__":
    main()
