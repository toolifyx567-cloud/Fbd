from flask import Flask, request, jsonify, redirect, Response
from flask_cors import CORS
import yt_dlp
import time
import threading
import sqlite3
import os
import re
import random
import string
from dotenv import load_dotenv
import requests

# ===== LOAD ENV VARIABLES =====
load_dotenv()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

if not ADMIN_PASSWORD:
    raise ValueError("ADMIN_PASSWORD not set in environment variables")

# ===== RANDOM STRING HELPER =====
def random_string(length=6):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))


app = Flask(__name__)
CORS(app)

# ====== SQLITE SETUP ======
DB_FILE = "toolifyx_stats.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            requests INTEGER DEFAULT 0,
            downloads INTEGER DEFAULT 0,
            cache_hits INTEGER DEFAULT 0,
            videos_served INTEGER DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS unique_ips (
            ip TEXT PRIMARY KEY
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS download_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT,
            url TEXT,
            timestamp INTEGER
        )
    """)

    c.execute("INSERT OR IGNORE INTO stats (id) VALUES (1)")

    conn.commit()
    conn.close()

init_db()

# ====== STATS STORAGE (RAM CACHE + SQLITE SYNC) ======
stats = {
    "requests": 0,
    "downloads": 0,
    "cache_hits": 0,
    "videos_served": 0,
    "unique_ips": set(),
    "download_logs": []
}

cache = {}

# ===== SQLITE HELPERS =====

def increment_stat(field):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(f"UPDATE stats SET {field} = {field} + 1 WHERE id = 1")
    conn.commit()
    conn.close()

def add_unique_ip(ip):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO unique_ips (ip) VALUES (?)", (ip,))
    conn.commit()
    conn.close()

def add_download_log(ip, url):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO download_logs (ip, url, timestamp) VALUES (?, ?, ?)",
        (ip, url, int(time.time()))
    )
    conn.commit()
    conn.close()

def get_db_stats():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("SELECT requests, downloads, cache_hits, videos_served FROM stats WHERE id=1")
    stats_row = c.fetchone()

    c.execute("SELECT COUNT(*) FROM unique_ips")
    unique_ips = c.fetchone()[0]

    c.execute("SELECT ip, url, timestamp FROM download_logs ORDER BY id DESC LIMIT 100")
    logs = c.fetchall()

    conn.close()

    return {
        "requests": stats_row[0],
        "downloads": stats_row[1],
        "cache_hits": stats_row[2],
        "videos_served": stats_row[3],
        "unique_ips": unique_ips,
        "download_logs": [
            {"ip": log[0], "url": log[1], "timestamp": log[2]}
            for log in logs
        ]
    }

# ====== CLEAN + RANDOM FILENAME FUNCTION =====
def clean_filename(name):
    name = re.sub(r'[^a-zA-Z0-9 ]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    if len(name) > 40:
        name = name[:40]
    rand = random_string()
    return f"{name} ToolifyX Downloader_{rand}.mp4"

# ====== NEW: RESOLVE FACEBOOK SHARE LINKS ======
def resolve_facebook_url(url):
    try:
        if "facebook.com/share/" in url:
            response = requests.get(url, allow_redirects=True, timeout=10)
            return response.url
        return url
    except:
        return url

# ====== Helper function with timeout ======

def extract_video(url, result_holder):

    try:

        ydl_opts = {
            "format": "best",
            "quiet": True,
            "noplaylist": True,
            "socket_timeout": 15,
            "retries": 2,
            "nocheckcertificate": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:

            info = ydl.extract_info(url, download=False)

            result_holder["url"] = info.get("url")
            result_holder["title"] = info.get("title", "Video")
            result_holder["author"] = info.get("uploader", "")
            result_holder["thumbnail"] = info.get("thumbnail", "")

    except Exception as e:

        result_holder["error"] = str(e)


def fetch_facebook_video(url):

    result = {}

    t = threading.Thread(target=extract_video, args=(url, result))

    t.start()

    t.join(timeout=20)

    if t.is_alive():

        return None

    video_url = result.get("url")
    title = result.get("title", "Video")
    author = result.get("author", "")
    thumbnail = result.get("thumbnail", "")

    if video_url:

        return {
            "video_url": video_url,
            "title": title,
            "author": author,
            "thumbnail": thumbnail
        }

    return None


# ====== Download route ======

@app.route("/download", methods=["POST"])

def download_video():

    stats["requests"] += 1

    increment_stat("requests")

    ip = request.remote_addr

    stats["unique_ips"].add(ip)

    add_unique_ip(ip)

    data = request.get_json()

    url = data.get("url")

    if not url:

        return jsonify({"success": False, "error": "No URL provided"}), 400

    # resolve share links first
    url = resolve_facebook_url(url)

    try:

        result = fetch_facebook_video(url)

        if not result:

            return jsonify({
                "success": False,
                "error": "Facebook blocked this video or server timeout"
            }), 408

        video_url = result["video_url"]
        title = result["title"]
        author = result["author"]
        thumbnail = result["thumbnail"]

        stats["downloads"] += 1

        stats["videos_served"] += 1

        increment_stat("downloads")

        increment_stat("videos_served")

        stats["download_logs"].append({
            "ip": ip,
            "url": url,
            "timestamp": int(time.time())
        })

        add_download_log(ip, url)

        filename = clean_filename(title)

        return jsonify({
            "success": True,
            "url": video_url,
            "filename": filename,
            "title": title,
            "author": author,
            "thumbnail": thumbnail,
            "videoId": url
        })

    except Exception as e:

        return jsonify({"success": False, "error": str(e)}), 500


# ====== FILE SERVING (RE-FETCH FRESH URL) ======

@app.route("/file")
def serve_file():

    # Support both old "url" param and new "videoId" param
    video_url = request.args.get("url")
    video_id = request.args.get("videoId")
    mode = request.args.get("mode", "preview")

    # If videoId provided, re-fetch fresh URL
    if video_id and not video_url:
        result = fetch_facebook_video(video_id)
        if result:
            video_url = result["video_url"]
        else:
            return jsonify({"success": False, "message": "Could not re-fetch video. Link may be expired or invalid."}), 500

    if not video_url:
        return jsonify({"success": False, "message": "No video URL"}), 400

    try:
        # Parse Range header from client (e.g., "bytes=0-1023" or "bytes=1024-")
        range_header = request.headers.get("Range")

        # Build request headers to forward to source
        source_headers = {}
        if range_header:
            source_headers["Range"] = range_header

        # Request from source with range support
        r = requests.get(video_url, stream=True, timeout=15, headers=source_headers)

        rand = random_string()
        filename = f"ToolifyX Downloader-{rand}.mp4"

        # Determine response status
        status_code = 206 if r.status_code == 206 else 200

        # Build response headers
        headers = {
            "Content-Type": r.headers.get("Content-Type", "video/mp4"),
            "Accept-Ranges": "bytes",  # Tell client we support resume
        }

        # Forward Content-Range if source sent it (partial content)
        if "Content-Range" in r.headers:
            headers["Content-Range"] = r.headers["Content-Range"]

        # Forward Content-Length (either full or partial)
        if "Content-Length" in r.headers:
            headers["Content-Length"] = r.headers["Content-Length"]

        # Content-Disposition
        disposition = (
            f'attachment; filename="{filename}"'
            if mode == "download"
            else f'inline; filename="{filename}"'
        )
        headers["Content-Disposition"] = disposition

        # Stream generator
        def generate():
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk

        return Response(
            generate(),
            status=status_code,
            headers=headers
        )

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ====== Stats route ======

@app.route("/stats", methods=["GET"])

def get_stats():

    return jsonify(get_db_stats())


# ====== ADMIN RESET ROUTE ======

@app.route("/admin/reset", methods=["POST"])
def reset_stats():
    data = request.get_json()
    password = data.get("password")

    if password != ADMIN_PASSWORD:
        return jsonify({"success": False, "message": "Wrong password"}), 401

    stats["requests"] = 0
    stats["downloads"] = 0
    stats["cache_hits"] = 0
    stats["videos_served"] = 0
    stats["unique_ips"] = set()
    stats["download_logs"] = []

    cache.clear()

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("UPDATE stats SET requests=0, downloads=0, cache_hits=0, videos_served=0 WHERE id=1")
    c.execute("DELETE FROM unique_ips")
    c.execute("DELETE FROM download_logs")

    conn.commit()
    conn.close()

    return jsonify({"success": True})


# ====== Start server ======

if __name__ == "__main__":

    app.run(host="0.0.0.0", port=10000, threaded=True)
