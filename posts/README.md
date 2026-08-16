# Writing a new post

1. Create a file in this folder, e.g.  my-new-essay.md
2. First line:  # The title of the post      (then a blank line, then your text in Markdown)
3. Publish:     python3 tools/sync.py --push   (from the blog folder, in Terminal)

Optional front matter instead of the '# Title' line:

    ---
    title: "The title"
    subtitle: "One line under the title"
    date: 2026-09-01
    ---

Images: put them in assets/img/<anything>/ and reference them as
![description]({{ site.baseurl }}/assets/img/<anything>/picture.jpg)

Unfinished pieces can live in ../drafts/ until you move them here: that folder is ignored by git,
so drafts stay on this Mac and are never uploaded (remember the repository itself is public).
Editing a file here and re-running the sync updates the post; its date and address stay the same.
