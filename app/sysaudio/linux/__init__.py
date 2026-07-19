"""Linux (PipeWire/PulseAudio-compat) native audio backend implementations.

Staged incrementally behind `sysaudio.is_supported()` (Faz 3+ of the Linux
port) -- importing this package has no effect on Windows behavior, and
`is_supported()` still gates Linux to the "not supported yet" decline path
until every piece (capture, own-output routing, ducking) is wired in.
"""
