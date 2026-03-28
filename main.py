"""Empathy Engine - online/offline auto-fallback TTS."""

from __future__ import annotations

import argparse
import asyncio
import re
import socket
import uuid
from pathlib import Path

import pyttsx3
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

try:
    import edge_tts
except ImportError:
    edge_tts = None


INQUISITIVE_HINTS = {"how", "why", "what", "when", "where", "could", "would", "can", "please explain"}
SURPRISED_HINTS = {"wow", "unbelievable", "amazing", "really", "no way", "incredible", "suddenly"}
CONCERNED_HINTS = {
    "sorry",
    "concerned",
    "worried",
    "problem",
    "issue",
    "frustrated",
    "upset",
    "disappointed",
    "urgent",
}


def parse_ssml_controls(text: str) -> tuple[str, dict[str, float]]:
    controls = {"rate_delta_pct": 0.0, "pitch_delta_hz": 0.0, "volume_delta_pct": 0.0}

    def break_replacer(match: re.Match[str]) -> str:
        ms = float(match.group(1))
        if ms >= 900:
            return ". "
        if ms >= 450:
            return ", "
        return " "

    text = re.sub(r"<break\s+time=['\"]?(\d+)ms['\"]?\s*/?>", break_replacer, text, flags=re.IGNORECASE)

    def emphasis_replacer(match: re.Match[str]) -> str:
        level = (match.group(1) or "moderate").lower()
        body = match.group(2).strip()
        if level == "strong":
            controls["pitch_delta_hz"] += 2.0
            controls["rate_delta_pct"] += 3.0
            return f"{body}!"
        if level == "reduced":
            controls["rate_delta_pct"] -= 4.0
            return f", {body},"
        return body

    text = re.sub(
        r"<emphasis(?:\s+level=['\"]?(strong|moderate|reduced)['\"]?)?>(.*?)</emphasis>",
        emphasis_replacer,
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    def prosody_replacer(match: re.Match[str]) -> str:
        attrs = match.group(1) or ""
        body = match.group(2)
        rate_match = re.search(r"rate=['\"]?([+-]?\d+)%['\"]?", attrs, flags=re.IGNORECASE)
        pitch_match = re.search(r"pitch=['\"]?([+-]?\d+)Hz['\"]?", attrs, flags=re.IGNORECASE)
        volume_match = re.search(r"volume=['\"]?([+-]?\d+)%['\"]?", attrs, flags=re.IGNORECASE)
        if rate_match:
            controls["rate_delta_pct"] += float(rate_match.group(1))
        if pitch_match:
            controls["pitch_delta_hz"] += float(pitch_match.group(1))
        if volume_match:
            controls["volume_delta_pct"] += float(volume_match.group(1))
        return body

    text = re.sub(
        r"<prosody([^>]*)>(.*?)</prosody>",
        prosody_replacer,
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r"</?speak[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip(), controls


def compute_intensity(text: str, compound: float) -> float:
    abs_compound = abs(compound)
    exclamations = min(text.count("!"), 5)
    upper_words = sum(1 for w in text.split() if len(w) >= 3 and w.isupper())
    upper_ratio = upper_words / max(1, len(text.split()))
    intensity = abs_compound + exclamations * 0.05 + min(0.15, upper_ratio * 0.4)
    return max(0.0, min(1.0, intensity))


def detect_emotion(text: str) -> tuple[str, float]:
    analyzer = SentimentIntensityAnalyzer()
    scores = analyzer.polarity_scores(text)
    compound = scores["compound"]
    lowered = text.lower()
    intensity = compute_intensity(text, compound)

    if "?" in text and any(h in lowered for h in INQUISITIVE_HINTS) and compound > -0.35:
        return "inquisitive", intensity
    if (
        any(h in lowered for h in SURPRISED_HINTS)
        or text.count("!") >= 2
        or re.search(r"\b(oh my|what a)\b", lowered)
    ) and compound >= -0.25:
        return "surprised", intensity
    if any(h in lowered for h in CONCERNED_HINTS) and compound <= 0.15:
        return "concerned", intensity
    if compound >= 0.05:
        return "positive", intensity
    if compound <= -0.05:
        return "negative", intensity
    return "neutral", intensity


def build_voice_profile(emotion: str, intensity: float) -> dict[str, float]:
    i = max(0.0, min(1.0, intensity))
    profiles: dict[str, dict[str, float]] = {
        "positive": {
            "rate_wpm": 168 + int(i * 58),
            "volume": min(1.0, 0.80 + i * 0.20),
            "pitch_hz": 3 + int(i * 10),
            "edge_rate_pct": 8 + int(i * 30),
            "edge_volume_pct": 6 + int(i * 24),
            "edge_pitch_hz": 3 + int(i * 11),
        },
        "negative": {
            "rate_wpm": 148 - int(i * 26),
            "volume": max(0.42, 0.76 - i * 0.28),
            "pitch_hz": -(2 + int(i * 8)),
            "edge_rate_pct": -(6 + int(i * 20)),
            "edge_volume_pct": -(4 + int(i * 16)),
            "edge_pitch_hz": -(2 + int(i * 8)),
        },
        "neutral": {
            "rate_wpm": 155,
            "volume": 0.80,
            "pitch_hz": 0,
            "edge_rate_pct": 0,
            "edge_volume_pct": 0,
            "edge_pitch_hz": 0,
        },
        "inquisitive": {
            "rate_wpm": 158 + int(i * 20),
            "volume": min(1.0, 0.80 + i * 0.08),
            "pitch_hz": 2 + int(i * 6),
            "edge_rate_pct": 3 + int(i * 14),
            "edge_volume_pct": 2 + int(i * 8),
            "edge_pitch_hz": 4 + int(i * 8),
        },
        "surprised": {
            "rate_wpm": 176 + int(i * 62),
            "volume": min(1.0, 0.85 + i * 0.15),
            "pitch_hz": 6 + int(i * 10),
            "edge_rate_pct": 14 + int(i * 30),
            "edge_volume_pct": 10 + int(i * 18),
            "edge_pitch_hz": 8 + int(i * 12),
        },
        "concerned": {
            "rate_wpm": 142 - int(i * 14),
            "volume": max(0.46, 0.74 - i * 0.18),
            "pitch_hz": -(1 + int(i * 5)),
            "edge_rate_pct": -(8 + int(i * 12)),
            "edge_volume_pct": -(5 + int(i * 12)),
            "edge_pitch_hz": -(2 + int(i * 6)),
        },
    }
    return profiles.get(emotion, profiles["neutral"]).copy()


def apply_ssml_controls(profile: dict[str, float], controls: dict[str, float]) -> dict[str, float]:
    updated = profile.copy()
    updated["edge_rate_pct"] += controls["rate_delta_pct"]
    updated["edge_pitch_hz"] += controls["pitch_delta_hz"]
    updated["edge_volume_pct"] += controls["volume_delta_pct"]
    updated["pitch_hz"] += controls["pitch_delta_hz"]
    updated["rate_wpm"] = int(updated["rate_wpm"] * (1 + controls["rate_delta_pct"] / 100))
    updated["volume"] = max(0.3, min(1.0, updated["volume"] * (1 + controls["volume_delta_pct"] / 100)))
    return updated


def internet_available(timeout_seconds: float = 1.2) -> bool:
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=timeout_seconds).close()
        return True
    except OSError:
        return False


async def speak_online_edge(text: str, output_file: Path, profile: dict[str, float]) -> None:
    if edge_tts is None:
        raise RuntimeError("edge-tts package is not installed.")

    communicate = edge_tts.Communicate(
        text=text,
        voice="en-US-JennyNeural",
        rate=f"{int(profile['edge_rate_pct']):+d}%",
        volume=f"{int(profile['edge_volume_pct']):+d}%",
        pitch=f"{int(profile['edge_pitch_hz']):+d}Hz",
    )
    await communicate.save(str(output_file))


def speak_offline_pyttsx3(text: str, output_file: Path, profile: dict[str, float]) -> Path:
    engine = pyttsx3.init()
    engine.setProperty("rate", int(profile["rate_wpm"]))
    engine.setProperty("volume", float(profile["volume"]))
    offline_target = output_file if output_file.suffix.lower() == ".wav" else output_file.with_suffix(".wav")
    engine.save_to_file(text, str(offline_target))
    engine.runAndWait()
    return offline_target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Empathy Engine TTS")
    parser.add_argument("--text", type=str, help="Input text to synthesize")
    parser.add_argument(
        "--mode",
        choices=["auto", "online", "offline"],
        default="auto",
        help="TTS mode selection (default: auto)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="output.mp3",
        help="Output audio path (default: output.mp3)",
    )
    return parser


def synthesize_text(
    text: str,
    mode: str = "auto",
    output_path: str = "output.mp3",
    use_ssml: bool = True,
    emotion_override: str | None = None,
    intensity_scale: float = 1.0,
    quiet: bool = False,
) -> dict[str, str | float]:
    if not text.strip():
        raise ValueError("Input text cannot be empty.")

    processed_text = text
    ssml_controls = {"rate_delta_pct": 0.0, "pitch_delta_hz": 0.0, "volume_delta_pct": 0.0}
    if use_ssml and "<" in text and ">" in text:
        processed_text, ssml_controls = parse_ssml_controls(text)

    emotion, intensity = detect_emotion(processed_text)
    if emotion_override:
        emotion = emotion_override.strip().lower()
    intensity = max(0.0, min(1.0, intensity * max(0.2, min(2.0, intensity_scale))))
    profile = build_voice_profile(emotion, intensity)
    profile = apply_ssml_controls(profile, ssml_controls)

    output_file = Path(output_path)
    final_file = output_file
    should_try_online = mode in {"auto", "online"}
    should_use_offline = mode == "offline"
    provider = ""

    if not quiet:
        print("Empathy Engine")
        print("--------------")
        print(f"Detected emotion: {emotion} (intensity: {intensity:.2f})")
        print(
            f"Voice profile -> rate: {profile['rate_wpm']} wpm, "
            f"volume: {profile['volume']:.2f}, pitch: {profile['pitch_hz']}Hz"
        )
        if use_ssml and processed_text != text:
            print("SSML controls: enabled")

    if mode == "auto":
        should_try_online = internet_available()
        should_use_offline = not should_try_online
        if not quiet:
            print(f"Mode: auto (internet {'available' if should_try_online else 'unavailable'})")
    elif not quiet:
        print(f"Mode: {mode}")

    if should_try_online:
        try:
            asyncio.run(speak_online_edge(processed_text, output_file, profile))
            provider = "online-edge-tts"
            final_file = output_file
        except Exception as exc:
            if mode == "online":
                raise RuntimeError(f"Online mode failed: {exc}") from exc
            if not quiet:
                print(f"Online TTS failed ({exc}); falling back to offline pyttsx3.")
            should_use_offline = True

    if should_use_offline and not provider:
        final_file = speak_offline_pyttsx3(processed_text, output_file, profile)
        provider = "offline-pyttsx3"

    if not quiet:
        print(f"Provider: {provider}")
        print(f"Audio saved to: {final_file}")

    return {
        "emotion": emotion,
        "intensity": round(float(intensity), 4),
        "provider": provider,
        "output_file": str(final_file),
        "processed_text": processed_text,
        "rate_wpm": str(profile["rate_wpm"]),
        "volume": str(profile["volume"]),
        "pitch_hz": str(profile["pitch_hz"]),
    }


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    text = args.text or input("Enter your text: ").strip()
    if not text:
        raise SystemExit("Input text cannot be empty.")

    try:
        synthesize_text(text=text, mode=args.mode, output_path=args.output, quiet=False)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


def unique_output_name(extension: str = ".mp3") -> str:
    ext = extension if extension.startswith(".") else f".{extension}"
    return f"audio_{uuid.uuid4().hex[:12]}{ext}"


if __name__ == "__main__":
    main()