# ig-queue

Scheduled Instagram posts for @pofulandkids. `schedule.json` lists the posts, `media/` holds their files
(served by GitHub Pages so Instagram can fetch them), and `.github/workflows/instagram.yml` runs
`ig.py due --yes` every 10 minutes. Published posts go to `published_log.json` and their media is removed.
