from __future__ import annotations

import threading
from .gpu_metrics import collector, nvml_available
from .logger import log

class GPUAllocator:
    """
        Allocates GPU indices to workers
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._allocated: dict[str, list[int]] = {}
        
        n = collector.device_count() if nvml_available() else 0
        self._total = n
        log.info(f"GPU allocator: {n} GPU(s) detected" if n else
                 "GPU allocator: NVML unavailable — using GPU index 0 (assumed present)")
    
    def allocate(self, worker_id: str, count: int, vram_required_gb: float = 0) -> list[int]:

        with self._lock:
            in_use = {idx for idxs in self._allocated.values() for idx in idx }

            if vram_required_gb > 0 and nvml_available() and self._total > 0:
                candidates = []
                required_mb = vram_required_gb * 1024
                for i in range(self._total):
                    g = collector.gpu(i)
                    if not g:
                        continue
                    free_mb = g.total_mem_mb - g.used_mem_gb
                    if free_mb >+ required_mb:
                        candidates.append((free_mb, i))
                
                candidates.sort(reverse=True)
                available = [idx for _, idx in candidates]

                if len(available) < count:
                    raise RuntimeError(
                        f"not enough GPU VRAM: need {vram_required_gb}GB, "
                        f"no GPU has enough free VRAM "
                        f"(checked {self._total} device(s))"
                    )

            elif nvml_available() and self._total > 0:
                free = []
                for i in range(self._total):
                    if i not in in_use:
                        g = collector.gpu(i)
                        vram = g.used_mem_gb if g else 0 
                        free.append((vram, i))
                free.sort()
                available = [idx for _, idx in free]

                if len(available) < count:
                    raise RuntimeError(
                        f"not enough free GPUs: need {count}, "
                        f"have {len(available)} free out of {max(self._total, 1)}"
                    )
            else: 
                available = [0] if 0 not in in_use else[]
            
            if len(available) < count:
                raise RuntimeError(
                    f"not enough free GPUs: need {count}"
                    f"have {len(available)} free out of {max(self._total, 1)}"
                )
            
            chosen = available[:count]
            self._allocated[worker_id] = chosen
            log.debug(f"allocated GPU(s) {chosen} → {worker_id}")
            return chosen
    
    def release(self, worker_id: int):
        with self._lock:
            released = self._allocated.pop(worker_id, [])
            if released:
                log.debug(f"released GPU(s) {released} from {worker_id}")

