# Features

_Current as of 2026-10-23. What's live on hirlumo.com may be older: see "Not deployed yet"
below. How things are built is in [ARCHITECTURE.md](ARCHITECTURE.md); the business case
and pricing in [GO_LIVE_PLAN.md](GO_LIVE_PLAN.md)._

## Current features

### For anyone, no account needed

- **Job search for Canada.** Search by title and place, or type a sentence ("remote HR
  jobs in Toronto over 80k"); spelling is fixed and the sentence is read into filters.
  Radius search ("within 25 km of Victoria"). Signed-out visitors see jobs from company
  careers sites; job-board results are for members (they spend a daily request budget).
- **Only current jobs, each once.** Job-board ads over 30 days old and company-site jobs
  over 60 days old are never shown; a company feed that can't be read for a day is
  hidden until it can. The same company, title and city is one job, from the most
  reliable source.
- **Job pages** for any job, plus browse pages by role and city (`/job-search`), a
  companies page and career resources, all indexable by search engines (sitemap,
  structured data for Google for Jobs).
- **Free Canadian resume check** (`/canadian-resume-check`): upload a PDF or Word resume
  and get two scores, Canada-ready and ATS readability, after a short wait with
  progress steps. Every finding has a documented source; nothing is claimed that
  can't be backed up. A file that isn't a resume (an ID card, a letter, an invoice, a
  job posting) is refused with no score: it needs resume sections, job dates or
  contact details (`canadian.evidence`). The same rule applies to uploads on My resume. Visitors see scores and counts; the details and fixes are for
  members. The file is held in memory for 30 minutes so a new member can fix it
  without uploading again, then deleted; it isn't saved and isn't sent to any other
  service. A member who doesn't want it can delete it at once ("No thanks, delete it").

- **Plans page** (`/plans`): says Hirlumo is in beta and lists every feature with its
  limit, read from the platform settings so it's always current. One plan (Free) today;
  each plan is a card, with left/right arrows, dots and arrow keys once there's more
  than one (`usage/plans.py`).

- **Help centre** (`/help`, footer, account and mobile menus): 14 articles in
  `app/modules/help/articles/*.md`, one per area (getting started, searching, job pages,
  tracking, resume, Canadian check, match analysis, tailoring, alerts, profile, plans,
  account and privacy, feedback, troubleshooting). Each `##` heading is a question; `?:`
  lines list other ways people ask it; `{{ setting }}` quotes live platform limits.
  Searchable at `/help?q=`, each article at `/help/<slug>`, indexed and in the sitemap.
- **Help assistant**: a Help button on every page (signed in or not) opens a chat that
  answers from the articles, with a link to the section it came from and related
  questions. It runs on our own language matching (`help/nlp.py`: synonyms such as
  CV = resume, typo fixing, BM25 ranking, follow-ups), so it's free, instant, unlimited
  and can't invent features; questions it can't answer get "I couldn't find that" and
  Contact us. 60 questions per 10 minutes per visitor. The conversation follows you
  across pages for the browser session.
- **Contact us** (`/contact`, footer and account menu): a signed-in member picks a
  topic and writes a title and message; their name and email come from the account.
  Each message is emailed to hirlumo@gmail.com with the member as Reply-To (so you
  answer by replying) and kept for the admin page. At most 5 messages an hour per
  account. Visitors are asked to sign in, or shown the email address.

### For members

- **Review card**: after 5 minutes of real use (tab in view, clicking or typing in the
  last minute; counted across pages), a card in the corner asks once for 1-5 stars on
  enjoying the jobs, finding enough jobs, the resume builder and the other features,
  plus an optional comment. "Maybe later" asks again in 7 days; "Don't ask again" and
  sending stop it. "Rate Hirlumo" in the account menu opens it any time
  (`app/modules/feedback`).

- **Sign in** by email code or password, or with Google, Facebook or LinkedIn.
  Account settings, sign-out of other sessions, and full account deletion.
- **Job board results** alongside company-site jobs, with a second board as a fallback
  when the first can't answer.
- **Saved jobs** and **application tracking** (saved, applied, interview, offer,
  rejected, archived), with each job's files kept together.
- **Match score and gap analysis** against the member's resume: a reproducible keyword
  score, and an AI fit assessment on request.
- **Tailored resumes** for a job, in the member's own Word design, where every rewritten
  bullet must trace back to one of their own (invented numbers are rejected). DOCX and
  PDF. **Quick Tailor**: paste a job description, get a tailored resume.
- **My resume**: upload (PDF or Word), see what was read, edit section by section in the
  browser.
- **Resume versions**: every upload, edit and conversion is a new version. Compare any
  two (changed, added, removed and moved lines, word by word), download any, and go back
  to any one at any time; nothing is deleted.
- **Canadian resume check and converter**:
  - Checks for a photo, personal details, a declaration, references, high school,
    numeric dates, spelling, length, location, phone, credentials and objective lines.
  - Every suggested change is listed in its own line, under the resume heading it sits
    in, with what changes marked. Members tick single changes, a whole fix, a whole
    resume section, or everything, and see each change before it's made.
  - A Word resume is fixed inside the member's own file, keeping its fonts and design.
    A PDF becomes a Word file in a standard layout.
  - Canadian equivalents of job titles, typed or suggested by AI, added in brackets
    after the original title and never more senior.
  - After converting: download, "See what changed", or undo back to the previous version.
- **ATS readability check and fixes**: tables, text boxes, columns, contact details only
  in the page header, pictures, file size, PDF text that doesn't copy out, missing email
  or phone, unusual headings. Tables (one by one), text boxes (one by one), columns and
  header contact details are fixed inside the Word file on request.
- **Profile** (experience, education, preferences), filled by hand or imported from a
  LinkedIn PDF or data export.
- **Job alerts**: saved searches ("watches") scanned on a schedule, sent by email (after
  every scan, daily or weekly, with one-click unsubscribe) or Telegram.
- **Hourly limits** on searches and AI actions, shown before they're used.
- **Free plan**: lifetime allowances per person, set in Admin → Platform settings and per
  user:
  - **10 job-board searches** in total (a new search that actually reached a job board;
    paging, repeats within the hour, cached answers and failed searches don't count).
    Each scheduled job-alert scan that reaches a job board counts as one too. After that,
    searches still run on company careers-site jobs only, with a note, and alert scans
    skip the job boards.
  - **10 resume actions** in total (match analyses, tailored resumes, Quick Tailor,
    Canadian job titles, and the search box asking the analysis service to read a search
    its rules can't; answers from cache don't count), plus the hourly limit. Members'
    alert scans make no paid analysis calls (rules-only scores, no automatic tailoring).
  - Counted per person, not per account: accounts that share an email address (Gmail
    dots and "+tags" ignored), a phone number or a resume's text share one allowance at
    once; the same name only flags them for an admin. Only keyed one-way hashes are
    stored (key in `config/free_plan.key`), and they and the counts outlive a deleted
    account, so deleting and signing up again doesn't reset them.
  - Shown on the results page, next to resume actions, and on /plans. Limit messages,
    the beta notice and /plans give hirlumo@gmail.com for feedback and higher limits
    (`usage/plans.py` `CONTACT_EMAIL`).

### For administrators

- Users: search, roles, suspension, per-user limits (hourly and the free plan's total);
  every change audited.
- **Duplicate accounts** (`/admin/duplicates`): pairs to review (same name), pairs sharing
  an allowance, and pairs marked as different people; "Same person" / "Different
  people" on each, also on the user's page.
- **Feedback & messages** (`/admin/feedback`): average stars per question, every review
  with its comment, Contact us messages to answer / answered, with Reply and
  "Mark as answered", and **Help questions**: what people asked the assistant or
  searched in the Help centre (no names; kept 180 days), filterable to the ones the
  articles couldn't answer.
- Roles and permissions, feature switches (turn any feature off for everyone).
- Activity and usage dashboards, AI call costs.
- Job sources: company feeds (Greenhouse, Lever, Ashby, SmartRecruiters, Workable), a
  list of 45 checked non-tech Canadian employers to add in one click, and job-board
  status and daily budget.
- Platform settings (limits, job age, session lengths, AI provider and more).
- Health page: database, scheduler, backups (with **Back up now**), accounts, which
  integrations are configured, PDF export, the last 24 hours' activity, and settings.

### Running the site

- One Docker container behind Cloudflare Tunnel; runs as an unprivileged user with a
  read-only filesystem.
- **Data outside the code folder** (`HIRLUMO_DATA_DIR`, `scripts/move-data.sh`) so
  copying in a new version can't replace it; the app won't start on a missing folder.
- **Deploy script** (`scripts/deploy.sh`): runs the tests, copies only code and build
  files, rebuilds and waits until healthy.
- **Nightly backups**: database, resumes, application files and settings; encrypted
  before they leave the server; kept 7 daily, 4 weekly, 6 monthly; restore into a new
  folder only ([BACKUPS.md](BACKUPS.md)).
- Search-engine setup: sitemap, robots, structured data ([REACHING_PEOPLE.md](REACHING_PEOPLE.md)).
- **CI on GitHub** (not deployed yet: needs a push): tests, lint, security analysis (bandit, CodeQL),
  package vulnerabilities, secret scan, Docker build + start + image scan on every push; Dependabot
  updates weekly (README.md, "CI").
- YouTube channel: https://www.youtube.com/@hirlumo

## Not deployed yet

Built and tested in development since the version running on hirlumo.com on 2026-10-22
(check what's live before announcing any of them): per-change choosing on the
Canadian check, version compare and go-back, ATS layout fixes, the "No thanks, delete
it" button for a checked resume, refusing files that aren't resumes, the resume check's wait when reached from a link,
the second job board, SmartRecruiters and Workable feeds, the suggested employers,
the job age and duplicate rules, backups, the data move and the deploy script, the plans page, the free plan's lifetime limit and duplicate-account detection (with the privacy policy and data-deletion page wording for it), the review card, Contact us and the admin Feedback & messages page, the Help centre and help assistant, and tailoring that keeps rewrites: each line gets its length limit, and a rewrite that
comes back too long or with an outcome the line doesn't state ("enhancing user experience") is fixed once or twice
before the checks (before, most rewrites of short resume lines were thrown away as too long), and company-feed jobs labelled with the
workplace the feed states (an Ashby job marked hybrid that allows remote days was shown as Remote).
Also: a search for two jobs joined by "and", "&" or "/" ("HR and Admin", "HR & Admin") is two searches, one per job
(before, it looked for titles containing both words and found almost nothing); "Sales and Marketing Manager" is
"Sales Manager" and "Marketing Manager", and one field's name ("Health and Safety") stays whole.

## Upcoming

### Waiting on the owner

- **Backups off the server**: make the key (`cli.py backup-key`), create the R2 bucket
  and fill in `BACKUP_S3_*`, then press Back up now.
- **Move the data** on the server: `sudo ./scripts/move-data.sh` once.
- **Second job board key** (`JOOBLE_API_KEY`); the integration hasn't been tried with a
  live key yet.
- **Job board licence**: Adzuna's terms need a licence for commercial use and an
  "Adzuna" label on each ad, which conflicts with not naming sources. A decision is needed.
- **Add the suggested employers** in production (Admin → Job sources → Add all).

### Next up (small, ready to build)

- **Resume check files on disk instead of in memory** (encrypted, deleted after 30
  minutes) and a limit on checks running at once. Needed before more users: memory is
  capped, but held files are dropped under load, lost on restart, and don't work with
  more than one app process.
- **Tell visitors more jobs are available after a free sign-in**: signed-out searches
  show company-site jobs only, which can look like very few.
- **Tighten "remove the declaration"** so it stops at a line that looks like a job,
  a date range or another section.
- **Link the YouTube channel** in the footer and the site's structured data.
- **Cloudflare Web Analytics** is blocked by the site's Content Security Policy: allow
  it or switch it off.
- **Quick Tailor with a lost Word file** shows an error page instead of a message.

### Planned

- **Workday careers sites**, which many large employers (universities, hospitals, banks)
  use. Public JSON; not built yet.
- **Faster job pool search** (SQLite full-text search), keeping only Canadian, current jobs.
- **Resume templates** (parked): build a resume from a template, filled by pasting or
  from the profile, saved as a new version. Two questions open: does a built resume
  become the one in use right away, and should the profile gain a summary and bullet
  points?
- **How-to videos** (parked): one-minute videos per feature, with the owner's own intro
  clip. Waiting on the clip and a working OpenAI key.

### Ideas from the go-live plan, not started

Cover letters with the same truthfulness check; a browser extension to tailor for any
posting the member is looking at; Job Bank and newcomer-friendly filters; credential and
regulated-profession hints; French; interview preparation from the gap analysis; an ATS
keyword coverage score; paid plans; and, for agencies, a counsellor dashboard, outcome
reports and their own branding. Paid plans would be added as more cards on /plans.

### When the site grows

PostgreSQL when writes grow; shared state (rate limits, caches, held files) before
running more than one app process; the scheduler in its own container. See "Future
scalability" in [ARCHITECTURE.md](ARCHITECTURE.md).

### Not planned

- Auto-apply bots.
- Scraping or scripting LinkedIn or Indeed.
- Copying a job board's listings into our own store (the terms forbid it without a
  licence, and the free limits couldn't keep up).
