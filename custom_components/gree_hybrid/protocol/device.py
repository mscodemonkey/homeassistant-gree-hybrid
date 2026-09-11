"""Transport-neutral Gree device state model."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from enum import Enum, IntEnum
from typing import Any

from .models import DeviceInfo

TEMP_MIN = 8
TEMP_MAX = 30
TEMP_MIN_F = 46
TEMP_MAX_F = 86
TEMP_SENSOR_OFFSET = 40


class Props(str, Enum):
    POWER = "Pow"
    MODE = "Mod"
    HUM_SET = "Dwet"
    HUM_SENSOR = "DwatSen"
    CLEAN_FILTER = "Dfltr"
    WATER_FULL = "DwatFul"
    DEHUMIDIFIER_MODE = "Dmod"
    TEMP_SET = "SetTem"
    TEMP_SENSOR = "TemSen"
    TEMP_UNIT = "TemUn"
    TEMP_BIT = "TemRec"
    TEMP_HALF_ENABLED = "HalfTemEn"
    TEMP_DECI = "SetDeciTem"
    TEMP_HALF_DEGREE = "Add0.5"
    FAN_SPEED = "WdSpd"
    FRESH_AIR = "Air"
    XFAN = "Blo"
    ANION = "Health"
    SLEEP = "SwhSlp"
    SLEEP_MODE = "SlpMod"
    LIGHT = "Lig"
    SWING_HORIZ = "SwingLfRig"
    SWING_VERT = "SwUpDn"
    QUIET = "Quiet"
    TURBO = "Tur"
    STEADY_HEAT = "StHt"
    POWER_SAVE = "SvSt"


class TemperatureUnits(IntEnum):
    C = 0
    F = 1


class Mode(IntEnum):
    Auto = 0
    Cool = 1
    Dry = 2
    Fan = 3
    Heat = 4


class FanSpeed(IntEnum):
    Auto = 0
    Low = 1
    MediumLow = 2
    Medium = 3
    MediumHigh = 4
    High = 5


class HorizontalSwing(IntEnum):
    Default = 0
    FullSwing = 1
    Left = 2
    LeftCenter = 3
    Center = 4
    RightCenter = 5
    Right = 6


class VerticalSwing(IntEnum):
    Default = 0
    FullSwing = 1
    FixedUpper = 2
    FixedUpperMiddle = 3
    FixedMiddle = 4
    FixedLowerMiddle = 5
    FixedLower = 6
    SwingUpper = 7
    SwingUpperMiddle = 8
    SwingMiddle = 9
    SwingLowerMiddle = 10
    SwingLower = 11


def _bool_descriptor(prop: Props) -> property:
    return property(
        lambda self: self._bool_property(prop),
        lambda self, value: self.set_property(prop, int(value)),
    )


def _integer_descriptor(prop: Props) -> property:
    return property(
        lambda self: self.get_property(prop),
        lambda self, value: self.set_property(prop, int(value)),
    )


class GreeDevice(ABC):
    """Common state and mutation behaviour shared by LAN and cloud devices."""

    def __init__(self, device_info: DeviceInfo) -> None:
        self.device_info = device_info
        self.hid: str | None = None
        self.version: str | None = device_info.version
        self.raw_properties: dict[str, Any] = {}
        self._dirty: list[str] = []

    def get_property(self, prop: Props) -> Any:
        return self.raw_properties.get(prop.value)

    def set_property(self, prop: Props, value: Any) -> None:
        if self.raw_properties.get(prop.value) == value:
            return
        self.raw_properties[prop.value] = value
        if prop.value not in self._dirty:
            self._dirty.append(prop.value)

    def set_raw_property(self, name: str, value: Any) -> None:
        if self.raw_properties.get(name) == value:
            return
        self.raw_properties[name] = value
        if name not in self._dirty:
            self._dirty.append(name)

    def apply_state(self, values: dict[str, Any]) -> None:
        state = dict(values)
        if hid := state.pop("hid", None):
            self.hid = hid
            match = re.search(r"V([\d.]+)\.bin$", hid)
            if match:
                self.version = match.group(1)
        self.raw_properties.update(state)

    @property
    def power(self) -> bool | None:
        value = self.get_property(Props.POWER)
        return None if value is None else bool(value)

    @power.setter
    def power(self, value: bool) -> None:
        self.set_property(Props.POWER, int(value))

    @property
    def mode(self) -> int | None:
        return self.get_property(Props.MODE)

    @mode.setter
    def mode(self, value: int) -> None:
        self.set_property(Props.MODE, int(value))

    @property
    def temperature_units(self) -> int | None:
        return self.get_property(Props.TEMP_UNIT)

    @property
    def target_temperature(self) -> float | None:
        whole = self.get_property(Props.TEMP_SET)
        if whole is None:
            return None
        if self.temperature_units == TemperatureUnits.F:
            bit = self.get_property(Props.TEMP_BIT) or 0
            return round((whole + 0.5 * bit) * 9 / 5 + 32)
        deci = self.get_property(Props.TEMP_DECI)
        return (
            deci / 10
            if deci is not None
            else whole + 0.5 * (self.get_property(Props.TEMP_BIT) or 0)
        )

    @target_temperature.setter
    def target_temperature(self, value: float) -> None:
        if self.temperature_units == TemperatureUnits.F:
            if not TEMP_MIN_F <= value <= TEMP_MAX_F:
                raise ValueError("Temperature is outside the supported range")
            celsius = (value - 32) * 5 / 9
        else:
            if not TEMP_MIN <= value <= TEMP_MAX:
                raise ValueError("Temperature is outside the supported range")
            celsius = value
        whole = int(celsius)
        half = int(celsius - whole >= 0.5)
        self.set_property(Props.TEMP_SET, whole)
        self.set_property(Props.TEMP_BIT, half)
        if self.get_property(Props.TEMP_HALF_ENABLED) == 1:
            self.set_property(Props.TEMP_HALF_DEGREE, half)
            self.set_property(Props.TEMP_DECI, round(celsius * 10))

    @property
    def current_temperature(self) -> float | None:
        raw = self.get_property(Props.TEMP_SENSOR)
        if raw in (None, 0):
            return self.target_temperature
        celsius = raw if raw < TEMP_SENSOR_OFFSET else raw - TEMP_SENSOR_OFFSET
        return (
            round(celsius * 9 / 5 + 32)
            if self.temperature_units == TemperatureUnits.F
            else celsius
        )

    @property
    def fan_speed(self) -> int | None:
        return self.get_property(Props.FAN_SPEED)

    @fan_speed.setter
    def fan_speed(self, value: int) -> None:
        self.set_property(Props.FAN_SPEED, int(value))

    def _bool_property(self, prop: Props) -> bool | None:
        value = self.get_property(prop)
        return None if value is None else bool(value)

    fresh_air = _bool_descriptor(Props.FRESH_AIR)
    xfan = _bool_descriptor(Props.XFAN)
    anion = _bool_descriptor(Props.ANION)
    light = _bool_descriptor(Props.LIGHT)
    turbo = _bool_descriptor(Props.TURBO)
    steady_heat = _bool_descriptor(Props.STEADY_HEAT)
    power_save = _bool_descriptor(Props.POWER_SAVE)
    horizontal_swing = _integer_descriptor(Props.SWING_HORIZ)
    vertical_swing = _integer_descriptor(Props.SWING_VERT)

    @property
    def quiet(self) -> bool | None:
        return self._bool_property(Props.QUIET)

    @quiet.setter
    def quiet(self, value: bool) -> None:
        self.set_property(Props.QUIET, 2 if value else 0)

    @property
    def sleep(self) -> bool | None:
        return self._bool_property(Props.SLEEP)

    @sleep.setter
    def sleep(self, value: bool) -> None:
        self.set_property(Props.SLEEP, int(value))
        self.set_property(Props.SLEEP_MODE, int(value))

    @abstractmethod
    async def bind(self) -> None: ...

    @abstractmethod
    async def update_state(self) -> None: ...

    @abstractmethod
    async def push_state_update(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...
