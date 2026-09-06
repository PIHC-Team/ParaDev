"""Regression coverage for the independent compiled-content integration gate."""

import importlib.util
from pathlib import Path

SCRIPT = Path("projects/PIHC3/scripts/check_integration.py")
spec = importlib.util.spec_from_file_location("pihc_integration", SCRIPT)
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def test_tooltip_scan_tracks_dependencies_without_country_prefix_filter(tmp_path):
    path = tmp_path / "common/decisions/imported.txt"
    path.parent.mkdir(parents=True)
    path.write_text(
        'custom_effect_tooltip = C15_NEW_TOOLTIP\ncustom_modifier_tooltip = C44_NEW_TOOLTIP\ntooltip = "Human readable sentence"\n# tooltip = COMMENTED\ntooltip = "$DYNAMIC$"\n'
    )
    assert set(gate.tooltip_references(tmp_path)) == {"C15_NEW_TOOLTIP", "C44_NEW_TOOLTIP"}


def test_missing_translation_detected_even_when_other_language_exists(tmp_path):
    mod = tmp_path / "mod"
    reference = tmp_path / "game"
    (mod / "common").mkdir(parents=True)
    (mod / "common/test.txt").write_text("custom_effect_tooltip = CROSS_COUNTRY_DEPENDENCY")
    for language in ("english", "simp_chinese", "russian"):
        folder = mod / "localisation" / language
        folder.mkdir(parents=True)
        for table in ("state_names", "victory_points"):
            (folder / f"{table}_l_{language}.yml").write_text(f"l_{language}:\n")
    (mod / "localisation/english/tooltip_l_english.yml").write_text('l_english:\n CROSS_COUNTRY_DEPENDENCY:0 "English"\n')
    errors = gate.check(mod, reference)["errors"]
    assert len(errors) == 1
    assert "simp_chinese" in errors[0] and "CROSS_COUNTRY_DEPENDENCY" in errors[0]
    folder = reference / "localisation/simp_chinese"
    folder.mkdir(parents=True)
    (folder / "tooltip_l_simp_chinese.yml").write_text('l_simp_chinese:\n CROSS_COUNTRY_DEPENDENCY:0 "中文"\n')
    assert gate.check(mod, reference)["errors"] == []


def test_correct_translation_at_wrong_path_still_fails(tmp_path):
    folder = tmp_path / "localisation/replace/simp_chinese"
    folder.mkdir(parents=True)
    (folder / "state_names_l_simp_chinese.yml").write_text('l_simp_chinese:\n STATE_4:0 "驼丁汉"\n')
    errors = gate.check(tmp_path, tmp_path / "game")["errors"]
    assert any("Missing direct" in e and "state_names_l_simp_chinese" in e for e in errors)
    assert any("Obsolete duplicate" in e for e in errors)
