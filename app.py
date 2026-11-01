#!/usr/bin/env python3
"""
Vaani — YouTube audience intelligence (Flask API + static page).

- Fetches video metadata and up to MAX_COMMENTS_LIMIT comments (YouTube Data API v3)
- Falls back to a clearly-labelled demo dataset only when no API key is configured
- English / Hindi (Devanagari) / Hinglish sentiment, aspects, demands, questions, spam
- Engagement metrics, audience pulse score, trend, top phrases, emojis, superfans
- SQLite history: reload, delete one, clear all
"""
import json
import math
import os
import random
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request, send_file
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from werkzeug.middleware.proxy_fix import ProxyFix

BASE = os.path.dirname(os.path.abspath(__file__))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE, ".env"))
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Config (all via environment — never hardcode keys)
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip() or "AIzaSyDJFAnch85SVMlPf5fTUx50Zbfm0bempf4"
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE, "vaani.db"))
HARD_MAX = int(os.environ.get("MAX_COMMENTS_LIMIT", "1500"))
DEFAULT_MAX = 300
HISTORY_KEEP = 100
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_MIN", "8"))

app = Flask(__name__, static_folder=None)  # nothing in the project folder is served by accident
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
VADER = SentimentIntensityAnalyzer()


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Lexicons (word-boundary matched — fixes the old substring false positives)
# ---------------------------------------------------------------------------
def _rx(words):
    ordered = sorted({w.lower() for w in words}, key=len, reverse=True)
    edge_l, edge_r = r"(?<![A-Za-z0-9\u0900-\u097F])", r"(?![A-Za-z0-9\u0900-\u097F])"
    return re.compile(edge_l + "(?:" + "|".join(re.escape(w) for w in ordered) + ")" + edge_r, re.I)


POS_RE = _rx([
    "badhiya", "badiya", "bahut accha", "bahut achha", "bahut badhiya", "mast", "zabardast", "lajawab",
    "pyar", "pasand", "shandar", "kamaal", "behtareen", "sundar", "dhanyavad", "shukriya", "faadu",
    "samajh aa gaya", "samajh aa gayi", "mazedaar", "jhakaas", "ekdum sahi", "kya baat", "dil jeet",
    "बहुत अच्छा", "बढ़िया", "शानदार", "जबरदस्त", "धन्यवाद", "शुक्रिया", "कमाल", "लाजवाब", "बेहतरीन", "मस्त",
])
NEG_RE = _rx([
    "bekar", "bakwas", "ghatiya", "faltu", "time waste", "waste of time", "clickbait", "bura laga",
    "samajh nahi aaya", "samajh nahi aata", "samajh nhi aaya", "accha nahi", "achha nahi", "pasand nahi",
    "mast nahi", "thak gaya", "boring hai", "dhokha", "bewakoof", "pakau", "paisa waste",
    "बेकार", "बकवास", "घटिया", "फालतू", "समझ नहीं आया", "अच्छा नहीं",
])
EMO_POS = re.compile("[❤💖💕😍🥰🔥👏🙏👍💯🤩😊🎉]")
EMO_NEG = re.compile("[😡👎🤮😤💔😒🙄😞🤬]")

HINGLISH = set("""hai hain hu hoon kya nahi nahin nhi bhai yaar bahut bohot bhot accha achha acha matlab matlb dekho
suno samajh karo karna wala wali waala banao banaiye lekin magar aur kab kaise kyun kyu mujhe tum aap thoda thodi
bilkul sach mast zabardast lajawab mera meri tera aapka kuch sab bhi toh hum humein""".split())

ASPECTS = {
    "audio": ["audio", "sound", "echo", "noise", "mic", "microphone", "volume", "loud", "quiet", "awaz", "awaj",
              "voice", "bgm", "background music", "आवाज"],
    "content": ["script", "content", "topic", "research", "depth", "explanation", "explain", "information",
                "informative", "story", "storyline", "kahani", "knowledge", "concept", "detail", "details",
                "original", "originality", "repetitive", "formulaic", "recycled"],
    "visuals": ["visual", "visuals", "editing", "edit", "edited", "thumbnail", "graphics", "video quality",
                "camera", "cinematography", "animation", "jump cut", "jump cuts", "transition", "lighting",
                "resolution", "4k", "hd"],
    "pacing": ["pace", "pacing", "slow", "fast", "dragged", "drag", "lengthy", "too long", "too short", "lamba",
               "lambi", "chhota", "intro", "hook", "boring", "skip", "rushed"],
    "presenter": ["host", "presenter", "anchor", "voice", "personality", "energy", "charisma", "smile", "accent",
                  "you explain", "aap explain", "your style"],
    "guest": ["guest", "interview", "collaboration", "collab", "appearance"],
    "sponsor": ["sponsor", "sponsored", "sponsorship", "ad", "ads", "promotion", "promo", "paid"],
}
ASPECT_RX = {k: _rx(v) for k, v in ASPECTS.items()}

DEMAND_RE = re.compile(
    r"(please (make|do|upload|add|create|cover|share|bring)|pls (make|do|upload)|next video|next part|"
    r"part\s*(2|two|3|three)|make (a )?(video|series|tutorial)|can you (make|do|cover|explain|show)|"
    r"could you (make|do|cover)|do a (video|series)|video on|series on|tutorial on|banao|banaiye|banaye|bana do|"
    r"chahiye|kab aayega|kab aa raha|kab ayega|waiting for|we want|need more|more (videos|content|such)|"
    r"request|कृपया|बनाइए|बनाओ|चाहिए)", re.I)
QUESTION_RE = re.compile(r"(\?|^\s*(how|why|what|when|where|which|who|can|could|is|are|do|does|kya|kaise|kyun|kyu|kab|kaun|kaha|kahan)\b)", re.I)
URL_RE = re.compile(r"(https?://|www\.|\b\w+\.(com|in|xyz|top|ru)\b|t\.me/|wa\.me/)", re.I)
SPAM_RE = re.compile(r"(subscribe to my|check (out )?my (channel|video)|sub4sub|follow me|whatsapp|telegram|"
                     r"earn \$?\d+|crypto|giveaway|dm me|free (iphone|robux|v-?bucks)|investment (plan|opportunity))", re.I)
TOXIC_RE = _rx(["idiot", "stupid", "moron", "dumb", "shut up", "loser", "trash", "garbage", "hate you",
                "chutiya", "madarchod", "bhenchod", "gandu", "harami", "kutta", "kamina", "bc", "mc"])
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\u2764\u2B50]")
TOKEN_RE = re.compile(r"[A-Za-z\u0900-\u097F][A-Za-z\u0900-\u097F']+")

STOP = set("""the a an and or but in on at to for of is are was were be been being this that these those it its with as by
from have has had not you your yours i me my we our they them their he she his her will would can could should just
so very really also than then there here what when where which who how why all any some more most much many too
about into out up down over only own same such no nor if because while do does did done get got make made like
video videos channel one even still ever let lets say said see know think thing things way lot well bro sir
hai hain kya ka ki ke ko se me mein par pe ye yeh wo woh aur bhi toh to hi tha thi the ho hoga hogi karo kar raha
rahi rahe wala wali liye lekin magar mujhe tum aap hum mera meri nahi nahin nhi bhai yaar""".split())

NICHE_TRENDS = {
    "tech": ["AI tools and automation walkthroughs are peaking", "Short vertical explainers (60-90s) lift click-through",
             "X vs Y comparison videos keep performing", "Build-in-public and behind-the-scenes content converts well"],
    "education": ["Step-by-step problem solving holds retention best", "Exam-oriented and previous-year-question series trend",
                  "Whiteboard / visual explanations stay trusted", "Hinglish delivery widens reach in India"],
    "gaming": ["Commentary with personality beats raw gameplay", "First-look and release reaction videos spike fast",
               "Funny-moment clips drive discovery", "Live-stream highlights repurpose well"],
    "lifestyle": ["Realistic day-in-the-life routines", "Honest product reviews with pros and cons",
                  "Before/after transformation stories", "Minimalism and productivity systems"],
    "finance": ["Practical money tips with real numbers", "Tax and investing explained in plain language",
                "Myth-busting beats generic advice", "Real portfolio case studies"],
    "entertainment": ["Reaction and review formats with strong opinions", "Series with recurring characters or segments",
                      "Clips cut for Shorts drive subscribers", "Community polls fuel next-episode ideas"],
    "general": ["A strong hook in the first 15 seconds is critical", "Make the value clear in title plus thumbnail",
                "End screens and cards extend session time", "Community posts and polls build loyalty"],
}
CATEGORY_NICHE = {"10": "entertainment", "20": "gaming", "24": "entertainment", "27": "education", "28": "tech",
                  "26": "lifestyle", "19": "lifestyle", "22": "lifestyle", "25": "general"}
NICHE_WORDS = {
    "tech": ["ai", "python", "code", "coding", "programming", "tech", "software", "app", "gadget", "phone", "laptop", "review"],
    "education": ["exam", "study", "learn", "tutorial", "class", "education", "course", "lecture", "syllabus"],
    "gaming": ["game", "gameplay", "pubg", "bgmi", "valorant", "minecraft", "esports", "gta", "fortnite"],
    "finance": ["money", "invest", "investing", "stock", "stocks", "finance", "tax", "sip", "mutual", "crypto"],
    "lifestyle": ["vlog", "lifestyle", "routine", "travel", "fitness", "recipe", "cooking", "fashion"],
}

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("""CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT NOT NULL, title TEXT, channel TEXT,
            thumbnail TEXT, mode TEXT, total INTEGER, pulse INTEGER, avg_compound REAL,
            created_at TEXT NOT NULL, payload TEXT NOT NULL)""")


init_db()

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
VID_RE = re.compile(r"^[0-9A-Za-z_-]{11}$")


def extract_video_id(raw):
    raw = (raw or "").strip()
    if VID_RE.match(raw):
        return raw
    try:
        u = urllib.parse.urlparse(raw if re.match(r"^https?://", raw, re.I) else "https://" + raw)
    except ValueError:
        return None
    host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    if host == "youtu.be":
        cand = u.path.lstrip("/")[:11]
        return cand if VID_RE.match(cand) else None
    if host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        v = urllib.parse.parse_qs(u.query).get("v", [""])[0]
        if VID_RE.match(v):
            return v
        m = re.match(r"^/(?:embed|shorts|live|v)/([0-9A-Za-z_-]{11})", u.path)
        if m:
            return m.group(1)
    return None


def parse_iso(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def duration_seconds(iso):
    m = re.match(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def duration_text(sec):
    if not sec:
        return ""
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def detect_language(text):
    letters = sum(ch.isalpha() for ch in text) or 1
    deva = len(re.findall(r"[\u0900-\u097F]", text))
    if deva / letters > 0.3:
        return "hindi"
    toks = [t.lower() for t in TOKEN_RE.findall(text)]
    hits = sum(t in HINGLISH for t in toks)
    if hits >= 2 or (hits == 1 and len(toks) <= 5):
        return "hinglish"
    return "english"


def score_text(text):
    vader = VADER.polarity_scores(text)["compound"]
    low = text.lower()
    neg = len(NEG_RE.findall(low))
    rest = NEG_RE.sub(" ", low)  # "accha nahi" must not count as "accha"
    pos = len(POS_RE.findall(rest)) + len(EMO_POS.findall(text))
    neg += len(EMO_NEG.findall(text))
    compound = max(-1.0, min(1.0, vader + max(-0.6, min(0.6, 0.3 * (pos - neg)))))
    label = "positive" if compound >= 0.05 else "negative" if compound <= -0.05 else "neutral"
    return label, round(compound, 3)


def detect_aspects(text):
    found = [a for a, rx in ASPECT_RX.items() if rx.search(text)]
    return found or ["general"]


def guess_niche(video):
    cat = CATEGORY_NICHE.get(str(video.get("category") or ""))
    toks = set(re.findall(r"[a-z0-9]+", " ".join([video.get("title") or "", " ".join(video.get("tags") or [])]).lower()))
    scores = {n: len(toks & set(w)) for n, w in NICHE_WORDS.items()}
    best = max(scores, key=scores.get)
    if scores[best] > 0:
        return best
    return cat or "general"


# ---------------------------------------------------------------------------
# YouTube Data API
# ---------------------------------------------------------------------------
class YTError(Exception):
    def __init__(self, message, status=502, reason=""):
        super().__init__(message)
        self.status, self.reason = status, reason


def yt_get(endpoint, params):
    q = urllib.parse.urlencode({**params, "key": API_KEY})
    try:
        with urllib.request.urlopen(f"https://www.googleapis.com/youtube/v3/{endpoint}?{q}", timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode())["error"]
            reason, msg = err["errors"][0].get("reason", ""), err.get("message", "")
        except Exception:
            reason, msg = "", f"HTTP {e.code}"
        raise YTError(msg, e.code, reason)
    except (urllib.error.URLError, TimeoutError):
        raise YTError("Could not reach the YouTube API. Check the server's internet connection.", 502, "network")


def friendly(e):
    r = e.reason
    if r in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
        return "The YouTube API quota for today is used up. Try again tomorrow or use another API key.", 429
    if r in ("keyInvalid", "badRequest") or "API key" in str(e):
        return "The server's YouTube API key is invalid or the YouTube Data API v3 isn't enabled for it.", 502
    if r in ("videoNotFound", "notFound"):
        return "That video was not found. It may be private, deleted or the link is wrong.", 404
    return str(e) or "YouTube API request failed.", e.status if 400 <= e.status < 600 else 502


def fetch_metadata(video_id):
    if not API_KEY:
        rnd = random.Random(video_id)
        return {"id": video_id, "title": f"Demo analysis · {video_id}", "channel": "Demo Channel",
                "description": "Demo data. Add YOUTUBE_API_KEY on the server to analyse real videos.",
                "published": now_iso(), "view_count": rnd.randint(50000, 800000),
                "like_count": rnd.randint(2000, 45000), "comment_count": 240, "duration": "PT12M34S",
                "tags": ["demo"], "category": "28", "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"}
    d = yt_get("videos", {"part": "snippet,statistics,contentDetails", "id": video_id})
    if not d.get("items"):
        raise YTError("Video not found", 404, "videoNotFound")
    it = d["items"][0]
    sn, st, cd = it["snippet"], it.get("statistics", {}), it.get("contentDetails", {})
    th = sn.get("thumbnails", {})
    pick = lambda k: int(st[k]) if k in st else None
    return {"id": video_id, "title": sn.get("title", ""), "channel": sn.get("channelTitle", ""),
            "description": sn.get("description", "")[:1500], "published": sn.get("publishedAt", ""),
            "view_count": pick("viewCount"), "like_count": pick("likeCount"), "comment_count": pick("commentCount"),
            "duration": cd.get("duration", ""), "tags": sn.get("tags", []) or [], "category": sn.get("categoryId", ""),
            "thumbnail": (th.get("maxres") or th.get("high") or th.get("medium") or th.get("default") or {}).get("url", "")}


def clean(t):
    return re.sub(r"\s+", " ", (t or "")).strip()[:1500]


def fetch_comments(video_id, limit, order, replies):
    """Returns (comments, warnings)."""
    out, seen, warns, token = [], set(), [], None

    def add(cid, sn):
        text = clean(sn.get("textDisplay") or sn.get("textOriginal"))
        if cid in seen or not text or len(out) >= limit:
            return
        seen.add(cid)
        out.append({"id": cid, "author": sn.get("authorDisplayName", "Unknown"), "text": text,
                    "likes": int(sn.get("likeCount", 0)), "published": sn.get("publishedAt", "")})

    while len(out) < limit:
        p = {"part": "snippet,replies" if replies else "snippet", "videoId": video_id, "order": order,
             "maxResults": min(100, limit - len(out)), "textFormat": "plainText"}
        if token:
            p["pageToken"] = token
        try:
            data = yt_get("commentThreads", p)
        except YTError as e:
            if e.reason == "commentsDisabled":
                return [], ["Comments are turned off for this video."]
            if out:  # keep what we already have
                warns.append("Stopped early: " + friendly(e)[0])
                break
            raise
        for item in data.get("items", []):
            top = item["snippet"]["topLevelComment"]
            add(top["id"], top["snippet"])
            for rp in (item.get("replies") or {}).get("comments", []):
                add(rp["id"], rp["snippet"])
        token = data.get("nextPageToken")
        if not token:
            break
    if len(out) < limit and not warns and out:
        warns.append(f"YouTube returned {len(out)} comments (fewer than the {limit} requested).")
    return out, warns


DEMO_TEXTS = [
    ("TechFan", "This video is fire! Editing and visuals are top notch."),
    ("CuriousMind", "Finally a deep analysis, love the script and research."),
    ("SoundGuy", "Audio quality is crystal clear this time, great job!"),
    ("Rahul_Sharma", "Bhai bahut badhiya video hai, samajh aa gaya pura concept."),
    ("PriyaK", "Mast explanation! Please next part bhi banao on the same topic."),
    ("AmitVerma", "Audio me thoda echo aa raha hai, mic quality improve karo."),
    ("Neha", "Script thoda repetitive lag raha hai, kuch naya try karo."),
    ("DesignNerd", "Thumbnail is eye-catching and the intro is perfect."),
    ("Sandeep", "Bahut accha content hai yaar, lekin video thodi lambi ho gayi."),
    ("HonestViewer", "Interesting topic but the middle section dragged a bit."),
    ("Vikram", "Please more examples do next time, theory se practical samajh nahi aata."),
    ("SkipAds", "The sponsorship segment felt a little long."),
    ("TiredOfSame", "Same formulaic script again. Feels repetitive and predictable."),
    ("Ananya", "Editing looks rushed, jump cuts are distracting. Visuals improve karo."),
    ("OldFan", "I used to love this channel but the content feels recycled now."),
    ("Rohit", "Bhai next video AI tools pe banao, bohot demand hai uski."),
    ("ComebackKid", "Wow the new format is refreshing! Great pivot. 🔥"),
    ("HappyAgain", "Finally different structure and deeper research. Amazing."),
    ("QualityCheck", "Audio is perfect and the visuals pop. Best episode in months."),
    ("Meera", "Lajawab video! Part 2 kab aa raha hai? Waiting!"),
    ("LiveFan", "Loved the guest and the spontaneous discussion. More of this!"),
    ("Karan", "Thoda slow pace hai starting me, hook strong banao pehle 15 sec me."),
    ("FeedbackWorks", "Great recovery. The team listened to feedback. Respect. 🙏"),
    ("PerfectScore", "Clear audio, engaging script, solid research. 10/10"),
    ("Sneha", "Bahut helpful tha, especially beginners ke liye. Thank you!"),
    ("DeepDive", "Visuals are okay but the script needs more depth and originality."),
    ("QuietVoice", "Sound quality dropped in the middle. Hard to hear at times."),
    ("NextPlease", "Please make a series on this topic. Public demand is high!"),
    ("Asha", "बहुत बढ़िया वीडियो, धन्यवाद! अगला भाग जल्दी बनाइए।"),
    ("Spammer", "Subscribe to my channel for free giveaway https://spam.example.xyz"),
    ("Curious", "How do you record the screen in this style? Which software is it?"),
    ("Grumpy", "Honestly boring and a waste of time. Clickbait title."),
]


def demo_comments(video_id, limit):
    rnd = random.Random(video_id)
    n = min(limit, 240)
    base = datetime.now(timezone.utc) - timedelta(days=21)
    out = []
    for i in range(n):
        a, t = DEMO_TEXTS[i % len(DEMO_TEXTS)]
        when = base + timedelta(days=rnd.random() * 21, hours=rnd.random() * 24)
        out.append({"id": f"demo{i}", "author": a if i < len(DEMO_TEXTS) else f"{a}{i}", "text": t,
                    "likes": int(rnd.paretovariate(1.3) * 3), "published": when.isoformat().replace("+00:00", "Z")})
    return out


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def bucket_key(dt, gran):
    if gran == "day":
        return dt.strftime("%Y-%m-%d")
    if gran == "week":
        return (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m")


def analyze(comments, video):
    rows, labels, langs = [], Counter(), Counter()
    lang_sent = defaultdict(Counter)
    asp = defaultdict(Counter)
    asp_neg = {}
    asp_pos = {}
    words, bigrams, emojis = Counter(), Counter(), Counter()
    authors = defaultdict(lambda: {"count": 0, "likes": 0, "sum": 0.0})
    n_q = n_spam = n_tox = 0
    total_len = 0
    wsum = wtot = 0.0
    dts = []

    for c in comments:
        text = c["text"]
        label, comp = score_text(text)
        lang = detect_language(text)
        aspects = detect_aspects(text)
        flags = []
        if QUESTION_RE.search(text):
            flags.append("question")
        if DEMAND_RE.search(text):
            flags.append("demand")
        if SPAM_RE.search(text) or (URL_RE.search(text) and len(text) < 160):
            flags.append("spam")
        if TOXIC_RE.search(text):
            flags.append("toxic")
        n_q += "question" in flags
        n_spam += "spam" in flags
        n_tox += "toxic" in flags
        rows.append({**c, "label": label, "score": comp, "lang": lang, "aspects": aspects, "flags": flags})
        labels[label] += 1
        langs[lang] += 1
        lang_sent[lang][label] += 1
        total_len += len(text)
        w = 1 + math.log1p(c["likes"])
        wsum += w * comp
        wtot += w
        for a in aspects:
            asp[a][label] += 1
            if label == "negative" and (a not in asp_neg or c["likes"] > asp_neg[a]["likes"]):
                asp_neg[a] = c
            if label == "positive" and (a not in asp_pos or c["likes"] > asp_pos[a]["likes"]):
                asp_pos[a] = c
        au = authors[c["author"]]
        au["count"] += 1
        au["likes"] += c["likes"]
        au["sum"] += comp
        for e in EMOJI_RE.findall(text):
            emojis[e] += 1
        toks = [t for t in (x.lower().strip("'") for x in TOKEN_RE.findall(text)) if len(t) > 2]
        keep = [t for t in toks if t not in STOP]
        words.update(keep)
        bigrams.update(f"{a} {b}" for a, b in zip(toks, toks[1:]) if a not in STOP and b not in STOP)
        d = parse_iso(c["published"])
        if d:
            dts.append((d, label, comp))

    total = len(rows)
    t = max(total, 1)
    pos, neu, neg = (labels.get(k, 0) for k in ("positive", "neutral", "negative"))
    avg = sum(r["score"] for r in rows) / t
    wc = wsum / wtot if wtot else 0.0
    pulse = int(round(50 * (1 + 0.6 * wc + 0.4 * (pos - neg) / t))) if total else 0
    pulse = max(0, min(100, pulse))
    pulse_label = ("Thriving" if pulse >= 75 else "Healthy" if pulse >= 60 else "Mixed" if pulse >= 45
                   else "Strained" if pulse >= 30 else "Critical") if total else "No data"
    controversy = round((min(pos, neg) / max(pos, neg, 1)) * ((pos + neg) / t), 2) if total else 0.0

    # timeline (granularity adapts to the date span)
    timeline, gran, trend = [], "day", {"direction": "stable", "delta": 0.0}
    if dts:
        dts.sort(key=lambda x: x[0])
        span = (dts[-1][0] - dts[0][0]).days
        gran = "day" if span <= 45 else "week" if span <= 400 else "month"
        buckets = defaultdict(lambda: {"positive": 0, "neutral": 0, "negative": 0})
        for d, lab, _ in dts:
            buckets[bucket_key(d, gran)][lab] += 1
        timeline = [{"date": k, **v, "total": sum(v.values())} for k, v in sorted(buckets.items())]
        if len(dts) >= 20:
            half = len(dts) // 2
            a1 = sum(x[2] for x in dts[:half]) / half
            a2 = sum(x[2] for x in dts[half:]) / (len(dts) - half)
            delta = round(a2 - a1, 3)
            trend = {"direction": "improving" if delta >= 0.08 else "declining" if delta <= -0.08 else "stable",
                     "delta": delta}

    aspects = {}
    for a, cnt in asp.items():
        tot = sum(cnt.values())
        aspects[a] = {"positive": cnt["positive"], "neutral": cnt["neutral"], "negative": cnt["negative"], "total": tot,
                      "score": round((cnt["positive"] - cnt["negative"]) / max(tot, 1), 3),
                      "praise": asp_pos[a]["text"][:160] if a in asp_pos else "",
                      "complaint": asp_neg[a]["text"][:160] if a in asp_neg else ""}

    def pick(rs, n=8):
        out, seen = [], set()
        for r in rs:
            k = r["text"].lower()[:50]
            if k in seen:
                continue
            seen.add(k)
            out.append({"text": r["text"][:260], "likes": r["likes"], "author": r["author"]})
            if len(out) >= n:
                break
        return out

    by_likes = sorted(rows, key=lambda r: -r["likes"])
    demands = pick([r for r in by_likes if "demand" in r["flags"] and "spam" not in r["flags"]], 10)
    questions = pick([r for r in by_likes if "question" in r["flags"] and "spam" not in r["flags"]], 8)
    top_pos = pick([r for r in by_likes if r["label"] == "positive" and "spam" not in r["flags"]], 5)
    top_neg = pick([r for r in by_likes if r["label"] == "negative" and "spam" not in r["flags"]], 5)
    top_auth = sorted(({"author": a, "count": v["count"], "likes": v["likes"], "avg": round(v["sum"] / v["count"], 2)}
                       for a, v in authors.items() if v["count"] >= 2 or v["likes"] >= 50),
                      key=lambda x: (-x["count"], -x["likes"]))[:6]

    views, likes, cc = video.get("view_count"), video.get("like_count"), video.get("comment_count")
    like_rate = round(100 * likes / views, 2) if views and likes is not None else None
    comment_rate = round(100 * cc / views, 3) if views and cc is not None else None
    lang_pct = {k: round(100 * v / t, 1) for k, v in langs.items()}

    summary = {
        "total": total, "positive": pos, "neutral": neu, "negative": neg,
        "positive_pct": round(100 * pos / t, 1), "neutral_pct": round(100 * neu / t, 1),
        "negative_pct": round(100 * neg / t, 1), "avg_compound": round(avg, 3), "weighted_compound": round(wc, 3),
        "pulse": pulse, "pulse_label": pulse_label, "controversy": controversy, "trend": trend,
        "languages": dict(langs), "language_pct": lang_pct, "avg_len": round(total_len / t),
        "question_pct": round(100 * n_q / t, 1), "spam_pct": round(100 * n_spam / t, 1),
        "toxic_pct": round(100 * n_tox / t, 1), "like_rate": like_rate, "comment_rate": comment_rate,
        "unique_authors": len(authors), "total_likes_on_comments": sum(r["likes"] for r in rows),
    }
    insights = build_insights(summary, aspects, demands)
    return {
        "summary": summary, "timeline": timeline, "granularity": gran, "aspects": aspects,
        "lang_sentiment": {k: dict(v) for k, v in lang_sent.items()},
        "top_words": [{"word": w, "count": c} for w, c in words.most_common(40)],
        "top_phrases": [{"phrase": p, "count": c} for p, c in bigrams.most_common(12) if c >= 2],
        "emojis": [{"emoji": e, "count": c} for e, c in emojis.most_common(10)],
        "demands": demands, "questions": questions, "top_positive": top_pos, "top_negative": top_neg,
        "top_authors": top_auth, "insights": insights, "comments": rows,
        "generated_at": now_iso(),
    }


def build_insights(s, aspects, demands):
    if not s["total"]:
        return ["No comments were available to analyse."]
    out = [f"Audience pulse is {s['pulse']}/100 ({s['pulse_label'].lower()}): {s['positive_pct']}% positive, "
           f"{s['negative_pct']}% negative."]
    tr = s["trend"]
    if tr["direction"] != "stable":
        out.append(f"Sentiment is {tr['direction']} — newer comments score {abs(tr['delta']):.2f} "
                   f"{'higher' if tr['delta'] > 0 else 'lower'} than older ones.")
    ranked = sorted((a for a in aspects.items() if a[0] != "general" and a[1]["total"] >= 3), key=lambda x: x[1]["score"])
    if ranked:
        best, worst = ranked[-1], ranked[0]
        if best[1]["score"] > 0.15:
            out.append(f"Most praised: {best[0]} ({best[1]['positive']} positive of {best[1]['total']} mentions).")
        if worst[1]["score"] < -0.1:
            out.append(f"Biggest pain point: {worst[0]} ({worst[1]['negative']} negative of {worst[1]['total']} mentions).")
    if demands:
        out.append(f"{len(demands)} viewers asked for specific content; the most-liked request has "
                   f"{demands[0]['likes']} likes.")
    nonen = 100 - s["language_pct"].get("english", 0)
    if nonen >= 25:
        out.append(f"{nonen:.0f}% of comments are Hindi or Hinglish — bilingual titles and captions can widen reach.")
    if s["controversy"] >= 0.5:
        out.append("The audience is split: strong positive and negative reactions are about equal.")
    if s["question_pct"] >= 10:
        out.append(f"{s['question_pct']}% of comments are questions — a pinned FAQ or follow-up video would help.")
    if s["spam_pct"] >= 5 or s["toxic_pct"] >= 5:
        out.append(f"Moderation needed: {s['spam_pct']}% spam-like and {s['toxic_pct']}% abusive comments.")
    return out


def recommend(video, an):
    s, asp, recs = an["summary"], an["aspects"], []
    add = lambda p, t, d: recs.append({"priority": p, "title": t, "detail": d})
    if not s["total"]:
        return [{"priority": "medium", "title": "No comments to learn from",
                 "detail": "Ask a question at the end of the video to start the conversation."}]
    if s["negative_pct"] > 25:
        add("high", "Respond to negative feedback", f"{s['negative_pct']}% of comments are negative. Reply to the top "
            "complaints and pin a short clarification.")
    if s["positive_pct"] > 60:
        add("medium", "Build on what works", "The audience is largely positive. Make a follow-up on the most praised topics.")
    for name, thr, pr, title, detail in [
        ("audio", -0.15, "high", "Fix audio quality", "Viewers mention echo, noise or volume. A better mic or room treatment pays off fast."),
        ("content", -0.2, "high", "Refresh content structure", "Comments call the script repetitive or shallow. Try a stronger hook or a new angle."),
        ("visuals", -0.1, "medium", "Upgrade visuals and editing", "Editing or thumbnail issues came up. A/B test thumbnails and soften jump cuts."),
        ("pacing", -0.1, "medium", "Tighten the pacing", "Viewers feel the video drags. Trim intros and sponsor reads."),
        ("sponsor", -0.1, "low", "Rework sponsor segments", "Sponsored sections get negative reactions. Shorten them or move them later."),
    ]:
        if asp.get(name, {}).get("total", 0) >= 3 and asp[name]["score"] < thr:
            add(pr, title, detail)
    if an["demands"]:
        add("high", "Make what viewers ask for", "Top requests: " + "; ".join(f"“{d['text'][:70]}”" for d in an["demands"][:3]))
    if s["question_pct"] >= 10:
        add("medium", "Answer common questions", "Pin an FAQ comment or cover the top questions in a short follow-up.")
    if s["spam_pct"] >= 5 or s["toxic_pct"] >= 5:
        add("medium", "Tighten comment moderation", "Turn on held-for-review and add blocked words in YouTube Studio.")
    if s["like_rate"] is not None and s["like_rate"] < 2:
        add("medium", "Ask for the like at the right moment", f"Like rate is {s['like_rate']}% (healthy is roughly 3–5%). Ask right after the key payoff.")
    if s["trend"]["direction"] == "declining":
        add("high", "Reverse the downward trend", "Newer comments are less positive. Compare this upload with your best-received one.")
    if len(video.get("title") or "") > 70:
        add("low", "Shorten the title", "Titles over about 60–70 characters get truncated in search and suggested.")
    if not video.get("tags"):
        add("low", "Add specific tags", "No tags found. Add 8–12 specific ones.")
    add("low", "Strengthen the first 15 seconds", "State the value immediately and tease the payoff.")
    add("low", "Use end screens and cards", "Send viewers to the next best video to extend session time.")
    order = {"high": 0, "medium": 1, "low": 2}
    recs.sort(key=lambda r: order[r["priority"]])
    return recs[:9]


# ---------------------------------------------------------------------------
# Rate limiting + security headers
# ---------------------------------------------------------------------------
_hits, _lock = defaultdict(list), threading.Lock()


def limited(ip):
    now = time.time()
    with _lock:
        _hits[ip] = [t for t in _hits[ip] if now - t < 60]
        if len(_hits[ip]) >= RATE_LIMIT:
            return True
        _hits[ip].append(now)
        return False


@app.after_request
def headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
        "img-src 'self' data: https://i.ytimg.com https://*.ytimg.com https://*.ggpht.com; "
        "frame-src https://www.youtube-nocookie.com; connect-src 'self'; base-uri 'self'; form-action 'self'")
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return send_file(os.path.join(BASE, "index.html"), max_age=0)


@app.get("/favicon.ico")
def favicon():
    return "", 204


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "mode": "live" if API_KEY else "demo", "max_comments": HARD_MAX,
                    "default_comments": DEFAULT_MAX})


@app.post("/api/analyze")
def api_analyze():
    if limited(request.remote_addr or "?"):
        return jsonify({"error": f"Too many requests. Limit is {RATE_LIMIT} analyses per minute."}), 429
    data = request.get_json(silent=True) or {}
    url = str(data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Paste a YouTube video link to analyse."}), 400
    vid = extract_video_id(url)
    if not vid:
        return jsonify({"error": "That doesn't look like a YouTube video link."}), 400
    try:
        limit = max(20, min(HARD_MAX, int(data.get("max_comments") or DEFAULT_MAX)))
    except (TypeError, ValueError):
        limit = DEFAULT_MAX
    order = data.get("order") if data.get("order") in ("relevance", "time") else "relevance"
    replies = bool(data.get("replies"))

    try:
        video = fetch_metadata(vid)
        if API_KEY:
            comments, warns = fetch_comments(vid, limit, order, replies)
        else:
            comments, warns = demo_comments(vid, limit), ["Demo mode: showing sample data. Add a YouTube API key on the server for real comments."]
    except YTError as e:
        msg, code = friendly(e)
        return jsonify({"error": msg}), code
    except Exception as e:  # never leak a traceback to the browser
        app.logger.exception("analyze failed")
        return jsonify({"error": "Something went wrong while analysing this video. Please try again."}), 500

    video["url"] = f"https://www.youtube.com/watch?v={vid}"
    video["niche"] = guess_niche(video)
    video["duration_text"] = duration_text(duration_seconds(video.get("duration")))
    video.pop("description", None)
    an = analyze(comments, video)
    payload = {
        "mode": "live" if API_KEY else "demo", "warnings": warns, "video": video, "analysis": an,
        "recommendations": recommend(video, an),
        "trends": {"niche": video["niche"], "items": NICHE_TRENDS.get(video["niche"], NICHE_TRENDS["general"])},
        "settings": {"requested": limit, "order": order, "replies": replies},
    }
    with db() as c:
        cur = c.execute("""INSERT INTO reports (video_id,title,channel,thumbnail,mode,total,pulse,avg_compound,created_at,payload)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (vid, video.get("title"), video.get("channel"), video.get("thumbnail"), payload["mode"],
                         an["summary"]["total"], an["summary"]["pulse"], an["summary"]["avg_compound"], now_iso(),
                         json.dumps(payload, ensure_ascii=False)))
        payload["id"] = cur.lastrowid
        c.execute("DELETE FROM reports WHERE id NOT IN (SELECT id FROM reports ORDER BY id DESC LIMIT ?)", (HISTORY_KEEP,))
    return jsonify(payload)


@app.get("/api/history")
def history():
    with db() as c:
        rows = c.execute("""SELECT id, video_id, title, channel, thumbnail, mode, total, pulse, avg_compound, created_at
                            FROM reports ORDER BY id DESC LIMIT 50""").fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/history/<int:rid>")
def history_one(rid):
    with db() as c:
        row = c.execute("SELECT payload FROM reports WHERE id=?", (rid,)).fetchone()
    if not row:
        return jsonify({"error": "That report no longer exists."}), 404
    payload = json.loads(row["payload"])
    payload["id"] = rid
    return jsonify(payload)


@app.delete("/api/history/<int:rid>")
def history_delete(rid):
    with db() as c:
        c.execute("DELETE FROM reports WHERE id=?", (rid,))
    return jsonify({"ok": True})


@app.delete("/api/history")
def history_clear():
    with db() as c:
        n = c.execute("DELETE FROM reports").rowcount
    return jsonify({"ok": True, "deleted": n})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    print(f"Vaani running on http://127.0.0.1:{port}  ·  mode: {'LIVE' if API_KEY else 'DEMO (set YOUTUBE_API_KEY)'}")
    app.run(host="0.0.0.0", port=port, debug=False)
