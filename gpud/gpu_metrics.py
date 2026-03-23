"""
    Read Nvidia GPU telemetry using NVML 
    Falls back to zeroed metrics gracefully if NVML is unavailable
    so the daemon can still start on machines without a GPU.
"""

from __future__ import annotations
import threading 
from dataclasses import dataclass, field
from typing import Optional 
import pathlib 

_nvml_ok = False

try: 
    try:
        import nvidia_ml_py as pynvml
    except:
        import pynvml
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        pynvml.nvmlInit()
    _nvml_ok = True
except Exception: 
    pass

@dataclass
class GPUInfo:
    index:        int
    name:         str
    total_mem_mb: int
    used_mem_mb:  int
    free_mem_mb:  int
    util_pct:     float   # GPU compute utilisation 0-100
    mem_util_pct: float   # memory bandwidth utilisation 0-100
    temp_c:       float
    power_w:      float
    fan_pct:      Optional[float] = None
    
    @property
    def used_mem_gb(self) -> float:
        return round(self.used_mem_mb/1024, 1)
    
    @property
    def total_mem_gb(self) -> float:
        return round(self.total_mem_mb / 1024, 1)
    
    @property
    def vram_pct(self) -> float:
        return round(self.used_mem_mb / max(self.total_mem_mb, 1) * 100, 1)

@dataclass
class GPUProcessInfo:
    pid:         int
    name:        str
    used_mem_mb: int 

class GPUCollector:

    def __init__(self):
        self._lock = threading.Lock()
        self._handles: dict[int, object] = {}
        if _nvml_ok:
            count = pynvml.nvmlDeviceGetCount()
            for i in range(count):
                self._handles[i] = pynvml.nvmlDeviceGetHandleByIndex(i)
    
    def device_count(self) -> int:
        return len(self._handles)            

    def all_gpus(self) -> list[GPUInfo]:
        if not _nvml_ok:
            return []
        
        result = []
        with self._lock:
            for idx, handle in self._handles.items():
                try:
                    result.append(self._read(idx, handle))
                except Exception:
                    pass
        return result    

    def gpu(self, index: int) -> Optional[GPUInfo]:
        if not _nvml_ok or index not in self._handles:
            return None
        with self._lock:
            try: 
                return self._read(index, self._handles[index])
            except Exception:
                return None
            
    def gpus_for_container(self,  container_id: int) -> list[GPUInfo]:
        "Return Info of a GPU thats given a container"
        if not _nvml_ok:
            return []
        pids = self._container_pids(container_id)
        if not pids:
            return []
        result = []
        with self._lock:
            for idx, handle in self._handles.items():
                try:
                    procs = pynvml.nvmlDeviceGetComputeRunningProcesses(handle)
                    if any(p.pid in pids for p in procs):
                        result.append(self._read(idx, handle))
                except Exception:
                    pass
        return result
    
    def processes(self, index: int) -> list[GPUProcessInfo]:
        if not _nvml_ok or index not in self._handles:
            return []
        handle = self._handles[index]
        out = []
        with self._lock:
            try: 
                for p in pynvml.nvmlDeviceGetComputeRunningProcesses(handle):
                    try: 
                        name = pynvml.nvmlSystemGetProcessName(p.pid).decode()
                    except Exception: 
                        name = "?"
                    out.append(GPUProcessInfo(
                        pid  = p.pid,
                        name = name, 
                        used_mem_mb=p.usedGpuMemory 

                    ))
            except Exception: 
                pass                
        return out 

    # private
    def _read(self, idx: int, handle) -> GPUInfo:
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        util = pynvml.nvmlDeviceGetUtilizationRates(handle)
        temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)

        try:
            power = pynvml.nvmlDeviceGetPowerUsage(handle)
        except Exception:
            power = 0.0

        try: 
            fan = float(pynvml.nvmlDeviceGetFanSpeed(handle))
        except Exception:
            fan = None

        name = pynvml.nvmlDeviceGetName(handle)
        if isinstance(name, bytes):
            name = name.decode()

        return GPUInfo(
            index        = idx,
            name         = name,
            total_mem_mb = mem.total  // 1024 // 1024,
            used_mem_mb  = mem.used   // 1024 // 1024,
            free_mem_mb  = mem.free   // 1024 // 1024,
            util_pct     = float(util.gpu),
            mem_util_pct = float(util.memory),
            temp_c       = float(temp),
            power_w      = power,
            fan_pct      = fan,
        )                    
    
    def _container_pids(self, container_id: str) -> set[int]:
        pids = set()
        try: 
            cgroup_path = pathlib.Path(f"/sys/fs/cgroup/memory/docker/{container_id}/cgroup.procs")
            if cgroup_path.exists():
                for line in cgroup_path.read_text().splitlines():
                    try: 
                        pids.add(int(line.strip()))
                    except ValueError:
                        pass 
        except Exception:
            pass

        return pids
    
    def shutdown(self):
        if _nvml_ok:
            try: 
                pynvml.nvmlShutdown()
            except Exception:
                pass


collector = GPUCollector()

def nvml_available() -> bool:
    return _nvml_ok
