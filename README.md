# Vaani — Audience intelligence for YouTube

Paste a YouTube link → get sentiment (English / Hindi / Hinglish), audience pulse score, topics, viewer requests,
questions, engagement metrics, recommendations and exportable reports. History is saved so you can reopen or delete reports.

## Run locally

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then put your YOUTUBE_API_KEY in .env
python app.py               # http://127.0.0.1:5000
```

Without `YOUTUBE_API_KEY` the app runs in **Demo mode** (clearly labelled, sample comments).

## Get a YouTube API key
Google Cloud Console → new project → enable **YouTube Data API v3** → Credentials → API key.
Restrict the key to the YouTube Data API v3 (and to your server IP if your host has a fixed one).

## Deploy (Render / Railway / any Linux host)
- Build: `pip install -r requirements.txt`
- Start: `gunicorn app:app --workers 2 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT` (also in `Procfile`)
- Environment variable: `YOUTUBE_API_KEY`
- History uses SQLite. Many hosts wipe local disk on redeploy. To keep history, attach a persistent disk and set
  `DB_PATH=/path/to/disk/vaani.db`.

| Variable | Default | Purpose |
|---|---|---|
| `YOUTUBE_API_KEY` | – | Live data (required for real videos) |
| `MAX_COMMENTS_LIMIT` | 1500 | Upper limit users can request |
| `RATE_LIMIT_PER_MIN` | 8 | Analyses per IP per minute |
| `DB_PATH` | `./vaani.db` | SQLite location |

## API
| Endpoint | Method | Notes |
|---|---|---|
| `/api/analyze` | POST | `{url, max_comments, order: relevance|time, replies}` |
| `/api/history` | GET / DELETE | list recent reports / clear all |
| `/api/history/<id>` | GET / DELETE | reopen / delete one report |
| `/api/health`, `/healthz` | GET | status, mode, limits |

Each analysis uses roughly 1 YouTube quota unit per 100 comments plus 1 for video details (default quota: 10,000/day).
