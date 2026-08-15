# Deploying your own SearXNG on Render (free tier)

This gives Forge-Search a private, reliable web-search fallback tier —
no rate limits from other users, no 403s from JSON being disabled.

## 1. Create a new Render Web Service

In the Render dashboard: **New → Web Service** → connect the same
`Forge-search` GitHub repo you already have connected.

## 2. Fill in these exact fields

| Field | Value |
|---|---|
| **Name** | `forge-search-searxng` (or anything you like) |
| **Language / Environment** | **Docker** (not Python this time) |
| **Root Directory** | `forge-search-sqlite/searxng` |
| **Dockerfile Path** | `Dockerfile` (relative to Root Directory above) |
| **Instance Type** | Free |

Leave Build Command and Start Command blank — Docker deployments use the
Dockerfile's own instructions instead.

## 3. Before you deploy: change the secret key

Open `searxng/settings.yml` in this repo and replace
`change-this-to-a-random-string-before-deploying` on the `secret_key:`
line with any random string of your own (mash the keyboard, doesn't need
to be memorable). This is required by SearXNG and shouldn't be left as
the placeholder value.

## 4. Deploy and grab the URL

Click **Create Web Service**. Same as your API deploy, it'll build for a
minute or two and give you a URL like:

    https://forge-search-searxng.onrender.com

## 5. Verify it actually works

Once live, open this in your browser (replace with your real URL):

    https://forge-search-searxng.onrender.com/search?q=python&format=json

You should see raw JSON with search results — not HTML, not a 403. If you
see JSON, it's working.

## 6. Connect it to your main API

Go back to your **main** Render service (`forge-search`, the Python API) →
**Environment** tab → add a new environment variable:

| Key | Value |
|---|---|
| `SEARXNG_BASE_URL` | `https://forge-search-searxng.onrender.com` (your URL from step 4, no trailing slash) |

Save — this triggers an automatic redeploy of your main API. Once it's
back up, your fallback tier will use your own private instance instead of
the public ones that were rate-limiting you.

## Notes

- **Free tier sleep**: like your main API, this SearXNG service sleeps
  after 15 minutes of no traffic and takes ~30-60 seconds to wake up on
  the next request. That first fallback search after idle time will be
  slow — expected, not a bug.
- **750 free hours/month**: Render gives 750 free instance-hours per
  workspace per month, shared across all your free services. Two small
  services (API + SearXNG) both sleeping when idle should comfortably
  fit within that.
- **Only DuckDuckGo, Wikipedia, Bing, and Google are enabled** as
  upstream engines in `settings.yml` — kept minimal on purpose so
  responses stay fast and there are fewer engines that can individually
  fail. Add more later by editing that file if you want broader coverage.
