# -*- coding: utf-8 -*-
"""
模拟遥测数据源。
真实环境中替换为 Basilisk 仿真器或星载传感器驱动。
"""
import time
import random
import hashlib
import json
from typing import Dict, Any, List, Optional
from agent.noise_model import NoiseModel


class TelemetrySource:
    """模拟卫星遥测数据源，生成电压、电流、温度、功率等物理量。"""

    def __init__(self, sat_id: str = "SAT-001", noise_config: Optional[Dict[str, Any]] = None):
        self.sat_id = sat_id
        self._seq = 0
        self._faulty_sensors: Dict[str, float] = {}
        self._start_time = time.time()
        self._noise = NoiseModel(noise_config)
        self._dropped_packets = 0

    def set_faulty_sensors(self, sensors: Dict[str, float]):
        """设置故障传感器（用于多源矛盾故障注入）。"""
        self._faulty_sensors = sensors

    def clear_faulty_sensors(self):
        self._faulty_sensors = {}

    def get_telemetry(self) -> Optional[Dict[str, Any]]:
        """
        获取一帧遥测数据。

        Returns:
            遥测数据帧；如果发生数据丢包则返回 None
        """
        self._seq += 1
        now = time.time()
        elapsed = now - self._start_time

        # 数据丢包检查（帧级噪声）
        if self._noise.should_drop_packet():
            self._dropped_packets += 1
            return None

        # 正常遥测（模拟 28V 母线系统）
        telemetry = {
            "bus_voltage": round(28.0 + random.uniform(-0.3, 0.3), 3),
            "bus_current": round(5.0 + random.uniform(-0.5, 0.5), 3),
            "sa_voltage": round(30.0 + random.uniform(-1.0, 1.0), 3),
            "sa_current": round(4.5 + random.uniform(-0.3, 0.3), 3),
            "bcr_voltage": round(28.2 + random.uniform(-0.2, 0.2), 3),
            "bcr_current": round(4.8 + random.uniform(-0.3, 0.3), 3),
            "battery_soc": round(85.0 - elapsed * 0.001 + random.uniform(-0.5, 0.5), 2),
            "battery_voltage": round(24.5 + random.uniform(-0.2, 0.2), 3),
            "temp_battery": round(18.0 + random.uniform(-1.0, 1.0), 2),
            "temp_obc": round(25.0 + random.uniform(-1.0, 1.0), 2),
            "temp_sa": round(35.0 + random.uniform(-2.0, 2.0), 2),
            "power_consumption_w": round(140.0 + random.uniform(-5.0, 5.0), 2),
            "power_generation_w": round(150.0 + random.uniform(-5.0, 5.0), 2),
            "attitude_error_deg": round(random.uniform(0, 0.5), 4),
            "data_storage_used_mb": round(100 + elapsed * 0.01, 2),
            # Gateway physical_state_snapshot 必须字段（P0）
            "collision_risk": 1e-6,
            "seu_rate": 0.0,
            # Gateway physical_state_snapshot 可选字段（P2）
            "altitude_km": 550.0,
            "velocity_kms": 7.59,
            "orbital_inclination_deg": 28.5,
            "is_in_eclipse": False,
        }

        # 先应用噪声模型（高斯/漂移/脉冲，值级噪声）
        telemetry = self._noise.apply(telemetry)

        # 再注入故障传感器值（多源矛盾故障，优先级高于噪声，覆盖噪声值）
        for sensor, value in self._faulty_sensors.items():
            if sensor in telemetry:
                telemetry[sensor] = value

        frame = {
            "sat_id": self.sat_id,
            "seq": self._seq,
            "timestamp_ms": int(now * 1000),
            "telemetry": telemetry,
            "physics": {
                "orbit_altitude_km": 550.0,
                "eclipse": False,
                "velocity_kms": 7.59,
            },
            "extra": {
                "power_detail": {
                    "battery": {"avg_soc": telemetry["battery_soc"], "voltage": telemetry["battery_voltage"]},
                    "solar_array": {"power_w": telemetry["power_generation_w"]},
                    "load": {"power_w": telemetry["power_consumption_w"]},
                }
            },
            "status_summary": "NOMINAL" if not self._faulty_sensors else "SENSOR_ANOMALY",
            "noise_status": self._noise.get_noise_status(),
        }
        return frame

    def get_telemetry_hash(self, telemetry: Dict[str, Any]) -> str:
        """计算遥测数据的哈希（用于证据绑定和篡改检测）。"""
        raw = json.dumps(telemetry, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def get_dropped_packet_count(self) -> int:
        """获取累计丢包数。"""
        return self._dropped_packets
