"""Load the exact privately maintained lifecycle policy; never fetch or publish it."""

import hashlib
import json
import linecache
import os
from pathlib import Path
import re
import sys
import types


def load(lock_path=None):
    lock_path = Path(lock_path) if lock_path is not None else Path(__file__).with_name("maintenance.lock.json")
    if lock_path.is_symlink() or not lock_path.is_file():
        raise ValueError("The reviewed maintenance lock is missing or not a regular file")
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if (not isinstance(lock, dict) or set(lock) != {"schema", "repository", "revision", "module_sha256", "api_version"}
            or lock["schema"] != 1 or type(lock["schema"]) is not int
            or lock["repository"] != "warelik/machine-maintenance"
            or not isinstance(lock["revision"], str) or not re.fullmatch(r"[0-9a-f]{40}", lock["revision"])
            or not isinstance(lock["module_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", lock["module_sha256"])
            or type(lock["api_version"]) is not int or lock["api_version"] != 1):
        raise ValueError("The reviewed maintenance lock has an invalid identity")
    directory = os.environ.get("FORK_MAINTENANCE_ROOT")
    if not directory:
        raise ValueError("The exact private maintenance checkout is unavailable")
    root = Path(directory)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError("The private maintenance checkout must be a real absolute directory")
    module_path = root / "fork_lifecycle.py"
    receipt_path = root / ".maintenance-source.json"
    if any(path.is_symlink() or not path.is_file() for path in (module_path, receipt_path)):
        raise ValueError("The private maintenance checkout has no checked source receipt")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt != {"repository": lock["repository"], "revision": lock["revision"]}:
        raise ValueError("The acquired private maintenance revision differs from the reviewed lock")
    if module_path.stat().st_size > 262144:
        raise ValueError("The private maintenance module exceeds its source bound")
    source = module_path.read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    if digest != lock["module_sha256"]:
        raise ValueError("The private maintenance module differs from its reviewed checksum")
    name = "_fork_lifecycle_" + digest
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        module.__file__ = str(module_path)
        sys.modules[name] = module
        # Public job tracebacks retain the error and call locations without
        # copying private source lines into public workflow logs.
        linecache.cache[str(module_path)] = (len(source), None, [], str(module_path))
        try:
            exec(compile(source, str(module_path), "exec"), module.__dict__)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    if getattr(module, "API_VERSION", None) != lock["api_version"]:
        raise ValueError("The private maintenance API differs from the reviewed lock")
    module._maintenance_lock_identity = {
        "repository": lock["repository"], "revision": lock["revision"],
        "module_sha256": lock["module_sha256"], "api_version": lock["api_version"],
    }
    return module


def validated_bundle(candidate, comments, read_native):
    """Verify a private policy bundle and bind its proof to this exact loader lock."""
    module = load()
    verifier = getattr(module, "validated_bundle", None)
    if not callable(verifier):
        raise ValueError("The locked private maintenance revision has no bundle verifier")
    proof = verifier(candidate, comments, read_native)
    if proof is None:
        return None
    if not isinstance(proof, dict):
        raise ValueError("The private maintenance module returned a malformed bundle proof")
    if proof.get("shared_source") != module._maintenance_lock_identity:
        raise ValueError("Bundle proof differs from the exact private maintenance lock")
    return proof
