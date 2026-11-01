"""Simulated Modbus devices (energy meter, temperature controller, frequency converter)
with behaviour and fault injection, over Modbus TCP, RTU over TCP and serial RTU."""
__version__ = "0.2.0"

from .bench import Bench, BenchError
from .client import ModbusClient, ModbusError
from .device import Device
from .profile import ProfileError, load_profile

__all__ = ["Bench", "BenchError", "Device", "ModbusClient", "ModbusError", "ProfileError",
           "load_profile", "__version__"]
