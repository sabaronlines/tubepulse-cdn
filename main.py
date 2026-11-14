from fastapi import FastAPI, Body
from fastapi.middleware.cors import CORSMiddleware
from youtube_transcript_api import YouTubeTranscriptApi
from deep_translator import GoogleTranslator
import yt_dlp
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def extract_video_id(url_or_query: str):
    if len(url_or_query) == 11 and not " " in url_or_query:
        return url_or_query
    youtube_regex = r'(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:[^\/\n\s]+\/\S+\/|(?:v|e(?:mbed)?)\/|\S*?[?&]v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})'
    match = re.search(youtube_regex, url_or_query)
    if match:
        return match.group(1)
    
    ydl_opts = {'quiet': True, 'extract_flat': True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            result = ydl.extract_info(f"ytsearch1:{url_or_query}", download=False)
            if 'entries' in result and len(result['entries']) > 0:
                return result['entries'][0]['id']
        except Exception:
            pass
    return None

@app.get("/api/extract")
def extract_youtube_data(q: str):
    video_id = extract_video_id(q)
    if not video_id:
        return {"error": "Invalid YouTube URL or Search Query"}
    
    response_data = {"video_id": video_id}
    ydl_opts = {'quiet': True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            response_data["title"] = info.get("title")
            response_data["duration"] = info.get("duration_string")
            response_data["uploader"] = info.get("uploader")
            response_data["thumbnail"] = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
            
            formats = info.get("formats", [])
            download_links = []
            for f in formats:
                if f.get("url") and f.get("ext") in ["mp4", "m4a", "webm"]:
                    download_links.append({
                        "resolution": f.get("resolution") or f.get("format_note"),
                        "ext": f.get("ext"),
                        "url": f.get("url")
                    })
            response_data["downloads"] = download_links[:5]
    except Exception as e:
        response_data["download_error"] = str(e)

    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        transcript = None
        detected_lang = "en"
        for lang in ['hi', 'or', 'en', 'bn']:
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
            response_data["language"] = detected_lang
            response_data["transcript"] = transcript.fetch()
        else:
            response_data["transcript"] = []
    except Exception:
        response_data["transcript"] = []

    return response_data

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
