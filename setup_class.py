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

org_name = "nic-dgl123-26FA-cvs1" 
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

template_repo_name = "php-project-26FA"

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

    repo_name = f"php-project-26FA-{gh_username}"
    
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
                "description": f"DGL123 Project for {student_name}",
                "include_all_branches": False # Set to True if your template has multiple branches you want to keep
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
