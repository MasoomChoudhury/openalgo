"""Export deployed sources and a review manifest. Does not certify or trade.

Run from the running OpenAlgo container after reviewing actual broker captures.
The execution plane must independently validate and record this manifest.
"""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil


def export(root, destination, *, server_version, reviewed_by, explanation):
    spec = importlib.util.spec_from_file_location(
        "kotak_source_times", root / "broker/kotak/streaming/source_times.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    destination.mkdir(parents=True, exist_ok=True)
    sources = []
    for filename in module.SOURCE_FILES:
        path = root / filename
        target = destination / path.name
        # Avoid overwriting evidence with potentially different deployed code.
        if target.exists():
            raise FileExistsError(f"Use a new export directory; evidence exists: {target.name}")
        shutil.copyfile(path, target)
        sources.append(
            {"path": path.name, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
        )
    manifest = {
        "audit_version": 2,
        "broker": "kotak",
        "server_version": server_version,
        "time_schema_version": module.TIME_SCHEMA_VERSION,
        "adapter_revision": module.adapter_revision(),
        "timestamp_field": "broker_price_time",
        "timestamp_fields": {"price": "broker_price_time", "depth": "broker_depth_time"},
        "clock_semantics": "broker_exchange",
        "reviewed_by": reviewed_by,
        "explanation": explanation,
        "exchanges": ["NSE_INDEX", "NFO"],
        "modes": [2, 3],
        "sources": sources,
    }
    (destination / "audit.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--server-version", required=True, help="Actual deployed commit/image identifier"
    )
    parser.add_argument("--reviewed-by", required=True)
    parser.add_argument(
        "--explanation",
        required=True,
        help="Observed units, semantics, index and depth paths, cache/reconnect checks",
    )
    args = parser.parse_args()
    export(
        Path(__file__).resolve().parents[1],
        args.output_dir,
        server_version=args.server_version,
        reviewed_by=args.reviewed_by,
        explanation=args.explanation,
    )
    print(f"Exported evidence to {args.output_dir}; no audit certificate was issued")
