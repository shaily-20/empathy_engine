from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from main import synthesize_text, unique_output_name


class SynthesisRequest(BaseModel):
    text: str = Field(..., min_length=1, description="Input text to synthesize")
    mode: str = Field(default="auto", pattern="^(auto|online|offline)$")
    use_ssml: bool = Field(default=True, description="Enable basic SSML parsing")
    auto_detect_emotion: bool = Field(default=True, description="Auto detect emotion from text")
    emotion: str = Field(default="neutral", pattern="^(positive|negative|neutral|inquisitive|surprised|concerned)$")
    intensity: int = Field(default=55, ge=10, le=100, description="Emotion intensity percent")


app = FastAPI(title="Empathy Engine API", version="1.0.0")

audio_dir = Path("generated_audio")
audio_dir.mkdir(exist_ok=True)
app.mount("/audio", StaticFiles(directory=str(audio_dir)), name="audio")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse("static/index.html")


@app.post("/synthesize")
def synthesize(req: SynthesisRequest) -> dict[str, str | float]:
    suffix = ".mp3" if req.mode != "offline" else ".wav"
    output_name = unique_output_name(suffix)
    output_path = audio_dir / output_name

    try:
        result = synthesize_text(
            text=req.text,
            mode=req.mode,
            output_path=str(output_path),
            use_ssml=req.use_ssml,
            emotion_override=None if req.auto_detect_emotion else req.emotion,
            intensity_scale=req.intensity / 55.0,
            quiet=True,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    final_path = Path(str(result["output_file"]))
    return {
        "emotion": str(result["emotion"]),
        "intensity": float(result["intensity"]),
        "provider": str(result["provider"]),
        "output_file": final_path.name,
        "audio_url": f"/audio/{final_path.name}",
        "processed_text": str(result["processed_text"]),
        "rate_wpm": str(result["rate_wpm"]),
        "volume": str(result["volume"]),
        "pitch_hz": str(result["pitch_hz"]),
    }
