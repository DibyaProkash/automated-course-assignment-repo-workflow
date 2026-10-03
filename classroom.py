#!/usr/bin/env python3
"""Keep student assignment repos in line with the course files and rosters.

Courses live in courses/<course>.yml. Each one lists its assignments once and
its sections, and each section has its own organization and roster. A run
fixes only what's missing or out of date, for every student in every
released assignment:

  * creates the repo from the template if it doesn't exist yet
  * invites the student, or re-sends the invitation if it expired (7 days)
  * gives write access before the deadline and read-only access after it,
    honouring per-section deadlines and per-student extensions
  * tags the repo with course, section and assignment topics
  * when a repo locks, tags the last commit as a submission snapshot
  * on the archive date, archives the repos (read-only for everyone)

It also warns two weeks before GH_ADMIN_TOKEN expires.

Running it twice in a row changes nothing the second time.

Environment:
  GH_ADMIN_TOKEN      token that can administer every course organization (required)
  MODE                full (default) checks everything; due only handles
                      releases and deadlines from the last few hours (hourly runs)
  DRY_RUN=true        preview only
  ONLY                limit to a course (dgl123) or a section (dgl123/cvs1)
  NOW                 pretend it's this time, e.g. "2026-12-06 09:00"
  CHANGED_FILES_PATH  file listing changed paths; only affected sections run
"""

import csv
import glob
import io
import os
import re
import sys
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import yaml
from github import (Auth, BadCredentialsException, Github, GithubException,
                    UnknownObjectException)

COURSE_GLOB = "courses/*.y*ml"
DUE_WINDOW = timedelta(hours=3)     # hourly runs look back this far (cron can run late)
ALWAYS_ALL = {"classroom.py", "requirements.txt", ".github/workflows/classroom.yml"}
TOKEN_WARN_DAYS = 14               # warn this long before the token expires
TOKEN_FAIL_DAYS = 7                # and turn even scheduled runs red (GitHub emails you)
SNAPSHOT_TAG = "submission"        # then submission-2, submission-3 if a repo is relocked

# What we ask for -> what the API reports back once it's granted.
PERMISSION_LEVEL = {"push": "write", "pull": "read"}


# ---------------------------------------------------------------- config ----

class ConfigError(Exception):
    pass


def parse_time(value, tz, end_of_day, where=""):
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
        raise ConfigError(f"{where}can't read the date '{text}'. Use e.g. 2026-10-20 23:59")


def topic(text):
    """GitHub topics: lowercase letters, digits and hyphens, max 50 chars."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]", "-", str(text).lower())).strip("-")[:50]


@dataclass
class Assignment:
    template: str            # "name" (looked up in template_org / section org) or "owner/name"
    prefix: str
    private: bool
    release: datetime | None
    deadline: datetime | None
    lock_after_deadline: bool
    archive_on: datetime | None = None
    extensions: dict = field(default_factory=dict)

    def deadline_for(self, username):
        return self.extensions.get(username.lower(), self.deadline)

    def locked_for(self, username, now):
        deadline = self.deadline_for(username)
        return self.lock_after_deadline and deadline is not None and now > deadline

    def archived(self, now):
        return self.archive_on is not None and now >= self.archive_on

    def key_times(self):
        times = [self.release, self.deadline, self.archive_on]
        if self.lock_after_deadline:
            times += list(self.extensions.values())
        return [t for t in times if t]


@dataclass
class Section:
    id: str                  # e.g. dgl123/cvs1
    title: str
    org: str
    roster: str
    files: set               # config + roster paths, for change detection
    template_org: str | None
    topics: list
    assignments: list

    def template_for(self, a):
        """owner/name of an assignment's template."""
        return a.template if "/" in a.template else f"{self.template_org or self.org}/{a.template}"


def parse_assignment(a, tz, where, course_archive=None):
    if isinstance(a, dict) and not a.get("template") and len(a) == 1:
        field_name = next(iter(a))
        raise ConfigError(
            f"{where}'{field_name}' is on its own line starting with '-'. Only the "
            f"'template:' line of an assignment starts with '-'; the lines under it "
            f"are indented with no dash.")
    if not isinstance(a, dict) or not a.get("template"):
        raise ConfigError(f"{where}every assignment needs a 'template'")
    where = f"{where}{a['template']}: "
    visibility = str(a.get("visibility", "private")).lower()
    if visibility not in ("private", "public"):
        raise ConfigError(f"{where}visibility must be private or public")
    template = str(a["template"])
    return Assignment(
        template=template,
        prefix=a.get("prefix") or template.split("/")[-1],
        private=visibility == "private",
        release=parse_time(a.get("release"), tz, False, where),
        deadline=parse_time(a.get("deadline"), tz, True, where),
        lock_after_deadline=bool(a.get("lock_after_deadline", True)),
        archive_on=parse_time(a.get("archive_on"), tz, False, where) if a.get("archive_on") else course_archive,
        extensions={str(u).lower().lstrip("@"): parse_time(d, tz, True, where)
                    for u, d in (a.get("extensions") or {}).items()},
    )


def apply_override(a, o, tz, where):
    """Per-section changes to an assignment's dates."""
    unknown = set(o) - {"release", "deadline", "lock_after_deadline", "archive_on", "extensions"}
    if unknown:
        raise ConfigError(f"{where}unknown override field(s): {', '.join(sorted(unknown))}")
    changes = {}
    if "release" in o:
        changes["release"] = parse_time(o["release"], tz, False, where)
    if "deadline" in o:
        changes["deadline"] = parse_time(o["deadline"], tz, True, where)
    if "archive_on" in o:
        changes["archive_on"] = parse_time(o["archive_on"], tz, False, where)
    if "lock_after_deadline" in o:
        changes["lock_after_deadline"] = bool(o["lock_after_deadline"])
    if o.get("extensions"):
        changes["extensions"] = {**a.extensions, **{
            str(u).lower().lstrip("@"): parse_time(d, tz, True, where)
            for u, d in o["extensions"].items()}}
    return replace(a, **changes)


def load_course(path):
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    where = f"{path}: "
    if not isinstance(raw.get("assignments") or [], list):
        raise ConfigError(f"{where}'assignments' must be a list")
    tz = ZoneInfo(raw.get("timezone", "America/Vancouver"))
    key = os.path.splitext(os.path.basename(path))[0]
    title = str(raw.get("course") or key)
    # An empty list is fine: the course is set up but has nothing released yet.
    course_archive = parse_time(raw.get("archive_on"), tz, False, where)
    assignments = [parse_assignment(a, tz, where, course_archive) for a in raw.get("assignments") or []]
    names = {n for a in assignments for n in (a.template, a.prefix)}

    sections = raw.get("sections")
    if not sections:
        raise ConfigError(f"{where}needs a 'sections' list (each with name, organization and roster)")

    course_topics = raw.get("course_topic") or raw.get("topic") or []
    course_topics = [course_topics] if isinstance(course_topics, str) else list(course_topics)

    result = []
    for s in sections:
        name = str(s.get("name", "")).strip()
        sid = f"{key}/{name}" if name else key
        swhere = f"{path} [{name or 'section'}]: "
        for need in ("organization", "roster"):
            if not s.get(need):
                raise ConfigError(f"{swhere}needs '{need}'")
        overrides = s.get("overrides") or {}
        for ref in overrides:
            if ref not in names:
                raise ConfigError(f"{swhere}override for unknown assignment '{ref}'")
        section_archive = parse_time(s.get("archive_on"), tz, False, swhere)
        section_assignments = []
        for a in assignments:
            if section_archive:
                a = replace(a, archive_on=section_archive)
            o = overrides.get(a.template) or overrides.get(a.prefix)
            section_assignments.append(apply_override(a, o, tz, swhere) if o else a)
        topics = [topic(t) for t in course_topics]
        if name:
            topics.append(topic(f"{key}-{name}"))
        result.append(Section(
            id=sid,
            title=f"{title} · {name}" if name else title,
            org=s["organization"],
            roster=s["roster"],
            files={os.path.normpath(path), os.path.normpath(s["roster"])},
            template_org=s.get("template_org") or raw.get("template_org"),
            topics=[t for t in topics if t],
            assignments=section_assignments,
        ))
    return result


def load_all_sections():
    paths = sorted(glob.glob(COURSE_GLOB))
    if not paths:
        sys.exit("No course files found. Add one under courses/, e.g. courses/dgl123.yml")
    sections = []
    for path in paths:
        try:
            sections += load_course(path)
        except ConfigError as e:
            sys.exit(str(e))
        except yaml.YAMLError as e:
            sys.exit(f"{path} isn't valid YAML: {e}")
    ids = [s.id for s in sections]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        sys.exit(f"Section names must be unique within a course: {', '.join(sorted(dupes))}")
    return sections


def select_sections(sections, only, changed):
    if only:
        picked = [s for s in sections if s.id == only or s.id.split("/")[0] == only]
        if not picked:
            sys.exit(f"Nothing matches '{only}'. Try one of: {', '.join(s.id for s in sections)}")
        sections = picked
    if changed is not None and not (changed & ALWAYS_ALL or "ALL" in changed):
        sections = [s for s in sections if s.files & changed]
    return sections


def due_assignments(section, now):
    """Assignments with a release, deadline or extension in the last few hours."""
    return [a for a in section.assignments
            if any(now - DUE_WINDOW < t <= now for t in a.key_times())]


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
        return [], [f"roster file not found: {path}"]

    reader = csv.DictReader(io.StringIO(text))
    headers = [(h or "").strip().lower() for h in (reader.fieldnames or [])]
    if "github_username" not in headers:
        return [], [f"{path} needs a 'github_username' column (found: {', '.join(headers)})"]

    students, problems, seen = [], [], set()
    for line, row in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "").strip()
               for k, v in row.items() if isinstance(v, str)}
        if not any(row.values()):
            continue
        username = row.get("github_username", "").lstrip("@")
        name = row.get("first_name") or row.get("name") or "student"
        if not username:
            problems.append(f"{path} line {line}: no GitHub username for {name}")
        elif username.lower() in seen:
            problems.append(f"{path} line {line}: {username} is listed twice")
        else:
            seen.add(username.lower())
            students.append(Student(line, username, name))
    return students, problems


# ---------------------------------------------------------------- github ----

def role_of(user):
    role = user.raw_data.get("role_name")
    if role:
        return role
    p = user.permissions
    return "admin" if p.admin else "write" if p.push else "read"


class GitHubCache:
    """Look things up once per run: users, org repo lists, templates."""

    def __init__(self, g):
        self.g = g
        self.orgs, self.repos, self.templates, self.users = {}, {}, {}, {}

    def org(self, name):
        if name not in self.orgs:
            try:
                self.orgs[name] = self.g.get_organization(name)
            except UnknownObjectException:
                raise RuntimeError(f"organization '{name}' not found, or the token can't see it")
        return self.orgs[name]

    def repo(self, org_name, repo_name):
        if org_name not in self.repos:  # one paginated listing instead of a call per student
            self.repos[org_name] = {r.name.lower(): r for r in self.org(org_name).get_repos(type="all")}
        return self.repos[org_name].get(repo_name.lower())

    def remember(self, org_name, repo):
        self.repos.setdefault(org_name, {})[repo.name.lower()] = repo

    def template(self, full_name):
        if full_name not in self.templates:
            try:
                t = self.g.get_repo(full_name)
            except UnknownObjectException:
                raise RuntimeError(f"template '{full_name}' not found, or the token can't see it")
            if not t.is_template:
                raise RuntimeError(f"'{full_name}' isn't marked as a template repository")
            self.templates[full_name] = t
        return self.templates[full_name]

    def login(self, username):
        key = username.lower()
        if key not in self.users:
            try:
                self.users[key] = self.g.get_user(username).login
            except UnknownObjectException:
                self.users[key] = None
        return self.users[key]


def ensure_access(repo, username, permission, dry_run):
    """Make sure the student has `permission`. Returns (status, note)."""
    wanted = PERMISSION_LEVEL[permission]
    would = "would " if dry_run else ""

    collaborators = {u.login.lower(): role_of(u) for u in repo.get_collaborators(affiliation="direct")}
    current = collaborators.get(username.lower())
    if current:
        if current == "admin" or current == wanted:
            return "ok", None
        if not dry_run:
            repo.add_to_collaborators(username, permission)
        if permission == "pull":
            return "locked", "would lock (read-only)" if dry_run else "locked (read-only)"
        return "unlocked", "would unlock (write)" if dry_run else "unlocked (write)"

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


def ensure_snapshot(repo, prefix, deadline, now, dry_run, only_if_missing=False):
    """Tag the last commit when a repo locks, so there's a fixed record of the submission.

    The first lock creates `submission`. If the repo is reopened (e.g. an extension)
    and locked again with new commits, `submission-2` is added; earlier tags are kept.
    With only_if_missing (an already-locked repo), a snapshot is only made if there
    is none at all, so feedback commits you push later don't count as a resubmission.
    """
    numbered = {}  # snapshot number -> ref; "submission" is 1, "submission-2" is 2, ...
    for r in repo.get_git_matching_refs(f"tags/{SNAPSHOT_TAG}"):
        m = re.fullmatch(rf"refs/tags/{SNAPSHOT_TAG}(?:-(\d+))?", r.ref)
        if m:
            numbered[int(m.group(1) or 1)] = r
    if numbered and only_if_missing:
        return None

    try:
        head = repo.get_branch(repo.default_branch).commit
    except GithubException:
        return "no commits to snapshot"
    sha, committed = head.sha, head.commit.committer.date

    if numbered:
        latest = numbered[max(numbered)]
        target = latest.object.sha
        if latest.object.type == "tag":  # annotated tag: look through to the commit
            target = repo.get_git_tag(target).object.sha
        if target == sha:
            return None  # this exact submission is already recorded
        name = f"{SNAPSHOT_TAG}-{max(numbered) + 1}"
    else:
        name = SNAPSHOT_TAG

    late = deadline is not None and committed is not None and committed > deadline
    detail = f"`{name}` @ {sha[:7]}, last commit {committed.astimezone(now.tzinfo):%b %d %H:%M}" + \
             (" ⚠️ dated after the deadline" if late else "")
    if dry_run:
        return "would snapshot " + detail
    message = (f"Submission snapshot\n\nDeadline: {deadline:%Y-%m-%d %H:%M %Z}\n"
               f"Locked:   {now:%Y-%m-%d %H:%M %Z}\n") if deadline else "Submission snapshot\n"
    tag = repo.create_git_tag(tag=name, message=message, object=sha, type="commit")
    repo.create_git_ref(f"refs/tags/{name}", tag.sha)
    return "snapshot " + detail


def check_token(g, now):
    """Return a warning if the token expires soon; exit clearly if it's already dead."""
    try:
        headers, _ = g.requester.requestJsonAndCheck("GET", "/rate_limit")  # free call
    except BadCredentialsException:
        sys.exit("GH_ADMIN_TOKEN was rejected: it has expired or been revoked. Create a new "
                 "classic token (repo scope) and save it over the secret under "
                 "Settings → Secrets and variables → Actions.")
    raw = next((v for k, v in headers.items() if k.lower() == "github-authentication-token-expiration"), None)
    if not raw:
        return None, None  # token never expires
    m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s*(.*)", raw.strip())
    if not m:
        return None, None
    stamp, zone = m.groups()
    if zone in ("", "UTC"):
        expires = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
    else:
        expires = datetime.strptime(f"{stamp} {zone}", "%Y-%m-%d %H:%M:%S %z")
    days = (expires - now).total_seconds() / 86400
    if days > TOKEN_WARN_DAYS:
        return None, days
    return (f"GH_ADMIN_TOKEN expires {expires.astimezone(now.tzinfo):%a %b %d, %H:%M} "
            f"(in {max(days, 0):.0f} days). Create a new classic token (repo scope) and save it "
            f"over the secret, or runs will stop working."), days


def ensure_topics(repo, wanted, dry_run):
    current = list(repo.topics or [])
    missing = [t for t in wanted if t not in current]
    if not missing:
        return None
    if not dry_run:
        repo.replace_topics(current + missing)
    return ("would tag " if dry_run else "tagged ") + ", ".join(missing)


# ------------------------------------------------------------------- run ----

class Runner:
    def __init__(self, cache, now, dry_run):
        self.gh, self.now, self.dry_run = cache, now, dry_run

    def one(self, section, a, student):
        """Return (status, repo_name, note) for one student on one assignment."""
        repo = self.gh.repo(section.org, f"{a.prefix}-{student.username}")
        notes = []
        if repo is None:
            login = self.gh.login(student.username)
            if login is None:
                return "failed", None, f"no GitHub account named '{student.username}' (roster line {student.line})"
            repo_name = f"{a.prefix}-{login}"
            if self.dry_run:
                return "created", repo_name, "would create repo and invite"
            template = self.gh.template(section.template_for(a))
            description = (f"{a.prefix} repository for {student.name}" if a.private
                           else f"{a.prefix} repository")  # no names on public repos
            repo = self.gh.org(section.org).create_repo_from_template(
                repo_name, template, description=description,
                private=a.private, include_all_branches=False)
            self.gh.remember(section.org, repo)
            notes.append("repo created")

        if repo.archived:  # archived by hand or by an earlier run: read-only, leave it alone
            return "archived", repo.name, None

        permission = "pull" if a.locked_for(student.username, self.now) else "push"
        status, note = ensure_access(repo, student.username, permission, self.dry_run)
        notes.append(note)
        if permission == "pull":
            snap = ensure_snapshot(repo, a.prefix, a.deadline_for(student.username), self.now,
                                   self.dry_run, only_if_missing=status in ("ok", "pending"))
            notes.append(snap)
            if status == "ok" and snap:
                status = "snapshot"  # catching up on a snapshot an earlier run missed
        topic_note = ensure_topics(repo, section.topics + [topic(a.prefix)], self.dry_run)
        notes.append(topic_note)
        if notes[0] == "repo created":
            status = "created"
        elif status == "ok" and topic_note:
            status = "tagged"
        return status, repo.name, "; ".join(n for n in notes if n)

    def archive_one(self, section, a, student):
        """After the archive date: archive the repo instead of managing access."""
        repo = self.gh.repo(section.org, f"{a.prefix}-{student.username}")
        if repo is None:
            return "skipped", None, None  # counted only: no repo, and none made after archiving
        if repo.archived:
            return "archived", repo.name, None
        if not self.dry_run:
            repo.edit(archived=True)
        return "archived", repo.name, ("would archive" if self.dry_run else "archived") + " (read-only for everyone)"

    def assignment(self, section, a, students):
        """Returns (heading, rows, counts) for one assignment in one section."""
        heading = a.prefix
        archiving = a.archived(self.now)
        if a.release and self.now < a.release:
            return f"{heading}: releases {a.release:%a %b %d, %H:%M}", [], {}
        if archiving:
            heading += f": archived {a.archive_on:%a %b %d}"
        elif a.deadline:
            locked = a.lock_after_deadline and self.now > a.deadline
            heading += f": {'locked' if locked else 'due'} {a.deadline:%a %b %d, %H:%M}"

        if not archiving:
            try:  # check the template once, up front (dry runs too)
                self.gh.template(section.template_for(a))
            except RuntimeError as e:
                return heading, [("-", "failed", None, str(e))], {"failed": 1}

        rows, counts = [], {}
        for i, s in enumerate(students, start=1):
            print(f"    [{i}/{len(students)}] {s.username}", flush=True)
            try:
                if archiving:
                    status, repo_name, note = self.archive_one(section, a, s)
                else:
                    status, repo_name, note = self.one(section, a, s)
            except GithubException as e:
                msg = e.data.get("message", str(e)) if isinstance(e.data, dict) else str(e)
                status, repo_name, note = "failed", None, f"GitHub API {e.status}: {msg}"
            except Exception as e:  # keep going for the rest of the class
                status, repo_name, note = "failed", None, str(e)
            counts[status] = counts.get(status, 0) + 1
            if status != "ok" and note:  # nothing to say = nothing changed; just counted
                rows.append((s.username, status, repo_name, note))
        return heading, rows, counts


ICONS = {"failed": "❌", "pending": "⏳", "ok": "✅", "locked": "🔒", "unlocked": "🔓",
         "archived": "🗄️", "skipped": "➖"}


def render(results, mode, dry_run, now, warnings=()):
    out = [f"## {'DRY RUN · ' if dry_run else ''}{'Hourly check' if mode == 'due' else 'Full check'}",
           f"_{now:%a %b %d %Y, %H:%M %Z}_", ""]
    out += [f"> ⚠️ **{w}**\n" for w in warnings]
    if not results:
        out.append("Nothing to do: no releases or deadlines in the last few hours." if mode == "due"
                   else "No sections matched this run.")
    for section, problems, blocks in results:
        out += [f"### {section.title}", f"`{section.org}` · roster `{section.roster}`", ""]
        out += [f"- ❌ {p}" for p in problems]
        if not section.assignments and not problems:
            out += ["_No assignments yet._", ""]
        for heading, rows, counts in blocks:
            tally = " · ".join(f"{ICONS.get(s, '✅')} {n} {s}" for s, n in sorted(counts.items()))
            out.append(f"**{heading}**" + (f": {tally}" if tally else ""))
            if rows:
                out += ["", "| Student | Status | Repository | Details |", "|---|---|---|---|"]
                for user, status, repo_name, note in rows:
                    link = f"[{repo_name}](https://github.com/{section.org}/{repo_name})" if repo_name else "-"
                    out.append(f"| {user} | {ICONS.get(status, '✅')} {status} | {link} | {note or ''} |")
            out.append("")
    return "\n".join(out) + "\n"


def main():
    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    mode = os.getenv("MODE", "full").lower()
    if mode not in ("full", "due"):
        sys.exit("MODE must be full or due")

    sections = load_all_sections()
    tz = ZoneInfo("America/Vancouver")
    try:
        now = parse_time(os.getenv("NOW"), tz, False, "NOW: ") if os.getenv("NOW") else datetime.now(tz)
    except ConfigError as e:
        sys.exit(str(e))

    changed = None
    path = os.getenv("CHANGED_FILES_PATH")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            changed = {os.path.normpath(l.strip()) for l in f if l.strip()}
    sections = select_sections(sections, os.getenv("ONLY", "").strip(), changed)

    work = []
    for s in sections:
        assignments = due_assignments(s, now) if mode == "due" else s.assignments
        if assignments or (mode == "full" and not s.assignments):
            work.append((s, assignments))  # full runs still check a course with no assignments yet

    results, warnings, token_days = [], [], None
    if work or mode == "full":
        token = os.getenv("GH_ADMIN_TOKEN")
        if not token:
            sys.exit("GH_ADMIN_TOKEN isn't set. Add it under Settings → Secrets and variables → Actions.")
        g = Github(auth=Auth.Token(token), per_page=100)
        if mode == "full":  # daily, on edits and by hand; hourly runs stay quiet
            warning, token_days = check_token(g, datetime.now(tz))
            if warning:
                warnings.append(warning)
                print(f"::warning title=Token expiring::{warning}")
        runner = Runner(GitHubCache(g), now, dry_run)
        for section, assignments in work:
            print(f"\n{section.title} ({section.org})")
            students, problems = read_roster(section.roster)
            blocks = []
            try:
                runner.gh.org(section.org)
                for a in assignments:
                    print(f"  {a.prefix}")
                    blocks.append(runner.assignment(section, a, students))
            except RuntimeError as e:
                problems.append(str(e))
            results.append((section, problems, blocks))
        remaining = g.rate_limiting[0] if g.rate_limiting else None
        if remaining is not None:
            print(f"\nGitHub API calls left this hour: {remaining}")

    text = render(results, mode, dry_run, now, warnings)
    print(text)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text)

    failed = any(p or any(c.get("failed") for _, _, c in b) for _, p, b in results)
    # Scheduled runs only report problems; runs you start (or roster edits) go red,
    # so you hear about a bad username when you add it, not every hour afterwards.
    if failed and os.getenv("GITHUB_EVENT_NAME") != "schedule":
        sys.exit(1)
    # A token about to expire turns every run red, scheduled ones included, so GitHub emails you.
    if token_days is not None and token_days <= TOKEN_FAIL_DAYS:
        sys.exit(1)


if __name__ == "__main__":
    main()
