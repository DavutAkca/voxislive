"""Gemini Live translate session, TEXT-only output — the cascade's cloud leg.

Identical to LiveTranslator (same model, same simultaneous translation, same
rotation/reconnect/watchdog machinery, same ephemeral-key support) except the
expensive audio-out modality is dropped: the server streams translated TEXT
while the speaker talks, and the local TTS (see cascade_translator) voices it.
Verified live 2026-07-12: the translate-preview model accepts TEXT-only
response_modalities and paces text like speech.
"""
from .translator import LiveTranslator


class TextLiveTranslator(LiveTranslator):
    # No cloud audio ever arrives on this leg — the base voice watchdog's
    # "text flowing but no audio" warning would fire every session.
    VOICE_WATCHDOG = False

    def _build_config(self) -> dict:
        config = super()._build_config()
        config["response_modalities"] = ["TEXT"]
        # Text arrives as model_turn parts; the audio-transcription streams and
        # the voice pin are meaningless without an audio response.
        config.pop("speech_config", None)
        config.pop("output_audio_transcription", None)
        config.pop("input_audio_transcription", None)
        return config
