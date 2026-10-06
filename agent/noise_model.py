# -*- coding: utf-8 -*-
"""
真实遥测噪声模型。

模拟星载传感器在真实在轨环境下的4种噪声模式：
1. 高斯噪声（Gaussian）：传感器精度限制，每个值叠加正态分布扰动
2. 随机漂移（Drift）：传感器老化，值随时间缓慢偏移
3. 脉冲干扰（Spike）：辐射瞬态，随机时刻出现瞬时尖峰
4. 数据丢包（Packet Loss）：通信中断，整帧数据丢失

设计原则：
- 所有噪声可配置强度，通过 config.json 开关
- 噪声叠加在基础遥测值之上，不改变原始生成逻辑
- 丢包是帧级的，其他三种是值级的
"""
import random
import time
from typing import Dict, Any, Optional, List


class NoiseModel:
    """遥测噪声模型，支持4种噪声模式的独立配置与叠加。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        初始化噪声模型。

        config 示例:
        {
            "enabled": True,
            "gaussian": {"enabled": True, "sigma_scale": 0.1},
            "drift": {"enabled": True, "drift_rate_per_sec": 0.001},
            "spike": {"enabled": True, "probability": 0.02, "magnitude_scale": 3.0},
            "packet_loss": {"enabled": True, "probability": 0.01}
        }
        """
        self.config = config or {}
        self.enabled = self.config.get("enabled", False)
        self._start_time = time.time()
        self._drift_accumulator: Dict[str, float] = {}
        self._spike_active: Dict[str, float] = {}

    def is_enabled(self) -> bool:
        return self.enabled

    def should_drop_packet(self) -> bool:
        """判断当前帧是否应该丢包。"""
        if not self.enabled:
            return False
        cfg = self.config.get("packet_loss", {})
        if not cfg.get("enabled", False):
            return False
        return random.random() < cfg.get("probability", 0.01)

    def apply(self, telemetry: Dict[str, Any]) -> Dict[str, Any]:
        """
        对一帧遥测数据应用所有已启用的噪声。

        Args:
            telemetry: 原始遥测数据字典（值级，不含seq/sat_id等元数据）

        Returns:
            加噪后的遥测数据字典
        """
        if not self.enabled:
            return telemetry

        result = dict(telemetry)

        # 1. 高斯噪声
        result = self._apply_gaussian(result)

        # 2. 随机漂移
        result = self._apply_drift(result)

        # 3. 脉冲干扰
        result = self._apply_spike(result)

        return result

    def _apply_gaussian(self, telemetry: Dict[str, Any]) -> Dict[str, Any]:
        """高斯噪声：每个数值型传感器叠加正态分布扰动。"""
        cfg = self.config.get("gaussian", {})
        if not cfg.get("enabled", False):
            return telemetry

        sigma_scale = cfg.get("sigma_scale", 0.1)  # 噪声强度相对于基准值的比例
        result = dict(telemetry)

        for key, value in telemetry.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                # 基准值取绝对值，避免除零；无量纲小值用固定sigma
                base = max(abs(value), 0.01)
                sigma = base * sigma_scale
                result[key] = round(value + random.gauss(0, sigma), 4)

        return result

    def _apply_drift(self, telemetry: Dict[str, Any]) -> Dict[str, Any]:
        """随机漂移：传感器值随时间缓慢偏移（模拟老化）。"""
        cfg = self.config.get("drift", {})
        if not cfg.get("enabled", False):
            return telemetry

        drift_rate = cfg.get("drift_rate_per_sec", 0.001)
        elapsed = time.time() - self._start_time
        result = dict(telemetry)

        for key, value in telemetry.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                # 每个传感器有独立的漂移方向和速率
                if key not in self._drift_accumulator:
                    self._drift_accumulator[key] = random.uniform(-1.0, 1.0)
                drift_direction = self._drift_accumulator[key]
                base = max(abs(value), 0.01)
                drift_amount = base * drift_rate * elapsed * drift_direction
                result[key] = round(value + drift_amount, 4)

        return result

    def _apply_spike(self, telemetry: Dict[str, Any]) -> Dict[str, Any]:
        """脉冲干扰：随机时刻在随机传感器上出现瞬时尖峰。"""
        cfg = self.config.get("spike", {})
        if not cfg.get("enabled", False):
            return telemetry

        probability = cfg.get("probability", 0.02)  # 每帧出现尖峰的概率
        magnitude_scale = cfg.get("magnitude_scale", 3.0)  # 尖峰幅度相对于高斯sigma的倍数
        result = dict(telemetry)

        # 上一帧的尖峰在本帧消失（瞬时性）
        self._spike_active.clear()

        if random.random() < probability:
            # 随机选1-2个传感器施加尖峰
            numeric_keys = [k for k, v in telemetry.items()
                            if isinstance(v, (int, float)) and not isinstance(v, bool)]
            if numeric_keys:
                num_spikes = random.randint(1, min(2, len(numeric_keys)))
                spike_keys = random.sample(numeric_keys, num_spikes)
                for key in spike_keys:
                    value = telemetry[key]
                    base = max(abs(value), 0.01)
                    sigma = base * 0.1
                    spike_magnitude = sigma * magnitude_scale * random.choice([-1, 1])
                    result[key] = round(value + spike_magnitude, 4)
                    self._spike_active[key] = spike_magnitude

        return result

    def get_noise_status(self) -> Dict[str, Any]:
        """获取当前噪声状态（用于日志和调试）。"""
        return {
            "enabled": self.enabled,
            "active_modes": [
                mode for mode in ["gaussian", "drift", "spike", "packet_loss"]
                if self.config.get(mode, {}).get("enabled", False)
            ],
            "spike_active": dict(self._spike_active),
            "elapsed_s": round(time.time() - self._start_time, 2),
        }
