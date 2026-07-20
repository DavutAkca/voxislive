"""Process-exclude WASAPI loopback (Windows 10 2004+, no extra installs).

Captures the system audio mix excluding our own process tree
(ApplicationLoopback API), so the translation's own TTS never re-enters the
capture — no echo gate required, video dialogue keeps flowing to Gemini even
while playback is active.

The format is requested directly as 16 kHz mono PCM16 so no resampling is
needed downstream.
"""
import collections
import ctypes
import logging
import threading
import time
from ctypes import POINTER, Structure, Union, byref, c_uint64, c_void_p, sizeof
from ctypes.wintypes import BYTE, DWORD, LPCWSTR, WORD

import comtypes
import numpy as np
from comtypes import COMMETHOD, COMObject, GUID, IUnknown
from pycaw.api.audioclient import IAudioClient, WAVEFORMATEX

UINT32 = ctypes.c_uint32
HRESULT = ctypes.HRESULT

VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE = 1
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
VT_BLOB = 65
RATE = 16000

_log = logging.getLogger("voxis.process_loopback")


class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(Structure):
    _fields_ = [("TargetProcessId", DWORD), ("ProcessLoopbackMode", DWORD)]


class _ActUnion(Union):
    _fields_ = [("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS)]


class AUDIOCLIENT_ACTIVATION_PARAMS(Structure):
    _fields_ = [("ActivationType", DWORD), ("u", _ActUnion)]


class _BLOB(Structure):
    _fields_ = [("cbSize", DWORD), ("pBlobData", c_void_p)]


class PROPVARIANT(Structure):
    class _U(Union):
        _fields_ = [("blob", _BLOB)]
    _anonymous_ = ("u",)
    _fields_ = [("vt", WORD), ("r1", WORD), ("r2", WORD), ("r3", WORD), ("u", _U)]


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetActivateResult",
                  (["out"], POINTER(HRESULT), "activateResult"),
                  (["out"], POINTER(POINTER(IUnknown)), "activatedInterface")),
    ]


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = [
        COMMETHOD([], HRESULT, "ActivateCompleted",
                  (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "op")),
    ]


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetBuffer",
                  (["out"], POINTER(POINTER(BYTE)), "ppData"),
                  (["out"], POINTER(UINT32), "pNumFramesToRead"),
                  (["out"], POINTER(DWORD), "pdwFlags"),
                  (["out"], POINTER(c_uint64), "pPos"),
                  (["out"], POINTER(c_uint64), "pQpc")),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], UINT32, "n")),
        COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(UINT32), "n")),
    ]


class IAgileObject(IUnknown):
    """Marker interface — declares the handler as apartment-agnostic.
    ActivateAudioInterfaceAsync returns E_ILLEGAL_METHOD_CALL without it."""
    _iid_ = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
    _methods_ = []


class _Handler(COMObject):
    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

    def __init__(self):
        super().__init__()
        self.done = threading.Event()

    def ActivateCompleted(self, op):
        self.done.set()
        return 0


def _activate_exclude(pid: int) -> "IAudioClient":
    """Activates a loopback IAudioClient that excludes our own process tree."""
    params = AUDIOCLIENT_ACTIVATION_PARAMS()
    params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
    params.u.ProcessLoopbackParams.TargetProcessId = pid
    params.u.ProcessLoopbackParams.ProcessLoopbackMode = \
        PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE

    pv = PROPVARIANT()
    pv.vt = VT_BLOB
    pv.blob.cbSize = sizeof(params)
    pv.blob.pBlobData = ctypes.cast(byref(params), c_void_p)

    handler = _Handler()
    h_ptr = handler.QueryInterface(IActivateAudioInterfaceCompletionHandler)

    fn = ctypes.windll.mmdevapi.ActivateAudioInterfaceAsync
    fn.restype = HRESULT
    fn.argtypes = [LPCWSTR, POINTER(GUID), POINTER(PROPVARIANT), c_void_p,
                   POINTER(POINTER(IActivateAudioInterfaceAsyncOperation))]
    op = POINTER(IActivateAudioInterfaceAsyncOperation)()
    hr = fn(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, byref(IAudioClient._iid_),
            byref(pv), ctypes.cast(h_ptr, c_void_p), byref(op))
    if hr != 0:
        raise OSError(f"ActivateAudioInterfaceAsync failed: 0x{hr & 0xFFFFFFFF:08X}")
    if not handler.done.wait(5):
        raise OSError("Process loopback activation timed out")
    res_hr, unk = op.GetActivateResult()
    if res_hr != 0:
        raise OSError(f"GetActivateResult failed: 0x{res_hr & 0xFFFFFFFF:08X}")
    return unk.QueryInterface(IAudioClient)


class ProcessExcludeLoopback:
    """Yields the system mix as 16 kHz mono float32, excluding our own process."""

    rate = RATE
    device_name = "Process-Exclude Loopback (16 kHz)"

    # Bounded buffer between capture and processing. WASAPI was activated with a
    # 200 ms buffer (2_000_000 hundred-ns); at ~4 ms packets that is ~50 chunks,
    # so a queue this size holds well over a buffer's worth before drop-oldest
    # kicks in.
    _QUEUE_MAX = 64

    def __init__(self, on_chunk, exclude_pid: int | None = None, rate: int = RATE):
        import os
        self._on_chunk = on_chunk
        # Per-instance capture rate: 16 kHz for Gemini; 24 kHz when the OpenAI
        # engine wants full-band input (overrides the class default).
        self.rate = int(rate)
        self._pid = exclude_pid or os.getpid()
        self._run = False
        self._err: Exception | None = None
        self.dropped = 0  # chunks lost to the bounded queue's drop-oldest (telemetry)
        self._ready = threading.Event()
        # Capture and processing are separated: the capture thread must only
        # GetBuffer/copy/ReleaseBuffer and hand off, never run on_chunk, so a
        # VAD/GC stall in the consumer cannot delay ReleaseBuffer and overflow
        # the WASAPI ring. The queue is bounded drop-oldest to bound latency.
        self._queue: collections.deque = collections.deque(maxlen=self._QUEUE_MAX)
        self._has_data = threading.Event()
        self._go = threading.Event()
        self._thread = threading.Thread(target=self._worker, daemon=True, name="ploopback")
        self._proc = threading.Thread(target=self._processor, daemon=True,
                                      name="ploopback-proc")
        # Start the capture thread immediately and wait so any setup failure
        # surfaces before start() is called.
        self._thread.start()
        self._ready.wait(8)
        if self._err:
            raise self._err
        self._proc.start()

    @property
    def failed(self) -> bool:
        """True once the capture thread has died from an unexpected fault (not a
        normal stop). Polled by the session liveness check so a dead capture
        stops billing and surfaces an error instead of accruing silent dead air."""
        return self._err is not None

    def _worker(self):
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass
        cap = None
        client = None
        try:
            client = _activate_exclude(self._pid)
            wfx = WAVEFORMATEX()
            wfx.wFormatTag = 1
            wfx.nChannels = 1
            wfx.nSamplesPerSec = self.rate
            wfx.wBitsPerSample = 16
            wfx.nBlockAlign = 2
            wfx.nAvgBytesPerSec = self.rate * 2
            wfx.cbSize = 0
            client.Initialize(AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_LOOPBACK,
                              2_000_000, 0, byref(wfx), None)
            cap = client.GetService(IAudioCaptureClient._iid_).QueryInterface(
                IAudioCaptureClient)
            client.Start()
        except Exception as e:
            self._err = e
            self._ready.set()
            # Tear down the apartment we initialized so a failed activation does
            # not leak a COM apartment on this thread.
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass
            return
        self._ready.set()
        self._go.wait()
        try:
            while self._run:
                try:
                    n = cap.GetNextPacketSize()
                    if not n:
                        time.sleep(0.004)
                        continue
                    data, frames, flags, _p, _q = cap.GetBuffer()
                    if flags & AUDCLNT_BUFFERFLAGS_SILENT:
                        x = np.zeros(frames, dtype=np.float32)
                    else:
                        raw = ctypes.string_at(data, frames * 2)
                        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                    # ReleaseBuffer immediately — never gated on the consumer.
                    cap.ReleaseBuffer(frames)
                    if len(self._queue) == self._QUEUE_MAX:
                        self.dropped += 1  # deque(maxlen) evicts silently — count it
                    self._queue.append(x)
                    self._has_data.set()
                except Exception as e:
                    # An unexpected mid-session WASAPI fault (endpoint unplugged,
                    # exclusive-mode grab, sleep/resume) — record it so the
                    # session can detect the dead capture, surface it, and stop
                    # billing dead air. A normal stop() exits via `while self._run`,
                    # not here, so this only fires on genuine faults.
                    if self._run:
                        self._err = e
                    break
        finally:
            # Release the audio client and COM apartment on the same thread that
            # created them so each start/stop cycle is leak-free.
            try:
                client.Stop()
            except Exception:
                pass
            self._has_data.set()  # wake the processor so it can exit
            cap = None
            client = None
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass

    def _processor(self):
        # Drains the bounded queue and runs the (potentially slow) on_chunk
        # callback off the capture thread.
        # Block until start() (or stop()) fires: the thread is created in
        # __init__ while _run is still False and the queue is empty, so without
        # this gate the loop guard below is immediately false and the processor
        # would exit before any audio ever arrives — leaving the engine deaf.
        self._go.wait()
        # Count consecutive consumer faults. A single bad frame is swallowed
        # (transient), but a persistent fault (ducker COM failure, resampler
        # error, downstream send) would otherwise be silently swallowed every
        # frame forever — capture stays "healthy", billing keeps running, yet no
        # audio reaches the translator. After a small run of back-to-back
        # failures, record it via the same _err the capture thread uses for
        # fatal faults so `failed` / _maybe_warn_capture_dead surfaces it and
        # billing stops. Reset on any success so it never trips on noise.
        fails = 0
        while self._run or self._queue:
            if not self._queue:
                self._has_data.wait(0.05)
                self._has_data.clear()
                continue
            try:
                x = self._queue.popleft()
            except IndexError:
                continue
            try:
                self._on_chunk(x)
                fails = 0
            except Exception as e:
                fails += 1
                # Rate-limited logging: first failure, then every ~200th, so a
                # persistent fault is visible without flooding the log per frame.
                if fails == 1 or fails % 200 == 0:
                    _log.warning("consumer fault #%d", fails, exc_info=True)
                if fails >= 50 and self._err is None:
                    self._err = e
                continue

    def start(self):
        self._run = True
        self._go.set()

    def stop(self):
        self._run = False
        self._go.set()
        self._has_data.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1.5)
        if self._proc.is_alive():
            self._proc.join(timeout=1.5)
