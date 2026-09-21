"""report_pdf 字体解析单测（纯逻辑，不需要数据库）。

对应当前真实 API：``_FONT_ENV`` / ``_BUNDLED_FONT_DIR`` /
``_SYSTEM_FONT_CANDIDATES`` / ``_FONT_NAME`` 缓存 / ``_cjk_font()``。
"""

from app.services import report_pdf as rp


def test_font_env_first(monkeypatch, tmp_path):
    env_font = tmp_path / "env.ttf"
    env_font.write_bytes(b"x")
    monkeypatch.setenv(rp._FONT_ENV, str(env_font))
    assert rp._find_font_file() == env_font


def test_font_falls_back_when_env_invalid(monkeypatch, tmp_path):
    # 环境变量指向不存在的文件 → 依次回落 bundled / 系统候选；
    # 都不存在时返回 None（_cjk_font 会走 CID 回退）。
    monkeypatch.setenv(rp._FONT_ENV, str(tmp_path / "nope.ttf"))
    monkeypatch.setattr(rp, "_BUNDLED_FONT_DIR", tmp_path / "empty")
    monkeypatch.setattr(rp, "_SYSTEM_FONT_CANDIDATES", (str(tmp_path / "none.ttc"),))
    assert rp._find_font_file() is None


def test_font_bundled_dir_hits_ttc(monkeypatch, tmp_path):
    bundled = tmp_path / "fonts"
    bundled.mkdir()
    (bundled / "mycjk.ttc").write_bytes(b"ttc")
    monkeypatch.delenv(rp._FONT_ENV, raising=False)
    monkeypatch.setattr(rp, "_BUNDLED_FONT_DIR", bundled)
    monkeypatch.setattr(rp, "_SYSTEM_FONT_CANDIDATES", ())
    assert rp._find_font_file() == bundled / "mycjk.ttc"


def test_cjk_font_cid_fallback(monkeypatch):
    # 找不到字体文件时不抛异常，退回 CID 字体名 STSong-Light。
    monkeypatch.setattr(rp, "_find_font_file", lambda: None)
    monkeypatch.setattr(rp, "_FONT_NAME", None)
    assert rp._cjk_font() == rp._CID_FONT == "STSong-Light"
