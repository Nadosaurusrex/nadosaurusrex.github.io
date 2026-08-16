# Blog

A plain Jekyll site published by GitHub Pages (built by GitHub on every push; no local tooling needed).

- Posts live in _posts/ (generated).  Write new ones as Markdown files in posts/ and run
  python3 tools/sync.py --push
- Look & feel: _layouts/ and assets/css/site.css (no theme gem, no build step).
- Settings: _config.yml (title, description, author).
- SETUP_LOG.md (kept locally, git-ignored) records what the automated setup did and decided.
