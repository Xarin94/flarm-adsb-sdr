#!/usr/bin/env python3
"""
PlutoSDR ADS-B tracker.

Backend locale per ricevere ADS-B 1090 MHz da PlutoSDR, decodificare Mode-S
DF17/DF18 e servire una UI Leaflet via HTTP/SSE.
"""

from __future__ import annotations

import argparse
import ctypes.util
import glob
import json
import math
import mimetypes
import os
import queue
import random
import signal
import socket
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import unquote, urlparse

import numpy as np

import flarm_legacy


MODE_S_POLY = 0xFFF409
ADS_B_FREQ_HZ = 1_090_000_000
FLARM_EU_FREQ_HZ = 868_200_000
DEFAULT_SAMPLE_RATE = 4_000_000
DEFAULT_RF_BW = 2_800_000
DEFAULT_GAIN_DB = 35.0
DEFAULT_MIN_GAIN_DB = 5.0
DEFAULT_MAX_GAIN_DB = 52.0
DEFAULT_AUTO_GAIN_CEILING_DB = 42.0
DEFAULT_UI_RATE_HZ = 12.0
DEFAULT_SPECTRUM_BINS = 1024
DEFAULT_SPECTRUM_FFT_SIZE = 16384
SPECTRUM_FLOOR_DB = -120.0
SPECTRUM_CEILING_DB = 0.0
# Full-scale di riferimento per lo spettro assoluto (Pluto: ADC 12 bit -> +/-2048).
ADC_FULL_SCALE = 2048.0
IQ_SCOPE_POINTS = 512
TRACK_TTL_S = 120.0
TRACK_TRAIL_TTL_S = 120.0
CPR_MAX_PAIR_AGE_S = 10.0
CPR_SCALE = 131072.0
MIN_TRACK_JUMP_GATE_KM = 8.0
MAX_TRACK_SPEED_KM_S = 1.2
KNOTS_PER_M_S = 1.943844
FT_MIN_PER_M_S = 196.850394
MIN_SPEED_ESTIMATE_DT_S = 0.5
MAX_SPEED_ESTIMATE_DT_S = 20.0
PROTOCOL_ADSB = "adsb"
PROTOCOL_FLARM = "flarm"
ALL_PROTOCOLS = (PROTOCOL_ADSB, PROTOCOL_FLARM)


def normalize_protocols(value: Any) -> list[str]:
    """Normalizza una selezione protocolli in un ordine stabile; almeno ADS-B."""
    if value is None:
        return [PROTOCOL_ADSB, PROTOCOL_FLARM]
    if isinstance(value, str):
        tokens = [token.strip().lower() for token in value.split(",")]
    else:
        tokens = [str(token).strip().lower() for token in value]
    selected = [proto for proto in ALL_PROTOCOLS if proto in tokens]
    return selected or [PROTOCOL_ADSB]
_DLL_DIRECTORY_HANDLES: list[Any] = []


def _prepend_runtime_path(directory: str) -> None:
    if not directory or not os.path.isdir(directory):
        return
    paths = os.environ.get("PATH", "").split(os.pathsep)
    if directory not in paths:
        os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        try:
            _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(directory))
        except OSError:
            pass


def _libiio_candidate_dirs() -> list[str]:
    candidates: list[str] = []

    def add_dir(value: Optional[str]) -> None:
        if not value:
            return
        directory = os.path.abspath(value)
        if directory not in candidates and os.path.isdir(directory):
            candidates.append(directory)

    def add_file(value: Optional[str]) -> None:
        if not value:
            return
        file_path = os.path.abspath(value)
        if os.path.isfile(file_path) and os.path.basename(file_path).lower() == "libiio.dll":
            add_dir(os.path.dirname(file_path))
        elif os.path.isdir(file_path):
            add_dir(file_path)

    for entry in os.environ.get("PLUTO_ADSB_LIBIIO_DIR", "").split(os.pathsep):
        add_file(entry)
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        add_dir(entry)

    add_dir(os.path.dirname(os.path.abspath(__file__)))
    add_dir(os.path.dirname(os.path.abspath(sys.executable)))
    add_dir(getattr(sys, "_MEIPASS", None))

    for prefix in (sys.prefix, sys.base_prefix):
        add_dir(prefix)
        add_dir(os.path.join(prefix, "DLLs"))
        add_dir(os.path.join(prefix, "Library", "bin"))

    patterns: list[str] = []
    for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if not root or not os.path.isdir(root):
            continue
        patterns.extend(
            [
                os.path.join(root, "*", "libiio.dll"),
                os.path.join(root, "*", "bin", "libiio.dll"),
                os.path.join(root, "*", "*", "libiio.dll"),
                os.path.join(root, "*", "*", "bin", "libiio.dll"),
            ]
        )
    for pattern in patterns:
        for dll_path in glob.glob(pattern):
            add_file(dll_path)

    return candidates


def prepare_libiio_runtime() -> Optional[str]:
    if not sys.platform.startswith("win"):
        return None
    try:
        found = ctypes.util.find_library("libiio.dll") or ctypes.util.find_library("iio")
    except Exception:
        found = None
    if found:
        return found

    for directory in _libiio_candidate_dirs():
        dll_path = os.path.join(directory, "libiio.dll")
        if not os.path.isfile(dll_path):
            continue
        _prepend_runtime_path(directory)
        try:
            found = ctypes.util.find_library("libiio.dll") or ctypes.util.find_library("iio")
        except Exception:
            found = None
        if found:
            return dll_path
    return None


def libiio_missing_message() -> str:
    return (
        "libiio.dll non trovato da Python. Installa il runtime libiio/Analog Devices "
        "oppure imposta PLUTO_ADSB_LIBIIO_DIR sulla cartella che contiene libiio.dll. "
        "Esempio PowerShell: $env:PLUTO_ADSB_LIBIIO_DIR='C:\\Program Files\\SDR-Radio.com (V3)'"
    )


def _host_from_iio_uri(uri: str) -> Optional[str]:
    text = str(uri or "")
    if not text.startswith("ip:"):
        return None
    host = text[3:]
    if host.startswith("//"):
        host = host[2:]
    if "/" in host:
        host = host.split("/", 1)[0]
    if ":" in host:
        host = host.split(":", 1)[0]
    return host or None


def _probe_iiod_tcp(host: str, timeout_s: float = 1.0) -> str:
    try:
        with socket.create_connection((host, 30431), timeout=timeout_s):
            return f"ip:{host} raggiungibile sulla porta IIO 30431"
    except OSError as exc:
        return f"ip:{host} non raggiungibile sulla porta IIO 30431 ({exc})"


def _finite_float(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def utc_now() -> float:
    return time.time()


def bits_from_bytes(payload: bytes, bit_count: int) -> list[int]:
    bits: list[int] = []
    for byte in payload:
        for shift in range(7, -1, -1):
            bits.append((byte >> shift) & 1)
            if len(bits) == bit_count:
                return bits
    return bits


def bytes_from_bits(bits: list[int]) -> bytes:
    out = bytearray((len(bits) + 7) // 8)
    for i, bit in enumerate(bits):
        if bit:
            out[i // 8] |= 1 << (7 - (i % 8))
    return bytes(out)


def mode_s_crc(frame: bytes, bit_count: int = 112) -> int:
    """Return the Mode-S CRC remainder. Valid DF17/DF18 frames return 0."""
    remainder = 0
    for bit in bits_from_bytes(frame, bit_count):
        remainder = (remainder << 1) | bit
        if remainder & (1 << 24):
            remainder ^= MODE_S_POLY
    return remainder & 0xFFFFFF


def mode_s_parity(payload: bytes, bit_count: int = 112) -> int:
    """Compute the 24-bit parity for payload bits excluding the parity field."""
    data_bits = bits_from_bytes(payload, bit_count - 24)
    remainder = 0
    for bit in data_bits:
        remainder = (remainder << 1) | bit
        if remainder & (1 << 24):
            remainder ^= MODE_S_POLY
    for _ in range(24):
        remainder <<= 1
        if remainder & (1 << 24):
            remainder ^= MODE_S_POLY
    return remainder & 0xFFFFFF


def decode_ac12_altitude(ac12: int) -> Optional[int]:
    """Decode a 12-bit ADS-B airborne altitude field with Q-bit set."""
    if not (ac12 & 0x10):
        return None
    n = ((ac12 & 0x0FE0) >> 1) | (ac12 & 0x000F)
    return n * 25 - 1000


def cpr_nl(lat_deg: float) -> int:
    lat = abs(lat_deg)
    if lat < 1e-12:
        return 59
    if lat >= 87.0:
        return 1
    nz = 15.0
    lat_rad = math.radians(lat)
    numerator = 1.0 - math.cos(math.pi / (2.0 * nz))
    denominator = math.cos(lat_rad) ** 2
    value = 1.0 - numerator / denominator
    value = min(1.0, max(-1.0, value))
    return int(math.floor((2.0 * math.pi) / math.acos(value)))


def cpr_mod(a: int, b: int) -> int:
    r = a % b
    return r + b if r < 0 else r


def decode_global_cpr(
    even_lat: int,
    even_lon: int,
    even_time: float,
    odd_lat: int,
    odd_lon: int,
    odd_time: float,
) -> Optional[tuple[float, float]]:
    even_lat_cpr = even_lat / CPR_SCALE
    even_lon_cpr = even_lon / CPR_SCALE
    odd_lat_cpr = odd_lat / CPR_SCALE
    odd_lon_cpr = odd_lon / CPR_SCALE

    dlat_even = 360.0 / 60.0
    dlat_odd = 360.0 / 59.0
    j = math.floor((59.0 * even_lat_cpr) - (60.0 * odd_lat_cpr) + 0.5)

    lat_even = dlat_even * (cpr_mod(j, 60) + even_lat_cpr)
    lat_odd = dlat_odd * (cpr_mod(j, 59) + odd_lat_cpr)

    if lat_even >= 270.0:
        lat_even -= 360.0
    if lat_odd >= 270.0:
        lat_odd -= 360.0

    if cpr_nl(lat_even) != cpr_nl(lat_odd):
        return None

    if even_time >= odd_time:
        lat = lat_even
        nl = cpr_nl(lat)
        ni = max(nl, 1)
        dlon = 360.0 / ni
        m = math.floor((even_lon_cpr * (nl - 1)) - (odd_lon_cpr * nl) + 0.5)
        lon = dlon * (cpr_mod(m, ni) + even_lon_cpr)
    else:
        lat = lat_odd
        nl = cpr_nl(lat)
        ni = max(nl - 1, 1)
        dlon = 360.0 / ni
        m = math.floor((even_lon_cpr * (nl - 1)) - (odd_lon_cpr * nl) + 0.5)
        lon = dlon * (cpr_mod(m, ni) + odd_lon_cpr)

    if lon >= 180.0:
        lon -= 360.0

    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return lat, lon


CALLSIGN_CHARS = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ#####_###############0123456789######"


@dataclass
class DecodedMessage:
    raw: str
    df: int
    icao: str
    timestamp: float
    type_code: Optional[int] = None
    callsign: Optional[str] = None
    altitude_ft: Optional[int] = None
    cpr_format: Optional[int] = None
    cpr_lat: Optional[int] = None
    cpr_lon: Optional[int] = None
    rssi: Optional[float] = None


def decode_callsign(me: bytes) -> str:
    me_value = int.from_bytes(me, "big")
    chars: list[str] = []
    for index in range(8):
        shift = 56 - 8 - ((index + 1) * 6)
        code = (me_value >> shift) & 0x3F
        char = CALLSIGN_CHARS[code] if code < len(CALLSIGN_CHARS) else "#"
        chars.append(" " if char in "#_" else char)
    return "".join(chars).strip()


def parse_adsb_frame(frame: bytes, timestamp: Optional[float] = None, rssi: Optional[float] = None) -> Optional[DecodedMessage]:
    if len(frame) != 14:
        return None

    df = frame[0] >> 3
    if df not in (17, 18):
        return None

    icao = f"{int.from_bytes(frame[1:4], 'big'):06X}"
    me = frame[4:11]
    type_code = frame[4] >> 3
    decoded = DecodedMessage(
        raw=frame.hex().upper(),
        df=df,
        icao=icao,
        timestamp=timestamp if timestamp is not None else utc_now(),
        type_code=type_code,
        rssi=rssi,
    )

    if 1 <= type_code <= 4:
        decoded.callsign = decode_callsign(me)
        return decoded

    if 9 <= type_code <= 18 or 20 <= type_code <= 22:
        ac12 = ((frame[5] << 4) | (frame[6] >> 4)) & 0x0FFF
        decoded.altitude_ft = decode_ac12_altitude(ac12)
        decoded.cpr_format = (frame[6] >> 2) & 1
        decoded.cpr_lat = ((frame[6] & 0x03) << 15) | (frame[7] << 7) | (frame[8] >> 1)
        decoded.cpr_lon = ((frame[8] & 0x01) << 16) | (frame[9] << 8) | frame[10]
        return decoded

    return decoded


@dataclass
class DecodeBatch:
    messages: list[DecodedMessage] = field(default_factory=list)
    preambles: int = 0
    crc_ok: int = 0
    crc_bad: int = 0
    noise_floor: float = 0.0
    clip_ratio: float = 0.0


class ADSBDecoder:
    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self.samples_per_us = sample_rate / 1_000_000.0
        self.half_samples = max(1, int(round(0.5 * self.samples_per_us)))
        self.bit_samples = max(2, int(round(self.samples_per_us)))
        self.preamble_samples = int(round(8.0 * self.samples_per_us))
        self.frame_samples = self.preamble_samples + 112 * self.bit_samples
        self.tail = np.empty(0, dtype=np.complex64)
        self.recent_frames: dict[str, float] = {}

    def decode_samples(self, samples: np.ndarray) -> DecodeBatch:
        now = utc_now()
        samples = np.asarray(samples)
        if samples.size == 0:
            return DecodeBatch()

        if self.tail.size:
            combined = np.concatenate((self.tail, samples.astype(np.complex64, copy=False)))
        else:
            combined = samples.astype(np.complex64, copy=False)

        mag = np.abs(combined).astype(np.float32, copy=False)
        if mag.size <= self.frame_samples + 1:
            self.tail = combined[-self.frame_samples :].copy()
            return DecodeBatch(noise_floor=float(np.median(mag)) if mag.size else 0.0)

        noise = float(np.median(mag))
        mad = float(np.median(np.abs(mag - noise))) + 1e-9
        percentile = float(np.percentile(mag, 99.5))
        threshold = max(noise + (6.0 * mad), percentile * 0.70)
        limit = mag.size - self.frame_samples - 1
        candidates = np.flatnonzero(mag[:limit] > threshold)

        batch = DecodeBatch(noise_floor=noise, clip_ratio=estimate_clip_ratio(samples))
        skip_until = -1

        for index in candidates:
            if index < skip_until:
                continue
            if not self._looks_like_preamble(mag, int(index), threshold, noise, mad):
                continue

            batch.preambles += 1
            skip_until = int(index) + self.preamble_samples
            bits, confidence = self._decode_bits(mag, int(index))
            frame = bytes_from_bits(bits)
            raw = frame.hex().upper()

            if self.recent_frames.get(raw, 0.0) > now - 0.35:
                continue
            self.recent_frames[raw] = now

            if mode_s_crc(frame, 112) == 0:
                batch.crc_ok += 1
                decoded = parse_adsb_frame(frame, now, confidence)
                if decoded is not None:
                    batch.messages.append(decoded)
            else:
                batch.crc_bad += 1

        if len(self.recent_frames) > 4096:
            cutoff = now - 2.0
            self.recent_frames = {raw: ts for raw, ts in self.recent_frames.items() if ts >= cutoff}

        self.tail = combined[-self.frame_samples :].copy()
        return batch

    def _window_mean(self, mag: np.ndarray, start: int, length: int) -> float:
        end = min(start + length, mag.size)
        if start < 0 or start >= end:
            return 0.0
        return float(np.mean(mag[start:end]))

    def _looks_like_preamble(
        self,
        mag: np.ndarray,
        index: int,
        threshold: float,
        noise: float,
        mad: float,
    ) -> bool:
        high_us = (0.0, 1.0, 3.5, 4.5)
        low_us = (0.5, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 5.5, 6.0, 6.5, 7.0, 7.5)
        highs = [
            self._window_mean(mag, index + int(round(us * self.samples_per_us)), self.half_samples)
            for us in high_us
        ]
        lows = [
            self._window_mean(mag, index + int(round(us * self.samples_per_us)), self.half_samples)
            for us in low_us
        ]
        min_high = min(highs)
        max_low = max(lows)
        mean_high = sum(highs) / len(highs)
        mean_low = sum(lows) / len(lows)

        if min_high < threshold:
            return False
        if min_high < noise + (5.0 * mad):
            return False
        if min_high < max_low * 1.45:
            return False
        if mean_high < mean_low + (4.0 * mad):
            return False
        return True

    def _decode_bits(self, mag: np.ndarray, index: int) -> tuple[list[int], float]:
        bits: list[int] = []
        confidence = 0.0
        data_start = index + self.preamble_samples
        for bit_index in range(112):
            bit_start = data_start + bit_index * self.bit_samples
            first = self._window_mean(mag, bit_start, self.half_samples)
            second = self._window_mean(mag, bit_start + self.half_samples, self.half_samples)
            bits.append(1 if first > second else 0)
            confidence += abs(first - second)
        return bits, confidence / 112.0


@dataclass
class FlarmBatch:
    bursts: int = 0
    decoded_messages: int = 0
    noise_floor: float = 0.0
    clip_ratio: float = 0.0
    targets: list["flarm_legacy.FlarmTarget"] = field(default_factory=list)
    sync_hits: int = 0


class FLARMDecoder:
    """Decoder del protocollo radio FLARM Legacy (v6/v7).

    Esegue demodulazione 2-FSK/GFSK + Manchester + sync + CRC sui campioni I/Q
    (vedi flarm_legacy), poi decifra e interpreta i pacchetti. Mantiene anche un
    rilevatore di burst come indicatore di attivita' RF sulla banda.
    """

    def __init__(self, sample_rate: int, ref_lat: float = 0.0, ref_lon: float = 0.0) -> None:
        self.sample_rate = sample_rate
        self.ref_lat = float(ref_lat)
        self.ref_lon = float(ref_lon)
        self.receiver = flarm_legacy.FlarmLegacyReceiver(sample_rate)
        self.recent: dict[str, float] = {}

    def decode_samples(self, samples: np.ndarray) -> FlarmBatch:
        samples = np.asarray(samples)
        if samples.size == 0:
            return FlarmBatch()

        now = utc_now()
        demod = self.receiver.process(samples.astype(np.complex64, copy=False))
        targets: list[flarm_legacy.FlarmTarget] = []
        for payload in demod.payloads:
            target = flarm_legacy.decode_payload(payload, self.ref_lat, self.ref_lon, now)
            if target is None:
                continue
            dedup_key = f"{target.version}:{target.addr_hex}"
            if self.recent.get(dedup_key, 0.0) > now - 0.8:
                continue
            self.recent[dedup_key] = now
            targets.append(target)
        if len(self.recent) > 1024:
            cutoff = now - 5.0
            self.recent = {k: ts for k, ts in self.recent.items() if ts >= cutoff}

        mag = np.abs(samples).astype(np.float32, copy=False)
        noise = float(np.median(mag))
        mad = float(np.median(np.abs(mag - noise))) + 1e-9
        threshold = noise + 7.0 * mad
        active = mag > threshold

        # Conteggio burst vettorizzato (indicatore di attivita' RF).
        min_len = max(8, int(self.sample_rate * 0.00025))
        padded = np.concatenate(([0], active.view(np.int8), [0]))
        edges = np.diff(padded)
        starts = np.flatnonzero(edges == 1)
        ends = np.flatnonzero(edges == -1)
        bursts = int(np.count_nonzero((ends - starts) >= min_len)) if starts.size else 0

        return FlarmBatch(
            bursts=bursts,
            decoded_messages=len(targets),
            noise_floor=noise,
            clip_ratio=estimate_clip_ratio(samples),
            targets=targets,
            sync_hits=demod.sync_hits,
        )


@dataclass
class Track:
    icao: str
    protocol: str = PROTOCOL_ADSB
    callsign: str = ""
    altitude_ft: Optional[int] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    last_seen: float = field(default_factory=utc_now)
    last_position: Optional[float] = None
    even_cpr: Optional[tuple[int, int, float]] = None
    odd_cpr: Optional[tuple[int, int, float]] = None
    trail: deque[tuple[float, float, float, Optional[int]]] = field(default_factory=lambda: deque(maxlen=480))
    rssi: Optional[float] = None
    speed_kt: Optional[float] = None
    vertical_ft_min: Optional[float] = None
    speed_source: str = ""
    vertical_source: str = ""


class AirTrafficTracker:
    def __init__(self, ttl_s: float = TRACK_TTL_S) -> None:
        self.ttl_s = ttl_s
        self.lock = threading.Lock()
        self.tracks: dict[str, Track] = {}

    def ingest(self, message: DecodedMessage) -> None:
        with self.lock:
            key = self._key(PROTOCOL_ADSB, message.icao)
            track = self.tracks.get(key)
            if track is None:
                track = Track(icao=message.icao, protocol=PROTOCOL_ADSB)
                self.tracks[key] = track

            track.last_seen = message.timestamp
            if message.callsign:
                track.callsign = message.callsign
            if message.altitude_ft is not None:
                track.altitude_ft = message.altitude_ft
            if message.rssi is not None:
                track.rssi = message.rssi

            if (
                message.cpr_format is not None
                and message.cpr_lat is not None
                and message.cpr_lon is not None
            ):
                cpr = (message.cpr_lat, message.cpr_lon, message.timestamp)
                if message.cpr_format == 0:
                    track.even_cpr = cpr
                else:
                    track.odd_cpr = cpr
                self._try_update_position(track)

            self._prune_locked(utc_now())

    def upsert_position(
        self,
        icao: str,
        lat: float,
        lon: float,
        altitude_ft: int,
        callsign: str,
        timestamp: Optional[float] = None,
        rssi: Optional[float] = None,
        protocol: str = PROTOCOL_ADSB,
        speed_kt: Optional[float] = None,
        vertical_ft_min: Optional[float] = None,
    ) -> None:
        now = timestamp if timestamp is not None else utc_now()
        with self.lock:
            key = self._key(protocol, icao)
            track = self.tracks.get(key)
            if track is None:
                track = Track(icao=icao, protocol=protocol)
                self.tracks[key] = track
            track.callsign = callsign
            track.last_seen = now
            track.rssi = rssi
            self._set_position(
                track,
                lat,
                lon,
                now,
                altitude_ft=altitude_ft,
                speed_kt=speed_kt,
                vertical_ft_min=vertical_ft_min,
            )
            self._prune_locked(now)

    def snapshot(self) -> list[dict[str, Any]]:
        now = utc_now()
        with self.lock:
            self._prune_locked(now)
            rows: list[dict[str, Any]] = []
            for track in sorted(self.tracks.values(), key=lambda item: item.icao):
                if track.lat is None or track.lon is None or track.last_position is None:
                    continue
                rows.append(
                    {
                        "icao": track.icao,
                        "protocol": track.protocol,
                        "callsign": track.callsign,
                        "lat": track.lat,
                        "lon": track.lon,
                        "altitude_ft": track.altitude_ft,
                        "last_seen_age": max(0.0, now - track.last_seen),
                        "last_position_age": max(0.0, now - track.last_position),
                        "rssi": track.rssi,
                        "speed_kt": track.speed_kt,
                        "vertical_ft_min": track.vertical_ft_min,
                        "speed_source": track.speed_source,
                        "vertical_source": track.vertical_source,
                        "trail": [
                            {"lat": lat, "lon": lon, "altitude_ft": altitude_ft}
                            for lat, lon, _ts, altitude_ft in self._snapshot_trail(track, now)
                        ],
                    }
                )
            return rows

    def active_count(self) -> int:
        return len(self.snapshot())

    def clear(self) -> None:
        with self.lock:
            self.tracks.clear()

    def _key(self, protocol: str, icao: str) -> str:
        return f"{protocol}:{icao}"

    def _try_update_position(self, track: Track) -> None:
        if track.even_cpr is None or track.odd_cpr is None:
            return
        even_lat, even_lon, even_time = track.even_cpr
        odd_lat, odd_lon, odd_time = track.odd_cpr
        if abs(even_time - odd_time) > CPR_MAX_PAIR_AGE_S:
            return
        decoded = decode_global_cpr(even_lat, even_lon, even_time, odd_lat, odd_lon, odd_time)
        if decoded is None:
            return
        lat, lon = decoded
        now = max(even_time, odd_time)
        if track.lat is not None and track.lon is not None and track.last_position is not None:
            if now + 0.5 < track.last_position:
                return
            distance_km = haversine_km(track.lat, track.lon, lat, lon)
            elapsed_s = max(0.5, now - track.last_position)
            max_distance_km = max(MIN_TRACK_JUMP_GATE_KM, elapsed_s * MAX_TRACK_SPEED_KM_S)
            if distance_km > max_distance_km:
                return
        self._set_position(track, lat, lon, now)

    def _snapshot_trail(self, track: Track, now: float) -> list[tuple[float, float, float, Optional[int]]]:
        self._trim_trail(track, now)
        trail = list(track.trail)
        if track.lat is None or track.lon is None:
            return trail
        current_ts = track.last_position if track.last_position is not None else track.last_seen
        if not trail:
            return [(track.lat, track.lon, current_ts, track.altitude_ft)]
        last_lat, last_lon, _last_ts, _last_altitude_ft = trail[-1]
        if haversine_km(last_lat, last_lon, track.lat, track.lon) * 1000.0 > 1.0:
            trail.append((track.lat, track.lon, current_ts, track.altitude_ft))
        return trail

    def _trim_trail(self, track: Track, now: float) -> None:
        while track.trail and now - track.trail[0][2] > TRACK_TRAIL_TTL_S:
            track.trail.popleft()

    def _set_position(
        self,
        track: Track,
        lat: float,
        lon: float,
        timestamp: float,
        altitude_ft: Optional[int] = None,
        speed_kt: Optional[float] = None,
        vertical_ft_min: Optional[float] = None,
    ) -> None:
        self._trim_trail(track, timestamp)
        self._update_motion(track, lat, lon, timestamp, altitude_ft, speed_kt, vertical_ft_min)
        if altitude_ft is not None:
            track.altitude_ft = altitude_ft
        track.lat = lat
        track.lon = lon
        track.last_position = timestamp
        if not track.trail:
            track.trail.append((lat, lon, timestamp, track.altitude_ft))
            return

        last_lat, last_lon, last_ts, _last_altitude_ft = track.trail[-1]
        moved_m = haversine_km(last_lat, last_lon, lat, lon) * 1000.0
        if moved_m >= 25.0 or timestamp - last_ts >= 2.0:
            track.trail.append((lat, lon, timestamp, track.altitude_ft))

    def _update_motion(
        self,
        track: Track,
        lat: float,
        lon: float,
        timestamp: float,
        altitude_ft: Optional[int],
        speed_kt: Optional[float],
        vertical_ft_min: Optional[float],
    ) -> None:
        reported_speed = _finite_float(speed_kt)
        if reported_speed is not None:
            track.speed_kt = max(0.0, reported_speed)
            track.speed_source = "reported"
        elif track.lat is not None and track.lon is not None and track.last_position is not None:
            elapsed_s = timestamp - track.last_position
            if MIN_SPEED_ESTIMATE_DT_S <= elapsed_s <= MAX_SPEED_ESTIMATE_DT_S:
                distance_m = haversine_km(track.lat, track.lon, lat, lon) * 1000.0
                estimated_speed_kt = (distance_m / elapsed_s) * KNOTS_PER_M_S
                track.speed_kt = max(0.0, estimated_speed_kt)
                track.speed_source = "estimated"

        reported_vertical = _finite_float(vertical_ft_min)
        if reported_vertical is not None:
            track.vertical_ft_min = reported_vertical
            track.vertical_source = "reported"
        elif altitude_ft is not None and track.altitude_ft is not None and track.last_position is not None:
            elapsed_s = timestamp - track.last_position
            if MIN_SPEED_ESTIMATE_DT_S <= elapsed_s <= MAX_SPEED_ESTIMATE_DT_S:
                vertical_m_s = ((altitude_ft - track.altitude_ft) * 0.3048) / elapsed_s
                track.vertical_ft_min = vertical_m_s * FT_MIN_PER_M_S
                track.vertical_source = "estimated"

    def _prune_locked(self, now: float) -> None:
        stale = []
        for icao, track in self.tracks.items():
            if track.last_position is None:
                if now - track.last_seen > self.ttl_s:
                    stale.append(icao)
            elif now - track.last_position > self.ttl_s:
                stale.append(icao)
        for icao in stale:
            del self.tracks[icao]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_km = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return earth_radius_km * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


class RuntimeStats:
    def __init__(self, source: str, args: argparse.Namespace) -> None:
        self.lock = threading.Lock()
        self.source = source
        self.requested_source = args.source
        self.center_frequency = args.center_frequency
        self.flarm_frequency = args.flarm_frequency
        self.pluto_uri = args.uri
        self.active_frequency = args.center_frequency
        self.active_protocol = PROTOCOL_ADSB
        self.rx0_scan = False
        self.enabled_protocols = normalize_protocols(getattr(args, "protocols", None))
        self.sample_rate = args.sample_rate
        self.rf_bandwidth = args.rf_bandwidth
        self.gain_mode = args.gain_mode
        self.gain_db: Optional[float] = args.gain
        self.rx1_active = False
        self.rx1_available = False
        self.rx1_status = "RX1 non inizializzato"
        self.rx_complex_streams = 0
        self.rx_data_channels: list[str] = []
        self.rx_state = "starting"
        self.last_error = ""
        self.buffers_total = 0
        self.preambles_total = 0
        self.crc_ok = 0
        self.crc_bad = 0
        self.adsb_messages_total = 0
        self.flarm_messages_total = 0
        self.flarm_bursts_total = 0
        self.messages_total = 0
        self.noise_floor = 0.0
        self.clip_ratio = 0.0
        self.last_buffer_time = 0.0
        self.message_times: deque[float] = deque(maxlen=512)

    def set_source(self, source: str) -> None:
        with self.lock:
            self.source = source

    def reset_for_source(self, source: str, state: str = "starting", error: str = "") -> None:
        with self.lock:
            self.source = source
            self.rx_state = state
            self.last_error = error
            self.buffers_total = 0
            self.preambles_total = 0
            self.crc_ok = 0
            self.crc_bad = 0
            self.adsb_messages_total = 0
            self.flarm_messages_total = 0
            self.flarm_bursts_total = 0
            self.messages_total = 0
            self.noise_floor = 0.0
            self.clip_ratio = 0.0
            self.last_buffer_time = 0.0
            self.rx1_active = False
            self.rx1_available = False
            self.rx1_status = ""
            self.rx_complex_streams = 0
            self.rx_data_channels = []
            self.active_frequency = self.center_frequency
            self.active_protocol = PROTOCOL_ADSB
            self.rx0_scan = False
            self.message_times.clear()

    def set_state(self, state: str, error: str = "") -> None:
        with self.lock:
            self.rx_state = state
            self.last_error = error

    def set_gain(self, gain_db: Optional[float]) -> None:
        with self.lock:
            self.gain_db = gain_db

    def set_gain_mode(self, gain_mode: str) -> None:
        with self.lock:
            self.gain_mode = gain_mode

    def set_rx1_active(self, active: bool) -> None:
        with self.lock:
            self.rx1_active = active

    def set_rx1_info(
        self,
        available: bool,
        status: str,
        complex_streams: Optional[int] = None,
        data_channels: Optional[list[str]] = None,
    ) -> None:
        with self.lock:
            self.rx1_available = available
            self.rx1_status = status
            if complex_streams is not None:
                self.rx_complex_streams = complex_streams
            if data_channels is not None:
                self.rx_data_channels = list(data_channels)

    def set_rx0_scan(self, enabled: bool) -> None:
        with self.lock:
            self.rx0_scan = enabled

    def set_active_tuning(self, protocol: str, frequency: int) -> None:
        with self.lock:
            self.active_protocol = protocol
            self.active_frequency = int(frequency)

    def set_enabled_protocols(self, protocols: list[str]) -> list[str]:
        with self.lock:
            self.enabled_protocols = normalize_protocols(protocols)
            return list(self.enabled_protocols)

    def get_enabled_protocols(self) -> list[str]:
        with self.lock:
            return list(self.enabled_protocols)

    def set_pluto_uri(self, uri: str) -> None:
        with self.lock:
            self.pluto_uri = uri

    def record_decode(self, batch: DecodeBatch) -> None:
        now = utc_now()
        with self.lock:
            self.buffers_total += 1
            self.preambles_total += batch.preambles
            self.crc_ok += batch.crc_ok
            self.crc_bad += batch.crc_bad
            self.adsb_messages_total += len(batch.messages)
            self.messages_total += len(batch.messages)
            self.noise_floor = batch.noise_floor
            self.clip_ratio = batch.clip_ratio
            self.last_buffer_time = now
            for _message in batch.messages:
                self.message_times.append(now)

    def record_sim_messages(self, count: int, noise_floor: float, clip_ratio: float) -> None:
        now = utc_now()
        with self.lock:
            self.buffers_total += 1
            self.preambles_total += count
            self.crc_ok += count
            self.messages_total += count
            self.adsb_messages_total += count
            self.noise_floor = noise_floor
            self.clip_ratio = clip_ratio
            self.last_buffer_time = now
            for _ in range(count):
                self.message_times.append(now)

    def record_flarm_decode(self, bursts: int, decoded_messages: int, noise_floor: float, clip_ratio: float) -> None:
        now = utc_now()
        shared_count = max(bursts, decoded_messages)
        with self.lock:
            self.flarm_bursts_total += bursts
            self.flarm_messages_total += decoded_messages
            self.messages_total += shared_count
            self.noise_floor = noise_floor
            self.clip_ratio = max(self.clip_ratio, clip_ratio)
            self.last_buffer_time = now
            for _ in range(shared_count):
                self.message_times.append(now)

    def snapshot(self, active_tracks: int) -> dict[str, Any]:
        now = utc_now()
        with self.lock:
            while self.message_times and self.message_times[0] < now - 1.0:
                self.message_times.popleft()
            crc_total = self.crc_ok + self.crc_bad
            crc_ratio = self.crc_ok / crc_total if crc_total else 0.0
            return {
                "source": self.source,
                "requested_source": self.requested_source,
                "rx_state": self.rx_state,
                "center_frequency": self.center_frequency,
                "flarm_frequency": self.flarm_frequency,
                "pluto_uri": self.pluto_uri,
                "active_frequency": self.active_frequency,
                "active_protocol": self.active_protocol,
                "enabled_protocols": list(self.enabled_protocols),
                "rx0_scan": self.rx0_scan,
                "sample_rate": self.sample_rate,
                "rf_bandwidth": self.rf_bandwidth,
                "gain_mode": self.gain_mode,
                "gain_db": self.gain_db,
                "rx1_active": self.rx1_active,
                "rx1_available": self.rx1_available,
                "rx1_status": self.rx1_status,
                "rx_complex_streams": self.rx_complex_streams,
                "rx_data_channels": list(self.rx_data_channels),
                "messages_total": self.messages_total,
                "messages_per_sec": len(self.message_times),
                "adsb_messages_total": self.adsb_messages_total,
                "flarm_messages_total": self.flarm_messages_total,
                "flarm_bursts_total": self.flarm_bursts_total,
                "preambles_total": self.preambles_total,
                "crc_ok": self.crc_ok,
                "crc_bad": self.crc_bad,
                "crc_ratio": crc_ratio,
                "buffers_total": self.buffers_total,
                "active_tracks": active_tracks,
                "noise_floor": self.noise_floor,
                "clip_ratio": self.clip_ratio,
                "last_error": self.last_error,
            }


class SharedVisuals:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rows = {
            PROTOCOL_ADSB: [SPECTRUM_FLOOR_DB] * DEFAULT_SPECTRUM_BINS,
            PROTOCOL_FLARM: [SPECTRUM_FLOOR_DB] * DEFAULT_SPECTRUM_BINS,
        }
        self.iq: list[list[float]] = []

    def reset(self) -> None:
        with self.lock:
            self.rows = {
                PROTOCOL_ADSB: [SPECTRUM_FLOOR_DB] * DEFAULT_SPECTRUM_BINS,
                PROTOCOL_FLARM: [SPECTRUM_FLOOR_DB] * DEFAULT_SPECTRUM_BINS,
            }
            self.iq = []

    def set_row(self, row: list[float], protocol: str = PROTOCOL_ADSB) -> None:
        with self.lock:
            self.rows[protocol] = row

    def set_iq(self, points: list[list[float]]) -> None:
        with self.lock:
            self.iq = points

    def snapshot(self) -> dict[str, list[float]]:
        with self.lock:
            return {
                PROTOCOL_ADSB: list(self.rows[PROTOCOL_ADSB]),
                PROTOCOL_FLARM: list(self.rows[PROTOCOL_FLARM]),
            }

    def iq_snapshot(self) -> list[list[float]]:
        with self.lock:
            return list(self.iq)


class SseBroker:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.clients: set[queue.Queue[dict[str, Any]]] = set()

    def register(self) -> queue.Queue[dict[str, Any]]:
        client_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=8)
        with self.lock:
            self.clients.add(client_queue)
        return client_queue

    def unregister(self, client_queue: queue.Queue[dict[str, Any]]) -> None:
        with self.lock:
            self.clients.discard(client_queue)

    def publish(self, payload: dict[str, Any]) -> None:
        with self.lock:
            clients = list(self.clients)
        for client_queue in clients:
            try:
                if client_queue.full():
                    client_queue.get_nowait()
                client_queue.put_nowait(payload)
            except queue.Full:
                pass


class SnapshotPublisher(threading.Thread):
    def __init__(
        self,
        broker: SseBroker,
        tracker: AirTrafficTracker,
        stats: RuntimeStats,
        visuals: SharedVisuals,
        stop_event: threading.Event,
        interval_s: float,
    ) -> None:
        super().__init__(daemon=True)
        self.broker = broker
        self.tracker = tracker
        self.stats = stats
        self.visuals = visuals
        self.stop_event = stop_event
        self.interval_s = max(1.0 / 30.0, min(1.0, float(interval_s)))

    def run(self) -> None:
        while not self.stop_event.is_set():
            tracks = self.tracker.snapshot()
            visuals = self.visuals.snapshot()
            payload = {
                "type": "snapshot",
                "time": utc_now(),
                "stats": self.stats.snapshot(len(tracks)),
                "tracks": tracks,
                "spectrum": visuals[PROTOCOL_ADSB],
                "waterfall": visuals[PROTOCOL_ADSB],
                "flarm_spectrum": visuals[PROTOCOL_FLARM],
                "flarm_waterfall": visuals[PROTOCOL_FLARM],
                "iq": self.visuals.iq_snapshot(),
            }
            self.broker.publish(payload)
            self.stop_event.wait(self.interval_s)


class PlutoSource:
    def __init__(self, args: argparse.Namespace) -> None:
        prepare_libiio_runtime()
        try:
            import adi  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Modulo pyadi-iio non installato. Installa con: python -m pip install pyadi-iio") from exc
        except (OSError, TypeError) as exc:
            if sys.platform.startswith("win") and (
                "NoneType" in str(exc) or "libiio" in str(exc).lower() or "iio" in str(exc).lower()
            ):
                raise RuntimeError(libiio_missing_message()) from exc
            raise

        self.args = args
        self.uri = str(args.uri)
        self.sdr = self._open_pluto(adi, self.uri)
        self.dual_rx = False
        self.rx_data_channels: list[str] = []
        self.rx_complex_streams = 0
        self.rx1_status = "RX1 non richiesto"
        self._refresh_rx_inventory()
        if args.dual_rx and self.rx_complex_streams >= 2 and len(self._rx_channel_names()) < 4:
            try:
                self.sdr.rx_destroy_buffer()
            except Exception:
                pass
            self.sdr = adi.ad9361(uri=self.uri)
            self._refresh_rx_inventory()
        if args.dual_rx:
            if self.rx_complex_streams >= 2:
                try:
                    self.sdr.rx_enabled_channels = [0, 1]
                    self.dual_rx = True
                    self.rx1_status = "RX1 attivo"
                except Exception as exc:
                    self.sdr.rx_enabled_channels = [0]
                    self.rx1_status = f"RX1 non attivo: {exc}"
            else:
                self.sdr.rx_enabled_channels = [0]
                self.rx1_status = f"RX1 non disponibile: {self.rx_complex_streams} stream complesso I/Q"
        else:
            self.sdr.rx_enabled_channels = [0]
            self.rx1_status = "RX1 disabilitato"
        self.sdr.sample_rate = int(args.sample_rate)
        self.current_frequency = int(args.center_frequency)
        self.sdr.rx_lo = self.current_frequency
        self.sdr.rx_rf_bandwidth = int(args.rf_bandwidth)
        self.sdr.rx_buffer_size = int(args.buffer_size)

        iio_gain_mode = "manual" if args.gain_mode in ("manual", "manual-fixed") else args.gain_mode
        self.sdr.gain_control_mode_chan0 = iio_gain_mode
        if self.dual_rx and hasattr(self.sdr, "gain_control_mode_chan1"):
            self.sdr.gain_control_mode_chan1 = iio_gain_mode
        if iio_gain_mode == "manual":
            gain = min(float(args.max_gain), max(float(args.min_gain), float(args.gain)))
            self.set_gain(gain)

    def _open_pluto(self, adi_module: Any, requested_uri: str) -> Any:
        errors: list[str] = []
        for uri in self._candidate_uris(requested_uri):
            try:
                sdr = adi_module.Pluto(uri=uri)
                self.uri = uri
                return sdr
            except Exception as exc:
                errors.append(f"{uri}: {exc}")
        joined = "; ".join(errors) if errors else "nessun URI candidato"
        detail = self._connection_hint(requested_uri, errors)
        raise RuntimeError(f"PlutoSDR non trovato ({joined}){detail}")

    def _connection_hint(self, requested_uri: str, errors: list[str]) -> str:
        lines: list[str] = []
        if sys.platform.startswith("win"):
            try:
                import iio  # type: ignore

                contexts = iio.scan_contexts()
            except Exception as exc:
                contexts = f"scan non disponibile: {exc}"
            lines.append(f"scan_contexts={contexts}")

            host = _host_from_iio_uri(requested_uri)
            if host:
                lines.append(_probe_iiod_tcp(host))

            if any("usb:" in error for error in errors):
                lines.append("fallback usb: provato ma nessun dispositivo IIO USB utilizzabile e stato trovato")

            lines.append(
                "Su Windows verifica che Pluto compaia come scheda USB Ethernet/RNDIS con IP 192.168.2.x "
                "oppure come dispositivo USB libiio/WinUSB. Se in Gestione dispositivi non compare ADALM-Pluto "
                "o USB Ethernet/RNDIS Gadget, controlla porta/cavo USB dati e driver."
            )

        return "\n" + "\n".join(lines) if lines else ""

    def _candidate_uris(self, requested_uri: str) -> list[str]:
        candidates: list[str] = [requested_uri]
        try:
            import iio  # type: ignore

            raw_contexts = iio.scan_contexts()
            contexts = raw_contexts if isinstance(raw_contexts, dict) else {}
            pluto_usb = [
                uri
                for uri, description in contexts.items()
                if str(uri).startswith("usb:")
                and (
                    "PlutoSDR" in str(description or "")
                    or "ADALM-PLUTO" in str(description or "")
                    or "0456:b673" in str(description or "")
                )
            ]
            if requested_uri not in contexts and pluto_usb:
                candidates = pluto_usb + candidates
            else:
                candidates.extend(pluto_usb)
        except Exception:
            pass

        if sys.platform.startswith("win") and not str(requested_uri).startswith("usb:"):
            candidates.append("usb:")

        deduped: list[str] = []
        for uri in candidates:
            if uri and uri not in deduped:
                deduped.append(uri)
        return deduped

    def _rx_channel_names(self) -> list[str]:
        names = getattr(self.sdr, "rx_channel_names", None)
        if names is None:
            return []
        if isinstance(names, str):
            return [names]
        try:
            return [str(name) for name in names]
        except TypeError:
            return []

    def _refresh_rx_inventory(self) -> None:
        channels: list[str] = []
        rxadc = getattr(self.sdr, "_rxadc", None)
        if rxadc is not None:
            try:
                channels = [
                    str(channel.id)
                    for channel in (getattr(rxadc, "channels", None) or [])
                    if getattr(channel, "scan_element", False)
                ]
            except TypeError:
                channels = []
        if not channels:
            channels = self._rx_channel_names()
        self.rx_data_channels = channels
        is_complex = bool(getattr(self.sdr, "_complex_data", False))
        self.rx_complex_streams = len(channels) // 2 if is_complex else len(channels)

    def set_frequency(self, frequency_hz: int, settle_ms: float = 0.0) -> None:
        frequency = int(frequency_hz)
        if frequency == self.current_frequency:
            return
        try:
            self.sdr.rx_destroy_buffer()
        except Exception:
            pass
        self.sdr.rx_lo = frequency
        self.current_frequency = frequency
        if settle_ms > 0:
            time.sleep(settle_ms / 1000.0)

    def read_channels(self) -> tuple[np.ndarray, Optional[np.ndarray]]:
        samples = self.sdr.rx()
        if isinstance(samples, (list, tuple)):
            rx0 = np.asarray(samples[0], dtype=np.complex64)
            rx1 = np.asarray(samples[1], dtype=np.complex64) if len(samples) > 1 else None
            return rx0, rx1
        arr = np.asarray(samples, dtype=np.complex64)
        if arr.ndim == 2:
            if arr.shape[0] >= 2 and arr.shape[0] <= 4:
                return arr[0], arr[1]
            if arr.shape[1] >= 2 and arr.shape[1] <= 4:
                return arr[:, 0], arr[:, 1]
        return arr, None

    def force_manual_gain(self) -> None:
        try:
            self.sdr.gain_control_mode_chan0 = "manual"
        except Exception:
            pass
        if self.dual_rx and hasattr(self.sdr, "gain_control_mode_chan1"):
            try:
                self.sdr.gain_control_mode_chan1 = "manual"
            except Exception:
                pass

    def set_gain(self, gain_db: float) -> None:
        gain = float(gain_db)
        self.force_manual_gain()
        self.sdr.rx_hardwaregain_chan0 = gain
        if self.dual_rx and hasattr(self.sdr, "rx_hardwaregain_chan1"):
            try:
                self.sdr.rx_hardwaregain_chan1 = gain
            except Exception:
                pass

    def destroy(self) -> None:
        try:
            self.sdr.rx_destroy_buffer()
        except Exception:
            pass


class AdaptiveGain:
    def __init__(self, source: PlutoSource, stats: RuntimeStats, args: argparse.Namespace) -> None:
        self.source = source
        self.stats = stats
        self.min_gain = float(args.min_gain)
        self.max_gain = float(args.max_gain)
        self.gain = min(self.max_gain, max(self.min_gain, float(args.gain)))
        self.auto_ceiling = min(self.max_gain, max(self.gain, DEFAULT_AUTO_GAIN_CEILING_DB))
        self.last_change = 0.0

    def set_gain(self, gain_db: float) -> None:
        self.gain = min(self.max_gain, max(self.min_gain, float(gain_db)))
        self.last_change = utc_now()

    def update(self, batch: DecodeBatch) -> None:
        now = utc_now()
        if now - self.last_change < 2.0:
            return

        next_gain = self.gain
        crc_total = batch.crc_ok + batch.crc_bad
        crc_ratio = batch.crc_ok / crc_total if crc_total else 0.0

        if batch.clip_ratio > 0.0005:
            next_gain -= 6.0
        elif batch.preambles >= 5 and crc_total >= 5 and crc_ratio < 0.10:
            next_gain -= 2.0
        elif batch.preambles == 0 and batch.clip_ratio < 0.00005 and self.gain < self.auto_ceiling:
            next_gain += 0.5

        next_gain = min(self.max_gain, self.auto_ceiling, max(self.min_gain, next_gain))
        if abs(next_gain - self.gain) >= 0.5:
            self.gain = next_gain
            self.source.set_gain(self.gain)
            self.stats.set_gain(self.gain)
            self.last_change = now


class PlutoReceiver(threading.Thread):
    def __init__(
        self,
        args: argparse.Namespace,
        tracker: AirTrafficTracker,
        stats: RuntimeStats,
        visuals: SharedVisuals,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True)
        self.args = args
        self.tracker = tracker
        self.stats = stats
        self.visuals = visuals
        self.stop_event = stop_event
        self.source = PlutoSource(args)
        self.decoder = ADSBDecoder(args.sample_rate)
        self.flarm_decoder = FLARMDecoder(args.sample_rate, args.rx_lat, args.rx_lon)
        self.rx0_scan_enabled = bool(args.rx0_scan and not self.source.dual_rx)
        self.freq_map = {
            PROTOCOL_ADSB: int(args.center_frequency),
            PROTOCOL_FLARM: int(args.flarm_frequency),
        }
        self.scan_index = 0
        self.next_scan_switch = 0.0
        self.gain_control = (
            AdaptiveGain(self.source, stats, args) if args.gain_mode == "manual" else None
        )
        if self.gain_control is not None:
            self.stats.set_gain(self.gain_control.gain)

    def set_gain(self, gain_db: float) -> None:
        gain = min(float(self.args.max_gain), max(float(self.args.min_gain), float(gain_db)))
        self.source.set_gain(gain)
        self.gain_control = None
        self.args.gain = gain
        self.args.gain_mode = "manual-fixed"
        self.stats.set_gain(gain)
        self.stats.set_gain_mode("manual-fixed")

    def _active_plan(self) -> tuple[tuple[str, int], ...]:
        enabled = self.stats.get_enabled_protocols()
        return tuple((proto, self.freq_map[proto]) for proto in enabled)

    def _current_scan_band(self) -> tuple[str, int]:
        plan = self._active_plan()
        # Un solo protocollo selezionato (o RX0 scan disattivo): resta sintonizzato.
        if len(plan) <= 1 or not self.rx0_scan_enabled:
            self.next_scan_switch = 0.0
            return plan[0]

        self.scan_index %= len(plan)
        now = utc_now()
        if self.next_scan_switch <= 0.0:
            protocol, _frequency = plan[self.scan_index]
            self.next_scan_switch = now + self._dwell_seconds(protocol)
        elif now >= self.next_scan_switch:
            self.scan_index = (self.scan_index + 1) % len(plan)
            protocol, _frequency = plan[self.scan_index]
            self.next_scan_switch = now + self._dwell_seconds(protocol)
        return plan[self.scan_index]

    def _dwell_seconds(self, protocol: str) -> float:
        dwell_ms = self.args.flarm_dwell_ms if protocol == PROTOCOL_FLARM else self.args.adsb_dwell_ms
        return max(0.05, float(dwell_ms) / 1000.0)

    def _process_adsb(self, samples: np.ndarray) -> None:
        self.visuals.set_row(compute_spectrum(samples), PROTOCOL_ADSB)
        self.visuals.set_iq(compute_iq(samples))
        batch = self.decoder.decode_samples(samples)
        for message in batch.messages:
            self.tracker.ingest(message)
        self.stats.record_decode(batch)
        if self.gain_control is not None:
            self.gain_control.update(batch)

    def _process_flarm(self, samples: np.ndarray) -> None:
        self.visuals.set_row(compute_spectrum(samples), PROTOCOL_FLARM)
        self.visuals.set_iq(compute_iq(samples))
        flarm_batch = self.flarm_decoder.decode_samples(samples)
        now = utc_now()
        for target in flarm_batch.targets:
            if target.no_track:
                continue
            self.tracker.upsert_position(
                target.addr_hex,
                target.latitude,
                target.longitude,
                target.altitude_m * 3.2808399,
                f"FL{target.addr_hex}",
                now,
                protocol=PROTOCOL_FLARM,
                speed_kt=target.speed_kt,
                vertical_ft_min=target.vs_ft_min,
            )
        self.stats.record_flarm_decode(
            flarm_batch.bursts,
            flarm_batch.decoded_messages,
            flarm_batch.noise_floor,
            flarm_batch.clip_ratio,
        )

    def run(self) -> None:
        self.stats.set_source("pluto")
        self.stats.set_state("running")
        self.stats.set_gain_mode(self.args.gain_mode)
        self.stats.set_rx1_active(False)
        self.stats.set_rx0_scan(self.rx0_scan_enabled)
        rx1_status = self.source.rx1_status
        if self.rx0_scan_enabled:
            rx1_status = (
                f"RX0 scan ADS-B {int(self.args.center_frequency) / 1_000_000:.3f} MHz / "
                f"FLARM {int(self.args.flarm_frequency) / 1_000_000:.3f} MHz"
            )
        self.stats.set_rx1_info(
            self.source.dual_rx,
            rx1_status,
            self.source.rx_complex_streams,
            self.source.rx_data_channels,
        )
        try:
            while not self.stop_event.is_set():
                protocol, frequency = self._current_scan_band()
                self.source.set_frequency(frequency, float(self.args.tune_settle_ms))
                self.stats.set_active_tuning(protocol, frequency)
                samples, flarm_samples = self.source.read_channels()

                if self.rx0_scan_enabled:
                    self.stats.set_rx1_active(False)
                    if protocol == PROTOCOL_FLARM:
                        self._process_flarm(samples)
                    else:
                        self._process_adsb(samples)
                    continue

                enabled = self.stats.get_enabled_protocols()
                # RX0 elabora il protocollo a cui e sintonizzato (di norma ADS-B).
                if protocol == PROTOCOL_FLARM:
                    self._process_flarm(samples)
                else:
                    self._process_adsb(samples)
                if PROTOCOL_ADSB not in enabled:
                    self.visuals.set_row([SPECTRUM_FLOOR_DB] * DEFAULT_SPECTRUM_BINS, PROTOCOL_ADSB)

                rx1_active = (
                    self.source.dual_rx
                    and PROTOCOL_FLARM in enabled
                    and flarm_samples is not None
                    and flarm_samples.size > 0
                )
                self.stats.set_rx1_active(rx1_active)
                if rx1_active and flarm_samples is not None:
                    self._process_flarm(flarm_samples)
                elif not self.source.dual_rx and protocol != PROTOCOL_FLARM:
                    self.visuals.set_row([SPECTRUM_FLOOR_DB] * DEFAULT_SPECTRUM_BINS, PROTOCOL_FLARM)
        except Exception as exc:
            self.stats.set_state("error", str(exc))
        finally:
            self.stats.set_rx1_active(False)
            self.stats.set_rx0_scan(False)
            self.source.destroy()


@dataclass
class SimAircraft:
    icao: str
    callsign: str
    lat: float
    lon: float
    altitude_ft: int
    heading_deg: float
    speed_kt: float
    vertical_ft_min: float


class SimulatedReceiver(threading.Thread):
    def __init__(
        self,
        tracker: AirTrafficTracker,
        stats: RuntimeStats,
        visuals: SharedVisuals,
        stop_event: threading.Event,
        center_lat: float = 45.4642,
        center_lon: float = 9.19,
    ) -> None:
        super().__init__(daemon=True)
        self.tracker = tracker
        self.stats = stats
        self.visuals = visuals
        self.stop_event = stop_event
        self.center_lat = center_lat
        self.center_lon = center_lon
        self.aircraft = self._make_aircraft()
        self.flarm_aircraft = self._make_flarm_aircraft()

    def _make_aircraft(self) -> list[SimAircraft]:
        callsigns = ("DLH4RT", "RYR72QB", "ITY221", "EZY49KM", "BAW58", "SWR19P", "AFR102")
        rows: list[SimAircraft] = []
        for index, callsign in enumerate(callsigns):
            radius = 0.25 + index * 0.08
            angle = math.radians(index * 47.0)
            rows.append(
                SimAircraft(
                    icao=f"4D{1200 + index:04X}"[-6:],
                    callsign=callsign,
                    lat=self.center_lat + math.sin(angle) * radius,
                    lon=self.center_lon + math.cos(angle) * radius,
                    altitude_ft=18000 + index * 2800,
                    heading_deg=(index * 52.0 + 30.0) % 360.0,
                    speed_kt=250.0 + index * 18.0,
                    vertical_ft_min=(-1) ** index * 80.0,
                )
            )
        return rows

    def _make_flarm_aircraft(self) -> list[SimAircraft]:
        callsigns = ("FLRMA1", "GLID42", "D-KXYZ", "I-ABCD")
        rows: list[SimAircraft] = []
        for index, callsign in enumerate(callsigns):
            radius = 0.08 + index * 0.035
            angle = math.radians(index * 82.0 + 25.0)
            rows.append(
                SimAircraft(
                    icao=f"FL{2100 + index:04X}"[-6:],
                    callsign=callsign,
                    lat=self.center_lat + math.sin(angle) * radius,
                    lon=self.center_lon + math.cos(angle) * radius,
                    altitude_ft=1600 + index * 850,
                    heading_deg=(index * 74.0 + 15.0) % 360.0,
                    speed_kt=55.0 + index * 8.0,
                    vertical_ft_min=(-1) ** index * 120.0,
                )
            )
        return rows

    def run(self) -> None:
        self.stats.set_source("sim")
        self.stats.set_state("running")
        self.stats.set_rx1_active(True)
        last = utc_now()
        while not self.stop_event.is_set():
            now = utc_now()
            dt = max(0.1, now - last)
            last = now
            message_count = 0
            enabled = self.stats.get_enabled_protocols()
            adsb_on = PROTOCOL_ADSB in enabled
            flarm_on = PROTOCOL_FLARM in enabled

            for aircraft in (self.aircraft if adsb_on else []):
                self._advance_aircraft(aircraft, dt)
                if random.random() < 0.98:
                    self.tracker.upsert_position(
                        aircraft.icao,
                        aircraft.lat,
                        aircraft.lon,
                        aircraft.altitude_ft,
                        aircraft.callsign,
                        now,
                        rssi=random.uniform(8.0, 35.0),
                        speed_kt=aircraft.speed_kt,
                        vertical_ft_min=aircraft.vertical_ft_min,
                    )
                    message_count += random.randint(2, 5)

            flarm_count = 0
            for aircraft in (self.flarm_aircraft if flarm_on else []):
                self._advance_aircraft(aircraft, dt)
                if random.random() < 0.95:
                    self.tracker.upsert_position(
                        aircraft.icao,
                        aircraft.lat,
                        aircraft.lon,
                        aircraft.altitude_ft,
                        aircraft.callsign,
                        now,
                        rssi=random.uniform(6.0, 28.0),
                        protocol=PROTOCOL_FLARM,
                        speed_kt=aircraft.speed_kt,
                        vertical_ft_min=aircraft.vertical_ft_min,
                    )
                    flarm_count += random.randint(1, 3)

            if adsb_on:
                self.visuals.set_row(simulated_spectrum(message_count), PROTOCOL_ADSB)
            else:
                self.visuals.set_row([SPECTRUM_FLOOR_DB] * DEFAULT_SPECTRUM_BINS, PROTOCOL_ADSB)
            if flarm_on:
                self.visuals.set_row(simulated_spectrum(flarm_count), PROTOCOL_FLARM)
            else:
                self.visuals.set_row([SPECTRUM_FLOOR_DB] * DEFAULT_SPECTRUM_BINS, PROTOCOL_FLARM)
            self.visuals.set_iq(simulated_iq(message_count if adsb_on else flarm_count))
            self.stats.record_sim_messages(message_count, noise_floor=random.uniform(0.8, 1.2), clip_ratio=0.0)
            self.stats.record_flarm_decode(flarm_count, flarm_count, noise_floor=random.uniform(0.8, 1.2), clip_ratio=0.0)
            self.stop_event.wait(0.5)

    def _advance_aircraft(self, aircraft: SimAircraft, dt: float) -> None:
        distance_nm = aircraft.speed_kt * dt / 3600.0
        distance_km = distance_nm * 1.852
        heading = math.radians(aircraft.heading_deg)
        dlat = (distance_km * math.cos(heading)) / 111.0
        dlon = (distance_km * math.sin(heading)) / (111.0 * max(0.2, math.cos(math.radians(aircraft.lat))))
        aircraft.lat += dlat
        aircraft.lon += dlon
        aircraft.altitude_ft = int(aircraft.altitude_ft + aircraft.vertical_ft_min * dt / 60.0)

        if haversine_km(self.center_lat, self.center_lon, aircraft.lat, aircraft.lon) > 120.0:
            bearing_back = math.degrees(math.atan2(self.center_lon - aircraft.lon, self.center_lat - aircraft.lat))
            aircraft.heading_deg = (bearing_back + random.uniform(-20.0, 20.0)) % 360.0


class ReceiverController:
    def __init__(
        self,
        args: argparse.Namespace,
        tracker: AirTrafficTracker,
        stats: RuntimeStats,
        visuals: SharedVisuals,
    ) -> None:
        self.args = args
        self.tracker = tracker
        self.stats = stats
        self.visuals = visuals
        self.lock = threading.Lock()
        self.worker: Optional[threading.Thread] = None
        self.worker_stop: Optional[threading.Event] = None

    def start_initial(self) -> None:
        if self.args.source == "sim":
            result = self.connect_sim()
            if not result["ok"]:
                raise RuntimeError(str(result["error"]))
            return

        result = self.connect_pluto()
        if result["ok"]:
            return
        if self.args.source == "auto":
            self.stats.set_state("fallback", str(result["error"]))
            self.connect_sim()
            return
        raise RuntimeError(str(result["error"]))

    def connect_pluto(self) -> dict[str, Any]:
        with self.lock:
            self.stats.set_state("connecting", "")
            stop_event = threading.Event()
            reconnecting_pluto = isinstance(self.worker, PlutoReceiver)
            if reconnecting_pluto:
                self._stop_current_locked()
            try:
                next_worker = PlutoReceiver(self.args, self.tracker, self.stats, self.visuals, stop_event)
            except Exception as exc:
                fallback_state = "running" if self.worker is not None and self.worker.is_alive() else "error"
                self.stats.set_state(fallback_state, str(exc))
                return {"ok": False, "source": self.stats.source, "error": str(exc)}

            self._stop_current_locked()
            self.args.uri = next_worker.source.uri
            self.tracker.clear()
            self.visuals.reset()
            self.stats.reset_for_source("pluto", "starting")
            self.stats.set_pluto_uri(next_worker.source.uri)
            self.worker = next_worker
            self.worker_stop = stop_event
            self.worker.start()
            message = "PlutoSDR riconnessa e riconfigurata" if reconnecting_pluto else "PlutoSDR connessa"
            return {"ok": True, "source": "pluto", "message": message, "uri": next_worker.source.uri}

    def connect_sim(self) -> dict[str, Any]:
        with self.lock:
            if self.worker is not None and self.worker.is_alive() and self.stats.source == "sim":
                self.stats.set_state("running", "")
                return {"ok": True, "source": "sim", "message": "Simulazione gia attiva"}

            stop_event = threading.Event()
            next_worker = SimulatedReceiver(self.tracker, self.stats, self.visuals, stop_event)
            self._stop_current_locked()
            self.tracker.clear()
            self.visuals.reset()
            self.stats.reset_for_source("sim", "starting")
            self.worker = next_worker
            self.worker_stop = stop_event
            self.worker.start()
            return {"ok": True, "source": "sim", "message": "Simulazione attiva"}

    def stop(self) -> None:
        with self.lock:
            self._stop_current_locked()
            self.stats.set_state("stopped")

    def set_protocols(self, protocols: list[str]) -> dict[str, Any]:
        applied = self.stats.set_enabled_protocols(protocols)
        self.args.protocols = ",".join(applied)
        return {"ok": True, "enabled_protocols": applied, "message": "Protocolli aggiornati"}

    def set_gain(self, gain_db: float) -> dict[str, Any]:
        with self.lock:
            gain = min(float(self.args.max_gain), max(float(self.args.min_gain), float(gain_db)))
            self.args.gain = gain
            if isinstance(self.worker, PlutoReceiver) and self.worker.is_alive():
                self.worker.set_gain(gain)
                return {"ok": True, "gain_db": gain, "message": "Gain Pluto aggiornato"}
            self.args.gain_mode = "manual-fixed"
            self.stats.set_gain(gain)
            self.stats.set_gain_mode("manual-fixed")
            return {"ok": True, "gain_db": gain, "message": "Gain salvato per la prossima connessione Pluto"}

    def _stop_current_locked(self) -> None:
        if self.worker_stop is not None:
            self.worker_stop.set()
        if isinstance(self.worker, PlutoReceiver):
            try:
                self.worker.source.destroy()
            except Exception:
                pass
        if self.worker is not None:
            self.worker.join(timeout=2.0)
        self.worker = None
        self.worker_stop = None


def estimate_clip_ratio(samples: np.ndarray) -> float:
    arr = np.asarray(samples)
    if arr.size == 0:
        return 0.0
    real = np.real(arr)
    imag = np.imag(arr)
    peak = max(float(np.max(np.abs(real))), float(np.max(np.abs(imag))), 1.0)
    if peak > 4096.0:
        threshold = 0.95 * 32768.0
    elif peak > 16.0:
        threshold = 0.95 * 2048.0
    else:
        threshold = 0.95
    clipped = (np.abs(real) >= threshold) | (np.abs(imag) >= threshold)
    return float(np.mean(clipped))


def compute_spectrum(samples: np.ndarray, bins: int = DEFAULT_SPECTRUM_BINS) -> list[float]:
    arr = np.asarray(samples, dtype=np.complex64)
    if arr.size == 0:
        return [SPECTRUM_FLOOR_DB] * bins
    size = min(DEFAULT_SPECTRUM_FFT_SIZE, arr.size)
    segment = arr[-size:]
    if size < 32:
        return [SPECTRUM_FLOOR_DB] * bins

    window = np.hanning(size).astype(np.float32)
    spectrum = np.fft.fftshift(np.fft.fft(segment * window))
    window_gain = max(float(np.sum(window)), 1.0)
    # dBFS assoluti rispetto al fondo scala fisso dell'ADC: 0 dB = tono a fondo scala.
    # Nessun fallback dipendente dal livello: a basso gain lo spettro scende verso il floor.
    db = 20.0 * np.log10((np.abs(spectrum) / window_gain / ADC_FULL_SCALE) + 1e-12)

    chunk = max(1, len(db) // bins)
    trimmed = db[: chunk * bins]
    if trimmed.size < bins:
        row = np.interp(np.linspace(0, len(db) - 1, bins), np.arange(len(db)), db)
    else:
        row = trimmed.reshape(bins, chunk).mean(axis=1)

    display = np.clip(row, SPECTRUM_FLOOR_DB, SPECTRUM_CEILING_DB)
    return display.astype(float).tolist()


def compute_iq(samples: np.ndarray, points: int = IQ_SCOPE_POINTS) -> list[list[float]]:
    """Decima i campioni complessi in una nuvola I/Q normalizzata in [-1, 1]."""
    arr = np.asarray(samples, dtype=np.complex64)
    if arr.size == 0:
        return []
    if arr.size > points:
        idx = np.linspace(0, arr.size - 1, points).astype(np.int64)
        arr = arr[idx]
    real = np.real(arr).astype(np.float32)
    imag = np.imag(arr).astype(np.float32)
    scale = max(float(np.max(np.abs(real))), float(np.max(np.abs(imag))), 1e-6)
    real = real / scale
    imag = imag / scale
    return [[round(float(i), 4), round(float(q), 4)] for i, q in zip(real, imag)]


def simulated_spectrum(message_count: int, bins: int = DEFAULT_SPECTRUM_BINS) -> list[float]:
    x = np.linspace(-1.0, 1.0, bins)
    # Livelli assoluti: rumore di fondo intorno a -85 dBFS, picchi verso -25 dBFS.
    noise = -85.0 + np.random.normal(0.0, 2.5, bins)
    center_hump = 13.0 * np.exp(-(x / 0.24) ** 2)
    burst = max(0.0, min(22.0, message_count * 1.4)) * np.exp(-((x - random.uniform(-0.08, 0.08)) / 0.08) ** 2)
    return np.clip(noise + center_hump + burst, SPECTRUM_FLOOR_DB, SPECTRUM_CEILING_DB).astype(float).tolist()


def simulated_iq(message_count: int, points: int = IQ_SCOPE_POINTS) -> list[list[float]]:
    base = 0.18 + min(0.6, message_count * 0.03)
    phase = np.linspace(0.0, 2.0 * np.pi * 6.0, points)
    real = base * np.cos(phase) + np.random.normal(0.0, 0.12, points)
    imag = base * np.sin(phase) + np.random.normal(0.0, 0.12, points)
    scale = max(float(np.max(np.abs(real))), float(np.max(np.abs(imag))), 1e-6)
    return [[round(float(i / scale), 4), round(float(q / scale), 4)] for i, q in zip(real, imag)]


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any] | list[Any]) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    handler.wfile.write(body)


def make_handler(
    static_dir: str,
    broker: SseBroker,
    tracker: AirTrafficTracker,
    stats: RuntimeStats,
    visuals: SharedVisuals,
    stop_event: threading.Event,
    controller: ReceiverController,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/events":
                self.handle_events()
                return
            if path == "/api/status":
                tracks = tracker.snapshot()
                visual_rows = visuals.snapshot()
                json_response(
                    self,
                    200,
                    {
                        "stats": stats.snapshot(len(tracks)),
                        "spectrum": visual_rows[PROTOCOL_ADSB],
                        "waterfall": visual_rows[PROTOCOL_ADSB],
                        "flarm_spectrum": visual_rows[PROTOCOL_FLARM],
                        "flarm_waterfall": visual_rows[PROTOCOL_FLARM],
                        "iq": visuals.iq_snapshot(),
                    },
                )
                return
            if path == "/api/tracks":
                json_response(self, 200, tracker.snapshot())
                return
            self.handle_static(path)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path == "/api/connect/pluto":
                result = controller.connect_pluto()
                status = 200 if result["ok"] else 503
                tracks = tracker.snapshot()
                result["stats"] = stats.snapshot(len(tracks))
                json_response(self, status, result)
                return
            if path == "/api/connect/sim":
                result = controller.connect_sim()
                tracks = tracker.snapshot()
                result["stats"] = stats.snapshot(len(tracks))
                json_response(self, 200, result)
                return
            if path == "/api/gain":
                try:
                    body_len = int(self.headers.get("Content-Length", "0") or "0")
                    body = self.rfile.read(body_len) if body_len else b"{}"
                    payload = json.loads(body.decode("utf-8") or "{}")
                    gain = float(payload["gain"])
                except Exception:
                    json_response(self, 400, {"ok": False, "error": "Payload gain non valido"})
                    return
                result = controller.set_gain(gain)
                tracks = tracker.snapshot()
                result["stats"] = stats.snapshot(len(tracks))
                json_response(self, 200, result)
                return
            if path == "/api/protocol":
                try:
                    body_len = int(self.headers.get("Content-Length", "0") or "0")
                    body = self.rfile.read(body_len) if body_len else b"{}"
                    payload = json.loads(body.decode("utf-8") or "{}")
                    protocols = payload.get("protocols")
                    if protocols is None:
                        raise ValueError("protocols mancante")
                except Exception:
                    json_response(self, 400, {"ok": False, "error": "Payload protocolli non valido"})
                    return
                result = controller.set_protocols(protocols)
                tracks = tracker.snapshot()
                result["stats"] = stats.snapshot(len(tracks))
                json_response(self, 200, result)
                return
            self.send_error(404)

        def handle_events(self) -> None:
            client = broker.register()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            try:
                while not stop_event.is_set():
                    try:
                        payload = client.get(timeout=15.0)
                        line = "data: " + json.dumps(payload, separators=(",", ":")) + "\n\n"
                    except queue.Empty:
                        line = ": ping\n\n"
                    self.wfile.write(line.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            finally:
                broker.unregister(client)

        def handle_static(self, path: str) -> None:
            if path == "/":
                path = "/index.html"
            path = unquote(path)
            safe_rel = os.path.normpath(path.lstrip("/"))
            if safe_rel.startswith(".."):
                self.send_error(404)
                return
            file_path = os.path.abspath(os.path.join(static_dir, safe_rel))
            if not file_path.startswith(os.path.abspath(static_dir)) or not os.path.isfile(file_path):
                self.send_error(404)
                return

            content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
            with open(file_path, "rb") as file_obj:
                body = file_obj.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def run_server(args: argparse.Namespace) -> int:
    stop_event = threading.Event()
    tracker = AirTrafficTracker()
    broker = SseBroker()
    visuals = SharedVisuals()
    stats = RuntimeStats(args.source, args)
    controller = ReceiverController(args, tracker, stats, visuals)

    ui_rate_hz = max(1.0, min(30.0, float(args.ui_rate_hz)))
    publisher = SnapshotPublisher(broker, tracker, stats, visuals, stop_event, 1.0 / ui_rate_hz)
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    handler = make_handler(static_dir, broker, tracker, stats, visuals, stop_event, controller)
    server = ThreadingHTTPServer((args.host, args.port), handler)

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    try:
        controller.start_initial()
    except Exception as exc:
        print(f"Errore sorgente iniziale: {exc}", file=sys.stderr)
        return 2

    publisher.start()
    actual_host, actual_port = server.server_address[:2]
    print(f"Pluto ADS-B Tracker: http://{actual_host}:{actual_port}")
    print(
        f"Sorgente attiva: {stats.snapshot(0)['source']}  "
        f"sample_rate={args.sample_rate} rf_bw={args.rf_bandwidth} ui_rate={ui_rate_hz:.1f}Hz"
    )

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        stop_event.set()
        controller.stop()
        server.server_close()
        publisher.join(timeout=2.0)
    return 0


def run_self_test() -> int:
    callsign_msg = bytes.fromhex("8D4840D6202CC371C32CE0576098")
    even_msg = bytes.fromhex("8D40621D58C382D690C8AC2863A7")
    odd_msg = bytes.fromhex("8D40621D58C386435CC412692AD6")

    assert mode_s_crc(callsign_msg, 112) == 0
    assert mode_s_parity(callsign_msg, 112) == int.from_bytes(callsign_msg[-3:], "big")
    callsign = parse_adsb_frame(callsign_msg)
    assert callsign is not None and callsign.callsign == "KLM1023"

    even = parse_adsb_frame(even_msg, timestamp=1005.0)
    odd = parse_adsb_frame(odd_msg, timestamp=1000.0)
    assert even is not None and odd is not None
    assert even.altitude_ft == 38000
    assert even.cpr_format == 0 and odd.cpr_format == 1
    pos = decode_global_cpr(
        even.cpr_lat or 0,
        even.cpr_lon or 0,
        even.timestamp,
        odd.cpr_lat or 0,
        odd.cpr_lon or 0,
        odd.timestamp,
    )
    assert pos is not None
    lat, lon = pos
    assert abs(lat - 52.2572) < 0.01
    assert abs(lon - 3.9193) < 0.01
    print("Self-test ADS-B OK")
    flarm_legacy.self_test()
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PlutoSDR ADS-B tracker con UI Leaflet.")
    parser.add_argument("--source", choices=("auto", "pluto", "sim"), default="auto")
    parser.add_argument("--uri", default="ip:192.168.2.1", help="URI IIO PlutoSDR, es. ip:192.168.2.1")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--protocols", default="adsb,flarm", help="Protocolli attivi separati da virgola: adsb, flarm o entrambi")
    parser.add_argument("--rx-lat", type=float, default=45.4642, help="Latitudine stazione: riferimento per decodificare le posizioni FLARM")
    parser.add_argument("--rx-lon", type=float, default=9.19, help="Longitudine stazione: riferimento per decodificare le posizioni FLARM")
    parser.add_argument("--center-frequency", type=int, default=ADS_B_FREQ_HZ)
    parser.add_argument("--flarm-frequency", type=int, default=FLARM_EU_FREQ_HZ)
    parser.add_argument("--sample-rate", type=int, default=DEFAULT_SAMPLE_RATE)
    parser.add_argument("--rf-bandwidth", type=int, default=DEFAULT_RF_BW)
    parser.add_argument("--buffer-size", type=int, default=262144)
    parser.add_argument("--ui-rate-hz", type=float, default=DEFAULT_UI_RATE_HZ, help="Frequenza refresh UI HTTP/SSE, 1-30 Hz")
    parser.add_argument("--dual-rx", action=argparse.BooleanOptionalAction, default=False, help="Abilita RX0+RX1 solo se il buffer IIO espone due stream complessi reali")
    parser.add_argument("--rx0-scan", action=argparse.BooleanOptionalAction, default=True, help="Alterna RX0 tra ADS-B e FLARM quando RX1 non e attivo")
    parser.add_argument("--adsb-dwell-ms", type=float, default=850.0)
    parser.add_argument("--flarm-dwell-ms", type=float, default=250.0)
    parser.add_argument("--tune-settle-ms", type=float, default=8.0)
    parser.add_argument("--gain", type=float, default=DEFAULT_GAIN_DB)
    parser.add_argument("--min-gain", type=float, default=DEFAULT_MIN_GAIN_DB)
    parser.add_argument("--max-gain", type=float, default=DEFAULT_MAX_GAIN_DB)
    parser.add_argument(
        "--gain-mode",
        choices=("manual", "manual-fixed", "slow_attack", "fast_attack"),
        default="manual",
    )
    parser.add_argument("--self-test", action="store_true", help="Esegue test CRC, quota e CPR senza avviare il server")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        return run_self_test()
    return run_server(args)


if __name__ == "__main__":
    raise SystemExit(main())
