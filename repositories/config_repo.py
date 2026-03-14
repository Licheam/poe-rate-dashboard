import json

import tomllib


CONFIG_FILE = "models_config.toml"


def load_config():
    with open(CONFIG_FILE, "rb") as f:
        return tomllib.load(f)


def save_config(cfg):
    handles = cfg.get("handles", [])
    serialized_handles = ", ".join(json.dumps(handle, ensure_ascii=False) for handle in handles)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write(f"handles = [{serialized_handles}]\n")
