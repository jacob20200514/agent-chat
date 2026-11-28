"""Bounded local transcription for browser recordings. No agent or network API."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import wave

MAX_BYTES = 8 * 1024 * 1024
MAX_SECONDS = 120
SAMPLE_RATE = 16000
UPLOAD_TIMEOUT = 30
TRANSCRIBE_TIMEOUT = 45


class VoiceError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def configuration(data):
    try:
        config = json.loads((data / 'transcription.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(config, dict):
        return None
    command = config.get('command')
    ffmpeg = config.get('ffmpeg', shutil.which('ffmpeg'))
    if (not isinstance(command, list) or not command
            or not all(isinstance(arg, str) and '\0' not in arg for arg in command)
            or not all(isinstance(path, str) and '\0' not in path and Path(path).is_absolute()
                       and Path(path).is_file() and os.access(path, os.X_OK) for path in (command[0], ffmpeg))):
        return None
    return ffmpeg, command


def container(data):
    if data[4:8] == b'ftyp':
        return 'mov'
    if data.startswith(b'\x1a\x45\xdf\xa3') and b'\x42\x82\x84webm' in data[:4096]:
        return 'matroska'
    raise VoiceError(415, 'VOICE_FORMAT', '这段录音格式不受支持，请使用 Safari 或 Chrome 重新录制。')


def run(argv, deadline):
    with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          start_new_session=True) as child:
        try:
            stdout, _ = child.communicate(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.communicate()
            raise VoiceError(504, 'VOICE_TIMEOUT', '转写超时，请试一段更短的录音。') from None
        return child.returncode, stdout


def transcribe(data, config):
    fmt = container(data)
    ffmpeg, command = config
    deadline = time.monotonic() + TRANSCRIBE_TIMEOUT
    with tempfile.TemporaryDirectory(prefix='agent-chat-voice-') as directory:
        source = Path(directory) / 'recording'
        wav = Path(directory) / 'speech.wav'
        source.write_bytes(data)
        # Limit actual decoded audio, even for WebM with no duration or a forged duration.
        # Decode a few extra frames to distinguish a recording at the limit from a longer one.
        demuxer_args = ['-enable_drefs', '0'] if fmt == 'mov' else []
        rc, _ = run([ffmpeg, '-nostdin', '-loglevel', 'error', '-xerror', '-protocol_whitelist', 'file',
                     '-f', fmt, *demuxer_args, '-threads', '1', '-i', str(source), '-map', '0:a:0',
                     '-ac', '1', '-ar', str(SAMPLE_RATE), '-t', str(MAX_SECONDS + 0.02),
                     '-c:a', 'pcm_s16le', str(wav)], deadline)
        if rc:
            raise VoiceError(422, 'VOICE_DECODE', '无法读取这段录音，请重新录制。')
        with wave.open(str(wav), 'rb') as audio:
            frames = audio.getnframes()
        if frames > SAMPLE_RATE * MAX_SECONDS:
            raise VoiceError(413, 'VOICE_DURATION', '录音不能超过 120 秒。')
        if not frames:
            raise VoiceError(422, 'VOICE_EMPTY', '没有听到语音，请重新录制。')
        rc, stdout = run([*command, str(wav)], deadline)
        if rc:
            raise VoiceError(503, 'VOICE_ENGINE', '本地转写暂不可用，请稍后重试或直接打字。')
        text = stdout.decode('utf-8').strip()
        if not text:
            raise VoiceError(422, 'VOICE_EMPTY', '没有听到语音，请重新录制。')
        return text
