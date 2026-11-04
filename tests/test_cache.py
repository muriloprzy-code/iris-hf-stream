import os

import hfstream.cache as cache


def test_user_setting_is_respected(tmp_path):
    env = {"HF_HOME": str(tmp_path)}
    assert cache.ensure_writable_cache(env) is None
    assert env == {"HF_HOME": str(tmp_path)}


def test_writable_default_is_left_alone(monkeypatch):
    monkeypatch.setattr(cache, "_writable", lambda folder: True)
    env = {}
    assert cache.ensure_writable_cache(env) is None
    assert env == {}


def test_unwritable_default_falls_back_to_temp(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "_writable", lambda folder: False)
    monkeypatch.setattr(cache.tempfile, "gettempdir", lambda: str(tmp_path))
    env = {}
    chosen = cache.ensure_writable_cache(env)
    assert chosen == os.path.join(str(tmp_path), cache.FALLBACK_NAME)
    assert env["HF_HOME"] == chosen and os.path.isdir(chosen)


def test_probe_detects_a_real_writable_folder(tmp_path):
    assert cache._writable(str(tmp_path / "sub"))
    assert os.listdir(tmp_path / "sub") == []  # the probe file is removed
