"""
Audio Transcription Router - OpenAI Whisper-compatible endpoint backed by Gemini.

Accepts multipart/form-data audio uploads at /v1/audio/transcriptions
and uses Gemini's native audio understanding for transcription.
"""

import base64
import json
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse

from log import log
from src.api.geminicli import non_stream_request
from src.utils import authenticate_bearer

router = APIRouter()

# Maximum upload size (bytes) — matches OpenAI's 25 MB limit
MAX_FILE_SIZE = 25 * 1024 * 1024

# File extension → MIME type mapping for audio formats
MIME_MAP = {
    "mp3": "audio/mp3",
    "mp4": "audio/mp4",
    "mpeg": "audio/mpeg",
    "mpga": "audio/mpeg",
    "m4a": "audio/mp4",
    "wav": "audio/wav",
    "webm": "audio/webm",
    "ogg": "audio/ogg",
    "flac": "audio/flac",
    "aiff": "audio/aiff",
    "aac": "audio/aac",
}

# Default model for transcription
DEFAULT_MODEL = "gemini-2.5-flash"


def _resolve_model(model: str) -> str:
    """Map whisper model names to Gemini models."""
    if model in ("whisper-1", "whisper"):
        return DEFAULT_MODEL
    return model


def _detect_mime_type(filename: Optional[str], content_type: Optional[str]) -> Optional[str]:
    """Detect audio MIME type from filename extension or upload content type."""
    if filename:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in MIME_MAP:
            return MIME_MAP[ext]
    if content_type and content_type.startswith("audio/"):
        return content_type
    return None


def _build_transcription_prompt(
    language: Optional[str] = None, prompt: Optional[str] = None
) -> str:
    """Build the transcription prompt for Gemini."""
    parts = [
        "Transcribe the following audio verbatim.",
        "Output ONLY the transcription text, nothing else.",
        "Do not add commentary, timestamps, speaker labels, or formatting.",
    ]
    if language:
        parts.append(f"The audio is in {language}.")
    if prompt:
        parts.append(f"Context: {prompt}")
    return " ".join(parts)


@router.post("/v1/audio/transcriptions")
async def audio_transcriptions(
    file: UploadFile = File(...),
    model: str = Form("whisper-1"),
    language: Optional[str] = Form(None),
    prompt: Optional[str] = Form(None),
    response_format: str = Form("json"),
    temperature: Optional[float] = Form(None),
    _token: str = Depends(authenticate_bearer),
):
    """OpenAI-compatible audio transcription endpoint backed by Gemini."""
    # 1. Read and validate the uploaded file
    audio_bytes = await file.read()
    if len(audio_bytes) > MAX_FILE_SIZE:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": f"File too large: {len(audio_bytes)} bytes (max {MAX_FILE_SIZE})",
                    "type": "invalid_request_error",
                }
            },
        )

    if len(audio_bytes) == 0:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "Empty audio file",
                    "type": "invalid_request_error",
                }
            },
        )

    # 2. Detect MIME type
    mime_type = _detect_mime_type(file.filename, file.content_type)
    if not mime_type:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": (
                        f"Unsupported audio format. "
                        f"Filename: {file.filename}, content_type: {file.content_type}. "
                        f"Supported: {', '.join(sorted(MIME_MAP.keys()))}"
                    ),
                    "type": "invalid_request_error",
                }
            },
        )

    # 3. Build Gemini request
    gemini_model = _resolve_model(model)
    audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
    transcription_prompt = _build_transcription_prompt(language, prompt)

    gen_config = {
        "temperature": temperature if temperature is not None else 0.0,
        "maxOutputTokens": 8192,
    }

    gemini_body = {
        "model": gemini_model,
        "request": {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": transcription_prompt},
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": audio_b64,
                            }
                        },
                    ]
                }
            ],
            "generationConfig": gen_config,
        },
    }

    log.info(
        f"[AUDIO] Transcription request: model={gemini_model}, "
        f"format={response_format}, size={len(audio_bytes)}, mime={mime_type}"
    )

    # 4. Send to Gemini via the existing non-stream path
    response = await non_stream_request(body=gemini_body)

    if response.status_code != 200:
        log.error(f"[AUDIO] Gemini returned status {response.status_code}")
        # Pass through the upstream error fully
        try:
            upstream_error = json.loads(response.body)
            return JSONResponse(
                status_code=response.status_code,
                content=upstream_error,
            )
        except Exception:
            return JSONResponse(
                status_code=response.status_code,
                content={
                    "error": {
                        "message": "Transcription failed",
                        "type": "api_error",
                    }
                },
            )

    # 5. Extract text from Gemini response
    try:
        resp_data = json.loads(response.body)
        # Unwrap the outer "response" wrapper if present
        if "response" in resp_data:
            resp_data = resp_data["response"]

        text = ""
        candidates = resp_data.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            for part in parts:
                if "text" in part:
                    text += part["text"]
        text = text.strip()
        log.info(f"[AUDIO] Transcription result: {len(text)} chars, text={text[:200]!r}")
    except Exception as e:
        log.error(f"[AUDIO] Failed to parse Gemini response: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": "Failed to parse transcription response",
                    "type": "api_error",
                }
            },
        )

    # 6. Format response per response_format
    if response_format == "text":
        return PlainTextResponse(content=text)
    elif response_format == "verbose_json":
        return JSONResponse(
            content={
                "task": "transcribe",
                "language": language or "en",
                "duration": 0.0,
                "text": text,
                "segments": [
                    {
                        "id": 0,
                        "seek": 0,
                        "start": 0.0,
                        "end": 0.0,
                        "text": text,
                        "tokens": [],
                        "temperature": 0.0,
                        "avg_logprob": 0.0,
                        "compression_ratio": 1.0,
                        "no_speech_prob": 0.0,
                    }
                ] if text else [],
            }
        )
    else:
        # "json" (default) and any unknown format
        return JSONResponse(content={"text": text})
