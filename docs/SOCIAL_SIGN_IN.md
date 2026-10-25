# Social sign-in: Google, Facebook and LinkedIn

How to create the developer app behind each "Continue with …" button, connect it to
Hirlumo, and try it on this computer before turning it on for hirlumo.com.

A button only appears when its client ID and secret are both set. Leave a provider
empty and its button stays hidden. Nothing else changes.

## What the app asks each provider for

| Provider | Asks for | Gets | Code |
|---|---|---|---|
| Google | `openid email profile` | name, email (and whether it's verified) | `app/modules/auth/oauth.py` |
| Facebook | `email,public_profile` | name, email (only addresses the person has confirmed) | same |
| LinkedIn | `openid profile email` | name, email, country | same |

Each token is used once to read the profile, then thrown away. If a provider doesn't
share a verified email, sign-in stops with "Your … account didn't share a verified
email address. Sign up with your email instead." Work history from LinkedIn isn't
available through sign-in. Members import it from their LinkedIn PDF or data export.

## The addresses to register

After sign-in, the provider sends the person back to `<PUBLIC_URL>/auth/<provider>/callback`.
That address has to match exactly what you registered: `http` or `https`, host,
port and path. Register both the dev and the live address in each app, so one app
covers both.

| Provider | Dev (this computer) | Live |
|---|---|---|
| Google | `http://127.0.0.1:8001/auth/google/callback` | `https://hirlumo.com/auth/google/callback` |
| Facebook | `http://127.0.0.1:8001/auth/facebook/callback` | `https://hirlumo.com/auth/facebook/callback` |
| LinkedIn | `http://127.0.0.1:8001/auth/linkedin/callback` | `https://hirlumo.com/auth/linkedin/callback` |

`docker-compose.dev.yml` sets `PUBLIC_URL` to `http://127.0.0.1:${APP_PORT}`, and
`docker-compose.yml` sets it to `https://${DOMAIN}`. On dev, always open the site as
`http://127.0.0.1:8001`, not `localhost`. Sign-in uses a cookie that only
the same address can read, so it fails with "Sign-in expired or was started
in another browser" otherwise.

## Logo files

Ready-made images for the provider consoles are in `docs/brand/`. They're made from
`app/web/static/brand/mark.svg` and `logo.svg`, the same artwork the site uses.

| File | Use it for |
|---|---|
| `docs/brand/hirlumo-icon-1024.png` | Facebook app icon (1024×1024) |
| `docs/brand/hirlumo-icon-400.png` | LinkedIn app logo, LinkedIn Page logo |
| `docs/brand/hirlumo-icon-120.png` | Google consent screen logo (120×120) |
| `docs/brand/hirlumo-logo-1200x300.png` | Anywhere a wide logo fits, e.g. a LinkedIn Page cover |

## Settings

The keys go in `.env` for dev and `.env.prod` for live. Never commit either file.

```
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
FACEBOOK_CLIENT_ID=          # Facebook calls it the App ID
FACEBOOK_CLIENT_SECRET=      # the App secret
LINKEDIN_CLIENT_ID=
LINKEDIN_CLIENT_SECRET=      # the Primary Client Secret
# FACEBOOK_GRAPH_VERSION=v23.0   (optional; the default is in app/modules/auth/settings.py)
```

Compose doesn't notice changes inside an env file, so recreate the container after
editing:

```
docker compose -f docker-compose.dev.yml up -d --force-recreate      # dev
docker compose --env-file .env.prod --profile tunnel up -d --force-recreate   # live
```

---

## 1. Google

You need a Google account. Use one you'll keep for the business, such as
hirlumo@gmail.com, so the app doesn't depend on a personal account.

1. Open https://console.cloud.google.com and sign in.
2. **Create a project:** use the project picker at the top, then **New project**.
   Name it `Hirlumo` and click **Create**, then select it.
3. **Consent screen:** go to **APIs & Services → OAuth consent screen**. Newer
   consoles call it **Google Auth Platform → Branding**.
   - Click **Get started**.
   - App name `Hirlumo`; user support email: your business Gmail.
   - Audience: **External**.
   - Contact email: your business Gmail. Agree to the policy and click **Create**.
4. **Branding** (same area): add the app logo, home page `https://hirlumo.com`,
   privacy policy `https://hirlumo.com/privacy` and terms `https://hirlumo.com/terms`.
   Under **Authorized domains**, add `hirlumo.com`.
5. **Data access** (scopes): add `openid`, `.../auth/userinfo.email` and
   `.../auth/userinfo.profile`. These are the basic scopes and need no extra review.
6. **Test users:** while the app is in **Testing**, only the addresses listed here can
   sign in. Add your own Gmail.
7. **Create the client:** go to **APIs & Services → Credentials → Create credentials →
   OAuth client ID**.
   - Application type: **Web application**, name `Hirlumo web`.
   - **Authorized JavaScript origins:** `http://127.0.0.1:8001` and `https://hirlumo.com`.
   - **Authorized redirect URIs:** both Google addresses from the table above.
   - Click **Create**.
8. Copy the **Client ID** and **Client secret** into `GOOGLE_CLIENT_ID` and
   `GOOGLE_CLIENT_SECRET`.
9. **Before launch:** on the consent screen or **Audience**, click **Publish app** so
   anyone can sign in, not just test users. With only the basic scopes, Google may
   still check the branding: the domain must be verified in Search Console, and the
   privacy link must work.

Changes to redirect addresses can take a few minutes to start working.

## 2. Facebook

You need a personal Facebook account. It becomes the app's administrator, and
Meta may ask you to register as a developer the first time (phone number check).

1. Open https://developers.facebook.com/apps and click **Create app**.
2. **App details:** name `Hirlumo`; contact email: your business email.
3. **Use case:** choose **Authenticate and request data from users with Facebook
   Login**.
4. **Business:** pick your business portfolio, or **I don't want to connect a
   business portfolio yet**. You can link one later. You need one to go Live with
   business verification.
5. Click **Create app** and enter your Facebook password if asked.
6. **Permissions:** under **Use cases → Authenticate … → Customize**, make sure
   `email` and `public_profile` are added. `public_profile` is always on. Click
   **Add** next to `email`.
7. **Settings:** in the same use case, open **Settings** (Facebook Login settings):
   - **Client OAuth login:** Yes. **Web OAuth login:** Yes. **Enforce HTTPS:** Yes.
   - **Valid OAuth Redirect URIs:** both Facebook addresses from the table above.
   - Save.
8. **App settings → Basic:**
   - Copy the **App ID** into `FACEBOOK_CLIENT_ID`. Click **Show** next to **App
     secret** and copy it into `FACEBOOK_CLIENT_SECRET`.
   - **App domains:** `hirlumo.com`.
   - **Privacy policy URL:** `https://hirlumo.com/privacy`.
   - **Terms of Service URL:** `https://hirlumo.com/terms`.
   - **User data deletion:** choose **Data deletion instructions URL** and enter
     `https://hirlumo.com/data-deletion`.
   - **App icon:** `docs/brand/hirlumo-icon-1024.png`. **Category:** Business and pages.
   - Save changes.
9. **Testing:** while the app is in **Development** mode, only people with a role
   on the app (**App roles → Roles**: administrators, developers, testers) can sign
   in. You're the administrator, so you can test right away.
10. **Before launch:** switch **App mode** to **Live**. It needs the privacy policy,
    the data deletion entry and the icon. Using `email` and `public_profile` only
    needs standard access; Meta may still ask for business verification.

**If the dev address is refused:** Facebook allows plain `http` redirect addresses
only for `localhost` while in Development mode, and may reject
`http://127.0.0.1:8001/…` ("URL blocked: This redirect failed because the redirect
URI is not whitelisted"). If that happens, the dev setup can switch to
`http://localhost:8001`. Then register the `localhost` versions of all three
addresses and open the site as `localhost`.

## 3. LinkedIn

You need a personal LinkedIn account and a **LinkedIn Page** for Hirlumo. LinkedIn
won't create an app without one.

1. **Create the Page** if you don't have one: on linkedin.com, **For Business** (the
   grid icon) → **Create a Company Page** → **Company**.
   - Name `Hirlumo`, LinkedIn public URL `hirlumo`, website `https://hirlumo.com`.
   - Add the industry, company size and type, and upload the logo. Then **Create page**.
2. Open https://www.linkedin.com/developers/apps and click **Create app**.
   - App name `Hirlumo`. LinkedIn Page: pick the Hirlumo page.
   - Privacy policy URL `https://hirlumo.com/privacy`. App logo: the Hirlumo logo
     (at least 100×100).
   - Agree to the legal terms and click **Create app**.
3. **Verify the app with the Page:** on the **Settings** tab, click **Verify**. This
   gives you a link. As the Page's administrator, open it and approve. Some
   products stay unavailable until this is done.
4. **Products** tab, inside your app (**My apps** → Hirlumo → **Products**). Don't use
   the **Products** link in the site's top menu: that's LinkedIn's general catalogue,
   and has no Request access buttons. Click **Request access** on **Sign In with LinkedIn using OpenID
   Connect** and agree. It's normally approved at once. This adds the `openid`,
   `profile` and `email` scopes.
5. **Auth** tab:
   - Copy the **Client ID** into `LINKEDIN_CLIENT_ID`, and the **Primary Client
     Secret** into `LINKEDIN_CLIENT_SECRET`.
   - Under **OAuth 2.0 settings → Authorized redirect URLs for your app**, add both
     LinkedIn addresses from the table above, then click **Update**.
   - Under **OAuth 2.0 scopes**, check that `openid`, `profile` and `email` are
     listed. If they aren't, step 4 isn't done.
6. There's no separate test mode. Once the product is added, anyone with a LinkedIn
   account can sign in.

---

## Trying it on this computer

1. Put the keys in `.env` and recreate the container (see "Settings").
2. Open http://127.0.0.1:8001/login. Each provider with keys has its own button.
3. Click a button, sign in on the provider's page and approve. You should land back
   in Hirlumo, signed in.
   - A **new** email gets a new account.
   - An email that **already** has an account is linked to it, so you can sign in
     either way afterwards.
4. Check the result under **Account**, which shows "Signed in with: …", or as admin
   under **Admin → Users**.
5. Sign out and sign in again with the same button to make sure the link sticks.

To see what went wrong in a failed sign-in, read the app's log:

```
docker compose -f docker-compose.dev.yml logs --since 10m app | grep -i -E "oauth|social|login|auth"
```

The log never contains tokens, codes or secrets.

## Common problems

| What you see | Why | Fix |
|---|---|---|
| Google: `Error 400: redirect_uri_mismatch` | The address isn't registered exactly | Add the exact address from the table (check `http`/`https`, port, no trailing slash) |
| Google: `Access blocked: … has not completed the Google verification process` | App in Testing, and your Gmail isn't a test user | Add it under Test users, or publish the app |
| Facebook: `URL blocked` / `redirect URI is not whitelisted` | The address isn't in Valid OAuth Redirect URIs, or it's `http` on a non-localhost host | Add it; for dev see "If the dev address is refused" |
| Facebook: `App not active` / `Feature unavailable` | The app is in Development and you have no role on it | Sign in with the admin account, or add yourself under App roles |
| LinkedIn: `unauthorized_scope_error` | The OpenID Connect product isn't added | Products tab → Sign In with LinkedIn using OpenID Connect |
| LinkedIn: `The redirect_uri does not match` | The address isn't in Authorized redirect URLs | Add it on the Auth tab |
| Hirlumo: "Sign-in expired or was started in another browser" | Opened as `localhost` while `PUBLIC_URL` is `127.0.0.1` (or the reverse), or more than 10 minutes passed | Open the site at the `PUBLIC_URL` address and try again |
| Hirlumo: "… didn't share a verified email address" | No confirmed email on that account, or you unticked email on the consent screen | Confirm the email with the provider, or sign up with email |
| No button for a provider | Its ID or secret is empty, or the container wasn't recreated | Set both, then `--force-recreate` |

## Going live checklist

- The same keys are in `.env.prod`, and the live addresses are registered in all three apps.
- Google: app published; `hirlumo.com` verified in Search Console; branding approved.
- Facebook: App mode **Live**; privacy URL, data deletion URL (`/data-deletion`) and icon are set.
- LinkedIn: app verified with the Hirlumo Page; the OpenID Connect product is added.
- Try each button on https://hirlumo.com in a private window with an account that
  has never used Hirlumo.
