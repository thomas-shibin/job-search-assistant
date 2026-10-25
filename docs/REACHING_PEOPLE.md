# Reaching people: search and organic growth

How Hirlumo gets found and grows without a large marketing budget. The first part
covers what the site already does for search engines. The second part is the
setup still to do, and the rest is the growth plan. Results build slowly: Google
usually takes 4 to 12 weeks to trust a new domain, and job pages tend to show up
first.

## What the site does for search engines

Code: `app/web/seo.py`, with the page tags in `app/web/templates/base.html`.

| What | Where | Why it matters |
|---|---|---|
| Job details Google can read (schema.org `JobPosting`) | Every job page from a company career site (`/jobs/posting/p…`) | Lets the job appear in Google's job search ("Google for Jobs"), the biggest free source of visitors for a job site in Canada |
| Browse pages | `/job-search` and `/job-search/<role>-jobs-in-<city>`, e.g. `software-engineer-jobs-in-toronto`, `remote-devops-jobs` | Each matches a search people actually type. There are 16 roles and 12 cities, plus remote jobs |
| `robots.txt` | `/robots.txt` | Keeps search engines out of accounts, admin, the API and job-board listings |
| Sitemap | `/sitemap.xml` | Lists public pages, browse pages that have jobs, and every open Canadian job from a company career site |
| One official address per page (canonical link) | Public pages | Stops duplicate pages from splitting rankings |
| `noindex` | Search results, login, account pages, empty browse pages | Keeps thin or private pages out of Google |
| Site name, logo and FAQ data (`WebSite`, `Organization`, `FAQPage`) | Home page | Google shows "Hirlumo" as the site name, and the FAQ answers what people search before signing up |
| Keyword-focused titles and descriptions | Home, jobs, companies, resources, sign-up and job pages | These are the text people see in search results |
| Free Canadian resume and ATS check | `/canadian-resume-check` (in the sitemap, the top menu, the home page and the footer) | Matches the searches newcomers make ("Canadian resume format", "ATS resume checker"), and anyone can use it without signing up |
| Speed | All pages | Scripts no longer hold up the page, and browsers keep static files for a year. Google ranks faster sites higher |

Job-board listings are never marked up or indexed: they're members-only and not
ours to republish.

**Keeping the FAQ accurate:** it says accounts, search, saving jobs and alerts are
free. If any of those becomes paid, update the `FAQ` list in `app/web/seo.py`.

**Adding roles and cities:** edit `ROLES` and `CITIES` in `app/web/seo.py`. Choose
them from what Search Console shows people searching.

## Setup to do once

1. **Public address:** set `PUBLIC_URL=https://hirlumo.com` on the server. It's
   already set in `docker-compose.yml` and `.env.prod`. The sitemap and each page's
   official address are built from it.
2. **Google Search Console:** add hirlumo.com as a **Domain** property, verify it
   with a DNS TXT record in Cloudflare, then submit `https://hirlumo.com/sitemap.xml`.
   You can verify with an HTML tag instead by setting `GOOGLE_SITE_VERIFICATION`.
3. **Bing Webmaster Tools:** import the site from Search Console, or verify it with
   `BING_SITE_VERIFICATION`. Bing's index also feeds ChatGPT search and DuckDuckGo.
4. **Check the job markup:** paste a job page address into Google's
   [Rich Results Test](https://search.google.com/test/rich-results). It should show
   a valid job posting.
5. **Analytics:** turn on Cloudflare Web Analytics. It's free and needs no cookie
   banner.

## Growth plan

### 1. Google for Jobs and the browse pages (biggest return)

Each job added from a company career site becomes a page Google can list.
- **Add more companies** in Admin → Job sources. More jobs means more pages, more
  searches matched and more visitors.
- **Add roles and cities over time.** Nurse, accountant, electrician and new-grad
  roles, and Surrey, Hamilton, London and Saskatoon, all have large search volume.

### 2. Write for Canada and newcomers, where Hirlumo can win

The big job sites own searches like "jobs in Toronto". They're weak on questions
like these:
- "Canadian resume format"
- "how to write a resume for Canada as a newcomer"
- "Canadian resume vs US resume"
- "how to get a job in Canada from India"

Publish one useful guide a week under Career resources. End each one with a link
to the free Canadian resume check (`/canadian-resume-check`). It's the hook
nobody else has.

### 3. The free Canadian resume check (built)

`/canadian-resume-check` gives anyone two scores out of 100, with no account
needed: **Canada-ready** (Canadian conventions) and **ATS readability** (whether
applicant tracking software can read the file). Fixing the resume needs a free
account, and the checked file carries over, so people don't upload it again.
It's the main way in for newcomers. Free tools get shared, and other sites link
to them, which raises Google ranking more than anything else.

How the page convinces people:
- **What it costs them:** each problem has a "What happens" line, such as "Job Bank
  says a photo can lower your chances of getting the job".
- **Their own resume, marked up:** the lines with problems are highlighted in red,
  amber or blue.
- **The fixed version:** a preview of the resume after the one-click fixes, with the
  score the same checks give it ("36 → 88"). Guests see the top of it, and the
  rest asks them to sign up.
- **A real countdown:** the time left on the 30-minute hold of their file.
- **Ready resumes:** when there's nothing to fix, the page points people to match
  scores on jobs instead.
- **Details need an account:** visitors see both scores, the number of problems and
  how serious each is (as locked rows), and the before→after score. What each
  problem is, their resume marked up, and the fixed text are only shown after
  sign-up, and none of it is in the page's HTML, so it can't be copied from the
  page source to fix by hand elsewhere.
- **Five at a time (members):** the five most important problems show first, and the rest
  sit behind "Show N more". The guide below shows three expectations in a line each,
  then "And 5 more things employers look for", pointing to the check rather than
  listing them: the check is how people find out the rest.
- **Checking another resume** hides the last results while the new check runs.
- **Checking:** after "Check my resume", the page ticks through six steps over about
  six seconds ("Reading your resume" … "Preparing your results") before the results.
  The steps are worded indirectly, so they don't say what the check looks at.

Rules it keeps, and that should stay (they're what make it safe to share with
settlement agencies and Reddit, and safe under Canada's Competition Act):
- Every rule has a source in `app/modules/resumes/check_sources.py`, and its
  "What happens" line says no more than that source does. Visitors don't see the
  reference links; keep them in the file so any claim can be backed up when asked.
- Nothing claims a resume *will* be rejected. No ATS publishes a pass mark, so the
  ATS check reports what ATS makers document as breaking their parsing, and says
  so on the page.
- The "after" score is measured on the fixed text, never assumed. No fake
  counters, timers or findings.
- Don't quote "75% of resumes are rejected by ATS". It traces back to a 2012 sales
  pitch, not a study.

How to use it:
- Link straight to it in every Reddit answer, group post, video and partner email.
  It works the moment someone clicks.
- Track it in Search Console under "Canadian resume format" and "Canadian resume
  checker" searches.
- Ask settlement agencies and college career centres to link to it as a free
  resource.

### 4. Go where newcomers and students already are

- **Reddit:** r/ImmigrationCanada, r/PersonalFinanceCanada, r/cscareerquestionsCAD
  and city subreddits. Answer resume questions properly and mention the tool only
  when it fits.
- **Groups:** Facebook groups for newcomers by country (for example "Indians in
  Toronto" or "Filipinos in Calgary"), and WhatsApp and Telegram job groups.
- **Institutions:** university and college career centres, and newcomer
  settlement agencies such as ACCES Employment, COSTI and S.U.C.C.E.S.S. Offer them
  a free tool for their clients. A link from one of their sites is worth hundreds
  of posts.

### 5. Short videos

Post 30–60 second clips on TikTok, Instagram Reels and YouTube Shorts. Examples:
- "3 things to remove from your resume in Canada"
- "I tailored my resume to this Shopify job in 20 seconds"

The parked how-to video pipeline fits this directly.

The channel is **https://www.youtube.com/@hirlumo** (handle set 2026-10-22). Its
description names no job-board vendors and promises no ATS pass or hire, in line
with the resume check. Link it from the site's footer and structured data (`sameAs`)
so search engines connect the two (not done yet).

### 6. Turn users into marketers

- **Sharing:** after someone saves or applies, prompt them to share the job.
  Shared job links bring in new people.
- **Invites:** add "Invite a friend", rewarded with something cheap like extra AI
  tailoring credits. Friends' sign-ups already show word of mouth works.
- **Alerts:** put the best-match job in the weekly alert email's subject line.
  Opened emails bring people back without any spending.

### 7. Launch posts (one-time spikes)

Post on Product Hunt, Hacker News ("Show HN"), BetaKit, r/SideProject and
LinkedIn. A personal LinkedIn story ("I built a job search tool for Canadians")
usually does better than ads.

### 8. Measure and adjust

Check Search Console every week:
- **Which searches show Hirlumo:** turn the strongest into new browse pages or
  guides.
- **Pages with many views but few clicks:** rewrite their titles and descriptions.
- **Indexing problems:** fix pages Google reports as excluded, especially job pages.
