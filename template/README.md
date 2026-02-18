# My Analysis

[![Launch Notebook](PAGES_URL/assets/launch-badge.svg)](PAGES_URL/launch.html?project=GROUP%2FPROJECT)

## Setup

If you created this project via **create.html**, the badge URL is already
configured — no manual steps needed.

Otherwise, replace the badge URL above:
- `PAGES_URL` → your marimo Pages site URL
- `GROUP%2FPROJECT` → your project's full path, URL-encoded
  (e.g. `my-group%2Fmy-analysis` for `my-group/my-analysis`,
   or just use the numeric project ID from Settings → General)

Or skip the README badge and use a **group-level badge** instead:
Group → Settings → General → Badges, with link
`PAGES_URL/launch.html?project=%{project_id}` — applies to all projects
in the group automatically.

## Usage

1. Edit `notebook.py` with `marimo edit notebook.py`
2. Put any data files in `data/` — they are auto-discovered at launch time
3. Push and click the Launch badge
