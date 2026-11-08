#!/usr/bin/python3
"""Shared hardware, config, and protocol logic for Predator Fan Manager.

Target: Acer Predator Helios 300 PH315-51 (2018, i7-8750H + GTX 1060).
Fan control and monitoring use the BIOS AcerGamingFunction WMI interface via
acpi_call, with a tested EC fallback. CoolBoost uses the EC.
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import tempfile
import time

MODEL = "PH315-51"
ACPI_CALL = "/proc/acpi/call"
EC_IO = "/sys/kernel/debug/ec/ec0/io"
SOCKET_PATH = os.environ.get(
    "PREDATOR_FAN_SOCKET", "/run/predator-fan-manager/control.sock"
)
CONFIG_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "predator-fan-manager",
    "config.json",
)

FANS = ("cpu", "gpu")
MODES = ("auto", "max", "custom")
GREEN_BELOW = 70.0
YELLOW_BELOW = 85.0
POLL_SECONDS = 1.0

WMI_METHOD_PATH = r"\_SB.PCI0.WMID.WMBH"
WMI_METHOD_IDS = {
    "SetGamingFanBehavior": 14,
    "SetGamingFanSpeed": 16,
    "GetGamingSysInfo": 5,
}
SENSOR_IDS = {
    "cpu_temp": 1,
    "cpu_rpm": 2,
    "gpu_rpm": 6,
    "gpu_temp": 10,
}
FAN_BITMAP = {"cpu": 0x1, "gpu": 0x8}
FAN_SPEED_ID = {"cpu": 0x1, "gpu": 0x4}
FAN_MODE_SHIFT = {"cpu": 16, "gpu": 22}
FAN_MODE_VALUE = {"auto": 1, "max": 2, "custom": 3}

EC_REG = {
    "cpu": {
        "mode": 0x22, "speed": 0x37, "auto": 0x54, "max": 0x58, "custom": 0x5C,
    },
    "gpu": {
        "mode": 0x21, "speed": 0x3A, "auto": 0x50, "max": 0x60, "custom": 0x70,
    },
}
EC_KNOWN_MODE = {
    "cpu": {0x00: "auto", 0x54: "auto", 0x58: "max", 0x5C: "custom", 0x5D: "custom"},
    "gpu": {0x00: "auto", 0x50: "auto", 0x60: "max", 0x70: "custom"},
}
EC_COOLBOOST = 0x10

DEFAULT_CONFIG = {
    "fans": {fan: {"mode": "auto", "percent": 50} for fan in FANS},
    "coolboost": False,
    "show_temp": True,
}


class HardwareError(RuntimeError):
    pass


def clamp_percent(value):
    return max(0, min(100, int(round(value))))


def read_text(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return default


def write_text(path, value):
    with open(path, "w", encoding="ascii") as handle:
        handle.write(str(value) + "\n")


def model_supported():
    product = (read_text("/sys/class/dmi/id/product_name", "") or "").upper()
    return MODEL in product


def ensure_modules():
    """Load acpi_call and ec_sys when possible. Returns diagnostics."""
    messages = []
    if not os.path.exists(ACPI_CALL):
        result = subprocess.run(
            ["modprobe", "acpi_call"], capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            messages.append(
                "acpi_call unavailable"
                + (f": {result.stderr.strip()}" if result.stderr.strip() else "")
            )
    if not os.path.exists(EC_IO):
        result = subprocess.run(
            ["modprobe", "ec_sys", "write_support=1"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            messages.append(
                "ec_sys unavailable"
                + (f": {result.stderr.strip()}" if result.stderr.strip() else "")
            )
    return messages


# ---- WMI encoding -------------------------------------------------------
def encode_u64(value):
    return int(value & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little")


def encode_u32(value):
    return int(value & 0xFFFFFFFF).to_bytes(4, "little")


def build_call(method_id, payload, method_path=WMI_METHOD_PATH):
    return f"{method_path} 0 {method_id} b{payload.hex()}"


def parse_result(text):
    text = text.strip().rstrip("\x00")
    if text.startswith("Error"):
        raise HardwareError(text)
    if text.startswith("{"):
        numbers = re.findall(r"0x([0-9a-fA-F]+)", text)
        return int.from_bytes(
            bytes(int(number, 16) & 0xFF for number in numbers), "little"
        )
    if text.startswith("0x"):
        return int(text, 16)
    raise HardwareError(f"acpi_call: unexpected result {text!r}")


def fan_behavior_arg(fan, mode):
    return FAN_BITMAP[fan] | (FAN_MODE_VALUE[mode] << FAN_MODE_SHIFT[fan])


def fan_speed_arg(fan, percent):
    return FAN_SPEED_ID[fan] | (clamp_percent(percent) << 8)


def sysinfo_arg(index):
    return 1 | (index << 8)


class WmiBackend:
    name = "wmi"

    def __init__(self, call_path=ACPI_CALL):
        self.call_path = call_path

    @classmethod
    def available(cls):
        return os.path.exists(ACPI_CALL)

    def _call(self, method, payload):
        method_id = WMI_METHOD_IDS[method]
        with open(self.call_path, "w", encoding="ascii") as handle:
            handle.write(build_call(method_id, payload))
        with open(self.call_path, "r", encoding="ascii") as handle:
            return parse_result(handle.read())

    def set_u64(self, method, value):
        result = self._call(method, encode_u64(value))
        if result & 0xFF:
            raise HardwareError(f"{method} error 0x{result & 0xFF:02X}")
        return result

    def get_sensor(self, key):
        result = self._call("GetGamingSysInfo", encode_u32(sysinfo_arg(SENSOR_IDS[key])))
        if result & 0xFF:
            raise HardwareError(f"GetGamingSysInfo error 0x{result & 0xFF:02X}")
        return (result >> 8) & 0xFFFF

    def probe(self):
        try:
            value = self.get_sensor("cpu_temp")
            return 0 < value < 120
        except (HardwareError, OSError):
            return False

    def capabilities(self):
        return {"fan_control": True, "fan_rpm": True, "coolboost": False}

    def set_fan(self, fan, mode, percent=None):
        self.set_u64("SetGamingFanBehavior", fan_behavior_arg(fan, mode))
        if mode == "custom":
            self.set_fan_speed(fan, percent if percent is not None else 50)

    def set_fan_speed(self, fan, percent):
        self.set_u64("SetGamingFanSpeed", fan_speed_arg(fan, percent))

    def set_coolboost(self, _on):
        raise HardwareError("CoolBoost is not available through WMI")

    def read_sensors(self):
        out = {}
        for key in ("cpu_temp", "gpu_temp", "cpu_rpm", "gpu_rpm"):
            try:
                value = self.get_sensor(key)
            except (HardwareError, OSError):
                value = None
            if key.endswith("_temp") and not (value and 0 < value < 120):
                value = None
            out[key] = value
        return out

    def restore_auto(self):
        for fan in FANS:
            self.set_fan(fan, "auto")


class EcBackend:
    name = "ec"

    def __init__(self, path=EC_IO):
        self.path = path

    @classmethod
    def available(cls):
        return (
            model_supported()
            and os.path.exists(EC_IO)
            and os.access(EC_IO, os.R_OK | os.W_OK)
        )

    def _read(self, address):
        with open(self.path, "rb") as handle:
            handle.seek(address)
            data = handle.read(1)
        if not data:
            raise HardwareError(f"EC read failed at 0x{address:02X}")
        return data[0]

    def _write(self, address, value):
        if not 0 <= value <= 0xFF:
            raise HardwareError(f"EC value out of range: {value}")
        with open(self.path, "rb+") as handle:
            handle.seek(address)
            if handle.read(1) == bytes([value]):
                return
            handle.seek(address)
            handle.write(bytes([value]))

    def capabilities(self):
        return {"fan_control": True, "fan_rpm": False, "coolboost": True}

    def set_fan(self, fan, mode, percent=None):
        registers = EC_REG[fan]
        if mode == "custom":
            self._write(registers["speed"], clamp_percent(percent or 0))
        self._write(registers["mode"], registers[mode])

    def set_fan_speed(self, fan, percent):
        self._write(EC_REG[fan]["speed"], clamp_percent(percent))

    def set_coolboost(self, enabled):
        self._write(EC_COOLBOOST, 1 if enabled else 0)

    def get_coolboost(self):
        return self._read(EC_COOLBOOST) == 1

    def get_fans(self):
        out = {}
        for fan, registers in EC_REG.items():
            raw = self._read(registers["mode"])
            out[fan] = {
                "mode": EC_KNOWN_MODE[fan].get(raw, "auto"),
                "percent": self._read(registers["speed"]),
            }
        return out

    def read_sensors(self):
        return {}

    def restore_auto(self):
        for fan in FANS:
            self.set_fan(fan, "auto")


class Hardware:
    """Detect and serialize access to the PH315-51 fan hardware."""

    def __init__(self):
        if not model_supported():
            raise HardwareError(
                f"unsupported model: {read_text('/sys/class/dmi/id/product_name', 'unknown')}"
            )
        self.wmi = None
        self.ec = None
        self.fan = None
        self.sensors_backend = None

        if WmiBackend.available():
            candidate = WmiBackend()
            if candidate.probe():
                self.wmi = candidate
                self.sensors_backend = candidate
        if EcBackend.available():
            self.ec = EcBackend()

        self.fan = self.wmi or self.ec
        if not self.fan:
            raise HardwareError(
                "No supported fan interface found (need acpi_call or ec_sys)"
            )
        if not self.sensors_backend:
            self.sensors_backend = self.wmi

    @property
    def backend(self):
        if self.fan is self.wmi and self.ec:
            return "wmi+ec"
        return self.fan.name

    def capabilities(self):
        caps = dict(self.fan.capabilities())
        caps["coolboost"] = bool(self.ec)
        caps["fan_rpm"] = bool(self.wmi)
        return caps

    def set_fan(self, fan, mode, percent=None):
        self.fan.set_fan(fan, mode, percent)

    def set_coolboost(self, enabled):
        if self.ec:
            self.ec.set_coolboost(enabled)

    def read_sensors(self):
        out = {"cpu_temp": None, "gpu_temp": None, "cpu_rpm": None, "gpu_rpm": None}
        if self.wmi:
            out.update(self.wmi.read_sensors())
        if out["cpu_temp"] is None:
            out["cpu_temp"] = cpu_temp_fallback()
        if out["gpu_temp"] is None:
            out["gpu_temp"] = gpu_temp_fallback()
        if out["cpu_rpm"] is None or out["gpu_rpm"] is None:
            rpm = rpm_fallback()
            out["cpu_rpm"] = out["cpu_rpm"] if out["cpu_rpm"] is not None else rpm.get("cpu")
            out["gpu_rpm"] = out["gpu_rpm"] if out["gpu_rpm"] is not None else rpm.get("gpu")
        return out

    def restore_auto(self):
        for fan in FANS:
            try:
                self.fan.set_fan(fan, "auto")
            except (HardwareError, OSError):
                if self.ec and self.fan is not self.ec:
                    try:
                        self.ec.set_fan(fan, "auto")
                    except (HardwareError, OSError):
                        pass
        if self.ec:
            try:
                self.ec.set_coolboost(False)
            except (HardwareError, OSError):
                pass


# ---- fallback sensors ---------------------------------------------------
def find_hwmon(name):
    for path in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        if read_text(os.path.join(path, "name")) == name:
            return path
    return None


def cpu_temp_fallback():
    path = find_hwmon("coretemp")
    if not path:
        return None
    for label_path in sorted(glob.glob(os.path.join(path, "temp*_label"))):
        if (read_text(label_path, "") or "").startswith("Package"):
            value = read_text(label_path.replace("_label", "_input"))
            if value and value.lstrip("-").isdigit():
                return int(value) / 1000
    value = read_text(os.path.join(path, "temp1_input"))
    if value and value.lstrip("-").isdigit():
        return int(value) / 1000
    return None


def gpu_temp_fallback():
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        text = result.stdout.strip().splitlines()
        if text and text[0].strip().isdigit():
            return float(text[0].strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def rpm_fallback():
    path = find_hwmon("acer")
    if not path:
        return {}
    out = {}
    for index, fan in ((1, "cpu"), (2, "gpu")):
        value = read_text(os.path.join(path, f"fan{index}_input"))
        if value and value.isdigit():
            out[fan] = int(value)
    return out


# ---- configuration ------------------------------------------------------
def normalize_config(raw):
    config = {
        "fans": {fan: dict(DEFAULT_CONFIG["fans"][fan]) for fan in FANS},
        "coolboost": False,
        "show_temp": True,
    }
    if not isinstance(raw, dict):
        return config
    for fan in FANS:
        source = (raw.get("fans") or {}).get(fan) or {}
        mode = str(source.get("mode", "auto")).lower()
        config["fans"][fan]["mode"] = mode if mode in MODES else "auto"
        try:
            percent = clamp_percent(source.get("percent", 50))
        except (TypeError, ValueError):
            percent = 50
        config["fans"][fan]["percent"] = percent
    config["coolboost"] = bool(raw.get("coolboost", False))
    config["show_temp"] = bool(raw.get("show_temp", True))
    return config


def control_state(config):
    config = normalize_config(config)
    return {"fans": config["fans"], "coolboost": config["coolboost"]}


def load_config(path=CONFIG_PATH):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return normalize_config(json.load(handle))
    except (OSError, ValueError, TypeError):
        return normalize_config({})


def save_config(config, path=CONFIG_PATH):
    config = normalize_config(config)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".config-", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def temp_color(celsius):
    if celsius is None:
        return "grey"
    if celsius < GREEN_BELOW:
        return "green"
    if celsius < YELLOW_BELOW:
        return "yellow"
    return "red"


def hottest(*values):
    values = [value for value in values if value is not None]
    return max(values) if values else None
