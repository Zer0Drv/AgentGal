"""repository/player_persona 的单元测试：读写、六段解析、块构造。"""

import pytest

import repository.player_persona as pp


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """把 persona 读写隔离到 tmp，避免污染真实 CHARACTERS_DIR。"""
    monkeypatch.setattr(pp, "CHARACTERS_DIR", tmp_path)
    yield
    pp.clear()


def test_default_persona_has_six_sections():
    for tag in ["identity", "goal", "past", "habits", "reactions", "voice"]:
        assert f"<{tag}>" in pp.DEFAULT_PLAYER_PERSONA


def test_read_missing_returns_empty():
    assert pp.read_player_persona() == ""


def test_write_then_read_roundtrip():
    pp.write_player_persona("<identity>程序员</identity>")
    assert pp.read_player_persona() == "<identity>程序员</identity>"


def test_write_empty_is_noop():
    pp.write_player_persona("   ")
    assert not pp.persona_path().exists()


def test_clear_removes_file():
    pp.write_player_persona("<identity>x</identity>")
    assert pp.persona_path().exists()
    pp.clear()
    assert not pp.persona_path().exists()


def test_parse_sections():
    text = "<identity>程序员</identity>\n<goal>变强</goal>\n<voice>冷静</voice>"
    sec = pp.parse_sections(text)
    assert sec["identity"] == "程序员"
    assert sec["goal"] == "变强"
    assert sec["voice"] == "冷静"
    assert sec["habits"] == ""


def test_parse_nested_multiline():
    text = "<habits>\n早起\n冥想\n</habits>"
    assert pp.parse_sections(text)["habits"] == "早起\n冥想"


def test_parse_unknown_tag_ignored():
    text = "<mystery>foo</mystery><voice>巴</voice>"
    assert pp.parse_sections(text)["voice"] == "巴"


def test_build_block_includes_name_and_sections():
    block = pp.build_player_block("小明", "<identity>程序员</identity>")
    assert "小明" in block
    assert "程序员" in block
    assert block.startswith("<player>") and block.endswith("</player>")


def test_build_block_empty_returns_empty():
    assert pp.build_player_block("", "") == ""


def test_build_block_only_name():
    block = pp.build_player_block("小明", "")
    assert "display_name: 小明" in block
    assert "persona:" not in block
