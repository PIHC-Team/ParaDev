"""PIHC3 GUI compatibility and readability contracts for HoI4 1.19."""

from __future__ import annotations

import re
from pathlib import Path

from heavenbase.utils import load_txt

PIHC3_ROOT = Path("projects/PIHC3")
_INTERFACE_MATCHES = tuple((PIHC3_ROOT / "src/modules/interface").glob("INTERFACE_PIHC_INTERFACE - *"))
assert len(_INTERFACE_MATCHES) == 1
INTERFACE_ROOT = _INTERFACE_MATCHES[0] / "interface"


def _named_block(text: str, block_type: str, name: str) -> str:
    match = re.search(
        rf"{re.escape(block_type)}\s*=\s*\{{\s*name\s*=\s*\"{re.escape(name)}\"",
        text,
    )
    assert match is not None, f"missing {block_type} named {name}"

    start = text.index("{", match.start())
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unterminated {block_type} named {name}")


def test_country_selection_entries_supply_engine_required_new_content_controls() -> None:
    gui = load_txt(str(INTERFACE_ROOT / "frontendgamesetupview.gui"), encoding="utf-8")

    for entry_name in ("country_entry", "country_entry_medium", "country_entry_mini"):
        entry = _named_block(gui, "containerWindowType", entry_name)
        assert 'name = "new_content"' in entry
        assert 'spriteType = "GFX_unplayed_content_notification"' in entry


def test_single_bookmark_country_selection_exposes_main_menu_back() -> None:
    gui = load_txt(str(INTERFACE_ROOT / "frontendgamesetupview.gui"), encoding="utf-8")
    picker = _named_block(gui, "containerWindowType", "gamesetup_interesting_countries_window")
    picker_back = _named_block(picker, "ButtonType", "back_button")
    setup = _named_block(gui, "containerWindowType", "frontendgamesetupview")
    bottom = _named_block(setup, "containerWindowType", "bottom")
    main_menu_back = _named_block(bottom, "ButtonType", "back_button")

    assert "hide = yes" in picker_back
    assert 'shortcut = "ESCAPE"' not in picker_back
    assert "position = { x=0 y =-100 }" in bottom
    assert "show_position = { x=0 y =-100 }" in bottom
    assert 'buttonText = "MAIN_MENU_BACK"' in main_menu_back
    assert 'shortcut = "ESCAPE"' in main_menu_back


def test_shared_inverted_font_remains_unchanged_for_dark_panels() -> None:
    core = load_txt(str(INTERFACE_ROOT / "core.gfx"), encoding="utf-8")
    welcome = load_txt(str(INTERFACE_ROOT / "PIHC_welcome_screen.gui"), encoding="utf-8")
    font = _named_block(core, "bitmapfont", "hoi4_typewriter16_inverted")

    assert "color = 0xffffffff" in font
    assert "G = { 86 172 91 }" in font
    assert "R = { 222 86 70 }" in font
    assert "Y = { 238 201 35 }" in font
    assert "H = { 238 201 35 }" in font
    assert welcome.count("font = hoi4_typewriter16_inverted") == 4
    assert "pihc_equipment_designer_stat" not in welcome


def test_equipment_designer_stat_font_uses_scoped_dark_colors() -> None:
    core = load_txt(str(INTERFACE_ROOT / "core.gfx"), encoding="utf-8")
    gui = load_txt(str(INTERFACE_ROOT / "equipmentupgradedesignerwindow.gui"), encoding="utf-8")
    font = _named_block(core, "bitmapfont", "pihc_equipment_designer_stat")
    entry = _named_block(gui, "containerWindowType", "equipment_designer_stat_entry")

    assert "color = 0xff000000" in font
    assert "G = { 35 125 50 }" in font
    assert "R = { 176 45 35 }" in font
    assert "Y = { 166 92 0 }" in font
    assert "H = { 166 92 0 }" in font
    assert entry.count('font = "pihc_equipment_designer_stat"') == 2
    assert "hoi4_typewriter16_inverted" not in entry


def test_colored_event_font_uses_readable_grey() -> None:
    core = load_txt(str(INTERFACE_ROOT / "core.gfx"), encoding="utf-8")
    font = _named_block(core, "bitmapfont", "hoi4_typewriter16_colored")

    assert "g = { 128 128 128 }" in font


def test_blue_localization_color_is_lighter_everywhere() -> None:
    core = load_txt(str(INTERFACE_ROOT / "core.gfx"), encoding="utf-8")
    loading = load_txt(str(INTERFACE_ROOT / "load_screen_font.gfx"), encoding="utf-8")
    event_font = _named_block(core, "bitmapfont", "hoi4_typewriter16_colored")

    assert "B = { 81 112 243 }  # Blue" in core
    assert "B = { 81 112 243 }" in event_font
    assert "B = { 81 112 243 }  # Blue" in loading
    assert "B = { 0 0 255 }" not in core
    assert "B = { 0 0 255 }" not in loading
