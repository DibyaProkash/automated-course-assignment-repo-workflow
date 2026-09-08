# Automated Student Repository Creation from Template

This repository contains a GitHub Actions workflow and a Python script to automatically create individual student assignment/project repositories from a template repository within a GitHub Organization. It reads a CSV roster, generates private repositories for each student, and adds the student as a collaborator with push access.

## Overview

- Workflow: `.github/workflows/setup.yml` – triggers the automation manually via the GitHub UI.

- Script: `setup_class.py` – uses the PyGithub library to interact with the GitHub API.

- Roster: `class_roster.csv` – a CSV file containing student names and GitHub usernames.

The script performs the following steps for each student:

1. Creates a new private repository in your organization from a specified template.

2. Names the repository using a consistent pattern (e.g., `php-project-26FA-<github_username>`).

3. Adds the student as a collaborator with `push` permission.

## Prerequisites

Before setting up this automation, ensure you have:

- A GitHub Organization where the student repositories will be created.

- A template repository inside that organization. This template should contain the starter code or files for the assignment/project.

- Administrative access to the organization (to create repositories and add collaborators).

- A GitHub Personal Access Token (PAT) with the necessary permissions (see Secret Setup below).

- A CSV file (`class_roster.csv`) with student information.

## Setup Instructions

1. Create the Template Repository

- In your GitHub organization, create a new repository that will serve as the template (e.g., `php-project-26FA`).

- Populate it with the initial code, README, or any files students should start with.

- The template can be public or private - the script will work either way as long as the token used has access to it.

2. Create the Automation Repository

- Create a new repository (e.g., `repo-automation`) in your organization. This repository will hold the workflow, script, and CSV file.

- It is recommended to keep this repository private to avoid exposing student data.

3. Add the Workflow File

Create `.github/workflows/setup.yml` with the following content:

```yaml

name: Setup Student Repos

on:
workflow_dispatch: # Manual trigger from GitHub UI

jobs:
setup-repos:
runs-on: ubuntu-latest
steps: - name: Checkout repository
uses: actions/checkout@v3

      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.10'

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install PyGithub pandas

      - name: Run Setup Script
        env:
          GH_ADMIN_TOKEN: ${{ secrets.GH_ADMIN_TOKEN }}
        run: python setup_class.py

```

4. Add the Python Script

Place `setup_class.py` in the root of the repository. The script is provided below, but you will need to modify the following variables:

- `org_name` - your GitHub organization name.

- `template_repo_name` - the name of the template repository.

- Optionally adjust the repository naming convention and privacy settings.

```python

import os
import pandas as pd
from github import Github
import github.Auth
import time

# 1. Authentication

token = os.getenv("GH_ADMIN_TOKEN")
if not token:
raise ValueError("GH_ADMIN_TOKEN environment variable not set!")

auth = github.Auth.Token(token)
g = Github(auth=auth)

org_name = "YOUR_ORG_NAME" # <-- CHANGE THIS
org = g.get_organization(org_name)

# 2. CSV READING

csv_file = "class_roster.csv"
roster = None

encodings_to_try = ['utf-8-sig', 'utf-8', 'utf-16', 'cp1252', 'latin1']

for enc in encodings_to_try:
try:
roster = pd.read_csv(csv_file, encoding=enc, on_bad_lines='skip', engine='python')
print(f"Successfully read CSV using '{enc}' encoding.")
print(f"Note: {len(roster)} valid rows loaded.")
break
except (UnicodeDecodeError, UnicodeError):
continue

if roster is None:
raise ValueError(f"Could not read {csv_file}. Please ensure it is a valid CSV file.")

template_repo_name = "YOUR_TEMPLATE_REPO" # <-- CHANGE THIS

print(f"Successfully loaded roster with {len(roster)} students.")
print(f"Fetching template repository: {template_repo_name}...")

# Verify template repo exists

try:
template_repo = org.get_repo(template_repo_name)
except Exception as e:
raise ValueError(f"Could not find template repo '{template_repo_name}'. Error: {e}")

# 4. Loop through students and create repos

for index, student in roster.iterrows():
gh_username = str(student['github_username']).strip()

    student_name = "Student"
    if 'first_name' in roster.columns:
        student_name = str(student['first_name']).strip()

    repo_name = f"{template_repo_name}-{gh_username}"   # Adjust naming if needed

    print(f"\n[{index + 1}/{len(roster)}] Creating repo {repo_name} for {gh_username}...")

    try:
        # Check if repo already exists to prevent errors on re-runs
        try:
            org.get_repo(repo_name)
            print(f"  -> Skipped: Repo {repo_name} already exists.")
            continue
        except Exception:
            pass # Repo doesn't exist, proceed to create

        # --- Use the official "Generate from Template" endpoint ---
        headers, data = org._requester.requestJsonAndCheck(
            "POST",
            f"/repos/{org_name}/{template_repo_name}/generate",
            input={
                "owner": org_name,
                "name": repo_name,
                "private": True,
                "description": f"Assignment for {student_name}",
                "include_all_branches": False
            }
        )

        # Add the student as a collaborator with push access
        new_repo = org.get_repo(repo_name)
        new_repo.add_to_collaborators(gh_username, permission="push")

        print(f"  -> Success! Repo created from template and {gh_username} added as collaborator.")

        # Sleep to prevent hitting GitHub API secondary rate limits
        time.sleep(2)

    except Exception as e:
        print(f"  -> FAILED: {e}")

print("\n--- SCRIPT FINISHED ---")

```

5. Prepare the Class Roster CSV

Create a file named `class_roster.csv` in the root of the automation repository. The script expects at least a `github_username` column. An optional first_name column can be used for repository descriptions.

Example CSV:

```plaintext

first_name,github_username
John Doe,johndoe
Jane Smith,janesmith
Alex Lee,alexlee

```

- Encoding: The script attempts multiple common encodings (`utf-8-sig`, `utf-8`, `utf-16`, `cp1252`, `latin1`) to handle files exported from Excel or other tools.

- Invalid rows are skipped automatically.

- Ensure the `github_username` values are the exact GitHub usernames of your students.

6. Configure GitHub Secret

The workflow requires a secret named `GH_ADMIN_TOKEN`. This token is used by the Python script to authenticate with the GitHub API.

1. In your automation repository, go to **Settings → Secrets and variables → Actions**.

2. Click **New repository secret**.

3. Enter `GH_ADMIN_TOKEN` as the name.

4. Paste a Personal Access Token (PAT) as the value.

### Token Permissions Required:

- The token must belong to a user who is an **owner** of the organization (or has sufficient admin rights).

- Scopes needed:
  - `repo` (full control of private repositories) – to create repositories and add collaborators

  - `admin:org` - to manage organization members (needed if you want to add students as outside collaborators).

- Alternatively, a fine-grained PAT with appropriate repository and organization permissions can be used.

**Security Note:** Treat this token like a password. Never commit it to the repository. Using GitHub Actions secrets ensures it is encrypted and only exposed during workflow runs.

7. Commit and Push

After adding all files (`.github/workflows/setup.yml`, `setup_class.py`, `class_roster.csv`), commit and push them to the `main` branch of your automation repository.

## Usage

1. Navigate to your automation repository on GitHub.

2. Click the **Actions** tab.

3. In the left sidebar, select the **Setup Student Repos** workflow.

4. Click the **Run workflow** button.

5. Optionally select the branch (usually `main`) and click **Run workflow** again to confirm.

6. The workflow will start, and you can monitor the progress in the Actions log.

When the script finishes, each student will have a new private repository named according to the pattern defined in `setup_class.py`, and the student will be added as a collaborator with push access.

## Customization

You can easily adapt the script and workflow for different courses or assignments by modifying:

- Organization name (`org_name`) - change to your GitHub organization.

- Template repository (`template_repo_name`) – change to the appropriate template.

- Repository naming – adjust the `repo_name` line to follow your preferred pattern.

- Privacy – set `"private": False` if you want public repositories (not recommended for assignments).

- Collaborator permission – change `"push"` to `"admin"` or `"read"` as needed.

- Branch inclusion – set `"include_all_branches": True` if your template has multiple branches you want to copy.

You can also modify the CSV parsing logic to support additional columns or different data formats.

## Troubleshooting

### Script fails with "GH_ADMIN_TOKEN environment variable not set"

- Ensure the secret name in the repository settings matches exactly (`GH_ADMIN_TOKEN`).

- Check that the workflow file references `${{ secrets.GH_ADMIN_TOKEN }}` correctly.

### CSV reading errors

- The script tries multiple encodings. If none work, open the CSV in a text editor and save it as UTF-8 explicitly.

- Make sure the header row contains `github_username` (and optionally `first_name`). Extra columns are ignored.

### "Could not find template repo"

- Verify that `template_repo_name` matches exactly (case-sensitive).

- Ensure the token has access to the template repository. If the template is private, the token must have `repo` scope and belong to a user with read access.

### Repository creation fails with 403 or 404

- Check that the token has `repo` scope and the user is an org owner/admin.

- The organization may have restrictions on repository creation (e.g., only members can create repos). Adjust settings or use a different token.

### Collaborator addition fails

- Confirm the GitHub username is spelled correctly and the student has a GitHub account.

- If the student is not yet a member of the organization, they will receive an email invitation. The script adds them as an outside collaborator, which is fine.

- The token must have permission to add collaborators (usually requires admin rights on the repo or org).

### Rate limit issues

- The script includes a 2‑second delay between repository creations. If you have many students, the run may take a while but should avoid secondary rate limits.

- If you still hit limits, increase the sleep time or run the workflow for smaller batches.

### Re-running the workflow

- The script checks if a repository already exists and skips it, so it is safe to re-run. No duplicate repos will be created.

## Additional Notes

- The script uses the GitHub API endpoint `/repos/{owner}/{template_repo}/generate` to create a repository from a template. This is the same mechanism used by the GitHub UI's "Use this template" button.

- All student repositories are created as **private** by default. This can be changed in the script if needed.

The workflow triggers manually via `workflow_dispatch`; you can also schedule it (using `schedule`) or trigger on push if desired.

---

By following these instructions, you can automate the creation of student repositories with minimal effort and ensure consistency across your class or project cohort.
