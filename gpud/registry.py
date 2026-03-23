import json
import pathlib
from .config import DeploymentConfig

REGISTRY_DIR = pathlib.Path.home() / ".gpud" / "deployments"

class DeploymentRegistry:
    def __init__(self):
        REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    
    def save(self, name: str, cfg: DeploymentConfig):
        path = REGISTRY_DIR / f"{name}.json"
        path.write_text(json.dumps(cfg.to_dict(), indent=2))
    
    def load(self, name:str) -> DeploymentConfig | None:
        path = REGISTRY_DIR / f"{name}.json"
        if not path.exists():
            return None
        return DeploymentConfig.from_dict(json.loads(path.read_text()))
    
    def load_all(self) -> dict[str, DeploymentConfig]:
        result = {}
        for p in REGISTRY_DIR.glob("*.json"):
            try:
                cfg = DeploymentConfig.from_dict(json.loads(p.read_text()))
                result[p.stem] = cfg
            except Exception as e:
                from .logger import log
                log.warning(f"skipping corrupt registry entry {p.name}: {e}")
        return result
    
    def delete(self, name: str):
        path = REGISTRY_DIR / f"{name}.json"
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    
    def list_names(self) -> list[str]:
        return [p.stem for p in REGISTRY_DIR.glob("*.json")]
    