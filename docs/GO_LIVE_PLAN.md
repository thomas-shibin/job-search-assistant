# Go-Live Plan — Job Search Assistant (Canada)

_Prepared 2026-10-10. Figures are estimates in CAD unless noted._

## 1. Verdict

- **General consumer "AI job-search assistant" in Canada: it won't work.** The market is
  crowded, free ChatGPT is a substitute, and there's no job-data advantage.
- **A niche product for newcomers to Canada, sold to them and to the agencies and colleges
  that serve them: worth a 4-week, near-zero-cost validation test.** The realistic upside
  is a side business earning about $2–10k/month within 18–24 months. It is not a
  venture-scale company.
- Commit significant time only if real people and organizations say they will pay.

## 2. What the app has today

### Strengths (real differentiators)

- **Keeps the user's own DOCX design.** Only the wording changes. Most competitors force
  their own templates.
- **Truthfulness guardrail.** Every rewritten bullet traces back to a source bullet, and
  invented numbers are rejected. Marketable as "AI that can't lie for you."
- **Fit scoring, gap analysis and watchlist alerts** are already built.
- **Solid engineering:** migrations, tests, cost tracking, application state machine.

### Weaknesses (blocking as-is)

- **Single-user local install.** Needs Python, LibreOffice and OpenAI, Adzuna and Telegram
  keys. Almost no job seekers will set that up.
- **Weak job data.** Adzuna's free tier is about 250 requests/day and commercial use needs
  an agreement. Greenhouse and Lever cover mostly tech companies. LinkedIn and Indeed
  can't be scraped.
- **Telegram alerts.** Few Canadians use Telegram; email, SMS or push is needed.
- **Address-swap feature is a reputational risk.** Swapping the resume location to the
  job's city conflicts with the "truthful" positioning. Reframe it as "relocating to X" or
  don't market it.

## 3. Competitors

| Competitor | What they do | Price (approx.) | Threat |
|---|---|---|---|
| ChatGPT / Claude / Gemini | Paste the job and resume, get a tailored version | Free–$20 USD/mo | Highest |
| LinkedIn Premium / Indeed AI | Built-in job match, AI resume and cover-letter help | $40–60/mo / free | Very high (they own the jobs) |
| Teal | Tracker, resume builder, Chrome extension | Free / ~$29 USD/mo | High |
| Jobscan | ATS keyword match score | ~$50 USD/mo | Medium-high |
| Simplify, Jobright.ai, Careerflow, Huntr | Autofill, AI copilot, tracking | Free–$40 USD/mo | High |
| Rezi, Kickresume, Enhancv | AI resume builders | $20–30 USD/mo | Medium |
| Auto-apply tools (LazyApply etc.) | Mass applications | $50–250 one-time | Low (poor reputation) |
| Job Bank, Talent.com, Jobillico, Magnet | Canadian job boards and matching | Free | Medium for data, low for tailoring |

Unit economics in the general market don't work. Acquiring a paying user costs about
$30–80, users churn as soon as they're hired, and lifetime value is about $30–60.

## 4. Target niche: newcomers to Canada

- **Size:** about 380k permanent residents a year under the 2026 targets, plus a large
  temporary-resident and international-student population. Unemployment among recent
  immigrants and youth is well above the national rate.
- **Pain that US tools ignore:**
  - Converting foreign-style resumes to Canadian norms.
  - The "no Canadian experience" problem.
  - Regulated professions and credential recognition.
  - French and English requirements.
  - Knowing which employers hire newcomers.
- **Buyers with budgets:** IRCC-funded settlement agencies, college career centres and
  career coaches. They already pay for tools and must report employment outcomes.
  **B2B is the main revenue engine.**
- **Fit with the guardrail:** agencies can't be seen helping clients fabricate
  experience. Truthful tailoring with an audit trail is exactly what they need.

## 5. Features to add (prioritized)

### Must have before launch

1. **Hosted multi-user web app:** auth, Postgres, Stripe billing, per-user storage and a
   "delete my data" option. The existing FastAPI + HTMX stack is fine.
2. **Chrome extension, "Tailor for this job":** the user captures any LinkedIn, Indeed or
   Job Bank posting with one click. This solves job data legally because the user
   initiates it and nothing is scraped.
3. **Email (and optionally SMS) alerts** in place of or alongside Telegram.
4. **Canadian resume converter** (headline feature): flag and fix photo, date of birth,
   marital status, length, spelling and date formats, and translate foreign job titles.
5. **Cover letter generator** using the same truthfulness guardrail.

### Niche differentiators

6. **Job Bank integration** plus newcomer-friendly, bilingual and work-permit filters.
7. **Regulated-profession and credential hints** (P.Eng, CPA, RN pathways; link to WES).
8. **French and English** UI and resume output.
9. **Interview prep** generated from the existing gap analysis.
10. **ATS keyword coverage score** to compete with Jobscan.

### B2B (revenue engine)

11. **Counsellor dashboard:** one counsellor manages 50–200 clients.
12. **Outcome reports:** applications, interviews and hires, exportable for funders.
13. **White-label / agency branding.**

### Do not build

- **Auto-apply bots.** They violate job-board terms, get user accounts banned and destroy
  the trust positioning.

## 6. Phased plan with gates

### Phase 0: Validate (weeks 1–4). No new code.

- Interview 20–30 newcomers who are job hunting. Find them in LinkedIn groups, Reddit
  (r/ImmigrationCanada), Facebook and WhatsApp newcomer groups.
- Interview 5–10 people at settlement agencies, college career centres and career
  coaching practices. Ask what they use now, what they'd pay, and who approves purchases.
- Put up a one-page landing site ("Turn your resume into a Canadian resume, truthfully")
  with a waitlist.
- Run a concierge pilot: process 10 people's resumes with the local app yourself and send
  them the results.
- Tell pilot users plainly how their resume is used and that it goes to OpenAI for
  processing. Delete their files afterwards.
- **Gate:** 150+ waitlist signups, 5+ people asking to pay, and 1–2 organizations agreeing
  to a free pilot. **If the gate is missed, stop.**

### Phase 1: MVP (months 2–4)

- Build features 1–5 and launch to the waitlist.

### Phase 2: Launch and pricing (months 4–6)

- **Free:** resume conversion check, 3 tailored resumes/month, weekly email alerts.
- **Job Hunt Pass:** $29 for 3 months, or $12/month.
- **Agencies:** $5–10 per client seat per month, or a $3–10k/year site licence.
- **Channels:**
  - Daily LinkedIn and TikTok before/after resume makeovers.
  - Free workshops at libraries and agencies.
  - Partnerships with immigration consultants and newcomer communities.
  - SEO pages such as "Canadian resume format for nurses / engineers / accountants".

### Phase 3: B2B (months 6–12)

- Build features 11–13 for pilot agencies and convert pilots into paid contracts.
- Agency procurement takes 3–9 months, so start those conversations in Phase 0.

### Decision point (month 12)

- **Continue** with 200+ paying consumers or 2+ paying organizations, plus good retention
  while users are actively job-searching.
- **Stop, or keep it as a portfolio project,** with under $1k MRR and no paying
  organization.

## 7. Budget: spend only when it's needed

Spend money only to remove a blocker that stops someone from paying, never in
anticipation.

### Phase 0: Validation (about $0–100)

| Item | Spend |
|---|---|
| Landing page + waitlist (Carrd, Tally, Google Forms or a free tier) | $0–20 |
| Domain | ~$15–20/yr |
| Concierge pilot (local app, own OpenAI key) | Under $5 |
| Interviews | $0 (time only) |
| Incorporation, lawyer, hosting, paid ads | **$0 — skip** |

### Phase 1: MVP (about $50–150/month)

- Cheap VPS or free tier: about $10–25/month at first.
- Template privacy policy and terms (Termly, iubenda or similar): about $0–15/month.
- Sole proprietorship: about $0–60 to register a business name, depending on the province.

### Phase 2+: Only once revenue arrives

| Item | When | Cost |
|---|---|---|
| Incorporation | Revenue is real or liability matters | $500–1,000 |
| Lawyer for privacy (PIPEDA, Quebec Law 25) and contracts | First agency contract | $1,500–3,000 |
| Hosting at scale | Real traffic | $50–150/month |
| LLM costs (~$0.01–0.05 per tailored resume) | Grows with usage | $20–200/month |
| Job-data licence (Adzuna or similar) | Users demand job discovery beyond the extension, Job Bank and ATS boards | Negotiated |
| Marketing | Organic channels show what converts | $0–500/month |

### Summary

- **Before the first paying user:** under $300 total.
- **First 6 months after launch:** about $1–2k, mostly hosting and LLM costs.
- **Full year 1, B2B stage:** about $5–12k, largely covered by revenue at that point.
- **Time:** 40–60 hours for validation, and 400–700 hours in year 1 if the plan continues.

## 8. Timeline

| Milestone | When |
|---|---|
| Validation | Weeks 1–4 |
| Hosted MVP | Months 2–4 |
| First paying users / first agency pilot | Months 4–8 |
| Product-market fit signal (or kill) | Months 9–12 |
| Meaningful income ($3k+ MRR) | Months 15–24, if it works |

## 9. Housekeeping

- `docs/Shibin_Thomas.pdf` (a personal resume) is committed to the repo. Remove it from git
  history before making the repo public or sharing it with partners.
