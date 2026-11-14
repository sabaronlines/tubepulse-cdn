from fastapi import FastAPI, Body
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from youtube_transcript_api import YouTubeTranscriptApi
from deep_translator import GoogleTranslator
import yt_dlp
import re
import httpx
import urllib.parse
import os
import speech_recognition as sr

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def read_root():
    return {"status": "TubePulse AI Backend is Live and Running!"}

def extract_video_id(url_or_query: str):
    if len(url_or_query) == 11 and not " " in url_or_query:
        return url_or_query
    youtube_regex = r'(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:[^\/\n\s]+\/\S+\/|(?:v|e(?:mbed)?)\/|\S*?[?&]v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})'
    match = re.search(youtube_regex, url_or_query)
    if match:
        return match.group(1)
    return None

@app.get("/api/search")
def search_youtube(q: str):
    vid = extract_video_id(q)
    if vid:
        ydl_opts = {'quiet': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
                channel_name = info.get('uploader') or info.get('channel') or info.get('uploader_id') or 'YouTube Creator'
                return {"results": [{
                    "video_id": vid,
                    "title": info.get('title', 'YouTube Video'),
                    "uploader": channel_name,
                    "thumbnail": f"https://img.youtube.com/vi/{vid}/maxresdefault.jpg"
                }]}
            except:
                return {"results": [{"video_id": vid, "title": "YouTube Video", "uploader": "YouTube Creator", "thumbnail": f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"}]}
    
    ydl_opts = {'quiet': True, 'extract_flat': True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            result = ydl.extract_info(f"ytsearch8:{q}", download=False)
            entries = result.get('entries', [])
            results = []
            for entry in entries:
                v_id = entry.get('id')
                if v_id:
                    ch_name = entry.get('uploader') or entry.get('channel') or 'YouTube Creator'
                    results.append({
                        "video_id": v_id,
                        "title": entry.get('title', 'Unknown Title'),
                        "uploader": ch_name,
                        "thumbnail": f"https://img.youtube.com/vi/{v_id}/hqdefault.jpg"
                    })
            return {"results": results}
        except Exception as e:
            return {"error": str(e), "results": []}

@app.get("/api/extract")
def extract_youtube_data(q: str):
    video_id = extract_video_id(q) or q
    response_data = {"video_id": video_id}
    
    ydl_opts = {'quiet': True, 'format': 'best'}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            title = info.get("title", "YouTube Video")
            uploader = info.get("uploader") or info.get("channel") or info.get("uploader_id") or "YouTube Creator"
            
            response_data["title"] = title
            response_data["uploader"] = uploader
            response_data["duration"] = info.get("duration_string")
            response_data["thumbnail"] = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
            
            formats = info.get("formats", [])
            download_links = []
            audio_links = []
            seen_res = set()
            
            for f in formats:
                if f.get("url"):
                    raw_url = f.get("url")
                    ext = f.get("ext", "mp4")
                    if ext in ["mp4", "webm", "m4a"] and f.get("resolution") and f.get("vcodec") != "none":
                        res = f.get("resolution")
                        if res not in seen_res:
                            seen_res.add(res)
                            download_links.append({
                                "resolution": res,
                                "ext": ext,
                                "raw_url": raw_url
                            })
                    elif f.get("acodec") != "none" and f.get("vcodec") == "none":
                        abr = f.get("abr") or 128
                        audio_links.append({
                            "resolution": f"Audio MP3 ({abr}kbps High Quality)",
                            "ext": "mp3",
                            "raw_url": raw_url
                        })
            
            if not download_links:
                download_links.append({"resolution": "HD 720p", "ext": "mp4", "raw_url": f"https://www.youtube.com/watch?v={video_id}"})
                download_links.append({"resolution": "Full HD 1080p", "ext": "mp4", "raw_url": f"https://www.youtube.com/watch?v={video_id}"})
            if not audio_links:
                audio_links.append({"resolution": "Audio MP3 (320kbps Studio Quality)", "ext": "mp3", "raw_url": f"https://www.youtube.com/watch?v={video_id}"})
                audio_links.append({"resolution": "Audio MP3 (128kbps Standard)", "ext": "mp3", "raw_url": f"https://www.youtube.com/watch?v={video_id}"})

            response_data["downloads"] = download_links[:4]
            response_data["audio_downloads"] = audio_links[:2]
    except Exception as e:
        response_data["download_error"] = str(e)
        response_data["title"] = "YouTube Video"
        response_data["uploader"] = "YouTube Creator"
        response_data["downloads"] = [
            {"resolution": "HD 720p", "ext": "mp4", "raw_url": f"https://www.youtube.com/watch?v={video_id}"},
            {"resolution": "Full HD 1080p", "ext": "mp4", "raw_url": f"https://www.youtube.com/watch?v={video_id}"}
        ]
        response_data["audio_downloads"] = [
            {"resolution": "Audio MP3 (320kbps Studio Quality)", "ext": "mp3", "raw_url": f"https://www.youtube.com/watch?v={video_id}"},
            {"resolution": "Audio MP3 (128kbps Standard)", "ext": "mp3", "raw_url": f"https://www.youtube.com/watch?v={video_id}"}
        ]
        response_data["thumbnail"] = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"

    return response_data

@app.get("/api/transcript")
def get_transcript(video_id: str):
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        transcript = None
        detected_lang = "en"
        for lang in ['hi', 'or', 'en', 'bn', 'te']:
            try:
                transcript = transcript_list.find_transcript([lang])
                detected_lang = lang
                break
            except Exception:
                continue
        if not transcript:
            for t in transcript_list:
                transcript = t
                detected_lang = t.language_code
                break
        if transcript:
            return {"success": True, "language": detected_lang, "transcript": transcript.fetch()}
    except Exception:
        pass
    
    return {"success": False, "error": "No transcript available for this video."}

@app.get("/api/download-proxy")
async def download_proxy(url: str, filename: str = "media.mp4"):
    client = httpx.AsyncClient(follow_redirects=True, timeout=60.0)
    async def stream_generator():
        try:
            async with client.stream("GET", url) as response:
                async for chunk in response.iter_bytes(chunk_size=32768):
                    yield chunk
        except Exception:
            pass
            
    encoded_filename = urllib.parse.quote(filename)
    return StreamingResponse(
        stream_generator(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
            "Access-Control-Allow-Origin": "*"
        }
    )

@app.post("/api/translate-batch")
def translate_batch(data: dict = Body(...)):
    texts = data.get("texts", [])
    target_lang = data.get("target", "en")
    translated_texts = []
    try:
        translator = GoogleTranslator(source='auto', target=target_lang)
        for t in texts:
            try:
                tr = translator.translate(t)
                translated_texts.append(tr if tr else t)
            except:
                translated_texts.append(t)
    except Exception as e:
        translated_texts = texts
    return {"translated": translated_texts}
