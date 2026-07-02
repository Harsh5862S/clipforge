# ClipForge — Setup & Deployment Guide

ClipForge requires an account: sign up with email + password, verify via a
one-time code (OTP) sent to your email, then log in the same way each time.
The first account ever created automatically becomes the **admin**.

---

## 🚀 Deploy to Render (recommended — no local Postgres install needed)

Render gives you a free managed PostgreSQL database, so you never have to
install or configure Postgres yourself.

### 1. Push this project to GitHub
```
git remote add origin https://github.com/YOUR_USERNAME/clipforge.git
git branch -M main
git push -u origin main
```
(This repo is already git-initialized with everything committed.)

### 2. Create a Render Blueprint
1. Go to **https://dashboard.render.com**
2. Click **New → Blueprint**
3. Connect your GitHub account and select this repo
4. Render reads `render.yaml` automatically and provisions:
   - A **web service** (running the Dockerfile, which includes ffmpeg)
   - A **free PostgreSQL database**, auto-linked via `DATABASE_URL`

### 3. Add your secret API keys
After the blueprint deploys, go to your service → **Environment** tab and add:
| Key | Value |
|---|---|
| `GEMINI_API_KEY` | from https://aistudio.google.com/app/apikey |
| `BREVO_API_KEY` | from https://app.brevo.com/settings/keys/api |
| `SENDER_EMAIL` | an email verified as a sender in your Brevo account |
| `YOUTUBE_COOKIES` | see "Fixing download errors" section below |

Render redeploys automatically when you save these.

### 4. Fixing "Sign in to confirm you're not a bot" download errors
YouTube often blocks download requests from cloud server IPs (Render, AWS,
GCP, etc.). Fix this by giving yt-dlp your own logged-in YouTube session
cookies:

1. Install a cookie-export browser extension, e.g. **"Get cookies.txt
   LOCALLY"** for Chrome
2. Go to youtube.com while **logged in**, click the extension, export
   cookies in **Netscape format** (a `.txt` file)
3. Open that file in a text editor, select all, copy everything
4. In Render → your service → **Environment** tab, add:
   - Key: `YOUTUBE_COOKIES`
   - Value: paste the entire file contents
5. Save — Render redeploys, and downloads should now work

**Security note:** these cookies represent your logged-in YouTube session —
treat this environment variable as sensitive. If you ever suspect it's been
exposed, sign out of YouTube on all devices to invalidate the session, then
re-export fresh cookies.

### 5. Open your live site
Render gives you a URL like `https://clipforge.onrender.com` — open it,
sign up, and your first account becomes admin automatically.

**Free tier note:** Render's free web services spin down after 15 minutes of
inactivity and take ~30-60s to wake back up on the next request. The free
Postgres database also expires after 90 days unless upgraded — fine for
testing/personal projects, just something to know.

---

## 💻 Run locally instead

### 1. Install PostgreSQL locally
- **Windows/Mac:** https://www.postgresql.org/download/
- **Linux:** `sudo apt install postgresql postgresql-contrib`

Create the database:
```
psql -U postgres
CREATE DATABASE clipforge;
\q
```

### 2. Install Python dependencies
```
pip install -r requirements.txt
```

### 3. Install ffmpeg (for the download feature)
- **Windows:** https://ffmpeg.org/download.html — add the `bin` folder to PATH
- **Mac:** `brew install ffmpeg`
- **Linux:** `sudo apt install ffmpeg`

### 4. Set your environment variables
Either export them in your terminal, or edit the placeholder values directly
in `db.py`, `server.py`, and `email_service.py`:
```
export DB_PASSWORD="your_postgres_password"
export GEMINI_API_KEY="your_gemini_key"
export BREVO_API_KEY="your_brevo_key"
export SENDER_EMAIL="your_verified_sender@example.com"
```

### 5. Run it
```
python server.py
```
Open **http://localhost:3000**

---

## Using the app
- Paste a YouTube link, click **Forge**
- Choose **clip duration** (15-60s) and **number of clips** (3-10)
- Pick a clip, choose a layout: **Original (16:9)**, **Vertical (9:16)**, or
  **Square (1:1)**
- Click **Download clip** to save the MP4

## Video history
Every analysis you run is automatically saved to your account — visit the
**History** section (linked in the nav bar) to see every video you've
forged, including its thumbnail, title, channel, and clip count. Click
**Reload clips** on any entry to bring its full clip breakdown back into the
tool instantly, without re-running the AI analysis. Click **Delete** to
remove an entry you no longer want saved.

History is private per-account — each user only sees their own saved
analyses, never anyone else's.

## Admin panel
Visit `/admin` — only accessible to admin accounts (both at the page level
and the API level). View all users, recent activity, and ban/delete any
non-admin account.

To set up an admin account directly without email/OTP:
```
python set_admin_password.py
```

## Notes
- Sessions last 14 days via an HttpOnly cookie.
- Banned users can't log in or use the API.
- Never commit real API keys into `server.py` / `email_service.py` if this
  repo is public — use environment variables instead (as set up above).
- Please respect YouTube's Terms of Service and copyright when downloading
  and using video content.
- **Downloads run as background jobs.** Clicking "Download clip" starts a
  job and returns instantly, then the browser polls for completion every 3
  seconds. This avoids Render's ~30-100s proxy timeout, which would
  otherwise silently kill long-running download+reframe requests. Vertical/
  Square layouts can take 1-3 minutes; Original is usually under a minute.
- **YouTube rate limits (HTTP 429):** if downloads start failing with a
  "Too Many Requests" error, this usually clears up after a few minutes on
  its own. If it persists, your `YOUTUBE_COOKIES` may be stale — re-export
  fresh cookies from a logged-in browser session and update the env var.
