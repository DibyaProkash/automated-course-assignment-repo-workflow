# Automated course assignment repos

A replacement for GitHub Classroom, built on GitHub Actions. **One private repo manages every course and section you teach.** You describe each course once, keep one roster per section, and the workflow keeps GitHub matching them:

- **New students and assignments** get their repos within a minute of your commit. Only the affected section is touched.
- **Assignments with a release date** stay dormant until that date, then the repos are created.
- **At the deadline**, students drop to read-only, honouring per-section deadlines and per-student extensions. The last commit is tagged `submission` at that moment, so you always know what was handed in.
- **Expired invitations** (GitHub expires them after 7 days) are re-sent every morning.
- **At the end of term**, repos are archived on the date you set.
- **Before your token expires**, you get two weeks' warning.

```
courses/dgl123.yml          one file per course: assignments + sections
courses/dgl113.yml
rosters/dgl123-cvs1.csv     one roster per section
rosters/dgl123-cvs2.csv
rosters/dgl113-cvs1.csv
```

## One-time setup

1. **Keep this repo private.** The rosters contain student names. It can live on your own account or in any organization.
2. **Create a token that reaches every course organization.** Use a **classic** personal access token with the `repo` scope, created by an account that is an owner of every course org, and set it to expire after the term. Fine-grained tokens can't span several organizations, so they won't work here.
3. **Add the token as a secret.** Go to Settings → Secrets and variables → Actions and add it as `GH_ADMIN_TOKEN`.
4. **Set each course org's base permission to _No permission_.** Go to Org settings → Member privileges. Otherwise locking at the deadline has no effect on org members.
5. **Mark every starter repo as a template.** In each starter repo, go to Settings → General → _Template repository_.

## A course file

```yaml
# courses/dgl123.yml
course: DGL 123
course_topic: dgl123-fall-2026
timezone: America/Vancouver
template_org: nic-dgl123-26FA-templates # optional: one copy of each template for all sections

assignments:
  - template: php-project-26FA # repos: php-project-26FA-<username>
    deadline: 2026-12-05 23:59
  - template: php-a2-26FA
    release: 2026-10-14 09:00
    deadline: 2026-10-28 23:59
    extensions:
      some-username: 2026-10-31 23:59

sections:
  - name: cvs1
    organization: nic-dgl123-26FA-cvs1
    roster: rosters/dgl123-cvs1.csv
  - name: cvs2
    organization: nic-dgl123-26FA-cvs2
    roster: rosters/dgl123-cvs2.csv
    overrides: # only where this section differs
      php-a2-26FA:
        deadline: 2026-10-29 23:59
```

**Assignment fields**

| Field                 | Meaning                                                                                                                  |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `template`            | Template repo name. It's looked up in `template_org` if set, otherwise in the section's own org. `owner/name` also works |
| `prefix`              | Student repos are named `<prefix>-<username>`. Defaults to the template name                                             |
| `visibility`          | `private` (default) or `public`. Public repos never include the student's name                                           |
| `release`             | Date the repos are created. If blank, they're created right away                                                         |
| `deadline`            | Date students lose write access. A date with no time means 23:59 that day                                                |
| `lock_after_deadline` | Set to `false` to keep write access after the deadline                                                                   |
| `archive_on`          | Date the repos are archived. Can be set for the whole course, for one assignment, or in a section's `overrides`          |
| `extensions`          | Per-student deadlines, keyed by GitHub username                                                                          |

**Section fields:** `name`, `organization` and `roster` are required. `archive_on` archives the whole section on its own date. `overrides` can change `release`, `deadline`, `lock_after_deadline`, `archive_on` or add `extensions` for one assignment in that section only. A section can also set its own `template_org`.

Several sections can share one organization if you prefer. Repo names stay unique because each student is in only one section. Every repo is tagged with the course topic, a section topic (e.g. `dgl123-cvs1`) and the assignment, so you can filter them on GitHub.

## Rosters

Each roster is a CSV with a `github_username` column. A `first_name` or `name` column is optional and is used in private repo descriptions. Exports from Excel and Google Sheets work as-is. **Use GitHub usernames, not college IDs.** If an ID happens to match someone else's GitHub account, that person gets invited. Missing usernames, unknown usernames and duplicates are flagged in the run summary.

## When things run

| When                                            | What it does                                                                                                                       |
| ----------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| You commit a course file or roster              | Full check of the sections that file affects                                                                                       |
| Every hour                                      | Handles only releases, deadlines and extensions from the last 3 hours. Most hours there's nothing to do and it finishes in seconds |
| Daily, about 6 am Pacific                       | Full check of everything: re-sends expired invitations, and repairs anything missed                                                |
| Actions → **Sync Student Repos** → Run workflow | Dry run by default. _only_ limits it to `dgl123` or `dgl123/cvs1`. _now_ previews a future moment, such as just after a deadline   |

Each run ends with a summary per section that lists everything not yet done: pending invitations, locks and failures. A run you start, or one triggered by a commit, turns red if anything failed. The scheduled runs only report problems, so they won't email you every hour about the same bad username.

## Submission snapshots

When a repo locks at the deadline, the run tags its last commit `submission`. This is an annotated tag that records the deadline and the time it locked. In the repo it's under **Tags**, and you can clone exactly that state with `git clone --branch submission <repo>`.

- **Late commits are flagged.** If the last commit is dated after the deadline, the run summary flags it with ⚠️. Commit dates come from the student's computer, so treat this as a prompt to look, not proof.
- **Reopen and relock adds a new tag.** If you reopen a repo (an extension or a later deadline) and it locks again with new commits, it's tagged `submission-2`, then `submission-3`. Earlier tags are kept.
- **Your feedback commits don't count.** Commits you push to a locked repo don't create a new snapshot.
- **Missed snapshots are caught up.** If a locked repo has no snapshot at all, for example because tagging failed, the next run adds one.

## Archiving at the end of term

Add `archive_on: 2027-01-15` to a course file. On that date the run archives every student repo in the course, which makes them read-only for everyone, you included, until you unarchive them on GitHub. After the archive date, no new repos are created for that course, even if you add students. A repo that's already archived, whether by the run or by hand, is never changed or unarchived.

## Token expiry

The daily check reads the token's expiry date from GitHub.

- **14 days before expiry:** each run shows a warning at the top of its summary.
- **7 days before expiry:** every run turns red, scheduled ones included, so GitHub emails you.
- **After it has expired:** the run stops with a message saying exactly that.

To fix it, create a new classic token with the `repo` scope and save it over the `GH_ADMIN_TOKEN` secret.

## Day-to-day

| You want to…             | Do this                                                                                      |
| ------------------------ | -------------------------------------------------------------------------------------------- |
| Add a course             | Add `courses/<course>.yml` and its rosters                                                   |
| Add a section            | Add an entry under `sections` and a roster                                                   |
| Release an assignment    | Add it under `assignments`, with a `release` date if it shouldn't open yet                   |
| Add a late enrolment     | Add a row to that section's roster                                                           |
| Give an extension        | Add the student under the assignment's `extensions`                                          |
| Reopen an assignment     | Move its `deadline` later. Locked students get write access back on the next run             |
| Archive at term end      | Add `archive_on` to the course file                                                          |
| Grade what was submitted | Use the `submission` tag in each repo                                                        |
| Start a new term         | Copy `courses/`, update the dates, organizations and template names, and replace the rosters |

Students are never removed and repos are never deleted. Taking someone off a roster only stops new repos being created for them.
