# Changelog

Notable changes to Voxis. Version bumps are tagged in commit messages
(`vX.Y.Z: ...`); this file tracks contributions and fixes as they land.

## Unreleased

### Fixed
- Audio device diagnostics now reflect the device and signal actually being
  tested: output test tone, system-audio meter, and microphone meter are
  independent instead of conflated into one indicator. Raw audio activity is
  also distinguished from detected speech, so music/system sounds are no
  longer shown as speech.
- Translated-speech playback catch-up (WSOLA time-compression) is now shared
  between the Gemini and Qwen engines instead of being Qwen-only, so a long
  translated turn on either engine stays closer to the live captions.

### Added
- Meeting mode: an opt-in **"Listen to my translation"** monitor plays the
  outgoing translated speech through the user's own headphones in addition to
  the virtual microphone, so a speaker can verify what the other side hears.
- A language-swap control to exchange the two translation targets in one step.

*Thanks to [Vladimir Vorobyov (@uladzemer)](https://github.com/uladzemer) for
this contribution — [#39](https://github.com/DavutAkca/voxislive/pull/39).*
