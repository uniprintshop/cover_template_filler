"""Configuration defaults and validation, applied once at startup."""

import copy
import math
import re
from pathlib import Path


def normalize_config(data: dict, base: Path) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Configuration must be a JSON object")
    cfg = copy.deepcopy(data)
    for key in ("watch_dir", "output_dir", "templates_dir"):
        if not isinstance(cfg.get(key), str) or not cfg[key].strip():
            raise ValueError(f"{key} must be a non-empty directory path")
    cfg.setdefault("processed_dir", str(Path(cfg["watch_dir"]) / "processed"))
    cfg.setdefault("failed_dir", str(Path(cfg["watch_dir"]) / "failed"))
    for key in ("watch_dir", "output_dir", "templates_dir", "processed_dir", "failed_dir", "state_dir", "local_fail_dir"):
        if key not in cfg:
            continue
        if not isinstance(cfg[key], str) or not cfg[key].strip():
            raise ValueError(f"{key} must be a non-empty directory path")
        path = Path(cfg[key]).expanduser()
        cfg[key] = str((base / path).absolute() if not path.is_absolute() else path)
    numbers = {
        "ocr_min_chars": (80, 1, 10000), "ocr_dpi": (300, 72, 600),
        "ocr_image_max_edge": (1600, 400, 4096), "ocr_image_limit": (12, 1, 100),
        "ocr_timeout_seconds": (8, .1, 300), "ocr_budget_seconds": (20, .1, 600),
        "ocr_render_timeout_seconds": (15, .1, 300), "watch_settle_seconds": (1.5, .1, 60),
        "watch_rescan_interval": (60, 0, 86400), "watch_poll_interval": (5, .1, 3600),
        "retry_seconds": (60, .1, 86400), "watch_queue_limit": (256, 1, 10000),
    }
    integer_keys = {"ocr_min_chars", "ocr_dpi", "ocr_image_max_edge", "ocr_image_limit", "watch_queue_limit"}
    for key, (default, low, high) in numbers.items():
        value = cfg.setdefault(key, default)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not low <= value <= high
                or (key in integer_keys and not isinstance(value, int))):
            raise ValueError(f"{key} must be a number in [{low}, {high}]")
    cfg.setdefault("watch_polling", True)
    for key in ("watch_polling", "notify", "write_title_variant"):
        if key in cfg and not isinstance(cfg[key], bool):
            raise ValueError(f"{key} must be true or false")
    if not isinstance(cfg.get("ocr_lang", "deu+eng"), str):
        raise ValueError("ocr_lang must be a language string")
    model = cfg.setdefault("model", {})
    if not isinstance(model, dict) or model.get("backend", "ollama") not in ("ollama", "openai", "none"):
        raise ValueError("model.backend must be ollama, openai, or none")
    for key, default in (("timeout", 300), ("num_ctx", 16384)):
        value = model.setdefault(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"model.{key} must be a positive integer")
    if cfg.get("publish_year") and not re.fullmatch(r"(?:19|20)\d{2}", str(cfg["publish_year"])):
        raise ValueError("publish_year must be a four-digit year or empty")
    universities = cfg.setdefault("universities", [])
    if not isinstance(universities, list):
        raise ValueError("universities must be a list")
    seen_ids, owners = set(), {}
    for uni in universities:
        if not isinstance(uni, dict) or not isinstance(uni.get("id"), str) or not uni["id"].strip():
            raise ValueError("Each university needs a non-empty string id")
        if uni["id"] in seen_ids:
            raise ValueError(f"Duplicate university id: {uni['id']}")
        seen_ids.add(uni["id"])
        names = uni.get("templates") or ([uni["template"]] if uni.get("template") else [])
        if not isinstance(names, list):
            raise ValueError(f"templates must be a list for {uni['id']}")
        for name in names:
            if not isinstance(name, str) or Path(name).name != name or not name.lower().endswith(".upf"):
                raise ValueError(f"Invalid template filename for {uni['id']}: {name!r}")
            if name in owners and owners[name] != uni["id"]:
                raise ValueError(f"Template assigned to multiple institutions: {name}")
            owners[name] = uni["id"]
        names = list(dict.fromkeys(names))
        if uni.get("template") and uni["template"] not in names:
            raise ValueError(f"Primary template must belong to templates for {uni['id']}")
        uni["templates"] = names
        uni["template"] = uni.get("template") or (names[0] if names else "")
        aliases = uni.setdefault("aliases", [])
        if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
            raise ValueError(f"aliases must be a list of strings for {uni['id']}")
    return cfg
