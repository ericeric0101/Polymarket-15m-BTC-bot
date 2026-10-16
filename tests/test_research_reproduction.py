"""Reproduction harness: content-only digests and refusal of a tampered official cache."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("reproduce", Path("scripts/reproduce_research_iteration.py"))
reproduce = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reproduce)


def test_result_digest_ignores_only_generation_time(tmp_path):
    a, b, c = tmp_path / "a.json", tmp_path / "b.json", tmp_path / "c.json"
    a.write_text(json.dumps({"generated_at_utc": "x", "n": 1, "sub": {"generated_at_utc": "y", "v": [1, 2]}}))
    b.write_text(json.dumps({"sub": {"v": [1, 2], "generated_at_utc": "z"}, "n": 1, "generated_at_utc": "w"}))
    c.write_text(json.dumps({"generated_at_utc": "x", "n": 2, "sub": {"v": [1, 2]}}))
    assert reproduce.result_digest(a) == reproduce.result_digest(b) != reproduce.result_digest(c)


def test_official_cache_must_match_its_sidecar(tmp_path):
    cache = tmp_path / "official_resolutions_T.json"
    cache.write_text('{"markets": {}}')
    sidecar = cache.with_name(cache.name + ".sha256")
    sidecar.write_text(f"{hashlib.sha256(cache.read_bytes()).hexdigest()}  {cache.name}\n")
    assert reproduce.verify_official_cache(cache) == hashlib.sha256(cache.read_bytes()).hexdigest()
    cache.write_text('{"markets": {"m": "UP"}}')
    with pytest.raises(SystemExit):
        reproduce.verify_official_cache(cache)
    sidecar.unlink()
    with pytest.raises(SystemExit):
        reproduce.verify_official_cache(cache)
