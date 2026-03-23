import time
import math

class AutoScaler:
    """
        Supports three scaling metrics:
            rps       — requests-per-second per replica target
            gpu_util  — GPU utilisation % target
            queue     — pending queue depth target
    """

    def __init__(self):
        self._last_scale_up: dict[str, float] = {}
        self._last_scale_down:  dict[str, float] = {}
    
    def desired_replicas(
        self, 
        rps: float,
        ready: int,
        active: int,
        min_scale: int,
        max_scale: int,
        name: str = "default",
        metric: str = "rps",
        target: float = 30.0,
        scale_up_cooldown: int = 30,
        scale_down_cooldown: int = 120,
        gpu_util: float = 0.0,
        queue_depth: int = 0,
    ) -> int:
        now = time.time()

        if name not in self._last_scale_down:
            self._last_scale_down[name] = now
        if name not in self._last_scale_up:
            self._last_scale_up[name] = now
        

        if metric == "rps":
            load = rps
        elif metric == "gpu_util":
            load = gpu_util
        elif metric == "queue":
            load = float(queue_depth)
        else:
            load = rps
        
        if load == 0 and min_scale == 0:
            desired = 0
        else: 
            desired = math.ceil(load / max(target, 0.001))
            desired = max(desired, min_scale)
            desired = min(desired, max_scale)
        
        #Enforcing cooldown
        if desired > active:
            last_up = self._last_scale_up.get(name, 0)
            if now - last_up < scale_up_cooldown:
                return active
            self._last_scale_up[name] = now
        
        elif desired > active and active > 0:
            last_down = self._last_scale_up.get(name)
            if now - last_up < scale_up_cooldown:
                return active
            self._last_scale_down[name] = now

        return desired
    
    def explain(
        self,
        rps: float,
        active: int,
        desired: int,
        target: float = 30.0,
    ) -> str:
        """Human-readable explanation of the last scaling decision."""
        if desired > active:
            return (
                f"scaling UP: rps={rps:.1f}, {active} replicas at "
                f"{rps/max(active,1):.1f} rps each (target={target})"
            )
        elif desired < active:
            return (
                f"scaling DOWN: rps={rps:.1f}, {active} replicas at "
                f"{rps/max(active,1):.1f} rps each (target={target})"
            )
        return f"stable: rps={rps:.1f}, {active} replicas"