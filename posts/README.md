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

## A piece in two languages (Italian + English)

- English edition:  posts/<slug>.md       front matter as above plus   slug: <slug>   lang: en
- Italian edition:  posts/<slug>.it.md    the SAME  slug:  and  date:, plus   lang: it
- On BOTH files add   written_in: it   (or en): the language the piece was written in. Each page
  then carries one small line under the title: the Italian page «C'è anche in inglese. Read in English →»,
  the English page "Written in Italian; this is the English edition. Leggi in italiano →".
- Always write  slug:  explicitly on both files (the title «L'abbecedario.» would otherwise become
  'labbecedario', and two files whose slugs differ are two unrelated pieces). Put the subtitle also in
  description:  so link previews show it in the right language.
- Run the sync as usual; it must say "2 imported or updated". If it warns "would overwrite … skipped", the two
  files resolved to the same language (usually a forgotten  lang: it): fix the front matter, delete the one
  generated file it names in _posts/ (the one sanctioned hand-touch there) and run again. The English edition is published at
  /p/<slug>/ (generated file _posts/DATE-<slug>.md), the Italian at /it/p/<slug>/ (_posts/DATE-<slug>.it.md).
  The home page (/) and the Italian home (/it/) each list the piece once, in that page's language.
- A piece that exists in one language only needs nothing special: it is listed on the other home with a
  small «in inglese» / "in Italian" tag, and gains its twin later by adding the second file.
- Never set  permalink:  yourself and never hand-edit _posts/. If two editions really need different
  slugs, give both files the same   ref: <anything>   line.
