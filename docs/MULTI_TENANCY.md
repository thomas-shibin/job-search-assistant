# Multi-tenancy, accounts and security

The app serves many people from one server. Each person's resume, jobs, scores,
tailored resumes, alerts and settings are visible only to them.

## Who can do what

Access is role-based (RBAC, `app/modules/access/rbac.py`). A user holds one or more
**roles**; a role grants **permissions** named `resource.action`
(`jobs.view`, `users.suspend`, ...). Code checks permissions, never role
names. Roles, permissions and who holds what are stored in the database and
managed at **Admin dashboard > Roles & permissions** and **Users**.

| Built-in role | Gets by default |
|---|---|
| `SUPER_ADMIN` | Everything, including permissions added later. Can't be edited or restricted. |
| `ADMIN` | Admin dashboard, users (view, limits, suspend, assign ordinary roles), activity, job sources, features, audit log, viewing settings and roles |
| `EMPLOYER` | Employer permissions (`jobs.create`, `jobs.update.own`, ...). Reserved: there are no employer pages yet and public sign-up never gives this role. |
| `JOB_SEEKER` | Search, save, track applications, match analysis, tailoring, profile, resume, job alerts |

- **Every member and admin route** is listed in `ROUTE_PERMISSIONS` with the
  permission it needs, and checked on every request from the database, so a
  change applies on the user's next request. A route missing from the table is
  refused, and `tests/integration/test_rbac.py` fails if one is missing.
  Signed-in users without the permission get a 403 Access denied page (JSON
  `{"error": {"code": "forbidden"}}` from `/api`). Users who can't open the
  admin area get a plain 404 for admin pages.
- **Menus and buttons** are built from the same rules (`can()`,
  `can_route()` and `feature()` in templates; `window.jobAssistant.access`
  in `app.js`). They are for convenience only; the server checks every request.
- **Feature switches** (Admin dashboard > Features) turn an optional feature
  off for everyone: job search, saved jobs, application tracking, job alerts,
  resume editor, resume tailoring, job matching. When a feature is on, who may
  use it still depends on its permission.
- **No privilege escalation** (`app/modules/access/service.py`):
  - Nobody can grant a permission or assign a role beyond their own permissions.
  - Protected permissions (`admin.dashboard.view`, `users.roles.assign`,
    `roles.*`, `permissions.manage`, `settings.manage`), and roles containing
    them, can only be granted or assigned by a Super Admin.
  - Only a Super Admin can change an administrator's account.
  - Nobody can change their own roles or status.
  - The last active Super Admin can't lose the role, be suspended or delete
    their account.
- **The first Super Admin** comes from a trusted source:
  - accounts listed in `ADMIN_EMAILS` get the role when they sign up or
    confirm their email;
  - or from the server: `python cli.py grant-role --email you@example.com --role SUPER_ADMIN`.

  Public sign-up only ever gives `JOB_SEEKER`.
- **The audit log** (Admin dashboard > Audit log, `/api/admin/audit-logs`)
  records every change to roles, permissions, users' roles, status and limits,
  features, platform settings and job sources: who made it, when, the old value
  and the new value. Secrets are masked.
- **There is no guest access.** Searching while signed out opens the sign-in
  dialog, and the search runs as soon as the visitor has signed in or signed up.
- **The Activity dashboard** (`/admin/activity`, `/admin/users/{id}`) shows
  each user's account details and activity:
  - sign-ins, and searches with their terms;
  - the titles of jobs they saved, analyzed, tailored or tracked;
  - analysis usage and cost, and limit hits.

  It never shows resume content, profiles, tailored resumes or job
  descriptions. The Privacy Policy (`/privacy`) says so. Activity older than a
  year is deleted.

## Hourly limits

These limits protect the job-listing quota and the analysis budget
(`app/modules/usage/quota.py`). They are counted in the database.

| Limit | Default | Notes |
|---|---|---|
| New searches | 10/hour | Only a new title / location / country counts. Filters, pages, and running the same search again within the hour are free. Paging stops at 10 pages. A search that fails outright is given back. |
| Resume actions | 5/hour | Match analysis, analyze & tailor, re-tailor, Quick Tailor. Only actions that make a paid analysis call count. |

- Defaults live in **Platform settings → platform**.
- Admins are unlimited, and can override either limit per user (0 blocks it)
  or suspend an account from that user's Activity page.

## How isolation is enforced

Isolation is enforced centrally, in `app/core/tenancy.py`. It doesn't depend on each
query remembering to filter.

1. **An owner on every row of user data.** Every user-data model mixes in
   `TenantOwned`, which adds a `user_id` column: candidate profiles and all their
   children, jobs and all their children, analyses, tailored resumes, watches,
   alerts, scan runs, per-user settings and per-user search terms.
2. **A session belongs to one user.** Routes get their database session from
   `member_session` / `viewer_session` (`app/modules/auth/deps.py`), which bind it to
   the signed-in user before the route runs.
3. **Reads are filtered automatically.** For a bound session, SQLAlchemy adds
   `WHERE user_id = :me` to every ORM SELECT, UPDATE and DELETE on user data.
   That includes joins, subqueries, counts, relationship loads and
   `session.get()`. Another user's job id simply doesn't exist: the route
   returns 404.
4. **Writes are guarded.** On flush, new rows are stamped with the session's
   user. Inserting, changing or deleting a row owned by anyone else raises
   `TenantContextError`.
5. **Fail closed.** The app's sessions are *strict*: touching user data with no
   user bound raises instead of returning everyone's rows. Cross-user work is
   explicit (`system_session`) and limited to:
   - the scheduler choosing whose scans to run (each scan itself runs in that
     member's own bound session);
   - admin totals;
   - account deletion;
   - the Telegram bot resolving which account a chat is linked to.
6. **Files per user.** Uploads, tailored resumes and restored files live in
   per-user folders (`.../user_<id>/`). Downloads are served only from paths
   stored on rows the session can see.
7. **Shared vs private data.** The **job pool** is shared (`pool_companies`,
   `pool_jobs`, see `app/modules/jobs/pool/service.py`): public postings from the
   companies listed under Platform → Job sources, refreshed every
   `platform.pool_refresh_hours`. A posting becomes a user's own `Job` only
   when they analyze it. The built-in and market-scan search catalog is
   shared. Terms a user types go to that user's own list (`user_search_terms`),
   so nobody's searches show up in anyone else's suggestions. Settings files
   (`config.yaml`) only ever seed *platform* settings automatically, never
   someone's personal settings.
8. **Job alert emails** go only to the member's own verified address, with matches
   from their own watches. Every email has an unsubscribe link that works without
   signing in; it carries a random key stored with that member's settings, so it
   can't be guessed for another account.
9. **Telegram.** Each member links their own chat through a single-use,
   15-minute code (`t.me/<bot>?start=<code>`) from their Watchlist page. There is
   no shared chat fallback. `/health` answers only chats linked to an admin, and
   group chats are ignored.

`tests/integration/test_tenant_isolation.py` signs in as one user and tries to
read, change, download or delete another user's data through every route, the
JSON API and the database layer directly. `tests/integration/test_auth.py`
covers sign-in end to end.

Raw SQL (`text()`) bypasses the filter, so application code never uses it on
user tables. The extra hardening step is PostgreSQL row-level security with the
same rule.

## Accounts

Everything starts from an email address (`app/modules/auth/api.py`, driven by
`app/web/static/app.js` in the sign-in dialog and on `/login` and `/signup`):

1. **Email.** `POST /api/auth/register/start` emails a six-digit code: a
   sign-in code for an existing account, a verification code for a new
   address. The response is identical either way, so it never reveals
   whether an address has an account.
2. **Code.** `POST /api/auth/verify-email`:
   - an existing account is signed in (and its email marked verified);
   - a new address gets a 20-minute, single-use registration ticket in an
     `HttpOnly`, `SameSite=Strict` cookie.
3. **Details.** `POST /api/auth/register/complete` takes the name and
   password, creates the account (email already verified, `account_type =
   job_seeker`) and signs it in. A welcome email follows.

Members can also sign in with their password (`POST /api/auth/login`, or the
plain `/login` form without JavaScript), or with Google.

- **Email codes** (`app/modules/auth/otp.py`):
  - random, valid for 60 seconds, stored only as an HMAC keyed by
    `APP_SECRET_KEY`;
  - a new code retires the old one; resending waits 30 seconds;
  - at most 6 codes an hour per address and 20 an hour per network;
  - 5 wrong guesses retire a code;
  - codes never appear in API responses or logs.
- **Passwords:**
  - Argon2id hashes;
  - at least 12 characters, with no character-class rules, so passphrases
    and password managers work;
  - common words, your name or email, and too few different characters are
    refused;
  - 5 wrong attempts lock the account for 15 minutes.
- **Password reset:** a single-use link that expires in 1 hour. Resetting
  signs out every browser. The answer is the same whether or not the
  address has an account.
- **Sessions:**
  - a random token in an `HttpOnly`, `SameSite=Lax` cookie (`Secure` under
    https), stored server-side as a SHA-256 hash;
  - administrators are signed out after 30 minutes idle and 12 hours in all,
    everyone else after 7 days idle and 14 days in all (admin Settings ->
    "Sign-in sessions"); signing out ends them immediately;
  - nothing about the session is kept in JavaScript or localStorage.
- **After the first sign-in:** an optional "Let's build your professional
  profile" step. Either button dismisses it for good. The profile
  (`/profile`, `/api/profile`) is never required.
- **Web protections:**
  - cross-site POSTs are rejected (Origin/Referer check), which also covers
    the JSON API;
  - security headers: CSP, `X-Frame-Options: DENY`, `nosniff`, and HSTS under
    https;
  - rate limits on sign-in, codes and emails;
  - a 10 MB upload cap; resumes are private files, served only to their owner.

## Provider names stay server-side

Pages, messages and API responses never name the analysis or job-listings
providers. Users see "match analysis", "job board" and similar. Provider
choice, keys and costs live only in the server environment and on the admin
pages. Errors are logged on the server, and users see a neutral message.

## Setting it up

1. Fill in `.env` (see `.env.example`). The key settings are:
   - `PUBLIC_URL`: where people reach the app;
   - `ADMIN_EMAILS`;
   - `SMTP_*`: needed for confirmation and reset emails and job alert emails on a real server;
   - `MAIL_POSTAL_ADDRESS`: your mailing address, which job alert emails must carry (CASL);
   - `GOOGLE_*` and `FACEBOOK_*`: optional. Register
     `<PUBLIC_URL>/auth/google/callback` and `<PUBLIC_URL>/auth/facebook/callback`
     as redirect URIs. Facebook requires https.
2. `alembic upgrade head`. On an existing single-user install, the upgrade gives
   all existing data to one owner account (an admin):
   - with `ADMIN_EMAILS` set, it uses the first address there, confirmed;
   - without it, it creates a placeholder. Claim it with
     `python cli.py claim-legacy-data --email you@example.com`.
3. Configure email (`SMTP_*`) before inviting users: sign-up needs the
   emailed code. Until then, emails are saved under `data/mail_outbox/`. You
   can still set your own password from the server with
   `python cli.py set-password --email you@example.com`.
4. Per-user CLI commands take `--user EMAIL`, for example
   `python cli.py --user you@example.com import-resume resume.docx`. `serve` and
   `run` with no `--user` work for every member.

For more than a handful of users, use PostgreSQL (`DATABASE_URL`), run behind
an https reverse proxy, and set `COOKIE_SECURE=true`. Rate limits live in
memory, which is fine for the single `serve` process. Several processes would
need a shared store.
