# Sarnia Coaches Multi-URL Status Checker

This project checks two RegisterOBA pages for occurrences of "Sarnia", saves snapshots, and can notify Slack when records are added or removed.

The site now displays two explicit statuses and supports transition tracking:

- Certified
- In Progress

## Monitored URLs

- https://www.registeroba.ca/certified-coaches
- https://www.registeroba.ca/certification-inprogress-by-local

## Data Display

GitHub Pages reads data from docs/status.json and renders:

- Dashboard summary on docs/index.html
- Detailed Certified list on docs/certified.html
- Detailed In Progress list on docs/in-progress.html
- Compiled static summary on docs/current-summary.html

Each coach row includes name, level, position, and association.

## Transition Monitoring

The script compares the previous in-progress set against the latest certified set.
When a matching coach row appears in Certified, it is written to the transitions section in docs/status.json.

Current generator script:

- check_multi.py

Dynamic loading note:

- RegisterOBA pages are JavaScript-rendered and load in delayed batches.
- The script uses Playwright plus Wix cloud-data responses so it captures full result sets reliably.

Run locally:

1. pip install -r requirements.txt
2. python check_multi.py

This regenerates docs/status.json.

It also regenerates docs/current-summary.html, which is a static snapshot page containing both status tables.

## GitHub Repository Setup (robwinship)

Use this if your GitHub repo will be under the account `robwinship`.

1. Create a new repository on GitHub (example name: `coach-certification`).
2. Clone the repository:
   - SSH: `git clone git@github.com:robwinship/coach-certification.git`
   - HTTPS: `git clone https://github.com/robwinship/coach-certification.git`
3. Copy project files into the repo folder:
   - `check_multi.py`
   - `requirements.txt`
   - `.github/workflows/update_status.yml`
   - `.gitignore`
   - `README.md`
4. Create snapshots folder:
   - `mkdir snapshots`
5. Commit and push:
   - `git add .`
   - `git commit -m "Initial commit: Sarnia coaches checker"`
   - `git push origin main`

## GitHub Pages Setup

GitHub Pages can host documentation and project info, but it does not run your Python checker. The checker should run via GitHub Actions.

For a project page, your site URL will be:

- `https://robwinship.github.io/coach-certification/`

Steps:

1. Add a page file, for example `docs/index.md` (or `docs/index.html`).
2. In GitHub, open repository Settings > Pages.
3. Set Source to "Deploy from a branch".
4. Select Branch: `main`, Folder: `/docs`.
5. Save and wait for deployment.

If you prefer a user site (`https://robwinship.github.io/`), use a repository named `robwinship.github.io`.

## Optional Slack Secret

If you want Slack notifications:

1. Open GitHub repository Settings > Secrets and variables > Actions.
2. Select New repository secret.
3. Name: `SLACK_WEBHOOK_URL`
4. Value: your Slack incoming webhook URL.

## Test Locally

1. Create virtual environment:
   - Windows PowerShell: `python -m venv venv`
   - macOS/Linux: `python3 -m venv venv`
2. Activate environment:
   - Windows PowerShell: `venv\\Scripts\\Activate.ps1`
   - macOS/Linux: `source venv/bin/activate`
3. Install dependencies:
   - `pip install -r requirements.txt`
4. Run checker:
   - `python check_multi.py`

If `SLACK_WEBHOOK_URL` is not set, the script should still create snapshot files in `snapshots/`.

## Notes

- Workflow schedule can be adjusted in `.github/workflows/update_status.yml`.
- If pages are JavaScript-rendered, switch fetch logic to Playwright.
- Review https://www.registeroba.ca/robots.txt and site terms before scraping.

## Changelog And Releases

- Changelog file: `CHANGELOG.md`
- Release process convention: `RELEASE_CONVENTION.md`

Recommended release flow:

1. Keep ongoing entries in `CHANGELOG.md` under Unreleased.
2. Cut a version section in `CHANGELOG.md`.
3. Tag release as `vX.Y.Z`.
4. Publish a matching GitHub Release.
