from __future__ import annotations

from hashlib import sha256
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml
from heavenbase.utils import (
    copy_file,
    load_json,
    save_txt,
    save_yaml,
    sha256hash,
)

if TYPE_CHECKING:
    from paradev.build import BuildResult

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
sys.dont_write_bytecode = True
pytestmark = [pytest.mark.integration, pytest.mark.slow]


class _LayoutAwarePath(type(Path())):
    """Resolve stable PIHC3 ids through human-readable folder suffixes."""

    def __truediv__(self, key: str | os.PathLike[str]) -> _LayoutAwarePath:
        candidate = super().__truediv__(key)
        if candidate.exists():
            return candidate
        parts = candidate.parts
        for index, part in enumerate(parts):
            if part not in {"modules", "collections"} or index + 2 >= len(parts):
                continue
            family_root = type(self)(candidate.anchor).joinpath(*parts[1 : index + 2])
            object_id = parts[index + 2]
            matches = tuple(family_root.glob(f"{object_id} - *"))
            if len(matches) == 1:
                resolved = matches[0].joinpath(*parts[index + 3 :])
                if resolved.exists():
                    return resolved
                if resolved.name == "meta.yaml":
                    hidden = Path(resolved.parent) / ".paradev/meta.yaml"
                    if hidden.is_file():
                        return type(self)(hidden)
                return resolved
        if candidate.name == "meta.yaml":
            hidden = Path(candidate.parent) / ".paradev/meta.yaml"
            if hidden.is_file():
                return type(self)(hidden)
        return candidate


PIHC3_ROOT = _LayoutAwarePath(os.environ.get("PARADEV_PIHC3_ROOT", "projects/PIHC3")).expanduser().resolve()
PIHC3_FULL_BUILD_GROUP = pytest.mark.xdist_group("pihc3_full_build")

PIHC3_CONFIG_ONLY_DIRECTORY_TEMPLATES = (
    ("pihc3:idea_category/basic", "idea_category"),
    ("pihc3:focus/basic", "focus"),
    ("pihc3:doctrine/grand-basic", "doctrine"),
    ("pihc3:doctrine/subdoctrine-basic", "doctrine"),
    ("pihc3:opinion_modifier/basic", "opinion_modifier"),
    ("pihc3:special_project/basic", "special_project"),
    ("pihc3:special_project_reward/basic", "special_project_reward"),
    ("pihc3:balance_of_power/basic", "balance_of_power"),
    ("pihc3:intelligence_agency/basic", "intelligence_agency"),
    ("pihc3:achievement/basic", "achievement"),
    ("pihc3:equipment/basic", "equipment"),
    ("pihc3:equipment_module_category/basic", "equipment_module_category"),
    ("pihc3:equipment_module/basic", "equipment_module"),
    ("pihc3:country/basic", "country"),
    ("pihc3:texticon/basic", "texticon"),
    ("pihc3:ui/basic", "ui"),
)

PIHC3_COMPILER_TEMPLATE_SETTINGS = (
    (
        "pihc3:entity/basic",
        {"animation_file", "asset_kind", "authoring_contract", "mesh_file"},
    ),
)


def _pihc3_manifest(project_root: Path = PIHC3_ROOT) -> dict[str, object]:
    """Return the effective PIHC3 manifest using ParaDev's overlay order."""

    manifest: dict[str, object] = {}
    for filename in (".paradev.yaml", "paradev.yaml"):
        loaded = yaml.safe_load((project_root / filename).read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        manifest.update(loaded)
    return manifest


def test_pihc3_family_visibility_keeps_low_level_builds_out_of_navigation() -> None:
    from paradev.sdk import Project

    project = Project.load(PIHC3_ROOT)
    family_specs = {str(spec.family): spec for spec in project._build_registry(profile=project.game).families}
    browser_families = project.browser_summary()["families"]
    hidden_specs = {family for family, spec in family_specs.items() if not bool(getattr(spec, "visible", True))}

    assert hidden_specs == {
        "ai_config",
        "audio",
        "common_data",
        "country_history",
        "defines",
        "faction_rule",
        "font",
        "game_asset",
        "general_history",
        "interface",
        "leader_trait",
        "loading_screen",
        "localization",
        "modifier_definition",
        "mod_descriptor",
        "peace_conference",
        "raid",
        "scripted_localisation",
        "unit",
        "unit_leader",
    }
    assert not any(family.endswith("_component") for family in family_specs)
    assert len(browser_families) == 71
    assert sum(row["visible"] is False for row in browser_families) == 20
    assert sum(row["visible"] is True for row in browser_families) == 51


def test_pihc3_basic_idea_template_uses_minimal_metadata_and_cic_input() -> None:
    from paradev.sdk import Project
    from paradev.sdk.templates import _module_scaffold_draft, template_index

    project = Project.load(PIHC3_ROOT)
    templates = template_index(project.game, project._authoring_template_specs())
    template = templates["pihc3:idea/basic"]
    registry = project._build_registry(profile=project.game)

    plan, rendered_files, _path_diagnostics = _module_scaffold_draft(
        project_id=project.project_id,
        project_root=project.root,
        source_root=project.source_roots[0],
        template=template,
        object_id="IDEA_PARADEV_BATCH_TEMPLATE_TEST",
        values={
            "title": "Batch Template Test",
            "description": "Generated without legacy metadata.",
            "cic": 0.02,
        },
        source_slots=registry.source_slots_for("idea"),
    )
    content = {str(row["relative_module_path"]): str(row["content"]) for row in rendered_files}

    assert plan["blocked"] is False
    assert plan["values"]["cic"] == "0.02"
    assert set(content) == {"def.txt", "main.loc"}
    assert "picture = IDEA_PARADEV_BATCH_TEMPLATE_TEST" in content["def.txt"]
    assert "industrial_capacity_factory = 0.02" in content["def.txt"]
    assert "[en.IDEA_PARADEV_BATCH_TEMPLATE_TEST_desc]" in content["main.loc"]


def test_pihc3_project_preferred_language_drives_every_create_surface() -> None:
    from paradev.sdk import Project

    project = Project.load(PIHC3_ROOT)
    catalog = project.templates(authoring_ready=True)
    language_defaults = {row["args"]["language"]["default"] for row in catalog["templates"] if "language" in row["args"]}
    plan = project.create_module(
        "idea",
        "IDEA_PARADEV_PREFERRED_LANGUAGE",
        values={"title": "首选语言"},
        write=False,
    )
    explicit = project.create_module(
        "idea",
        "IDEA_PARADEV_EXPLICIT_LANGUAGE",
        values={"title": "Explicit Language", "language": "en"},
        write=False,
    )
    batch = project.create_modules(
        (
            {
                "family": "idea",
                "object_id": "IDEA_PARADEV_BATCH_LANGUAGE",
                "values": {"title": "批量语言"},
            },
        )
    )

    assert project.preferred_language == "zh"
    assert project.to_view()["preferred_language"] == "zh"
    assert catalog["preferred_language"] == "zh"
    assert language_defaults == {"zh"}
    assert plan["values"]["language"] == "zh"
    assert plan["folder_name"] == "IDEA_PARADEV_PREFERRED_LANGUAGE - 首选语言"
    assert explicit["values"]["language"] == "en"
    assert batch["modules"][0]["values"]["language"] == "zh"


def test_pihc3_basic_technology_template_plans_titled_directory_with_minimal_metadata() -> None:
    from paradev.sdk import Project
    from paradev.sdk.templates import _module_scaffold_draft, template_index

    project = Project.load(PIHC3_ROOT)
    template_view = project.templates(template_id="pihc3:technology/basic")["templates"][0]
    object_id = "TECHNOLOGY_PARADEV_DIRECTORY_CONTRACT"
    title = "Directory Contract Technology"
    module_root = project.source_roots[0] / "modules/technology" / f"{object_id} - {title}"

    plan = project.create_module(
        "pihc3:technology/basic",
        object_id,
        values={"title": title},
        write=False,
    )
    templates = template_index(project.game, project._authoring_template_specs())
    template = templates["pihc3:technology/basic"]
    registry = project._build_registry(profile=project.game)
    _draft, rendered_files, _path_diagnostics = _module_scaffold_draft(
        project_id=project.project_id,
        project_root=project.root,
        source_root=project.source_roots[0],
        template=template,
        object_id=object_id,
        values={"title": title},
        source_slots=registry.source_slots_for("technology"),
    )
    content = {str(row["relative_module_path"]): str(row["content"]) for row in rendered_files}

    assert template_view["directory"] == "{object_id} - {title}"
    assert plan["blocked"] is False
    assert plan["written"] is False
    assert plan["folder_name"] == f"{object_id} - {title}"
    assert plan["object_id"] == object_id
    assert plan["module_id"] == f"technology/{object_id}"
    assert plan["root"] == str(module_root)
    assert plan["authoring_plan"]["authoring_path"]["root"] == str(module_root)
    assert plan["files"][0]["relative_path"] == (f"src/modules/technology/{object_id} - {title}/def.txt")
    assert set(content) == {"def.txt", "main.loc"}
    assert f"[en.{object_id}_desc]\n" in content["main.loc"]
    assert plan["values"]["language"] == "zh"
    assert not module_root.exists()


def test_pihc3_visible_browser_families_all_have_authoring_templates() -> None:
    from paradev.sdk import Project

    project = Project.load(PIHC3_ROOT)
    visible_families = {row["family"] for row in project.browser_summary()["families"] if row["visible"]}
    authoring_families = {row["family"] for row in project.templates()["templates"] if row["authoring_ready"]}

    assert len(visible_families) == 51
    assert authoring_families == visible_families


def test_pihc3_focus_tree_and_remaining_family_templates_are_minimal_and_usable() -> None:
    from paradev.sdk import Project
    from paradev.sdk.templates import _module_scaffold_draft, template_index

    project = Project.load(PIHC3_ROOT)
    templates = template_index(project.game, project._authoring_template_specs())
    registry = project._build_registry(profile=project.game)
    cases = (
        (
            "pihc3:focus/basic",
            "FOCUS_PARADEV_START",
            {
                "title": "ParaDev Focus",
                "tree": "PARADEV_FOCUS_TREE",
            },
            {".paradev/meta.yaml", "def.txt", "main.loc"},
        ),
        (
            "pihc3:equipment_module_category/basic",
            "paradev_module_category",
            {"title": "ParaDev Module Category"},
            {"main.loc"},
        ),
        (
            "pihc3:special_project_reward/basic",
            "PARADEV_SPECIAL_REWARD",
            {"title": "ParaDev Special Reward"},
            {"def.txt", "main.loc"},
        ),
    )
    content_by_template: dict[str, dict[str, str]] = {}

    for template_id, object_id, values, expected_files in cases:
        template = templates[template_id]
        plan, rendered_files, _path_diagnostics = _module_scaffold_draft(
            project_id=project.project_id,
            project_root=project.root,
            source_root=project.source_roots[0],
            template=template,
            object_id=object_id,
            values=values,
            source_slots=registry.source_slots_for(template.family),
        )
        content = {str(row["relative_module_path"]): str(row["content"]) for row in rendered_files}

        assert plan["blocked"] is False
        assert set(content) == expected_files
        if template_id == "pihc3:focus/basic":
            assert content[".paradev/meta.yaml"] == "collection: PARADEV_FOCUS_TREE\n"
        content_by_template[template_id] = content

    focus_content = content_by_template["pihc3:focus/basic"]
    assert "id = FOCUS_PARADEV_START" in focus_content["def.txt"]
    assert "GFX_goal_generic_construct_civ_factory" in focus_content["def.txt"]
    assert focus_content["main.loc"] == "[zh.FOCUS_PARADEV_START]\nParaDev Focus\n\n" "[zh.FOCUS_PARADEV_START_desc]\n\n"

    category_content = content_by_template["pihc3:equipment_module_category/basic"]
    assert "[en.EQ_MOD_CAT_paradev_module_category_TITLE]" in category_content["main.loc"]

    reward_content = content_by_template["pihc3:special_project_reward/basic"]
    assert "country_effects = { add_political_power = 50 }" in reward_content["def.txt"]
    assert "[en.PARADEV_SPECIAL_REWARD_o0]" in reward_content["main.loc"]


def test_pihc3_new_focus_tree_workflow_creates_node_and_compiles_portably(
    tmp_path: Path,
) -> None:
    from paradev.sdk import Project

    project_root = tmp_path / "PIHC3"
    project_root.mkdir()
    manifest_text = (PIHC3_ROOT / "paradev.yaml").read_text(encoding="utf-8")
    shutil.copy2(PIHC3_ROOT / ".paradev.yaml", project_root / ".paradev.yaml")
    save_txt(
        f"{manifest_text.rstrip()}\noutput_root: build/mod\n",
        project_root / "paradev.yaml",
    )
    shutil.copytree(PIHC3_ROOT / "extensions", project_root / "extensions")
    (project_root / "src").mkdir()
    collection_root = project_root / "src/collections/focus/PARADEV_FOCUS_TREE - ParaDev Focus Tree"
    collection_root.mkdir(parents=True)
    save_yaml(
        {"title": "ParaDev Focus Tree", "members": []},
        str(collection_root / "meta.yaml"),
    )
    save_txt(
        "focus_tree = {\n" "  id = PARADEV_FOCUS_TREE\n" "  default = no\n" "  country = { factor = 0 modifier = { add = 100 tag = PDT } }\n" "}\n",
        str(collection_root / "def.txt"),
    )
    project = Project.load(project_root)
    requests = (
        {
            "template_id": "pihc3:focus/basic",
            "object_id": "FOCUS_PARADEV_START",
            "values": {
                "title": "A New Beginning",
                "description": "Created from the modular Focus template.",
                "tree": "PARADEV_FOCUS_TREE",
                "language": "en",
            },
        },
        {
            "template_id": "pihc3:equipment_module_category/basic",
            "object_id": "paradev_module_category",
            "values": {"title": "ParaDev Module Category"},
        },
        {
            "template_id": "pihc3:special_project_reward/basic",
            "object_id": "PARADEV_SPECIAL_REWARD",
            "values": {"title": "ParaDev Special Reward"},
        },
    )

    plan = project.create_modules(requests)
    assert plan["blocked"] is False
    applied = project.create_modules(
        requests,
        write=True,
        plan_hash=str(plan["plan_hash"]),
    )

    assert applied["blocked"] is False
    assert applied["written"] is True
    assert [row["module_id"] for row in applied["modules"]] == [
        "focus/FOCUS_PARADEV_START",
        "equipment_module_category/paradev_module_category",
        "special_project_reward/PARADEV_SPECIAL_REWARD",
    ]
    category_item = project.browser(family="equipment_module_category")["items"][0]
    assert category_item["image_targets"] == [
        {
            "slot": "icon",
            "slot_kinds": ["copy"],
            "name": "icon.png",
            "path": str(project_root / "src/modules/equipment_module_category" / "paradev_module_category - ParaDev Module Category" / "icon.png"),
            "relative_path": ("src/modules/equipment_module_category/" "paradev_module_category - ParaDev Module Category/icon.png"),
            "extension": "png",
            "exists": False,
        }
    ]

    projection = project.module_diagram("focus_tree")
    tree = next(row for row in projection["trees"] if row["id"] == "PARADEV_FOCUS_TREE")
    node_request = {
        "tree_id": "PARADEV_FOCUS_TREE",
        "focus_id": "FOCUS_PARADEV_NEXT",
        "x": 1,
        "y": 1,
        "title": "The Next Step",
        "description": "Created through the ParaDev tree editor contract.",
        "prerequisite_id": "FOCUS_PARADEV_START",
    }
    node_plan = project.edit_module_diagram("focus_tree", node_intents=[node_request])
    assert node_plan["blocked"] is False
    assert node_plan["intent"]["tree_source_revision"] == tree["source_revision"]
    node_applied = project.edit_module_diagram(
        "focus_tree",
        node_intents=[node_request],
        write=True,
        plan_hash=str(node_plan["plan_hash"]),
    )

    assert node_applied["blocked"] is False
    assert node_applied["written"] is True

    for family in (
        "focus",
        "equipment_module_category",
        "special_project_reward",
    ):
        result = project.build(family=family, emit_artifacts=True)

        assert result.blocked is False
        assert result.dry_run is False
        assert len(result.modules) == (2 if family == "focus" else 1)

    refreshed = project.module_diagram("focus_tree")
    node = next(row for row in refreshed["nodes"] if row["id"] == "FOCUS_PARADEV_NEXT")
    assert node["localized_titles"] == {"l_simp_chinese": "The Next Step"}
    assert (project_root / "build/mod/common/national_focus/PARADEV_FOCUS_TREE.txt").is_file()


def test_pihc3_config_only_directory_adoption_preserves_logical_identity() -> None:
    from paradev.sdk import Project

    project = Project.load(PIHC3_ROOT)
    template_views = {row["id"]: row for row in project.templates()["templates"] if str(row["id"]).startswith("pihc3:")}
    titled_directory_templates = {
        template_id: row["family"]
        for template_id, row in template_views.items()
        if row.get("kind") == "module" and row.get("directory") == "{object_id} - {title}"
    }

    assert len(titled_directory_templates) == 52
    assert titled_directory_templates.items() >= dict(PIHC3_CONFIG_ONLY_DIRECTORY_TEMPLATES).items()
    assert titled_directory_templates["pihc3:technology/basic"] == "technology"

    for template_id, family in PIHC3_CONFIG_ONLY_DIRECTORY_TEMPLATES:
        object_id = f"PARADEV_DIRECTORY_CONTRACT_{family.upper()}"
        title = f"Directory Contract {family.replace('_', ' ').title()}"
        folder_name = f"{object_id} - {title}"
        module_root = project.source_roots[0] / "modules" / family / folder_name
        values = {"title": title}
        if template_id == "pihc3:focus/basic":
            values["tree"] = "C08_MAIN"

        assert not module_root.exists()
        plan = project.create_module(
            template_id,
            object_id,
            values=values,
            write=False,
        )

        assert template_views[template_id]["directory"] == "{object_id} - {title}"
        assert plan["blocked"] is False
        assert plan["written"] is False
        assert plan["folder_name"] == folder_name
        assert plan["object_id"] == object_id
        assert plan["module_id"] == f"{family}/{object_id}"
        assert plan["root"] == str(module_root)
        assert plan["authoring_plan"]["authoring_path"]["root"] == str(module_root)
        assert all(str(row["relative_path"]).startswith(f"src/modules/{family}/{folder_name}/") for row in plan["files"])
        assert not module_root.exists()


def test_pihc3_project_templates_keep_only_authored_and_compiler_metadata() -> None:
    from paradev.sdk import Project
    from paradev.sdk.templates import _module_scaffold_draft, template_index

    project = Project.load(PIHC3_ROOT)
    templates = template_index(project.game, project._authoring_template_specs())
    registry = project._build_registry(profile=project.game)
    project_template_ids = {row["id"] for row in project.templates()["templates"] if row["source"] == "project" and row.get("kind") == "module"}
    expected_settings = dict(PIHC3_COMPILER_TEMPLATE_SETTINGS)
    observed_settings: dict[str, set[str]] = {}
    observed_collections: set[str] = set()

    for index, template_id in enumerate(sorted(project_template_ids)):
        template = templates[template_id]
        values: dict[str, object] = {"title": f"Metadata Contract {index}"}
        if template_id == "pihc3:state_lore/basic":
            values["state_id"] = 217
        if template_id == "pihc3:focus/basic":
            values["tree"] = "C08_MAIN"
        plan, rendered_files, _path_diagnostics = _module_scaffold_draft(
            project_id=project.project_id,
            project_root=project.root,
            source_root=project.source_roots[0],
            template=template,
            object_id=f"PARADEV_METADATA_CONTRACT_{index}",
            values=values,
            source_slots=registry.source_slots_for(template.family),
        )
        assert plan["blocked"] is False
        visible_files = [row for row in rendered_files if str(row["relative_module_path"]) == "meta.yaml"]
        hidden_files = [row for row in rendered_files if str(row["relative_module_path"]) == ".paradev/meta.yaml"]
        assert len(visible_files) <= 1
        assert len(hidden_files) <= 1

        visible_metadata = yaml.safe_load(str(visible_files[0]["content"])) if visible_files else {}
        hidden_metadata = yaml.safe_load(str(hidden_files[0]["content"])) if hidden_files else {}
        visible_metadata = visible_metadata or {}
        hidden_metadata = hidden_metadata or {}
        settings = hidden_metadata.get("settings") or {}
        rendered_metadata = "\n".join(str(row["content"]) for row in (*visible_files, *hidden_files))

        assert set(visible_metadata) <= {"inactive", "comment"}
        assert set(hidden_metadata) <= {"collection", "settings"}
        assert "title" not in visible_metadata
        assert "type" not in visible_metadata
        assert "tags" not in visible_metadata
        assert "authored/templates/" not in rendered_metadata
        assert "legacy_source" not in settings
        if "collection" in hidden_metadata:
            observed_collections.add(template_id)
        if settings:
            observed_settings[template_id] = set(settings)

    assert observed_settings == expected_settings
    assert observed_collections == {"pihc3:focus/basic"}


PIHC3_COMPILED_UNIT_PATHS = (
    "common/units/air.txt",
    "common/units/battlecruiser.txt",
    "common/units/battleship.txt",
    "common/units/cannon.txt",
    "common/units/carrier.txt",
    "common/units/destroyer.txt",
    "common/units/engineer.txt",
    "common/units/equipment/convoys.txt",
    "common/units/equipment/ship_hull_carrier.txt",
    "common/units/equipment/ship_hull_cruiser.txt",
    "common/units/equipment/ship_hull_heavy.txt",
    "common/units/equipment/ship_hull_light.txt",
    "common/units/equipment/ship_hull_submarine.txt",
    "common/units/equipment/trains.txt",
    "common/units/field_hospital.txt",
    "common/units/heavy_cruiser.txt",
    "common/units/infantry.txt",
    "common/units/light_cruiser.txt",
    "common/units/logistics.txt",
    "common/units/maintenance.txt",
    "common/units/military_police.txt",
    "common/units/signal.txt",
    "common/units/submarine.txt",
    "common/units/tank.txt",
)

PIHC3_NATIVE_UNIT_MODIFIER_IDS = (
    "modifier_army_sub_unit_military_police_attack_factor",
    "modifier_army_sub_unit_military_police_defence_factor",
    "modifier_army_sub_unit_military_police_speed_factor",
    "modifier_army_sub_unit_military_police_max_org_factor",
    "modifier_army_sub_unit_infantry_magical_attack_factor",
    "modifier_army_sub_unit_category_special_forces_max_org_factor",
)

PIHC3_REMOVED_INVALID_UNIT_MODIFIER_IDS = frozenset(
    {
        "modifier_army_sub_unit_armored_car_attack_factor",
        "modifier_army_sub_unit_armored_car_defence_factor",
        "modifier_army_sub_unit_armored_car_max_org_factor",
        "modifier_army_sub_unit_armored_car_recon_attack_factor",
        "modifier_army_sub_unit_armored_car_recon_defence_factor",
        "modifier_army_sub_unit_armored_car_recon_max_org_factor",
        "modifier_army_sub_unit_armored_car_recon_speed_factor",
        "modifier_army_sub_unit_armored_car_speed_factor",
        "modifier_army_sub_unit_blackshirt_assault_battalion_attack_factor",
        "modifier_army_sub_unit_blackshirt_assault_battalion_defence_factor",
        "modifier_army_sub_unit_blackshirt_assault_battalion_max_org_factor",
        "modifier_army_sub_unit_blackshirt_assault_battalion_speed_factor",
        "modifier_army_sub_unit_camelry_attack_factor",
        "modifier_army_sub_unit_camelry_defence_factor",
        "modifier_army_sub_unit_camelry_speed_factor",
        "modifier_army_sub_unit_cavalry_attack_factor",
        "modifier_army_sub_unit_cavalry_defence_factor",
        "modifier_army_sub_unit_cavalry_speed_factor",
        "modifier_army_sub_unit_infantry_attack_factor",
        "modifier_army_sub_unit_infantry_defence_factor",
        "modifier_army_sub_unit_infantry_speed_factor",
        "modifier_army_sub_unit_irregular_infantry_attack_factor",
        "modifier_army_sub_unit_irregular_infantry_defence_factor",
        "modifier_army_sub_unit_irregular_infantry_max_org_factor",
        "modifier_army_sub_unit_irregular_infantry_speed_factor",
        "modifier_army_sub_unit_light_tank_recon_attack_factor",
        "modifier_army_sub_unit_light_tank_recon_defence_factor",
        "modifier_army_sub_unit_light_tank_recon_max_org_factor",
        "modifier_army_sub_unit_light_tank_recon_speed_factor",
        "modifier_army_sub_unit_long_range_patrol_support_attack_factor",
        "modifier_army_sub_unit_long_range_patrol_support_defence_factor",
        "modifier_army_sub_unit_marine_attack_factor",
        "modifier_army_sub_unit_marine_defence_factor",
        "modifier_army_sub_unit_marine_max_org_factor",
        "modifier_army_sub_unit_marine_speed_factor",
        "modifier_army_sub_unit_militia_attack_factor",
        "modifier_army_sub_unit_militia_defence_factor",
        "modifier_army_sub_unit_militia_max_org_factor",
        "modifier_army_sub_unit_militia_org_recovery_cap_factor",
        "modifier_army_sub_unit_militia_speed_factor",
        "modifier_army_sub_unit_mountaineers_attack_factor",
        "modifier_army_sub_unit_mountaineers_defence_factor",
        "modifier_army_sub_unit_mountaineers_max_org_factor",
        "modifier_army_sub_unit_mountaineers_speed_factor",
        "modifier_army_sub_unit_paratrooper_attack_factor",
        "modifier_army_sub_unit_paratrooper_defence_factor",
        "modifier_army_sub_unit_paratrooper_max_org_factor",
        "modifier_army_sub_unit_paratrooper_speed_factor",
    }
)

PIHC3_SUPPORT_MAPARROW_PATHS = [
    "gfx/maparrows/ag_default_mask.dds",
    "gfx/maparrows/ag_default_pattern.dds",
    "gfx/maparrows/ag_default_unassigned_mask.dds",
    "gfx/maparrows/ag_defensive_line.dds",
    "gfx/maparrows/ag_defensive_line_mask.dds",
    "gfx/maparrows/ag_defensive_line_unassigned_mask.dds",
    "gfx/maparrows/ag_offensive_line.dds",
    "gfx/maparrows/ag_offensive_line_mask.dds",
    "gfx/maparrows/ag_offensive_line_unassigned_mask.dds",
    "gfx/maparrows/air_mission.dds",
    "gfx/maparrows/air_mission_mask.dds",
    "gfx/maparrows/blitz_line.dds",
    "gfx/maparrows/blitz_line_mask.dds",
    "gfx/maparrows/blitz_line_unassigned_mask.dds",
    "gfx/maparrows/blitz_mask.dds",
    "gfx/maparrows/blitz_pattern.dds",
    "gfx/maparrows/blitz_unassigned_mask.dds",
    "gfx/maparrows/default_mask.dds",
    "gfx/maparrows/default_pattern.dds",
    "gfx/maparrows/default_unassigned_mask.dds",
    "gfx/maparrows/defensive_line.dds",
    "gfx/maparrows/defensive_line_mask.dds",
    "gfx/maparrows/defensive_line_unassigned_mask.dds",
    "gfx/maparrows/maparrows.txt",
    "gfx/maparrows/offensive_line.dds",
    "gfx/maparrows/offensive_line_mask.dds",
    "gfx/maparrows/offensive_line_unassigned_mask.dds",
    "gfx/maparrows/x.dds",
]

PIHC3_SUPPORT_PARTICLE_PATHS = [
    "gfx/particles/entities/buildings_effects.gfx",
    "gfx/particles/entities/map_lights.asset",
    "gfx/particles/entities/mirror.gfx",
    "gfx/particles/entities/particles.gfx",
    "gfx/particles/environment/dustcloud.asset",
    "gfx/particles/environment/lightning_storm.NUDGE",
    "gfx/particles/environment/lightning_storm.asset",
    "gfx/particles/environment/lightning_storm_clouds.NUDGE",
    "gfx/particles/environment/lightning_storm_clouds.asset",
    "gfx/particles/environment/lightning_storm_small.NUDGE",
    "gfx/particles/environment/lightning_storm_small.asset",
    "gfx/particles/environment/lightning_storm_small_clouds.NUDGE",
    "gfx/particles/environment/lightning_storm_small_clouds.asset",
    "gfx/particles/environment/mirror.asset",
    "gfx/particles/environment/nuke.asset",
    "gfx/particles/environment/nuke_top.asset",
    "gfx/particles/environment/pihc_bgfx_fog.asset",
    "gfx/particles/environment/rain.asset",
    "gfx/particles/environment/rain_clouds.asset",
    "gfx/particles/environment/rain_small.asset",
    "gfx/particles/environment/rain_small_clouds.asset",
    "gfx/particles/environment/sandstorm.asset",
    "gfx/particles/environment/snow.asset",
    "gfx/particles/environment/snow_clouds.asset",
    "gfx/particles/environment/snow_small.asset",
    "gfx/particles/environment/snow_small_clouds.asset",
    "gfx/particles/environment/snow_storm.asset",
    "gfx/particles/environment/snow_storm_clouds.asset",
    "gfx/particles/environment/snow_storm_small.asset",
    "gfx/particles/environment/snow_storm_small_clouds.asset",
]

PIHC3_SUPPORT_MAP_TERRAIN_PATHS = [
    "map/terrain/RiverSurface_diffuse_0.dds",
    "map/terrain/RiverSurface_diffuse_1.dds",
    "map/terrain/RiverSurface_diffuse_2.dds",
    "map/terrain/RiverSurface_masks.dds",
    "map/terrain/RiverSurface_normal_0.dds",
    "map/terrain/RiverSurface_normal_1.dds",
    "map/terrain/RiverSurface_normal_2.dds",
    "map/terrain/Tree_season.bmp",
    "map/terrain/Tree_tint.bmp",
    "map/terrain/atlas0.dds",
    "map/terrain/atlas1.dds",
    "map/terrain/atlas2.dds",
    "map/terrain/atlas_normal0.dds",
    "map/terrain/atlas_normal1.dds",
    "map/terrain/atlas_normal2.dds",
    "map/terrain/border_country_0.dds",
    "map/terrain/border_country_1.dds",
    "map/terrain/border_country_2.dds",
    "map/terrain/border_impassable_0.dds",
    "map/terrain/border_impassable_1.dds",
    "map/terrain/border_impassable_2.dds",
    "map/terrain/border_province_0.dds",
    "map/terrain/border_province_1.dds",
    "map/terrain/border_province_2.dds",
    "map/terrain/border_sea_0.dds",
    "map/terrain/border_sea_1.dds",
    "map/terrain/border_sea_2.dds",
    "map/terrain/border_sea_region_0.dds",
    "map/terrain/border_sea_region_1.dds",
    "map/terrain/border_sea_region_2.dds",
    "map/terrain/border_state_0.dds",
    "map/terrain/border_state_1.dds",
    "map/terrain/border_state_2.dds",
    "map/terrain/citylights_rgb_snowmask_a_0.dds",
    "map/terrain/citylights_rgb_snowmask_a_1.dds",
    "map/terrain/citylights_rgb_snowmask_a_2.dds",
    "map/terrain/colormap_rgb_cityemissivemask_a.dds",
    "map/terrain/colormap_water_0.dds",
    "map/terrain/colormap_water_0.png",
    "map/terrain/colormap_water_1.dds",
    "map/terrain/colormap_water_2.dds",
    "map/terrain/fow_noise_0.dds",
    "map/terrain/fow_noise_1.dds",
    "map/terrain/fow_noise_2.dds",
    "map/terrain/fow_rgb_waterspec_a.dds",
    "map/terrain/ice_diffuse.dds",
    "map/terrain/ice_noise_0.dds",
    "map/terrain/ice_noise_1.dds",
    "map/terrain/ice_noise_2.dds",
    "map/terrain/lean1.dds",
    "map/terrain/lean2.dds",
    "map/terrain/mud_diffuse_rgb_gloss_a_0.dds",
    "map/terrain/mud_diffuse_rgb_gloss_a_1.dds",
    "map/terrain/mud_diffuse_rgb_gloss_a_2.dds",
    "map/terrain/mud_normal_rgb_spec_a_0.dds",
    "map/terrain/mud_normal_rgb_spec_a_1.dds",
    "map/terrain/mud_normal_rgb_spec_a_2.dds",
    "map/terrain/reflection.dds",
    "map/terrain/reflection_land_unit.dds",
    "map/terrain/snow_normal_rgb_diffuse_a.dds",
    "map/terrain/strait.dds",
    "map/terrain/underwater_terrain_0.dds",
    "map/terrain/underwater_terrain_1.dds",
    "map/terrain/underwater_terrain_2.dds",
]

PIHC3_SUPPORT_MAP_ROOT_PATHS = [
    "map/adjacencies.csv",
    "map/adjacencies_backup.csv",
    "map/adjacency_rules.txt",
    "map/airports.txt",
    "map/ambient_object.txt",
    "map/buildings.txt",
    "map/cities.bmp",
    "map/cities.txt",
    "map/color2prov.json",
    "map/colors.txt",
    "map/continent.txt",
    "map/default.map",
    "map/definition.csv",
    "map/definition_backup.csv",
    "map/heightmap.bmp",
    "map/positions.txt",
    "map/prov2state.json",
    "map/provinces.bmp",
    "map/railways.txt",
    "map/rivers.bmp",
    "map/seasons.txt",
    "map/state_adjacency_graph.json",
    "map/states_labels.png",
    "map/supply_nodes.txt",
    "map/terrain.bmp",
    "map/trees.bmp",
    "map/unitstacks.txt",
    "map/weatherpositions.txt",
    "map/world_normal.bmp",
]


def _game_asset_owner(path: str) -> str:
    source_path = Path(path)
    stem_parts = [source_path.stem]
    if source_path in {Path("map/cities.bmp"), Path("map/terrain/colormap_water_0.png")} or source_path.suffix.lower() == ".nudge":
        stem_parts.append(source_path.suffix.lstrip("."))
    text = "_".join((*source_path.parent.parts, *stem_parts))
    module_id = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").upper()
    return f"module:game_asset/GAME_ASSET_{module_id}"


def _common_data_owner(path: str) -> str:
    source_path = Path(path)
    local_path = source_path.relative_to("common")
    text = "_".join((*local_path.parent.parts, local_path.stem))
    module_id = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").upper()
    return f"module:common_data/COMMON_DATA_{module_id}"


def _strategic_region_owner(path: str) -> str:
    return f"module:strategic_region/{Path(path).stem}"


def _state_owner(path: str) -> str:
    return f"module:state/{Path(path).stem}"


def _section_localization_rows(path: Path) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    language: str | None = None
    key: str | None = None
    value_lines: list[str] = []

    def flush() -> None:
        if language is not None and key is not None:
            rows.setdefault(language, {})[key] = "\n".join(value_lines).strip()

    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"\[(?P<language>[^.]+)\.(?P<key>.+)]", line.strip())
        if match is None:
            value_lines.append(line)
            continue
        flush()
        language = match.group("language")
        key = match.group("key")
        value_lines = []
    flush()
    return rows


def _pdx_tree_fingerprint(block: object) -> tuple[object, ...]:
    from paradev.pdx import PDXBlock, PDXScalar

    assert isinstance(block, PDXBlock)
    rows: list[object] = []
    for entry in block.entries:
        assert isinstance(entry.key, PDXScalar)
        key = (entry.key.type, entry.key.val)
        if isinstance(entry.val, PDXBlock):
            value: object = ("block", _pdx_tree_fingerprint(entry.val))
        else:
            assert isinstance(entry.val, PDXScalar)
            value = ("scalar", entry.val.type, entry.val.val)
        rows.append((key, entry.op, value))
    return tuple(rows)


@pytest.fixture(scope="module")
def pihc3_full_build_result() -> BuildResult:
    """Plan the immutable PIHC3 tree once for all whole-project contracts."""

    from paradev.project import Project

    return Project.load(PIHC3_ROOT).build()


def _files_by_relative_path(root: Path) -> dict[str, Path]:
    return {path.relative_to(root).as_posix(): path for path in root.rglob("*") if path.is_file()}


def _assert_single_trailing_newline(path: Path) -> None:
    payload = path.read_bytes()
    assert payload.endswith(b"\n"), path
    assert not payload.endswith(b"\n\n"), path


def test_pihc3_descriptor_metadata_matches_launcher_requirements() -> None:
    manifest = _pihc3_manifest()
    visible_manifest = yaml.safe_load((PIHC3_ROOT / "paradev.yaml").read_text(encoding="utf-8"))

    assert "output_root" not in manifest
    assert set(visible_manifest) == {
        "project_id",
        "title",
        "game",
        "preferred_language",
        "source_roots",
        "mod_version",
    }
    assert visible_manifest["preferred_language"] == "zh"
    assert manifest["mod_version"] == "0.2.3"
    assert str(manifest["remote_file_id"]) == "3154495198"
    assert manifest["picture"] == "thumbnail.png"
    assert manifest["supported_version"] == "1.19.*"


def test_pihc3_output_root_uses_configured_hoi4_mod_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from paradev.sdk import Project

    mod_root = tmp_path / "hoi4-mod"
    monkeypatch.setenv("PARADEV_HOI4_MOD_ROOT", str(mod_root))

    project = Project.load(PIHC3_ROOT)

    assert project.output_root == (mod_root / "PIHC3").resolve()


def test_pihc3_project_build_wrappers_use_current_paradev_surfaces() -> None:
    compile_text = (PIHC3_ROOT / "compile.bash").read_text(encoding="utf-8")
    sdk_build_wrapper = PIHC3_ROOT / "scripts/build.py"
    readme_text = (PIHC3_ROOT / "README.md").read_text(encoding="utf-8")
    manual_text = Path("docs/user-manual/pihc3.md").read_text(encoding="utf-8")

    assert not sdk_build_wrapper.exists()
    assert 'DEFAULT_PARADEV_ROOT="$(cd "${PROJECT_ROOT}/../.." && pwd)"' in compile_text
    assert 'ROOT="$(cd "${PARADEV_ROOT:-${DEFAULT_PARADEV_ROOT}}" && pwd)"' in compile_text
    assert 'if [[ ! -f "${ROOT}/scripts/_env.bash" ]]; then' in compile_text
    assert 'echo "ParaDev repository does not contain scripts/_env.bash: ${ROOT}" >&2' in compile_text
    assert 'cd "${ROOT}"' in compile_text
    assert 'source "${ROOT}/scripts/_env.bash"' in compile_text
    assert compile_text.index('cd "${ROOT}"') < compile_text.index('source "${ROOT}/scripts/_env.bash"')
    assert 'PARADEV_CONFIG_ROOT_OVERRIDE="${PARADEV_CONFIG_ROOT:-}"' in compile_text
    assert 'export PARADEV_ROOT="${PARADEV_CONFIG_ROOT_OVERRIDE}"' in compile_text
    assert "unset PARADEV_ROOT" in compile_text
    assert compile_text.index('source "${ROOT}/scripts/_env.bash"') < compile_text.index("unset PARADEV_ROOT")
    assert "PYTHONDONTWRITEBYTECODE=1" in compile_text
    assert 'export PARADEV_HOI4_MOD_ROOT="${PIHC3_MOD_ROOT}"' in compile_text
    assert 'PIHC3_OUTPUT_ROOT="${PIHC3_MOD_ROOT}/PIHC3"' in compile_text
    assert "PIHC3_MOD_ROOT must be an absolute path" in compile_text
    assert "PIHC3_LAUNCHER_MOD" not in compile_text
    assert "sync_launcher_mod" not in compile_text
    assert "resolve_uv" in compile_text
    assert 'uv_run python "${PROJECT_ROOT}/scripts/check_source_layout.py" --quiet' in compile_text
    assert "uv_run paradev build" in compile_text
    assert "uv_run paradev summary" not in compile_text
    assert "--clean" in compile_text
    assert "--clean-only" in compile_text
    assert "clean_generated_outputs" in compile_text
    assert "--summary" in compile_text
    assert "--family FAMILY" in compile_text
    assert "--module MODULE" in compile_text
    assert "--collection COLLECTION" in compile_text
    assert "--family|--module|--module-id|--collection|--collection-id" in compile_text
    assert '"${PROJECT_ROOT}/.cache"' not in compile_text
    assert '"${PROJECT_ROOT}/.paradev"' in compile_text
    assert '"${PROJECT_ROOT}/build"' in compile_text
    assert 'find "${PROJECT_ROOT}" -path "${PROJECT_ROOT}/.git" -prune -o -type d -name __pycache__ -prune -exec rm -rf -- {} +' in compile_text
    assert 'find "${PROJECT_ROOT}" -path "${PROJECT_ROOT}/.git" -prune -o -type f -name \'*.pyc\' -delete' in compile_text
    assert "--emit-artifacts" in compile_text
    assert "--emit-manifests" in compile_text
    assert "--no-sync-launcher-descriptor" in compile_text
    assert "Compilation publishes project artifacts only and does not read or update the\n" "external HoI4 launcher descriptor." in compile_text
    assert "build_mod" not in compile_text
    assert "paradev.hoi4.mod" not in compile_text
    assert "conda run" not in compile_text

    assert "rtk bash projects/PIHC3/compile.bash --json" in readme_text
    assert "scripts/check_source_layout.py --json" in readme_text
    assert "rtk bash projects/PIHC3/compile.bash --clean --json" in readme_text
    assert "rtk bash projects/PIHC3/compile.bash --clean-only" in readme_text
    assert "rtk bash projects/PIHC3/compile.bash --summary --json" in readme_text
    assert "`compile.bash` is a convenience wrapper around the shared ParaDev build path" in readme_text
    assert "selectors keep their requested compilation scope" in readme_text
    assert "Cached and targeted builds never delete untracked files" in readme_text
    assert "compiling PIHC3 never reads or changes `PIHC3.mod` or its launcher ownership marker" in readme_text
    assert "It preserves the output directory itself and `PIHC3.mod`" in readme_text
    assert "`--clean-only` performs the same cleanup without running a build" in readme_text
    assert "`--summary` prints the compact build summary instead of the full build plan payload." in readme_text
    assert "It enters the ParaDev repo root before resolving `uv`, so it works from any current directory." in readme_text
    assert "When PIHC3 is checked out as an isolated worktree, set `PARADEV_ROOT`" in readme_text
    assert "The wrapper uses that variable only to select source and removes it before starting the CLI" in readme_text
    assert "Set `PARADEV_CONFIG_ROOT=/path/to/isolated-config` separately" in readme_text
    assert "rtk bash projects/PIHC3/compile.bash --plan-only --json" in manual_text
    assert "scripts/check_source_layout.py --json" in manual_text
    assert "rtk bash projects/PIHC3/compile.bash --clean --json" in manual_text
    assert "rtk bash projects/PIHC3/compile.bash --clean-only" in manual_text
    assert "rtk bash projects/PIHC3/compile.bash --summary --json" in manual_text
    assert "It preserves the output directory itself and `PIHC3.mod`" in manual_text
    assert "`--clean-only` performs the same cleanup without running a build" in manual_text
    assert ("`--summary` keeps the normal `paradev build` path but returns its compact\n" "summary payload") in manual_text
    assert "The wrapper enters the ParaDev repo root before resolving `uv`, so it works from any current directory." in manual_text
    assert "Direct CLI builds, `Project.build(...)`, desktop builds, and `compile.bash` all load" in manual_text
    assert "PIHC3 expands only the localization publication closure" in manual_text
    assert "It deliberately uses project-only publication" in manual_text


def test_pihc3_c08_decision_categories_replace_todo_editor_titles() -> None:
    root = PIHC3_ROOT / "src/collections/decision"
    expected = {
        "DECISION_CATEGORY_C08_EAST_ROUTE": ("东部航线", "East Route"),
        "DECISION_CATEGORY_C08_WASTELAND_DEVELOP": (
            "开发区建设",
            "Wasteland Development",
        ),
    }

    for category_id, (folder_title, english_title) in expected.items():
        category_root = root / category_id
        localization = (category_root / "main.loc").read_text(encoding="utf-8")

        assert category_root.name == f"{category_id} - {folder_title}"
        assert not (Path(str(category_root)) / "meta.yaml").exists()
        assert f"[en.{category_id}]\n{english_title}\n" in localization
        assert "\nTODO\n" not in localization


def test_pihc3_c08_idea_localization_matches_reviewed_hashes() -> None:
    expected_hashes = {
        "IDEA_C08_FADED_FRIENDSHIP_1": (
            "10df7590fb2342a1a766dbbb636774c1e1280b3ce3cae0b6c988615e6779089f",
            "250e69a3b27982170c8fbe3216ed1cbf30da326875b8b00f2989d7c144cdb0f3",
        ),
        "IDEA_C08_FADED_FRIENDSHIP_2": (
            "55f51c0b70b1ef053e077313fa05324fe8492d17f4df22ee3b0a7213b299f457",
            "250e69a3b27982170c8fbe3216ed1cbf30da326875b8b00f2989d7c144cdb0f3",
        ),
        "IDEA_C08_FADED_FRIENDSHIP_3": (
            "fa714f506ec5302e7794663f677663e048a2c96c98b6b5035a769c025d35a219",
            "250e69a3b27982170c8fbe3216ed1cbf30da326875b8b00f2989d7c144cdb0f3",
        ),
        "IDEA_C08_FADED_FRIENDSHIP_4": (
            "b11d94f6c28877cbabb8c3a62ef607eb4bc4d1544ec397e0bbc9ea83bff57ca6",
            "250e69a3b27982170c8fbe3216ed1cbf30da326875b8b00f2989d7c144cdb0f3",
        ),
        "IDEA_C08_NEVER_FORGET": (
            "db378da410f7e86c2ad156fd8ed763196d061c69657160134dac9dac02b4f669",
            "be6a21a5322109fe8e0966fbd1348889d45465c69182bdf34bdfa6b2acc97603",
        ),
    }

    for idea_id, (main_hash, _former_source_hash) in expected_hashes.items():
        module_root = PIHC3_ROOT / "src" / "modules" / "idea" / idea_id
        assert sha256hash((module_root / "main.loc").read_text(encoding="utf-8")) == main_hash
        assert not (module_root / "legacy").exists()


def test_pihc3_compile_clean_only_preserves_launcher_registration_without_build(
    tmp_path: Path,
) -> None:
    paradev_root = Path(__file__).resolve().parents[1]
    project_root = tmp_path / "PIHC3"
    mod_root = tmp_path / "mod"
    copy_file(PIHC3_ROOT / "compile.bash", project_root / "compile.bash")
    generated_dirs = [project_root / ".paradev", project_root / "build"]
    cache_dirs = [
        project_root / "system" / "__pycache__",
        project_root / "scripts" / "__pycache__",
        project_root / "scripts" / "review" / "__pycache__",
        project_root / "src" / "modules" / "__pycache__",
    ]
    generated_files = [
        generated_dirs[0] / ".cache/probe.txt",
        generated_dirs[0] / "probe.txt",
        generated_dirs[1] / "probe.txt",
        mod_root / "PIHC3/probe.txt",
        *(cache_dir / "probe.pyc" for cache_dir in cache_dirs),
    ]
    launcher = mod_root / "PIHC3.mod"

    for path in generated_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"generated")
    launcher.write_bytes(b"registered")
    output_root = mod_root / "PIHC3"
    output_identity = output_root.stat().st_ino

    result = subprocess.run(
        ["bash", str(project_root / "compile.bash"), "--clean-only"],
        capture_output=True,
        check=True,
        env={
            **os.environ,
            "PARADEV_ROOT": str(paradev_root),
            "PIHC3_MOD_ROOT": str(mod_root),
        },
        text=True,
    )

    assert result.stdout == ""
    assert result.stderr == ""
    assert all(not path.exists() for path in generated_dirs)
    assert all(not path.exists() for path in cache_dirs)
    assert not any(path.exists() for path in generated_files)
    assert output_root.stat().st_ino == output_identity
    assert launcher.read_bytes() == b"registered"


def test_pihc3_build_surfaces_share_registered_localisation_postprocessor() -> None:
    from paradev.sdk import Project

    compile_text = (PIHC3_ROOT / "compile.bash").read_text(encoding="utf-8")
    manifest = yaml.safe_load((PIHC3_ROOT / "paradev.yaml").read_text(encoding="utf-8"))
    postprocessor_path = PIHC3_ROOT / "extensions/localisation/__init__.py"
    descriptor_path = PIHC3_ROOT / "extensions/localisation/.paradev/meta.yaml"
    postprocessor_text = postprocessor_path.read_text(encoding="utf-8")
    descriptor_text = descriptor_path.read_text(encoding="utf-8")
    capabilities = Project.load(PIHC3_ROOT).families()

    assert "python_modules" not in manifest
    assert postprocessor_path.is_file()
    assert descriptor_path.is_file()
    assert "publication_scope" in postprocessor_text
    assert "publication_replaces" in postprocessor_text
    assert "kind: paradev_build_postprocessor" in descriptor_text
    assert "qualname: PIHC3LocalisationPostprocessor" in descriptor_text
    assert "_dedupe_localisation.py" not in compile_text
    assert "dedupe_localisation" not in compile_text
    assert "TARGETED_BUILD" not in compile_text
    assert '"${EXTRA_ARGS[@]}"' in compile_text
    assert not (PIHC3_ROOT / "scripts/_dedupe_localisation.py").exists()
    assert capabilities["postprocessors"] == [
        {
            "postprocessor_id": "pihc3.localisation",
            "kind": "PIHC3LocalisationPostprocessor",
        }
    ]
    assert capabilities["index"]["postprocessor_id"] == {"pihc3.localisation": [0]}


def test_pihc3_python_extensions_are_standalone_heavenbase_modules() -> None:
    project_root = PIHC3_ROOT
    manifest = yaml.safe_load((project_root / "paradev.yaml").read_text(encoding="utf-8"))
    extension_root = project_root / "extensions"
    module_roots = {path.name for path in extension_root.iterdir() if path.is_dir()}
    readme_text = (project_root / "README.md").read_text(encoding="utf-8")

    assert "python_modules" not in manifest
    assert len(module_roots) == 71
    assert {
        "entity",
        "equipment",
        "equipment_module",
        "inventory_item",
        "localisation",
        "state_lore",
    }.issubset(module_roots)
    assert all(
        not (Path(str(module)) / "meta.yaml").exists()
        and (Path(str(module)) / ".paradev/meta.yaml").is_file()
        and (Path(str(module)) / "__init__.py").is_file()
        for module in extension_root.iterdir()
        if module.is_dir()
    )
    assert not (project_root / "system").exists()
    assert "HeavenBase module folders under `extensions/`" in readme_text


def test_pihc3_replace_paths_hide_vanilla_special_forces_subdoctrines() -> None:
    project_root = PIHC3_ROOT
    manifest = _pihc3_manifest(project_root)

    assert "common/doctrines/subdoctrines/special_forces" in manifest["replace_path"]


def test_pihc3_frontend_game_setup_gui_matches_current_hoi4_country_selector_contract() -> None:
    frontend_setup = (PIHC3_ROOT / "src/modules/interface/INTERFACE_PIHC_INTERFACE/interface/frontendgamesetupview.gui").read_text(encoding="utf-8")

    for required_gui_name in ("country_filter", "filters", "more_countries"):
        assert f"name = {required_gui_name}" in frontend_setup or f'name = "{required_gui_name}"' in frontend_setup
    assert len(re.findall(r'name\s*=\s*"new_content"', frontend_setup)) == 3
    assert not re.search(r'name\s*=\s*"?country_shine"?\b', frontend_setup)
    assert re.search(
        r'name\s*=\s*"?countries_medium"?\b.*?slotsize\s*=\s*\{\s*width\s*=\s*150\s+height\s*=\s*0\s*\}',
        frontend_setup,
        re.DOTALL,
    )
    assert re.search(
        r'name\s*=\s*"?countries_mini_expanded"?\b.*?slotsize\s*=\s*\{\s*width\s*=\s*52\s+height\s*=\s*38\s*\}',
        frontend_setup,
        re.DOTALL,
    )
    assert re.search(
        r'name\s*=\s*"?country_entry_medium"?\b.*?name\s*=\s*"?country_flag"?\b.*?position\s*=\s*\{\s*x\s*=\s*28\s+y\s*=\s*22\s*\}',
        frontend_setup,
        re.DOTALL,
    )


def test_pihc3_current_hoi4_runtime_contracts_resolve_known_startup_errors() -> None:
    project_root = PIHC3_ROOT
    interface_root = project_root / "src/modules/interface/INTERFACE_PIHC_INTERFACE"
    manifest = _pihc3_manifest(project_root)

    countryfaction = (interface_root / "interface/factions/countryfactionview.gui").read_text(encoding="utf-8")
    assert "text = (XX%)" not in countryfaction
    assert 'text = "(XX%)"' in countryfaction

    core_gfx = (interface_root / "interface/core.gfx").read_text(encoding="utf-8")
    assert 'name = "GFX_button_238x38"' in core_gfx
    assert 'name = "GFX_tiled_window_3b_border"' in core_gfx
    assert 'textureFile = "gfx/interface/tiles/tiled_window_3b_border.dds"' in core_gfx

    unitview = (interface_root / "interface/unitview.gui").read_text(encoding="utf-8")
    for required_gui_name in (
        "hq_window",
        "deploy_button_label",
        "deploy_button_template_name",
        "deploy_button_cost",
        "deploy_button_no_commander_message",
        "template_icon",
        "deploy",
        "undeploy",
        "hq_template_frame",
    ):
        assert f"name = {required_gui_name}" in unitview or f'name = "{required_gui_name}"' in unitview

    countryarmyview = (interface_root / "interface/countryarmyview.gui").read_text(encoding="utf-8")
    for required_gui_name in ("naval_hq_open", "filter_button", "highlight"):
        assert f"name = {required_gui_name}" in countryarmyview or f'name = "{required_gui_name}"' in countryarmyview

    countrystate_gfx = (interface_root / "interface/countrystateview.gfx").read_text(encoding="utf-8")
    assert "GFX_modifiers_the_great_wall_icon" in countrystate_gfx

    for inventory_item_texture in (
        "gfx/interface/inventory_items/INVENTORY_ITEM_DEFAULT.dds",
        "gfx/interface/inventory_items/INVENTORY_ITEM_DEFAULT_SMALL.dds",
    ):
        assert (interface_root / inventory_item_texture).is_file()

    frontend_setup = (interface_root / "interface/frontendgamesetupview.gui").read_text(encoding="utf-8")
    for required_gui_name in ("country_filter", "filters", "more_countries"):
        assert f"name = {required_gui_name}" in frontend_setup or f'name = "{required_gui_name}"' in frontend_setup

    country_tags = (project_root / "src/modules/country_history/COUNTRY_HISTORY_COUNTRY_TAGS_00_COUNTRIES/common/country_tags/00_countries.txt").read_text(
        encoding="utf-8"
    )
    registered_country_tags = re.findall(r"^\s*([A-Z0-9]{3})\s*=", country_tags, re.MULTILINE)
    assert registered_country_tags == [f"C{index:02d}" for index in range(68)]
    assert not {"GER", "ENG", "SOV", "SWE", "NOR", "FIN", "FRA", "ITA"} & set(registered_country_tags)

    unit_tags = (project_root / "src/modules/common_data/COMMON_DATA_UNIT_TAGS_00_CATEGORIES/common/unit_tags/00_categories.txt").read_text(encoding="utf-8")
    for category in (
        "category_tanks",
        "category_line_artillery",
        "category_artillery",
        "category_support_battalions",
    ):
        assert category in unit_tags

    land_doctrine_tracks = (project_root / "src/modules/doctrine/PIHC_DOCTRINE_SUPPORT/common/doctrines/tracks/PIHC_land_doctrine_tracks.txt").read_text(
        encoding="utf-8"
    )
    assert "armored_car" not in land_doctrine_tracks

    specializations = (
        project_root
        / "src/modules/special_project/PIHC_SPECIAL_PROJECT_SUPPORT - PIHC Special Project Support/common/special_projects/specialization/specializations.txt"
    ).read_text(encoding="utf-8")
    for specialization_id in (
        "specialization_land",
        "specialization_air",
        "specialization_naval",
        "specialization_nuclear",
        "specialization_magic",
    ):
        assert f"{specialization_id} = {{" in specializations

    script_enums = (project_root / "src/modules/common_data/COMMON_DATA_SCRIPT_ENUMS/common/script_enums.txt").read_text(encoding="utf-8")
    for enum_id in (
        "support_ship_hull",
        "support_ship_1",
        "support_ship_2",
        "repair_ship_hull",
        "repair_ship_1",
        "repair_ship_2",
    ):
        assert re.search(rf"^\s*{re.escape(enum_id)}\s*$", script_enums, re.MULTILINE)

    hidden_project_tech_ids = (
        "TECHNOLOGY_SP_HIDDEN_GENERATOR",
        "TECHNOLOGY_SP_HIDDEN_ROCKET_ELEC",
        "TECHNOLOGY_SP_HIDDEN_ROCKET_OIL",
        "TECHNOLOGY_SP_HIDDEN_ROCKET_STEAM",
        "TECHNOLOGY_SP_HIDDEN_ROCKET_WITCHCRAFT",
    )
    for tech_id in hidden_project_tech_ids:
        technology_roots = list((project_root / "src/modules/technology").glob(f"{tech_id} - *"))
        assert len(technology_roots) == 1
        tech_def = (technology_roots[0] / "def.txt").read_text(encoding="utf-8")
        assert "is_special_project_tech = yes" in tech_def

    nukes_def = (
        project_root / "src/modules/technology/PIHC_TECHNOLOGY_SUPPORT - 共享技术支持/common/technologies/electronic_mechanical_engineering.txt"
    ).read_text(encoding="utf-8")
    assert re.search(r"^\s*nukes\s*=\s*\{", nukes_def, re.MULTILINE)
    assert "folder = {" not in nukes_def

    c01_angry_scripted_gui = (project_root / "src/modules/scripted_gui/C01_ANGRY_POWER_PROGRESSBAR/def.txt").read_text(encoding="utf-8")
    assert "C01_ANGRY_POWER_PROGRESSBAR = {" in c01_angry_scripted_gui
    assert "context_type = decision_category" in c01_angry_scripted_gui
    assert "window_name = C01_angry_power_progressbar_container" in c01_angry_scripted_gui
    assert "frame = C01.VAR_ANGRY_POWER" in c01_angry_scripted_gui

    angry_progress_gfx = (interface_root / "interface/PIHC_C01_ANGRY_POWER_PROGRESSBAR.gfx").read_text(encoding="utf-8")
    assert "name = GFX_C01_angry_power_progressbar" in angry_progress_gfx
    assert 'texturefile1 = "gfx/interface/C01_angry_power_progressbar_full.dds"' in angry_progress_gfx
    assert 'texturefile2 = "gfx/interface/C01_angry_power_progressbar_empty.dds"' in angry_progress_gfx

    angry_progress_gui = (interface_root / "interface/PIHC_C01_ANGRY_POWER_PROGRESSBAR.gui").read_text(encoding="utf-8")
    assert "name = C01_angry_power_progressbar_container" in angry_progress_gui
    assert "spriteType = GFX_C01_angry_power_progressbar" in angry_progress_gui
    assert 'text = "[?C01.VAR_ANGRY_POWER]"' in angry_progress_gui
    assert (interface_root / "gfx/interface/C01_angry_power_progressbar_full.dds").is_file()
    assert (interface_root / "gfx/interface/C01_angry_power_progressbar_empty.dds").is_file()

    expected_unit_support_files = (
        "UNIT_UNITS_RECON/common/units/recon.txt",
        "UNIT_UNITS_SUPPORT_SHIPS/common/units/support_ships.txt",
        "UNIT_UNITS_REPAIR_SHIPS/common/units/repair_ships.txt",
        "UNIT_UNITS_EQUIPMENT_SUPPORT_SHIPS/common/units/equipment/support_ships.txt",
        "UNIT_UNITS_EQUIPMENT_REPAIR_SHIPS/common/units/equipment/repair_ships.txt",
    )
    for relative_path in expected_unit_support_files:
        assert (project_root / "src/modules/unit" / relative_path).is_file()

    airship_loc = (project_root / "src/modules/equipment/EQUIPMENT_FRAME_AIRSHIP - 飞艇骨架/main.loc").read_text(encoding="utf-8")
    light_tank_loc = (project_root / "src/modules/equipment/ARCHETYPE_TANK_LIGHT - 轻型坦克/main.loc").read_text(encoding="utf-8")
    for tooltip_key in (
        "MODULE_SPECIAL_PARADROP_ONLY_1_TOOLTIP",
        "MODULE_SPECIAL_SUPPLY_ONLY_1_TOOLTIP",
    ):
        assert tooltip_key in airship_loc
    for tooltip_key in (
        "HEAVY_CHASSIS_REQUIRED_TOOLTIP",
        "MODULE_SPECIAL_ONLY_1_TOOLTIP",
    ):
        assert tooltip_key in light_tank_loc

    bba_vehicle_asset = (
        project_root
        / "src/modules/game_asset/GAME_ASSET_DLC_DLC036_BY_BLOOD_ALONE_GFX_ENTITIES_BBA_UNITS_VEHICLES/"
        / "dlc/dlc036_by_blood_alone/gfx/entities/BBA_units_vehicles.asset"
    ).read_text(encoding="utf-8")
    assert "ITA_mechanized_vehicle_1_entity" not in bba_vehicle_asset
    assert "generic_motorized_vehicle_entity" in bba_vehicle_asset
    assert "dlc/dlc036_by_blood_alone/gfx/entities" in manifest["replace_path"]

    buildings_gfx = (project_root / "src/modules/game_asset/GAME_ASSET_GFX_ENTITIES_BUILDINGS_GFX/gfx/entities/buildings.gfx").read_text(encoding="utf-8")
    for mesh_name in (
        "ENG_artillery_mesh",
        "ENG_anti_tank_mesh",
        "JAP_anti_tank_type_97_mesh",
    ):
        assert f'name = "{mesh_name}"' in buildings_gfx


def test_pihc3_state_temperature_native_sources_are_integrated() -> None:
    project_root = PIHC3_ROOT
    effect_path = project_root / "src/modules/scripted_effect/PIHC_STATE_TEMPERATURE/def.txt"
    on_action_path = project_root / "src/modules/on_action/PIHC_STATE_TEMPERATURE/def.txt"
    scripted_gui_path = project_root / "src/modules/scripted_gui/pihc_state_temperature/def.txt"
    modifier_root = project_root / "src/modules/modifier_definition/MODIFIER_DEFINITION_DYNAMIC_MODIFIERS_PIHC_STATE_TEMPERATURE"
    interface_root = project_root / "src/modules/interface/INTERFACE_PIHC_STATE_TEMPERATURE"
    cold_idea_path = project_root / "src/modules/idea_category/IDEA_CATEGORY_ERA_COLD - 耐寒政策/def.txt"
    dynamic_modifier_path = modifier_root / "common/dynamic_modifiers/PIHC_state_temperature_dynamic_modifiers.txt"
    loc_path = modifier_root / "main.loc"
    interface_gui_path = interface_root / "interface/PIHC_state_temperature.gui"
    interface_gfx_path = interface_root / "interface/PIHC_state_temperature.gfx"
    scripted_loc_root = project_root / "src/modules/scripted_localisation/" / "SCRIPTED_LOCALISATION_SCRIPTED_LOCALISATION_PIHC_STATE_TEMPERATURE"
    scripted_loc_path = scripted_loc_root / "common/scripted_localisation/PIHC_state_temperature.txt"
    c26_history_path = project_root / "src/modules/country_history/COUNTRY_HISTORY_COUNTRIES_C26/history/countries/C26.txt"
    terrain_var_script_path = project_root / "scripts/generate_state_temperature_terrain_vars.py"
    terrain_var_effect_path = project_root / "src/modules/scripted_effect/PIHC_STATE_TEMPERATURE_TERRAIN_VARS/def.txt"

    expected_files = (
        effect_path,
        on_action_path,
        scripted_gui_path,
        dynamic_modifier_path,
        loc_path,
        interface_gui_path,
        interface_gfx_path,
        scripted_loc_path,
        cold_idea_path,
        c26_history_path,
        terrain_var_script_path,
        terrain_var_effect_path,
        interface_root / "gfx/interface/PIHC_state_temperature/base.dds",
        interface_root / "gfx/interface/PIHC_state_temperature/mercury.dds",
        interface_root / "gfx/interface/PIHC_state_temperature/needle_strip.dds",
        interface_root / "gfx/interface/PIHC_state_temperature/progress_bar_frame.dds",
        interface_root / "gfx/interface/PIHC_state_temperature/progress_red.dds",
        interface_root / "gfx/interface/PIHC_state_temperature/scale.dds",
        *(interface_root / f"gfx/interface/PIHC_state_temperature/needle_{index}.dds" for index in range(1, 8)),
    )
    for expected_file in expected_files:
        assert expected_file.is_file(), expected_file.as_posix()

    effect_text = effect_path.read_text(encoding="utf-8")
    for effect_id in (
        "PIHC_update_current_month",
        "PIHC_update_monthly_global_variables",
        "PIHC_change_ERA_COLD_level",
        "PIHC_update_state_outdoor_temperature",
        "PIHC_update_state_outdoor_temperature_frame",
        "PIHC_update_state_indoor_temperature",
        "PIHC_update_state_indoor_temperature_frame",
        "PIHC_update_state_temperature_subsidies_modifier",
        "PIHC_update_state_temperature_buff",
        "PIHC_update_average_temperature_buff",
    ):
        assert re.search(rf"^\s*{re.escape(effect_id)}\s*=\s*\{{", effect_text, re.MULTILINE)
    assert "global.PIHC_var_state_temperature_factor_threat = global.threat" in effect_text
    assert "THIS.PIHC_var_state_temperature_factor_threat_con = THIS.has_added_tension_amount" in effect_text
    assert "THIS.PIHC_var_state_controller_outdoor_temperature_influence = THIS.VAR_OUTDOOR_TEMPERATURE_INFLUENCE" in effect_text
    assert re.search(
        r"set_variable\s*=\s*\{\s*THIS\.PIHC_var_state_terrain_temperature_correction\s*=\s*"
        r"THIS\.VAR_OUTDOOR_TEMPERATURE_INFLUENCE_MAX\s*\}.*?"
        r"subtract_from_variable\s*=\s*\{\s*THIS\.PIHC_var_state_terrain_temperature_correction\s*=\s*"
        r"THIS\.VAR_OUTDOOR_TEMPERATURE_INFLUENCE_MIN\s*\}.*?"
        r"set_variable_to_random\s*=\s*\{\s*var\s*=\s*THIS\.PIHC_var_state_terrain_temperature_roll.*?"
        r"min\s*=\s*0.*?max\s*=\s*1.*?\}.*?"
        r"multiply_variable\s*=\s*\{\s*THIS\.PIHC_var_state_terrain_temperature_correction\s*=\s*"
        r"THIS\.PIHC_var_state_terrain_temperature_roll\s*\}.*?"
        r"add_to_variable\s*=\s*\{\s*THIS\.PIHC_var_state_terrain_temperature_correction\s*=\s*"
        r"THIS\.VAR_OUTDOOR_TEMPERATURE_INFLUENCE_MIN\s*\}.*?"
        r"round_variable\s*=\s*THIS\.PIHC_var_state_terrain_temperature_correction.*?"
        r"add_to_variable\s*=\s*\{\s*THIS\.PIHC_var_state_outdoor_temperature\s*=\s*"
        r"THIS\.PIHC_var_state_terrain_temperature_correction\s*\}.*?"
        r"add_to_variable\s*=\s*\{\s*THIS\.PIHC_var_state_outdoor_temperature\s*=\s*"
        r"THIS\.PIHC_var_state_controller_outdoor_temperature_influence\s*\}.*?"
        r"subtract_from_variable\s*=\s*\{\s*THIS\.PIHC_var_state_outdoor_temperature\s*=\s*"
        r"global\.PIHC_var_state_temperature_factor_threat\s*\}.*?"
        r"subtract_from_variable\s*=\s*\{\s*THIS\.PIHC_var_state_outdoor_temperature\s*=\s*"
        r"THIS\.PIHC_var_state_temperature_factor_threat_con\s*\}",
        effect_text,
        re.DOTALL,
    )
    assert "has_terrain =" not in effect_text
    assert "PIHC_update_state_outdoor_temperature_terrain_correction" not in effect_text
    assert "tag = C26" not in effect_text
    assert "THIS.building_level@generator_complex" in effect_text
    assert "every_neighbor_state" in effect_text
    assert "TECHNOLOGY_SUPPORT_COLD_IV" in effect_text
    assert "IDEA_ERA_COLD_5_BURN" in effect_text
    assert "THIS.PIHC_var_state_indoor_temperature_frame" in effect_text
    assert "THIS.PIHC_var_state_outdoor_temperature_display = THIS.PIHC_var_state_outdoor_temperature" in effect_text
    assert "var = THIS.PIHC_var_state_outdoor_temperature_display" in effect_text
    assert "min = -39" in effect_text
    assert "max = 42" in effect_text
    assert "THIS.PIHC_temperature_mercury_y = THIS.PIHC_var_state_outdoor_temperature_display" in effect_text
    assert "THIS.PIHC_temperature_mercury_y = 129" in effect_text
    assert "THIS.PIHC_temperature_mercury_y = -4" in effect_text
    for threshold, expected_frame in (
        ("40", "7"),
        ("30", "6"),
        ("0", "5"),
        ("-10", "4"),
        ("-20", "3"),
        ("-40", "2"),
    ):
        assert f"THIS.PIHC_var_state_indoor_temperature = {threshold}" in effect_text
        assert f"THIS.PIHC_var_state_indoor_temperature_frame = {expected_frame}" in effect_text
    assert "THIS.PIHC_var_state_indoor_temperature_frame = 1" in effect_text
    for frame, score in (
        ("1", "1"),
        ("2", "3"),
        ("3", "7"),
        ("4", "8"),
        ("5", "10"),
        ("6", "9"),
        ("7", "1"),
    ):
        assert re.search(
            rf"check_variable\s*=\s*\{{\s*THIS\.PIHC_var_state_indoor_temperature_frame\s*=\s*{frame}\s*\}}.*?"
            rf"PIHC_var_state_indoor_temperature_comfort_score\s*=\s*{score}",
            effect_text,
            re.DOTALL,
        )
    assert "THIS.PIHC_var_state_indoor_temperature_comfort_percent = THIS.PIHC_var_state_indoor_temperature_comfort_score" in effect_text
    assert "THIS.PIHC_var_state_indoor_temperature_score = THIS.PIHC_var_state_indoor_temperature_frame" not in effect_text
    assert "THIS.PIHC_var_state_temperature_population_weight = THIS.state_population_k" in effect_text
    assert "THIS.PIHC_var_state_temperature_population_share_percent = THIS.PIHC_var_state_temperature_population_weight" in effect_text
    assert "THIS.PIHC_var_country_temperature_weighted_comfort_sum = 0" in effect_text
    assert "THIS.PIHC_var_state_temperature_weighted_comfort = THIS.PIHC_var_state_temperature_population_weight" in effect_text
    assert "THIS.PIHC_var_state_temperature_weighted_comfort = THIS.PIHC_var_state_indoor_temperature_comfort_score" in effect_text
    assert "PREV.PIHC_var_country_temperature_weighted_comfort_sum = THIS.PIHC_var_state_temperature_weighted_comfort" in effect_text
    assert "PREV.PIHC_var_country_temperature_population_sum = THIS.PIHC_var_state_temperature_population_weight" in effect_text
    assert "THIS.PIHC_var_country_temperature_comfort_score" in effect_text
    for threshold in ("2.5", "4.0", "6.0", "7.5"):
        assert f"THIS.PIHC_var_country_temperature_comfort_score = {threshold}" in effect_text
    assert "THIS.PIHC_var_state_controller_temperature_comfort_score = PREV.PIHC_var_country_temperature_comfort_score" in effect_text
    assert "THIS.PIHC_var_state_controller_monthly_population = PREV.PIHC_var_country_monthly_population" in effect_text
    assert ("THIS.PIHC_var_state_controller_production_speed_buildings_factor = " "PREV.PIHC_var_country_production_speed_buildings_factor") in effect_text
    assert "THIS.PIHC_var_country_production_speed_buildings_factor" in effect_text
    assert "THIS.PIHC_var_state_production_speed_buildings_factor" in effect_text
    assert "THIS.PIHC_var_state_average_temperature_class" not in effect_text
    assert "THIS.PIHC_var_state_average_temperature = THIS.num_controlled_states" not in effect_text

    c26_history_text = c26_history_path.read_text(encoding="utf-8")
    assert "VAR_OUTDOOR_TEMPERATURE_INFLUENCE = -25" in c26_history_text

    on_action_text = on_action_path.read_text(encoding="utf-8")
    assert "on_startup = {" in on_action_text
    assert "global.PIHC_var_current_month = 12" in on_action_text
    assert "PIHC_initialize_state_outdoor_temperature_influence = yes" in on_action_text
    for modifier_id in (
        "PIHC_ERA_COLD_DYNAMIC_MODIFIER",
        "PIHC_state_temperature_subsidies_DYNAMIC_MODIFIER",
        "PIHC_state_temperature_DYNAMIC_MODIFIER",
        "PIHC_average_temperature_DYNAMIC_MODIFIER",
    ):
        assert f"modifier = {modifier_id}" in on_action_text
    assert "on_monthly = {" in on_action_text
    assert "PIHC_update_current_month = yes" in on_action_text
    assert re.search(
        r"on_startup\s*=\s*\{.*?random_country\s*=\s*\{.*?PIHC_update_monthly_global_variables\s*=\s*yes",
        on_action_text,
        re.DOTALL,
    )
    assert re.search(
        r"on_monthly\s*=\s*\{.*?random_country\s*=\s*\{.*?PIHC_update_monthly_global_variables\s*=\s*yes",
        on_action_text,
        re.DOTALL,
    )
    assert not re.search(
        r"^\t{3}PIHC_update_monthly_global_variables\s*=\s*yes$",
        on_action_text,
        re.MULTILINE,
    )
    assert not re.search(r"^\t{3}PIHC_update_current_month\s*=\s*yes$", on_action_text, re.MULTILINE)
    assert "on_monthly_C26" not in on_action_text

    terrain_var_effect_text = terrain_var_effect_path.read_text(encoding="utf-8")
    assert "Generated by projects/PIHC3/scripts/generate_state_temperature_terrain_vars.py" in terrain_var_effect_text
    assert "PIHC_initialize_state_outdoor_temperature_influence = {" in terrain_var_effect_text
    assert "every_state = {" in terrain_var_effect_text
    for state_id, minimum, maximum in (
        ("3", "0", "5"),
        ("175", "5", "15"),
        ("5", "-10", "0"),
        ("7", "-3", "0"),
        ("2", "-2", "3"),
    ):
        assert re.search(
            rf"^\s*{state_id}\s*=\s*\{{.*?"
            rf"VAR_OUTDOOR_TEMPERATURE_INFLUENCE_MIN\s*=\s*{minimum}.*?"
            rf"VAR_OUTDOOR_TEMPERATURE_INFLUENCE_MAX\s*=\s*{maximum}",
            terrain_var_effect_text,
            re.DOTALL | re.MULTILINE,
        )

    dynamic_modifier_text = dynamic_modifier_path.read_text(encoding="utf-8")
    assert "country_resource_cost_coal = THIS.PIHC_var_country_modifier_cost_coal" in dynamic_modifier_text
    assert "supply_factor = THIS.PIHC_var_state_supply_factor" in dynamic_modifier_text
    assert "state_production_speed_buildings_factor = THIS.PIHC_var_state_production_speed_buildings_factor" in dynamic_modifier_text
    assert "monthly_population = THIS.PIHC_var_country_monthly_population" in dynamic_modifier_text
    assert "production_speed_buildings_factor = THIS.PIHC_var_country_production_speed_buildings_factor" in dynamic_modifier_text

    cold_idea_text = cold_idea_path.read_text(encoding="utf-8")
    assert "country_resource_cost_coal = 5" in cold_idea_text
    assert "country_resource_cost_logs = 20" in cold_idea_text
    assert "monthly_population = -0.15" in cold_idea_text
    assert "weekly_manpower = -20" not in cold_idea_text

    loc_text = loc_path.read_text(encoding="utf-8")
    for loc_key in (
        "PIHC_ERA_COLD_DYNAMIC_MODIFIER",
        "PIHC_state_temperature_subsidies_DYNAMIC_MODIFIER",
        "PIHC_state_temperature_DYNAMIC_MODIFIER",
        "PIHC_average_temperature_DYNAMIC_MODIFIER",
        "pihc_state_temperature_tt",
        "pihc_state_temperature_outdoor_value",
        "pihc_state_temperature_class_1_colored",
        "pihc_state_temperature_class_7_colored",
        "pihc_state_temperature_national_modifiers_best",
        "pihc_state_temperature_national_modifiers_worst",
    ):
        assert f"[en.{loc_key}]" in loc_text
        assert f"[zh.{loc_key}]" in loc_text
    assert "GetPIHCStateTemperatureClassName" in loc_text
    assert "GetPIHCStateTemperatureNationalModifierEffects" in loc_text
    assert "[THIS.GetName]" in loc_text
    assert "PIHC_var_state_indoor_temperature|1" in loc_text
    assert "PIHC_var_state_outdoor_temperature|1" in loc_text
    assert "PIHC_var_state_outdoor_temperature|0" not in loc_text
    assert "PIHC_var_state_indoor_temperature_comfort_score|1" in loc_text
    assert "PIHC_var_state_temperature_population_share_percent|1" in loc_text
    assert "PIHC_var_state_controller_temperature_comfort_score|1" in loc_text

    scripted_loc_text = scripted_loc_path.read_text(encoding="utf-8")
    for loc_name in (
        "GetPIHCStateTemperatureClassName",
        "GetPIHCStateTemperatureNationalModifierEffects",
    ):
        assert f"name = {loc_name}" in scripted_loc_text
    for loc_key in (
        "pihc_state_temperature_class_1_colored",
        "pihc_state_temperature_class_2_colored",
        "pihc_state_temperature_class_3_colored",
        "pihc_state_temperature_class_4_colored",
        "pihc_state_temperature_class_5_colored",
        "pihc_state_temperature_class_6_colored",
        "pihc_state_temperature_class_7_colored",
        "pihc_state_temperature_national_modifiers_best",
        "pihc_state_temperature_national_modifiers_worst",
    ):
        assert f"localization_key = {loc_key}" in scripted_loc_text

    scripted_gui_text = scripted_gui_path.read_text(encoding="utf-8")
    assert "context_type = selected_state_context" in scripted_gui_text
    assert "parent_window_token = selected_state_view" in scripted_gui_text
    assert "window_name = pihc_state_temperature" in scripted_gui_text
    assert "frame = THIS.PIHC_var_state_indoor_temperature_frame" in scripted_gui_text
    assert "y = THIS.PIHC_temperature_mercury_y" in scripted_gui_text
    assert "pihc_state_temperature_mercury_indicator" in scripted_gui_text
    assert "dirty = global.PIHC_temperature_revision" in scripted_gui_text

    interface_gui_text = interface_gui_path.read_text(encoding="utf-8")
    assert "name = pihc_state_temperature" in interface_gui_text
    assert "orientation = lower_right" in interface_gui_text
    assert re.search(
        r"name\s*=\s*pihc_state_temperature.*?position\s*=\s*\{\s*x\s*=\s*30\s*y\s*=\s*-362\s*\}",
        interface_gui_text,
        re.DOTALL,
    )
    assert "x = -225" not in interface_gui_text
    assert "spriteType = GFX_pihc_state_temperature_base" in interface_gui_text
    assert "spriteType = GFX_pihc_state_temperature_scale" in interface_gui_text
    assert "spriteType = GFX_pihc_state_temperature_indoor_needle" in interface_gui_text
    assert "spriteType = GFX_pihc_state_temperature_mercury_indicator" in interface_gui_text
    assert interface_gui_text.count("pdx_tooltip = pihc_state_temperature_tt") >= 2
    assert "pihc_state_temperature_indoor_tt" not in interface_gui_text
    assert "pihc_state_temperature_outdoor_tt" not in interface_gui_text
    assert "name = pihc_state_temperature_outdoor_value" in interface_gui_text
    assert "text = pihc_state_temperature_outdoor_value" in interface_gui_text

    interface_gfx_text = interface_gfx_path.read_text(encoding="utf-8")
    assert 'texturefile = "gfx/interface/PIHC_state_temperature/base.dds"' in interface_gfx_text
    assert 'texturefile = "gfx/interface/PIHC_state_temperature/needle_strip.dds"' in interface_gfx_text
    assert 'texturefile = "gfx/interface/PIHC_state_temperature/mercury.dds"' in interface_gfx_text
    assert 'texturefile = "gfx/interface/PIHC_state_temperature/scale.dds"' in interface_gfx_text
    assert "noOfFrames = 82" not in interface_gfx_text
    for index in range(1, 8):
        assert f'name = "GFX_pihc_state_temperature_needle_{index}"' in interface_gfx_text
        assert f'texturefile = "gfx/interface/PIHC_state_temperature/needle_{index}.dds"' in interface_gfx_text


def test_pihc3_state_temperature_terrain_var_initializer_is_generated_from_map_data() -> None:
    project_root = PIHC3_ROOT
    script_path = project_root / "scripts/generate_state_temperature_terrain_vars.py"
    generated_effect_path = project_root / "src/modules/scripted_effect/PIHC_STATE_TEMPERATURE_TERRAIN_VARS/def.txt"
    spec = importlib.util.spec_from_file_location("generate_state_temperature_terrain_vars", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    ranges = module.state_temperature_ranges(project_root)

    assert ranges[3] == (0, 5)
    assert ranges[175] == (5, 15)
    assert ranges[5] == (-10, 0)
    assert ranges[7] == (-3, 0)
    assert ranges[2] == (-2, 3)
    assert 1 not in ranges
    assert module.render_state_temperature_initializer(project_root) == generated_effect_path.read_text(encoding="utf-8")


@PIHC3_FULL_BUILD_GROUP
def test_pihc3_state_temperature_build_plan_emits_runtime_artifacts(
    pihc3_full_build_result: BuildResult,
) -> None:
    result = pihc3_full_build_result
    artifact_paths = {str(artifact.path) for artifact in result.artifacts}

    expected_paths = {
        "common/scripted_effects/PIHC_STATE_TEMPERATURE.txt",
        "common/scripted_effects/PIHC_STATE_TEMPERATURE_TERRAIN_VARS.txt",
        "common/on_actions/PIHC_STATE_TEMPERATURE.txt",
        "common/scripted_guis/pihc_state_temperature.txt",
        "common/scripted_localisation/PIHC_state_temperature.txt",
        "common/dynamic_modifiers/PIHC_state_temperature_dynamic_modifiers.txt",
        "localisation/english/MODIFIER_DEFINITION_DYNAMIC_MODIFIERS_PIHC_STATE_TEMPERATURE_l_english.yml",
        "localisation/simp_chinese/MODIFIER_DEFINITION_DYNAMIC_MODIFIERS_PIHC_STATE_TEMPERATURE_l_simp_chinese.yml",
        "interface/PIHC_state_temperature.gui",
        "interface/PIHC_state_temperature.gfx",
        "gfx/interface/PIHC_state_temperature/base.dds",
        "gfx/interface/PIHC_state_temperature/progress_bar_frame.dds",
        "gfx/interface/PIHC_state_temperature/progress_red.dds",
        *(f"gfx/interface/PIHC_state_temperature/needle_{index}.dds" for index in range(1, 8)),
    }
    assert expected_paths <= artifact_paths
    assert not [diagnostic for diagnostic in result.diagnostics if "temperature" in str(diagnostic).lower()]


def test_pihc3_unit_modifier_registrations_match_defined_units() -> None:
    project_root = PIHC3_ROOT
    unit_root = project_root / "src/modules/unit"
    unit_modifier_path = unit_root / "UNIT_UNITS_UNIT_MODIFIERS_UNIT_MODIFIERS/common/units/unit_modifiers/unit_modifiers.txt"
    unit_modifier_text = unit_modifier_path.read_text(encoding="utf-8")
    unit_modifiers = re.findall(r"^\s*(modifier_army_sub_unit_[a-z0-9_]+)\s*$", unit_modifier_text, re.MULTILINE)

    unit_ids: set[str] = set()
    for path in unit_root.glob("*/common/units/*.txt"):
        unit_text = path.read_text(encoding="utf-8")
        unit_ids.update(re.findall(r"^    ([a-z][a-z0-9_]*)\s*=\s*\{", unit_text, re.MULTILINE))

    unit_tag_text = (project_root / "src/modules/common_data/COMMON_DATA_UNIT_TAGS_00_CATEGORIES/common/unit_tags/00_categories.txt").read_text(
        encoding="utf-8"
    )
    unit_category_ids = set(re.findall(r"^\s*(category_[a-z0-9_]+)\s*$", unit_tag_text, re.MULTILINE))
    known_suffixes = (
        "_org_recovery_cap_factor",
        "_max_org_factor",
        "_attack_factor",
        "_defence_factor",
        "_speed_factor",
    )

    unresolved: list[str] = []
    for modifier in unit_modifiers:
        target = modifier.removeprefix("modifier_army_sub_unit_")
        for suffix in known_suffixes:
            if target.endswith(suffix):
                target = target.removesuffix(suffix)
                break
        else:
            unresolved.append(f"{modifier}:unknown_suffix")
            continue

        if target.startswith("category_"):
            if target not in unit_category_ids:
                unresolved.append(f"{modifier}:{target}")
        elif target not in unit_ids:
            unresolved.append(f"{modifier}:{target}")

    stale_runtime_references: list[str] = []
    for path in (project_root / "src/modules").rglob("*.txt"):
        text = path.read_text(encoding="utf-8")
        for modifier in sorted(PIHC3_REMOVED_INVALID_UNIT_MODIFIER_IDS):
            if modifier in text:
                stale_runtime_references.append(f"{path.relative_to(project_root).as_posix()}:{modifier}")

    assert unresolved == []
    assert stale_runtime_references == []


@PIHC3_FULL_BUILD_GROUP
def test_pihc3_declares_logged_synchronized_dynamic_tokens(
    pihc3_full_build_result: BuildResult,
) -> None:
    project_root = PIHC3_ROOT
    token_path = project_root / "src/modules/common_data/COMMON_DATA_SYNCHRONIZED_DYNAMIC_TOKENS/common/synchronized_dynamic_tokens/tokens.txt"
    logged_dynamic_tokens = {
        "EQUIPMENT_MUFFIN_I",
        "EQUIPMENT_MUFFIN_II",
        "EQUIPMENT_MUFFIN_III",
        "aluminium",
        "anarchy",
        "arms_factory",
        "cannot_call_allies_or_join_wars",
        "coal",
        "cold_climate",
        "corporation",
        "crystals",
        "democratic",
        "despotic",
        "equatism",
        "fascism",
        "harmonicism",
        "industrial_complex",
        "logs",
        "oil",
    }

    token_text = token_path.read_text(encoding="utf-8")
    declared_tokens = {line.strip() for line in token_text.splitlines() if line.strip() and not line.lstrip().startswith("#")}
    result = pihc3_full_build_result
    output_paths = {str(artifact.path) for artifact in result.artifacts if artifact.target_root == "output"}

    assert logged_dynamic_tokens <= declared_tokens
    assert "common/synchronized_dynamic_tokens/tokens.txt" in output_paths


@PIHC3_FULL_BUILD_GROUP
def test_pihc3_replaces_vanilla_script_constants_country_groups(
    pihc3_full_build_result: BuildResult,
) -> None:
    project_root = PIHC3_ROOT
    manifest = _pihc3_manifest(project_root)
    country_groups_path = project_root / "src/modules/common_data/COMMON_DATA_SCRIPT_CONSTANTS_COUNTRY_GROUPS/common/script_constants/country_groups.txt"
    logged_vanilla_group_tags = {
        "AFG",
        "ALB",
        "ALG",
        "AUS",
        "AZR",
        "BAN",
        "BEL",
        "BHR",
        "BLC",
        "BRN",
        "BSK",
        "BUK",
        "BUL",
        "CHM",
        "CHI",
        "CIN",
        "CRI",
        "CZE",
        "DAG",
        "DEN",
        "DJI",
        "EGY",
        "EST",
        "FIN",
        "FRA",
        "FSA",
        "GAM",
        "GDC",
        "GER",
        "GNA",
        "GNB",
        "GRE",
        "GSM",
        "GXC",
        "HAR",
        "HBC",
        "HOL",
        "HUN",
        "ICE",
        "IMO",
        "INS",
        "IRQ",
        "ITA",
        "JOR",
        "KAS",
        "KAZ",
        "KBK",
        "KHI",
        "KHM",
        "KKP",
        "KLT",
        "KOS",
        "KUM",
        "KUR",
        "KYR",
        "LAT",
        "LBA",
        "LEB",
        "LIT",
        "LUX",
        "MAL",
        "MLD",
        "MLI",
        "MOR",
        "MRT",
        "NGA",
        "NGR",
        "NOR",
        "NXM",
        "OMA",
        "PAK",
        "PAL",
        "PER",
        "POL",
        "POR",
        "PRC",
        "QAT",
        "RIF",
        "RNG",
        "ROM",
        "SAB",
        "SAU",
        "SEN",
        "SHX",
        "SIC",
        "SIE",
        "SIK",
        "SIN",
        "SND",
        "SOK",
        "SOM",
        "SPR",
        "SUD",
        "SWE",
        "SWI",
        "SYR",
        "TAJ",
        "TAT",
        "TMS",
        "TUN",
        "TUR",
        "UAE",
        "UZB",
        "VOL",
        "WES",
        "XIC",
        "XSM",
        "YEM",
        "YUG",
        "YUN",
    }

    country_groups_text = country_groups_path.read_text(encoding="utf-8")
    declared_group_tags = set(re.findall(r"^\s*([A-Z0-9]{3})\s*$", country_groups_text, re.MULTILINE))
    result = pihc3_full_build_result
    output_paths = {str(artifact.path) for artifact in result.artifacts if artifact.target_root == "output"}
    expected_base_constant_files = {
        "common/script_constants/special_project_constants.txt",
    }

    assert "common/script_constants" in manifest["replace_path"]
    assert "country_groups = {" in country_groups_text
    assert not logged_vanilla_group_tags & declared_group_tags
    assert "common/script_constants/country_groups.txt" in output_paths
    assert expected_base_constant_files <= output_paths

    special_project_constants = (
        project_root / "src/modules/common_data/COMMON_DATA_SCRIPT_CONSTANTS_BASE/common/script_constants/special_project_constants.txt"
    ).read_text(encoding="utf-8")
    for constant_category in (
        "sp_complexity",
        "sp_time",
        "sp_scientist_xp_gain",
        "sp_progress",
    ):
        assert re.search(
            rf"^\s*{re.escape(constant_category)}\s*=\s*\{{",
            special_project_constants,
            re.MULTILINE,
        )


def test_pihc3_army_hq_ai_template_is_self_contained() -> None:
    module_root = PIHC3_ROOT / "src/modules/ai_config/AI_CONFIG_AI_TEMPLATES_HQ_SUPPORT"
    template_text = (module_root / "common/ai_templates/hq_support.txt").read_text(encoding="utf-8")
    module_files = {path.relative_to(module_root).as_posix() for path in module_root.rglob("*") if path.is_file()}

    assert module_files == {"common/ai_templates/hq_support.txt"}
    assert not (Path(str(module_root)) / "meta.yaml").exists()
    assert not (Path(str(module_root)) / ".paradev/meta.yaml").exists()
    for field in (
        "hq_generic = {",
        "role = hq_role",
        "hq_default = {",
        "hq_support_company = 1",
        "hq_infantry = 2",
    ):
        assert field in template_text
    assert not re.search(r"^\s*infantry\s*=\s*4\s*$", template_text, re.MULTILINE)


def test_pihc3_army_hq_support_is_self_contained_and_uses_pihc3_scale() -> None:
    unit_root = PIHC3_ROOT / "src/modules/unit/UNIT_UNITS_HQ_SUPPORT"
    history_root = PIHC3_ROOT / "src/modules/general_history/GENERAL_HISTORY_GENERAL_TAOG_HQ_TEMPLATE"
    unit_text = (unit_root / "common/units/hq_support.txt").read_text(encoding="utf-8")
    history_text = (history_root / "history/general/taog_hq_template.txt").read_text(encoding="utf-8")

    assert not (Path(str(unit_root)) / "meta.yaml").exists()
    assert not (Path(str(unit_root)) / ".paradev/meta.yaml").exists()
    assert not (Path(str(history_root)) / "meta.yaml").exists()
    assert not (Path(str(history_root)) / ".paradev/meta.yaml").exists()
    assert re.findall(r"^\t([a-z_]+)\s*=\s*\{$", unit_text, re.MULTILINE) == [
        "hq_support_company",
        "hq_infantry",
    ]
    assert unit_text.count("manpower = 8") == 1
    assert unit_text.count("manpower = 20") == 1
    assert unit_text.count("ARCHETYPE_INFANTRY = 16") == 1
    assert unit_text.count("ARCHETYPE_INFANTRY = 40") == 1
    assert "manpower = 200" not in unit_text
    assert "manpower = 600" not in unit_text

    regiment_count = len(re.findall(r"^\s*hq_infantry\s*=\s*\{", history_text, re.MULTILINE))
    support_count = len(re.findall(r"^\s*hq_support_company\s*=\s*\{", history_text, re.MULTILINE))
    assert regiment_count == 2
    assert support_count == 1
    assert regiment_count * 20 + support_count * 8 == 48
    assert regiment_count * 40 + support_count * 16 == 96
    assert set(_files_by_relative_path(unit_root)) == {"common/units/hq_support.txt"}
    assert set(_files_by_relative_path(history_root)) == {"history/general/taog_hq_template.txt"}


@PIHC3_FULL_BUILD_GROUP
def test_pihc3_current_hoi4_119_startup_error_contracts(
    pihc3_full_build_result: BuildResult,
) -> None:
    project_root = PIHC3_ROOT
    manifest = _pihc3_manifest(project_root)
    for replace_path in (
        "common/ai_templates",
        "common/technologies",
        "common/units",
        "history/general",
    ):
        assert replace_path in manifest["replace_path"]
    result = pihc3_full_build_result
    output_paths = {str(artifact.path) for artifact in result.artifacts if artifact.target_root == "output"}
    output_owners = {str(artifact.path): artifact.owner for artifact in result.artifacts if artifact.target_root == "output"}

    naval_project_path = (
        project_root
        / "src/modules/special_project/PIHC_SPECIAL_PROJECT_SUPPORT - PIHC Special Project Support/"
        / "common/special_projects/projects/naval_projects.txt"
    )
    naval_project_text = naval_project_path.read_text(encoding="utf-8")
    for project_id in (
        "sp_naval_cruiser_submarine",
        "sp_naval_midget_submarine",
        "sp_naval_submarine_carrier",
        "sp_naval_support_ships",
        "sp_naval_escort_carrier",
    ):
        assert re.search(rf"^\s*{re.escape(project_id)}\s*=\s*\{{", naval_project_text, re.MULTILINE)
    assert "common/special_projects/projects/naval_projects.txt" in output_paths

    script_enums = (project_root / "src/modules/common_data/COMMON_DATA_SCRIPT_ENUMS/common/script_enums.txt").read_text(encoding="utf-8")
    for enum_id in (
        "carrier_sub_detection",
        "carrier_surface_detection",
        "submarine_carrier_size",
        "support_ship",
    ):
        assert re.search(rf"^\s*{re.escape(enum_id)}\s*$", script_enums, re.MULTILINE)
    for enum_id in (
        "ship_hull_escort_carrier",
        "ship_hull_fleet_submarine",
        "ship_hull_carrier_submarine",
    ):
        assert not re.search(rf"^\s*{re.escape(enum_id)}\s*$", script_enums, re.MULTILINE)

    for modifier_id in ("naval_general_support", "naval_repair_support"):
        modifier_path = project_root / f"src/modules/modifier/{modifier_id}/def.txt"
        modifier_text = modifier_path.read_text(encoding="utf-8")
        assert re.search(rf"^\s*{re.escape(modifier_id)}\s*=\s*\{{", modifier_text, re.MULTILINE)

    unit_modifiers = (project_root / "src/modules/unit/UNIT_UNITS_UNIT_MODIFIERS_UNIT_MODIFIERS/common/units/unit_modifiers/unit_modifiers.txt").read_text(
        encoding="utf-8"
    )
    assert "modifier_army_sub_unit_infantry_magical_attack_factor" in unit_modifiers

    hq_unit_root = project_root / "src/modules/unit/UNIT_UNITS_HQ_SUPPORT"
    hq_unit_text = (hq_unit_root / "common/units/hq_support.txt").read_text(encoding="utf-8")
    hq_template_root = project_root / "src/modules/general_history/GENERAL_HISTORY_GENERAL_TAOG_HQ_TEMPLATE"
    hq_template_text = (hq_template_root / "history/general/taog_hq_template.txt").read_text(encoding="utf-8")
    hq_ai_root = project_root / "src/modules/ai_config/AI_CONFIG_AI_TEMPLATES_HQ_SUPPORT"
    hq_ai_text = (hq_ai_root / "common/ai_templates/hq_support.txt").read_text(encoding="utf-8")
    assert "common/units/hq_support.txt" in output_paths
    assert "history/general/taog_hq_template.txt" in output_paths
    assert "common/ai_templates/hq_support.txt" in output_paths
    assert output_owners["common/units/hq_support.txt"] == "module:unit/UNIT_UNITS_HQ_SUPPORT"
    assert output_owners["history/general/taog_hq_template.txt"] == "module:general_history/GENERAL_HISTORY_GENERAL_TAOG_HQ_TEMPLATE"
    assert output_owners["common/ai_templates/hq_support.txt"] == "module:ai_config/AI_CONFIG_AI_TEMPLATES_HQ_SUPPORT"
    for field in ("hq_support_company = {", "hq_infantry = {"):
        assert field in hq_unit_text
    assert hq_unit_text.count("allow_in_army_hq = yes") == 2
    assert hq_unit_text.count("allow_in_non_army_hq = no") == 2
    assert hq_unit_text.count('required_dlc = { "Thunder at Our Gates" }') == 2
    assert "ARCHETYPE_INFANTRY" in hq_unit_text
    for field in (
        "manpower = 8",
        "manpower = 20",
        "ARCHETYPE_INFANTRY = 16",
        "ARCHETYPE_INFANTRY = 40",
    ):
        assert hq_unit_text.count(field) == 1
    assert not re.search(r"^\s*(infantry|support|motorized)_equipment\s*=", hq_unit_text, re.MULTILINE)
    for field in (
        "every_possible_country = {",
        'has_dlc = "Thunder at Our Gates"',
        'localization_key = "ARMY_HQ_TEMPLATE_NAME"',
        "template_counter = 121",
        "is_army_hq = yes",
        "hq_infantry = {",
        "hq_support_company = {",
    ):
        assert field in hq_template_text
    for field in (
        "hq_generic = {",
        "role = hq_role",
        "hq_default = {",
        "hq_support_company = 1",
        "hq_infantry = 2",
    ):
        assert field in hq_ai_text

    [sombra_root] = (project_root / "src/modules/trait").glob("TRAIT_KING_SOMBRA_KING_OF_MONSTERS - *")
    sombra_trait = (sombra_root / "def.txt").read_text(encoding="utf-8")
    assert "//" not in sombra_trait
    assert re.search(
        r"^\s*modifier_army_sub_unit_infantry_magical_attack_factor\s*=\s*0\.35\s*$",
        sombra_trait,
        re.MULTILINE,
    )
    assert not re.search(r"^\s*infantry_magical_attack_factor\s*=", sombra_trait, re.MULTILINE)
    assert not (sombra_root / ".paradev/meta.yaml").exists()

    c04_focus = (project_root / "src/modules/focus/FOCUS_C04_THE_INVESTIGATION_TEAM - 成立调查组/def.txt").read_text(encoding="utf-8")
    assert "custom_effect_tooltip = {" not in c04_focus
    assert "custom_effect_tooltip = FOCUS_C04_THE_INVESTIGATION_TEAM_TOOLTIP" in c04_focus
    assert "set_country_flag = PIHC_COUNTRY_FLAG_SEPAL_INVESTIGATION" in c04_focus

    abilities = (
        project_root / "src/modules/common_data/COMMON_DATA_ABILITIES_GENERIC_LEADER_ABILITIES/common/abilities/generic_leader_abilities.txt"
    ).read_text(encoding="utf-8")
    assert "add_temporary_buff_to_units" not in abilities
    assert "unit_modifiers = {" in abilities
    assert "combat_entrenchment = 0.25" in abilities

    doctrine_startup = (project_root / "src/modules/on_action/PIHC_ALL_DOCTRINE/def.txt").read_text(encoding="utf-8")
    assert "add_naval_experience" not in doctrine_startup
    assert "navy_experience = 50" in doctrine_startup

    occupation_laws = (
        project_root / "src/modules/common_data/COMMON_DATA_OCCUPATION_LAWS_OCCUPATION_LAWS/common/occupation_laws/occupation_laws.txt"
    ).read_text(encoding="utf-8")
    assert re.search(
        r"missing_garrison_scaled_effect\s*=\s*\{[^}]*missing_garrison_law\s*=\s*yes",
        occupation_laws,
        re.DOTALL,
    )

    focus_style_path = (
        project_root / "src/modules/game_asset/GAME_ASSET_COMMON_NATIONAL_FOCUS_TITLEBAR_STYLES/" / "common/national_focus/00_titlebar_styles.txt"
    )
    focus_style_text = focus_style_path.read_text(encoding="utf-8")
    assert re.search(
        r"style\s*=\s*\{[^}]*name\s*=\s*default_style[^}]*default\s*=\s*yes",
        focus_style_text,
        re.DOTALL,
    )
    assert "common/national_focus/00_titlebar_styles.txt" in output_paths


def test_pihc3_hoi4_119_nonexistent_runtime_tokens_are_removed() -> None:
    project_root = PIHC3_ROOT

    def enum_entries(text: str, enum_name: str) -> set[str]:
        match = re.search(
            rf"{re.escape(enum_name)}\s*=\s*\{{(?P<body>.*?)^\s*\}}",
            text,
            re.MULTILINE | re.DOTALL,
        )
        assert match is not None
        return {line.split("#", 1)[0].strip() for line in match.group("body").splitlines() if line.split("#", 1)[0].strip()}

    abilities = (
        project_root / "src/modules/common_data/COMMON_DATA_ABILITIES_GENERIC_LEADER_ABILITIES/common/abilities/generic_leader_abilities.txt"
    ).read_text(encoding="utf-8")
    assert "combat_entrenchment = 0.25" in abilities
    assert not re.search(r"^\s*entrenchment\s*=\s*0\.25\s*$", abilities, re.MULTILINE)

    script_enums = (project_root / "src/modules/common_data/COMMON_DATA_SCRIPT_ENUMS/common/script_enums.txt").read_text(encoding="utf-8")
    equipment_stats = enum_entries(script_enums, "script_enum_equipment_stat")
    equipment_categories = enum_entries(script_enums, "script_enum_equipment_category")
    equipment_bonus_types = enum_entries(script_enums, "script_enum_equipment_bonus_type")
    logged_equipment_stats = {
        "carrier_sub_detection",
        "carrier_surface_detection",
        "submarine_carrier_size",
    }
    undefined_hull_bonus_types = {
        "ship_hull_escort_carrier",
        "ship_hull_fleet_submarine",
        "ship_hull_carrier_submarine",
    }

    assert logged_equipment_stats <= equipment_stats
    assert "support_ship" in equipment_categories
    assert not logged_equipment_stats & equipment_bonus_types
    assert not undefined_hull_bonus_types & equipment_bonus_types

    doctrine_root = project_root / "src/modules/doctrine/PIHC_DOCTRINE_SUPPORT/common/doctrines/subdoctrines/sea"
    doctrine_text = "\n".join(path.read_text(encoding="utf-8") for path in doctrine_root.glob("*.txt"))
    for undefined_hull in sorted(undefined_hull_bonus_types):
        assert undefined_hull not in doctrine_text

    faction_goals = (
        project_root / "src/modules/faction_rule/FACTION_RULE_GOALS_FACTION_GOALS_SHORT_TERM/common/factions/goals/faction_goals_short_term.txt"
    ).read_text(encoding="utf-8")
    assert "mechanical_computing" not in faction_goals


def test_pihc3_hoi4_119_stale_scripted_references_are_removed() -> None:
    project_root = PIHC3_ROOT

    c01_angry_category = (project_root / "src/collections/decision/DECISION_CATEGORY_C01_ANGRY/def.txt").read_text(encoding="utf-8")
    assert "FOCUS_C01_ANGRY" not in c01_angry_category
    assert re.search(r"visible\s*=\s*\{[^}]*tag\s*=\s*C01", c01_angry_category, re.DOTALL)

    cmc_uprise_event = (project_root / "src/modules/event/CMC_UPRISE/def.txt").read_text(encoding="utf-8")
    assert "FOCUS_C08_CANTERLOT_CMC_RURAL" not in cmc_uprise_event
    assert "FOCUS_C08_CANTERLOT_CMC_CITY" not in cmc_uprise_event
    assert cmc_uprise_event.count("has_completed_focus = FOCUS_C08_UPRISE") == 2

    intelligence_upgrades = (
        project_root
        / "src/modules/common_data/COMMON_DATA_INTELLIGENCE_AGENCY_UPGRADES_INTELLIGENCE_AGENCY_UPGRADES/"
        / "common/intelligence_agency_upgrades/intelligence_agency_upgrades.txt"
    ).read_text(encoding="utf-8")
    assert "lar_local_recruitment" not in intelligence_upgrades

    bce_support_path = (
        project_root
        / "src/modules/decision/DECISION_SUPPORT_PIHC_BCE_BORDER_CONFLICT_SUPPORT - PIHC BCE Border Conflict Decision Support/"
        / "common/decisions/DECISION_BCE_border_conflicts.txt"
    )
    bce_support = bce_support_path.read_text(encoding="utf-8")
    bce_events = (project_root / "src/modules/event/BCE - Border conflict events/def.txt").read_text(encoding="utf-8")
    from paradev.sdk import Project

    registry = Project.load(project_root)._build_registry(profile="hoi4")
    decision_matches = {slot.match for slot in registry.source_slots_for("decision")}

    assert "BCE_border_conflict_time_until_cancelled = {" in bce_support
    assert "BCE_border_conflict_time_until_cancelled" in bce_events
    assert any("common/decisions" in match for match in decision_matches)


def test_pihc3_doctrine_subdoctrines_do_not_repeat_logged_modifier_keys() -> None:
    from collections import Counter

    from paradev.pdx import PDXBlock, PDXScalar

    project_root = PIHC3_ROOT
    duplicate_prone_modifier_keys = {
        "ace_effectiveness_factor",
        "additional_brigade_column_size",
        "air_accidents_factor",
        "air_ace_bonuses_factor",
        "air_ace_generation_chance_factor",
        "air_bombing_targetting",
        "air_cas_efficiency",
        "air_interception_detect_factor",
        "air_manpower_requirement_factor",
        "air_superiority_detect_factor",
        "air_superiority_efficiency",
        "air_untrained_pilots_penalty_factor",
        "air_weather_penalty",
        "army_bonus_air_superiority_factor",
        "army_core_defence_factor",
        "army_intel_decryption_bonus",
        "army_intel_to_others",
        "army_morale_factor",
        "army_org_factor",
        "army_speed_factor",
        "civilian_intel_to_others",
        "command_power_gain",
        "conscription_factor",
        "coordination_bonus",
        "dig_in_speed_factor",
        "experience_gain_army_factor",
        "experience_loss_factor",
        "ground_attack_factor",
        "industrial_capacity_factory",
        "intel_network_gain_factor",
        "land_bunker_effectiveness_factor",
        "land_reinforce_rate",
        "max_dig_in_factor",
        "minimum_training_level",
        "org_loss_when_moving",
        "planning_speed",
        "pocket_penalty",
        "recon_factor_while_entrenched",
        "resistance_damage_to_garrison_on_our_occupied_states",
        "strategic_bomb_visibility",
        "supply_factor",
        "supply_node_range",
        "surrender_limit",
        "terrain_penalty_reduction",
        "training_time_army_factor",
        "unit_upkeep_attrition_factor",
    }
    subdoctrine_paths: list[Path] = []
    for path in sorted((project_root / "src/modules/doctrine").glob("*/def.txt")):
        root = PDXBlock.from_str(path.read_text(encoding="utf-8"))
        if any(
            isinstance(doctrine.val, PDXBlock) and any(field.key_str == "track" and isinstance(field.val, PDXScalar) for field in doctrine.val.entries)
            for doctrine in root.entries
        ):
            subdoctrine_paths.append(path)

    assert len(subdoctrine_paths) == 43

    def count_keys(block: PDXBlock, counter: Counter[str]) -> None:
        for entry in block.entries:
            key = str(entry.key.val)
            if key in duplicate_prone_modifier_keys:
                counter[key] += 1
            if isinstance(entry.val, PDXBlock):
                count_keys(entry.val, counter)

    repeated: list[str] = []
    for path in subdoctrine_paths:
        root = PDXBlock.from_str(path.read_text(encoding="utf-8"))
        for subdoctrine in root.entries:
            if not isinstance(subdoctrine.val, PDXBlock):
                continue
            counts: Counter[str] = Counter()
            count_keys(subdoctrine.val, counts)
            for key, count in counts.items():
                if count > 1:
                    repeated.append(f"{path.as_posix()}:{subdoctrine.key.val}:{key}:{count}")

    assert repeated == []


def test_pihc3_project_tree_has_no_generated_python_or_tool_cache_files() -> None:
    project_root = PIHC3_ROOT
    generated = [
        path.relative_to(project_root).as_posix()
        for pattern in (
            ".pytest_cache",
            ".ruff_cache",
            "__pycache__",
            "*.pyc",
        )
        for path in project_root.rglob(pattern)
        if ".git" not in path.parts
    ]

    assert sorted(generated) == []


def test_pihc3_generated_runtime_directories_are_local_only() -> None:
    project_root = PIHC3_ROOT
    ignore_text = (project_root / ".gitignore").read_text(encoding="utf-8")
    readme_text = (project_root / "README.md").read_text(encoding="utf-8")

    for entry in (".paradev/", "build/"):
        assert entry in ignore_text
    for entry in (".DS_Store", ".pytest_cache/", ".ruff_cache/", "__pycache__/", "*.pyc"):
        assert entry in ignore_text
    assert "The clean wrapper removes that data while leaving the live output directory and `PIHC3.mod` present" in readme_text
    assert "Python bytecode, pytest caches, and Ruff caches anywhere under the PIHC3 project tree are also generated local-only files." in readme_text


def test_pihc3_source_root_contains_only_current_project_roots() -> None:
    project_root = PIHC3_ROOT
    source_roots = {path.name for path in (project_root / "src").iterdir() if path.is_dir()}
    migration_text = (project_root / "docs" / "migration" / "README.md").read_text(encoding="utf-8")
    readme_text = (project_root / "README.md").read_text(encoding="utf-8")

    assert source_roots == {"collections", "modules"}
    assert not (project_root / "src/.paradev").exists()
    inactive_bookmark = project_root / "src/modules/bookmark/PIHC_DIE_NEBENWELT - 新世界"
    assert (inactive_bookmark / "def.txt").is_file()
    assert yaml.safe_load((Path(str(inactive_bookmark)) / "meta.yaml").read_text(encoding="utf-8")) == {"inactive": True}
    assert not (project_root / "src/inactive_modules").exists()
    assert not (project_root / ".keep").exists()
    assert "Native modules live under `src/modules/`." in readme_text
    assert "Native collections live under `src/collections/`." in readme_text
    assert "src/general/" not in migration_text
    assert "src/countries/" not in migration_text
    assert "older v3 source folders" not in migration_text


def test_pihc3_inactive_bookmark_stays_authorable_but_not_compilable() -> None:
    from paradev.sdk import Project

    project = Project.load(PIHC3_ROOT)
    browser = project.browser(family="bookmark")
    modules = {row["module_id"]: row for row in browser["items"] if row["kind"] == "module"}

    assert set(modules) == {
        "bookmark/PIHC",
        "bookmark/PIHC_DIE_NEBENWELT",
    }
    assert modules["bookmark/PIHC"]["active"] is True
    assert modules["bookmark/PIHC_DIE_NEBENWELT"]["active"] is False
    assert [module.module_id for module in project.build(family="bookmark").modules] == ["bookmark/PIHC"]


def test_pihc3_generic_focus_tree_uses_collection_output_identity() -> None:
    from paradev.sdk import Project

    project_root = PIHC3_ROOT
    collection_root = project_root / "src/collections/focus/generic - Generic"
    project = Project.load(project_root)
    result = project.build(
        family="focus",
        collection_id="generic",
        strict_metadata=True,
        parallelism=1,
    )

    assert collection_root.name == "generic - Generic"
    assert not (Path(str(collection_root)) / "meta.yaml").exists()
    assert not (Path(str(collection_root)) / ".paradev/meta.yaml").exists()
    assert result.diagnostics == ()
    assert result.blocked is False
    assert len(result.collections) == 1
    collection = result.collections[0]
    assert collection.collection_id == "generic"
    assert collection.family == "focus"
    assert len(collection.module_ids) == 55
    assert "id = generic_focus" in (collection_root / "def.txt").read_text(encoding="utf-8")


@PIHC3_FULL_BUILD_GROUP
def test_pihc3_build_has_no_external_copy_root_dependencies(
    pihc3_full_build_result: BuildResult,
) -> None:
    from paradev.project import Project

    manifest = yaml.safe_load((PIHC3_ROOT / "paradev.yaml").read_text(encoding="utf-8"))
    project = Project.load(PIHC3_ROOT)
    result = pihc3_full_build_result
    copy_owned_paths = sorted(str(artifact.path) for artifact in result.artifacts if artifact.owner.startswith("copy_root:"))
    copy_root_diagnostics = sorted(diagnostic.code for diagnostic in result.diagnostics if diagnostic.code.startswith("copy_root."))

    assert "copy_roots" not in manifest
    assert not project.copy_roots
    assert copy_owned_paths == []
    assert copy_root_diagnostics == []


def test_pihc3_module_batch_request_previews_current_project_targets() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    module_id = "technology/TECHNOLOGY_AIR_CLOUDSHIP"
    module = next(module for module in project.discover_modules().modules if module.module_id == module_id)
    module_root = Path(module.root)
    module_relative_root = module_root.relative_to(project.root).as_posix()
    def_text = (module_root / "def.txt").read_text(encoding="utf-8")
    missing_relative_path = "migration/__paradev_batch_request_contract_DO_NOT_WRITE__.txt"
    missing_path = module_root / missing_relative_path
    missing_text = "generated by batch request contract test\n"

    assert not missing_path.exists()

    request = project.module_batch_edit_request(
        [
            {
                "module_id": module_id,
                "relative_path": "def.txt",
                "text": def_text,
            },
            {
                "module_id": module_id,
                "relative_path": missing_relative_path,
                "text": missing_text,
            },
        ],
        create=True,
    )

    assert request["schema"] == "paradev.module.batch_edit_request.v1"
    assert request["project_id"] == "PIHC3"
    assert request["summary"] == {
        "edit_count": 2,
        "module_count": 1,
        "source_root_count": 0,
        "existing_target_count": 1,
        "missing_target_count": 1,
        "changed_target_count": 1,
        "unchanged_target_count": 1,
        "create_enabled_count": 2,
        "encoding_count": 1,
    }
    assert request["targets"] == [
        {
            "edit_index": 0,
            "module_id": module_id,
            "family": "technology",
            "object_id": "TECHNOLOGY_AIR_CLOUDSHIP",
            "relative_path": "def.txt",
            "target_relative_path": f"{module_relative_root}/def.txt",
            "exists": True,
            "created": False,
            "changed": False,
            "create": True,
            "encoding": "utf-8",
            "size_bytes": len(def_text.encode("utf-8")),
        },
        {
            "edit_index": 1,
            "module_id": module_id,
            "family": "technology",
            "object_id": "TECHNOLOGY_AIR_CLOUDSHIP",
            "relative_path": missing_relative_path,
            "target_relative_path": f"{module_relative_root}/{missing_relative_path}",
            "exists": False,
            "created": True,
            "changed": True,
            "create": True,
            "encoding": "utf-8",
            "size_bytes": len(missing_text.encode("utf-8")),
        },
    ]
    assert request["target_index"]["changed"] == {"false": [0], "true": [1]}
    assert request["target_index"]["created"] == {"false": [0], "true": [1]}

    preview = project.write_module_files(
        request["edits"],
        create=request["create"],
        encoding=request["encoding"],
        write=False,
    )

    assert preview["schema"] == "paradev.module.batch_edit.v1"
    assert preview["written"] is False
    assert preview["file_count"] == 2
    assert preview["created_count"] == 1
    assert preview["updated_count"] == 1
    assert preview["changed_count"] == 1
    assert preview["unchanged_count"] == 1
    assert [row["module_relative_path"] for row in preview["files"]] == [
        "def.txt",
        missing_relative_path,
    ]
    assert [row["created"] for row in preview["files"]] == [False, True]
    assert [row["changed"] for row in preview["files"]] == [False, True]
    assert not missing_path.exists()


def test_pihc3_manual_source_policy_matches_current_project_roots() -> None:
    project_root = PIHC3_ROOT
    local_only_dirs = {
        ".git",
        ".cache",
        ".paradev",
        ".pytest_cache",
        ".ruff_cache",
        "build",
        "copies",
        "secret",
    }
    project_dirs = {path.name for path in project_root.iterdir() if path.is_dir() and path.name not in local_only_dirs}
    manual_text = Path("docs/user-manual/pihc3.md").read_text(encoding="utf-8")

    assert project_dirs == {"assets", "docs", "extensions", "scripts", "src"}
    assert "legacy `copies/` overlay" not in manual_text
    assert "| `copies/` |" not in manual_text
    assert (
        "| `extensions/` | Project-owned HeavenBase Entity and compiler code; "
        "generated descriptors stay under each extension's `.paradev/` folder. |" in manual_text
    )
    assert "| `system/` |" not in manual_text
    assert "| `scripts/review/` | Optional historical parity review utilities. |" in manual_text


def test_pihc3_inventory_items_are_minimal_self_contained_modules() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("inventory_item")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("inventory_item")

    assert by_name["definition"].match == "item.json"
    assert by_name["definition"].required is True
    assert by_name["definition"].kind is None
    assert by_name["icon"].kind == "copy"
    assert by_name["icon"].many is True
    assert family.pdx_path_template is None
    assert family.copy_path_template == "gfx/interface/inventory_items/{source_name}"
    assert {row["artifact_type"] for row in family.generated_outputs} == {
        "loc",
        "pdx",
    }

    module_roots = sorted(
        (PIHC3_ROOT / "src/modules/inventory_item").iterdir(),
        key=lambda path: path.name,
    )
    assert len(module_roots) == 80
    for module_root in module_roots:
        object_id, folder_title = module_root.name.split(" - ", 1)
        item_key = f"INVENTORY_ITEM_{object_id}"
        rows = _section_localization_rows(module_root / "main.loc")
        definition = load_json(str(module_root / "item.json"))
        visible_names = {path.name for path in module_root.iterdir() if not path.name.startswith(".")}

        assert not (module_root / "meta.yaml").exists()
        assert not (module_root / ".paradev").exists()
        assert not (module_root / "legacy").exists()
        assert folder_title == rows["l_simp_chinese"][item_key]
        assert rows["l_english"][item_key]
        assert definition == {"helper_quantities": ("1-100, 150-3000/50, 9999, 20000, 50000, 99999")}
        assert all(f"{item_key}_COUNT" not in language_rows for language_rows in rows.values())
        assert visible_names == {"icons", "item.json", "main.loc"}


def test_pihc3_superevent_family_uses_shared_picture_and_sprite_slots() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("superevent")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("superevent")

    assert by_name["pictures"].kind == "copy"
    assert by_name["pictures"].many is True
    assert family.copy_path_template == "gfx/event_pictures/{source_name}"
    assert family.sprite_gfx_path_template == "interface/PIHC3_superevents.gfx"
    assert family.sprite_slots == ("pictures",)


def test_pihc3_state_lore_family_uses_aggregate_slots() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("state_lore")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("state_lore")

    assert family.family_kind == "state_lore_aggregate"
    assert set(by_name) == {"variants", "loc"}
    assert by_name["variants"].kind == "pdx"
    assert by_name["variants"].required is False
    assert by_name["variants"].match == "variants.pdx"
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert by_name["loc"].required is True
    assert family.settings_keys == ()
    assert family.title_loc_keys == ()
    assert family.scripted_localisation_path == "common/scripted_localisation/PIHC_STATE_LORES.txt"
    assert family.on_actions_path == "common/on_actions/PIHC_STATE_LORES.txt"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"

    family.validate_source_text(
        module_id="state_lore/STATE_LORE_217",
        relative_path="variants.pdx",
        text=("variant = {\n" "    localization_key = STATE_LORE_217_0\n" "    trigger = { has_global_flag = PIHC_GLOBAL_FLAG_EXAMPLE }\n" "}\n"),
    )
    with pytest.raises(ValueError, match="compiler-owned check_variable"):
        family.validate_source_text(
            module_id="state_lore/STATE_LORE_217",
            relative_path="variants.pdx",
            text=(
                "variant = {\n" "    localization_key = STATE_LORE_217_0\n" "    trigger = { check_variable = { state_lore_text_state_id = 217.id } }\n" "}\n"
            ),
        )


def test_pihc3_compact_state_lores_preserve_aggregate_payloads(
    tmp_path: Path,
) -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    project = replace(
        project,
        output_root=tmp_path / "mod",
        build_root=tmp_path / "build",
    )

    result = project.build(
        family="state_lore",
        strict_metadata=True,
        emit_artifacts=True,
    )
    errors = [diagnostic for diagnostic in result.diagnostics if diagnostic.severity == "error"]
    scripted = project.output_root / "common/scripted_localisation/PIHC_STATE_LORES.txt"
    on_actions = project.output_root / "common/on_actions/PIHC_STATE_LORES.txt"

    assert result.blocked is False
    assert errors == []
    assert len(result.modules) == 79
    assert len(result.collections) == 0
    assert len(result.artifacts) == 9_299
    assert sha256hash(scripted.read_text(encoding="utf-8")) == ("823d0cf20ee03fe9ff427a90815a04ee8fa0c958b3a31579f883ceec2b4f64fa")
    assert sha256hash(on_actions.read_text(encoding="utf-8")) == ("08515f2d1c1bb9b3b9f022dab45dd65c8ee13523208c5e6c716c567ced2774b1")


def test_pihc3_equipment_module_family_uses_aggregate_slots_and_category_icons() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    module_slots = {slot.name: slot for slot in registry.source_slots_for("equipment_module")}
    category_slots = {slot.name: slot for slot in registry.source_slots_for("equipment_module_category")}
    module_family = registry.family("equipment_module")
    category_family = registry.family("equipment_module_category")

    assert module_family.family_kind == "equipment_module_aggregate"
    assert module_slots["def"].kind == "pdx"
    assert module_slots["def"].required is True
    assert module_slots["icon"].kind == "copy"
    assert module_slots["loc"].kind == "loc"
    assert module_family.output_paths == {
        "plane": "common/units/equipment/modules/00_plane_modules.txt",
        "tank": "common/units/equipment/modules/00_tank_modules.txt",
    }
    assert category_slots["icon"].kind == "copy"
    assert category_slots["loc"].kind == "loc"
    assert category_family.copy_path_template == "gfx/interface/modules/GFX_EMI_{object_id}{source_suffix}"
    assert category_family.sprite_gfx_path_template == "interface/PIHC3_equipment_module_categories.gfx"


def test_pihc3_equipment_family_preserves_compiled_interface_assets() -> None:
    from paradev.build import BuildContext, ModuleSourceBundle, PDXBlockSource
    from paradev.pdx import PDXBlock
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("equipment")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("equipment")

    assert by_name["def"].kind == "pdx"
    assert by_name["def"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert by_name["assets"].kind == "copy"
    assert by_name["assets"].many is True
    assert family.family_kind == "equipment_aggregate"
    assert family.output_path == "common/units/equipment/zz_all_equipments.txt"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"
    assert family.copy_path_template == "{source_path}"

    parent = ModuleSourceBundle(
        root="parent",
        source_slots={"def": ("def.txt",)},
        metadata={"object_id": "EQUIPMENT_PARENT", "settings": {"legacy_order": 1}},
        pdx_sources=(
            PDXBlockSource(
                slot="def",
                path="def.txt",
                block=PDXBlock.from_str("""
                    equipments = {
                        EQUIPMENT_PARENT = {
                            is_archetype = no
                        }
                    }
                    """),
            ),
        ),
    ).to_module(family="equipment")
    child = ModuleSourceBundle(
        root="child",
        source_slots={"def": ("def.txt",)},
        metadata={"object_id": "EQUIPMENT_CHILD", "settings": {"legacy_order": 2}},
        pdx_sources=(
            PDXBlockSource(
                slot="def",
                path="def.txt",
                block=PDXBlock.from_str("""
                    equipments = {
                        EQUIPMENT_CHILD = {
                            archetype = EQUIPMENT_PARENT
                            parent = EQUIPMENT_PARENT
                        }
                    }
                    """),
            ),
        ),
    ).to_module(family="equipment")

    artifacts = family.emit(BuildContext(project_id="PIHC3"), (child, parent), ())
    pdx_artifacts = {str(artifact.path): artifact.payload.to_str() for artifact in artifacts if artifact.artifact_type == "pdx"}

    assert set(pdx_artifacts) == {"common/units/equipment/zz_all_equipments.txt"}
    aggregate_text = pdx_artifacts["common/units/equipment/zz_all_equipments.txt"]
    assert aggregate_text.index("EQUIPMENT_PARENT = {") < aggregate_text.index("EQUIPMENT_CHILD = {")


def test_pihc3_equipment_sources_define_chassis_module_limit_tooltips() -> None:
    light_loc = (PIHC3_ROOT / "src/modules/equipment/ARCHETYPE_TANK_LIGHT - \u8f7b\u578b\u5766\u514b/main.loc").read_text(encoding="utf-8")
    medium_loc = (PIHC3_ROOT / "src/modules/equipment/ARCHETYPE_TANK_MEDIUM - \u4e2d\u578b\u5766\u514b/main.loc").read_text(encoding="utf-8")

    assert "[l_english.CHASSIS_HAS_ONLY_8_SLOTS_TOOLTIP]" in light_loc
    assert "This hull can only have §Y8§! modules installed." in light_loc
    assert "[l_simp_chinese.CHASSIS_HAS_ONLY_8_SLOTS_TOOLTIP]" in light_loc
    assert "[l_english.CHASSIS_HAS_ONLY_9_SLOTS_TOOLTIP]" in medium_loc
    assert "This hull can only have §Y9§! modules installed." in medium_loc
    assert "[l_simp_chinese.CHASSIS_HAS_ONLY_9_SLOTS_TOOLTIP]" in medium_loc


def test_pihc3_unit_family_preserves_compiled_unit_support_paths() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("unit")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("unit")

    assert by_name["pdx"].kind == "pdx"
    assert by_name["pdx"].many is True
    assert by_name["pdx"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert by_name["loc"].required is False
    assert family.pdx_path_template == "{source_path}"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"


def test_pihc3_native_unit_modifier_definition_uses_current_ids_and_localization() -> None:
    from paradev.pdx import PDXBlock

    module_root = PIHC3_ROOT / "src/modules/unit/UNIT_UNITS_UNIT_MODIFIERS_UNIT_MODIFIERS"
    source_path = module_root / "common/units/unit_modifiers/unit_modifiers.txt"
    loc_path = module_root / "main.loc"
    block = PDXBlock.from_file(source_path)

    assert set(_files_by_relative_path(module_root)) == {
        "main.loc",
        "common/units/unit_modifiers/unit_modifiers.txt",
    }
    assert not (module_root / ".paradev").exists()
    assert len(block.entries) == 1
    wrapper = block.entries[0]
    assert wrapper.key_str == "sub_unit_modifiers"
    assert isinstance(wrapper.val, PDXBlock)
    modifier_ids = tuple(entry.key_str for entry in wrapper.val.entries)
    assert modifier_ids == PIHC3_NATIVE_UNIT_MODIFIER_IDS
    assert PIHC3_REMOVED_INVALID_UNIT_MODIFIER_IDS.isdisjoint(modifier_ids)

    locs = _section_localization_rows(loc_path)
    localized_ids = {key for rows in locs.values() for key in rows}
    magical_id = "modifier_army_sub_unit_infantry_magical_attack_factor"
    other_ids = set(PIHC3_NATIVE_UNIT_MODIFIER_IDS) - {magical_id}
    assert localized_ids == set(PIHC3_NATIVE_UNIT_MODIFIER_IDS)
    assert PIHC3_REMOVED_INVALID_UNIT_MODIFIER_IDS.isdisjoint(localized_ids)
    assert locs["l_english"][magical_id] == "Infantry Magical Attack"
    assert locs["l_simp_chinese"][magical_id] == "步兵魔法攻击"
    assert set(locs["l_english"]) == set(PIHC3_NATIVE_UNIT_MODIFIER_IDS)
    assert set(locs["l_simp_chinese"]) == set(PIHC3_NATIVE_UNIT_MODIFIER_IDS)
    for language, rows in locs.items():
        if language not in {"l_english", "l_simp_chinese"}:
            assert set(rows) == other_ids

    source_text = source_path.read_text(encoding="utf-8")
    loc_text = loc_path.read_text(encoding="utf-8")
    assert not any(modifier_id in source_text for modifier_id in PIHC3_REMOVED_INVALID_UNIT_MODIFIER_IDS)
    assert not any(modifier_id in loc_text for modifier_id in PIHC3_REMOVED_INVALID_UNIT_MODIFIER_IDS)
    for path in _files_by_relative_path(module_root).values():
        _assert_single_trailing_newline(path)


def test_pihc3_entity_family_preserves_model_paths_with_generic_slots() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("entity")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("entity")
    route = family.routes["basic"]
    compiled_route = family.routes["compiled_records"]
    family_view = next(row for row in registry.to_view()["families"] if row["family"] == "entity")

    assert family.family_kind == "routed_source"
    assert by_name["pdx"].kind == "pdx"
    assert by_name["pdx"].match == "^gfx/models/.*\\.(gfx|asset)$"
    assert by_name["pdx"].many is True
    assert by_name["pdx"].required is False
    assert by_name["meshes"].kind == "copy"
    assert by_name["meshes"].match == "^gfx/models/.*\\.mesh$"
    assert by_name["meshes"].many is True
    assert by_name["meshes"].required is False
    assert by_name["animations"].kind == "copy"
    assert by_name["animations"].match == "^gfx/models/.*\\.anim$"
    assert by_name["animations"].many is True
    assert by_name["animations"].required is False
    assert by_name["textures"].kind == "copy"
    assert by_name["textures"].match == "^gfx/models/.*\\.(dds|png|tga)$"
    assert by_name["textures"].many is True
    assert by_name["textures"].required is False
    assert by_name["assignment"].match == ".paradev/entities.json"
    assert by_name["record"].match == "record.json"
    assert route.pdx_path_template == "{source_path}"
    assert route.copy_path_template == "{source_path}"
    assert compiled_route.pdx_path_template is None
    assert compiled_route.copy_path_template == "{source_path}"
    assert family.routes["record"].emits_artifacts is False
    assert family_view["stages"] == ["discover", "load", "normalize", "compile"]
    assert family_view["route_setting"] == "settings.asset_kind"
    assert family_view["routes"] == {
        "basic": {"pdx": "{source_path}", "copy": "{source_path}"},
        "compiled_records": {"copy": "{source_path}"},
        "record": {"emits_artifacts": False},
    }
    assert family_view["metadata"]["settings"]["asset_kind"] == {
        "required": True,
        "values": ["basic", "compiled_records", "record"],
    }
    assert "assignment_contract" not in family_view["metadata"]["settings"]
    assert "record_contract" not in family_view["metadata"]["settings"]
    assert family_view["outputs"] == [
        {
            "artifact_type": "pdx",
            "template_key": "pdx",
            "template": "{source_path}",
            "owner_kinds": ["module"],
            "target_root": "output",
            "route": "basic",
            "route_setting": "settings.asset_kind",
        },
        {
            "artifact_type": "copy",
            "template_key": "copy",
            "template": "{source_path}",
            "owner_kinds": ["module"],
            "target_root": "output",
            "route": "basic",
            "route_setting": "settings.asset_kind",
        },
        {
            "artifact_type": "copy",
            "template_key": "copy",
            "template": "{source_path}",
            "owner_kinds": ["module"],
            "target_root": "output",
            "route": "compiled_records",
            "route_setting": "settings.asset_kind",
        },
        {
            "artifact_type": "pdx",
            "generated": True,
            "owner_kinds": ["module"],
            "target_root": "output",
            "route": "compiled_records",
            "route_setting": "settings.asset_kind",
        },
    ]


def test_pihc3_division_family_preserves_compiled_oob_paths_with_generic_slots() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("division")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("division")

    assert by_name["history"].kind == "pdx"
    assert by_name["history"].many is True
    assert by_name["history"].required is True
    assert by_name["names"].kind == "pdx"
    assert by_name["names"].many is True
    assert family.pdx_path_template == "{source_path}"


def test_pihc3_ideology_family_uses_shared_def_and_loc_slots() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("ideology")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("ideology")

    assert by_name["def"].kind == "pdx"
    assert by_name["def"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert family.pdx_path_template == "common/ideologies/{object_id}.txt"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"
    assert family.required_loc_keys == ("{object_id}", "{object_id}_desc")


def test_pihc3_bookmark_family_uses_shared_def_loc_and_picture_slots() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("bookmark")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("bookmark")

    assert by_name["def"].kind == "pdx"
    assert by_name["def"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert by_name["picture"].kind == "copy"
    assert family.pdx_path_template == "common/bookmarks/{object_id}.txt"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"
    assert family.copy_path_template == "{source_path}"


def test_pihc3_building_family_uses_shared_def_and_loc_slots() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("building")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("building")

    assert by_name["def"].kind == "pdx"
    assert by_name["def"].match == "def.txt"
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert by_name["icon"].kind == "copy"
    assert by_name["icon"].match == r"^icon\.(png|dds|tga)$"
    assert family.pdx_path_template == "common/buildings/{object_id}.txt"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"


def test_pihc3_resource_family_uses_real_resource_localization_keys() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("resource")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("resource")

    assert by_name["def"].kind == "pdx"
    assert by_name["def"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert family.pdx_path_template == "common/resources/{object_id}.txt"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"
    assert set(family.required_loc_keys) == {
        "{object_id}_desc",
        "country_resource_{object_id}",
        "country_resource_cost_{object_id}",
    }


def test_pihc3_wargoal_family_uses_shared_def_and_loc_slots() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("wargoal")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("wargoal")

    assert by_name["def"].kind == "pdx"
    assert by_name["def"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert family.pdx_path_template == "common/wargoals/{object_id}.txt"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"
    assert family.required_loc_keys == (
        "{object_id}",
        "{object_id}_WAR_NAME",
        "{object_id}_desc",
    )


def test_pihc3_resistance_activity_family_uses_shared_def_and_loc_slots() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("resistance_activity")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("resistance_activity")

    assert by_name["def"].kind == "pdx"
    assert by_name["def"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert family.pdx_path_template == "common/resistance_activity/{object_id}.txt"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"
    assert family.required_loc_keys == ("{object_id}_alert",)


def test_pihc3_operation_family_uses_shared_def_and_loc_slots() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("operation")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("operation")

    assert by_name["def"].kind == "pdx"
    assert by_name["def"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert family.pdx_path_template == "common/operations/{object_id}.txt"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"
    assert family.required_loc_keys == ("{object_id}", "{object_id}_desc")


def test_pihc3_game_rule_family_uses_shared_def_and_loc_slots() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("game_rule")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("game_rule")

    assert by_name["def"].kind == "pdx"
    assert by_name["def"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert family.pdx_path_template == "common/game_rules/{object_id}.txt"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"
    assert family.required_loc_keys == ("{object_id}", "{object_id}_desc")


def test_pihc3_operation_token_family_uses_shared_def_and_loc_slots() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("operation_token")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("operation_token")

    assert by_name["def"].kind == "pdx"
    assert by_name["def"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert family.pdx_path_template == "common/operation_tokens/{object_id}.txt"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"
    assert family.required_loc_keys == ("{object_id}", "{object_id}_DESC")


def test_pihc3_difficulty_setting_family_uses_shared_def_and_loc_slots() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("difficulty_setting")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("difficulty_setting")

    assert by_name["def"].kind == "pdx"
    assert by_name["def"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert family.pdx_path_template == "common/difficulty_settings/{object_id}.txt"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"


def test_pihc3_on_action_family_uses_shared_def_slot() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("on_action")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("on_action")

    assert by_name["def"].kind == "pdx"
    assert by_name["def"].required is True
    assert family.pdx_path_template == "common/on_actions/{object_id}.txt"


def test_pihc3_scripted_gui_family_uses_shared_def_slot() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("scripted_gui")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("scripted_gui")

    assert by_name["def"].kind == "pdx"
    assert by_name["def"].required is True
    assert family.pdx_path_template == "common/scripted_guis/{object_id}.txt"


def test_pihc3_operative_codename_family_uses_shared_def_and_loc_slots() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("operative_codename")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("operative_codename")

    assert by_name["def"].kind == "pdx"
    assert by_name["def"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert family.pdx_path_template == "common/units/codenames_operatives/{object_id}.txt"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"
    assert family.required_loc_keys == ("{object_id}_NAME_THEME",)


def test_pihc3_faction_family_uses_shared_def_and_loc_slots() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("faction")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("faction")

    assert by_name["def"].kind == "pdx"
    assert by_name["def"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert family.pdx_path_template == "common/factions/templates/{object_id}.txt"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"
    assert family.required_loc_keys == ("{object_id}", "{object_id}_desc")


def test_pihc3_faction_rule_family_preserves_compiled_paths_with_generic_slots() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("faction_rule")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("faction_rule")

    assert by_name["pdx"].kind == "pdx"
    assert by_name["pdx"].many is True
    assert by_name["pdx"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert by_name["loc"].required is False
    assert family.pdx_path_template == "{source_path}"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"


def test_pihc3_operation_phase_family_uses_shared_def_and_loc_slots() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("operation_phase")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("operation_phase")

    assert by_name["def"].kind == "pdx"
    assert by_name["def"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert family.pdx_path_template == "common/operation_phases/{object_id}.txt"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"
    assert family.required_loc_keys == (
        "{object_id}",
        "{object_id}_desc",
        "{object_id}_outcome",
    )


def test_pihc3_unit_medal_family_uses_shared_def_and_loc_slots() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("unit_medal")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("unit_medal")

    assert by_name["def"].kind == "pdx"
    assert by_name["def"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert family.pdx_path_template == "common/unit_medals/{object_id}.txt"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"
    assert family.required_loc_keys == ("{object_id}",)


def test_pihc3_modifier_family_preserves_interface_asset_paths() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("modifier")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("modifier")

    assert family.family_kind == "collection_source"
    assert by_name["def"].kind == "pdx"
    assert by_name["def"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert by_name["assets"].kind == "copy"
    assert by_name["assets"].many is True
    assert family.pdx_path_template == "common/modifiers/{collection_id}.txt"
    assert family.module_pdx_path_template == "common/modifiers/{object_id}.txt"
    assert family.copy_path_template == "{source_path}"


@PIHC3_FULL_BUILD_GROUP
def test_pihc3_static_modifiers_compile_to_source_file_group(
    pihc3_full_build_result: BuildResult,
) -> None:
    result = pihc3_full_build_result
    pdx_artifacts = [artifact for artifact in result.artifacts if artifact.artifact_type == "pdx"]
    pdx_by_path = {str(artifact.path): artifact for artifact in pdx_artifacts}

    assert "common/modifiers/00_static_modifiers.txt" in pdx_by_path
    assert "common/modifiers/weather_rain_light.txt" not in pdx_by_path
    assert "common/modifiers/night.txt" not in pdx_by_path
    assert pdx_by_path["common/modifiers/00_static_modifiers.txt"].owner == "collection:00_static_modifiers"
    assert "weather_rain_light = {" in pdx_by_path["common/modifiers/00_static_modifiers.txt"].payload.to_str()
    assert "night = {" in pdx_by_path["common/modifiers/00_static_modifiers.txt"].payload.to_str()


def test_pihc3_modifier_definition_family_preserves_compiled_paths() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("modifier_definition")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("modifier_definition")

    assert by_name["pdx"].kind == "pdx"
    assert by_name["pdx"].match == "^(common/dynamic_modifiers|common/opinion_modifiers|common/peace_conference/cost_modifiers)/.*\\.txt$"
    assert by_name["pdx"].many is True
    assert by_name["pdx"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert family.pdx_path_template == "{source_path}"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"


def test_pihc3_country_history_family_preserves_compiled_paths() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("country_history")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("country_history")

    assert by_name["pdx"].kind == "pdx"
    assert by_name["pdx"].many is True
    assert by_name["pdx"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert family.pdx_path_template == "{source_path}"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"


def test_pihc3_military_industrial_organization_family_preserves_compiled_paths() -> None:
    from paradev.build.plan import BuildContext
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("military_industrial_organization")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("military_industrial_organization")

    assert by_name["pdx"].kind == "pdx"
    assert by_name["pdx"].many is True
    assert by_name["pdx"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert family.pdx_path_template == "{source_path}"
    assert family.loc_path_template == ("localisation/{language_folder}/" "MILITARY_INDUSTRIAL_ORGANIZATION_" "{source_stem}_{language}.yml")
    assert registry.publication_replacements_for("military_industrial_organization") == ()
    assert not hasattr(family, "retired_families")
    with pytest.raises(ValueError, match="not registered"):
        registry.family("military_industrial_organization_component")

    discovery = project.discover_modules(
        profile=project.game,
        registry=registry,
        strict_metadata=True,
        family="military_industrial_organization",
    )
    assert discovery.diagnostics == ()
    assert len(discovery.modules) == 7
    assert {module.module_id.rsplit("/", 1)[-1] for module in discovery.modules} == {
        "ai_bonus_weights",
        "air_policies",
        "c01_airship_organization",
        "debug_organization",
        "general_policies",
        "generic_organizations",
        "land_policies",
    }
    assert all(
        set(module.metadata) == {"note", "object_id", "type", "title"}
        and module.metadata["object_id"] == module.module_id.rsplit("/", 1)[-1]
        and module.metadata["type"] == "military_industrial_organization"
        and module.metadata["note"] == module.metadata["title"]
        for module in discovery.modules
    )
    localization_file_id = "MILITARY_INDUSTRIAL_ORGANIZATION_ORGANIZATIONS_00_GENERIC_ORGANIZATION"

    artifact_paths = {
        str(artifact.path)
        for artifact in family.emit(
            BuildContext(project_id="PIHC3"),
            discovery.modules,
            (),
        )
    }
    localization_paths = {path for path in artifact_paths if path.startswith("localisation/")}
    assert len(localization_paths) == 60
    assert f"localisation/english/{localization_file_id}_l_english.yml" in artifact_paths
    assert not any(Path(path).name.startswith("MIO_") for path in localization_paths)


def test_pihc3_copied_support_files_do_not_reference_removed_vanilla_technologies() -> None:
    checked_paths = [
        PIHC3_ROOT / "src/modules/doctrine/PIHC_DOCTRINE_SUPPORT/common/doctrines/subdoctrines/sea/navy_capital_subdoctrines.txt",
        PIHC3_ROOT / "src/modules/faction_rule/FACTION_RULE_GOALS_FACTION_GOALS_SHORT_TERM/common/factions/goals/faction_goals_short_term.txt",
        PIHC3_ROOT
        / (
            "src/modules/military_industrial_organization/"
            "generic_organizations/"
            "common/military_industrial_organization/organizations/00_generic_organization.txt"
        ),
    ]
    removed_vanilla_techs = {
        "basic_fortification_tech",
        "coastal_defense_ships",
        "coastal_fort_tech_2",
        "land_fort_tech_2",
        "rocket_artillery",
    }
    pattern = re.compile(r"\b(" + "|".join(re.escape(tech) for tech in sorted(removed_vanilla_techs)) + r")\b")

    matches = [
        f"{path.as_posix()}:{line_number}:{match.group(1)}"
        for path in checked_paths
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        for match in [pattern.search(line)]
        if match
    ]

    assert matches == []


def test_pihc3_raid_family_preserves_compiled_paths() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("raid")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("raid")

    assert by_name["pdx"].kind == "pdx"
    assert by_name["pdx"].many is True
    assert by_name["pdx"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert family.pdx_path_template == "{source_path}"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"


def test_pihc3_ai_config_family_preserves_compiled_paths() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("ai_config")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("ai_config")

    assert by_name["pdx"].kind == "pdx"
    assert by_name["pdx"].many is True
    assert by_name["pdx"].required is True
    assert "loc" not in by_name
    assert family.pdx_path_template == "{source_path}"
    assert family.loc_path_template is None


def test_pihc3_common_data_family_preserves_compiled_paths() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("common_data")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("common_data")

    assert by_name["pdx"].kind == "pdx"
    assert by_name["pdx"].many is True
    assert by_name["pdx"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert "state_category" in by_name["pdx"].match
    assert "terrain" in by_name["pdx"].match
    assert family.pdx_path_template == "{source_path}"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"


@PIHC3_FULL_BUILD_GROUP
def test_pihc3_common_data_owns_common_terrain_without_copy_overlay(
    pihc3_full_build_result: BuildResult,
) -> None:
    result = pihc3_full_build_result

    for path in [
        "common/terrain/00_terrain.txt",
        "common/terrain/01_terrain.txt",
    ]:
        owners = sorted(artifact.owner for artifact in result.artifacts if str(artifact.path) == path)
        assert owners == [_common_data_owner(path)]


def test_pihc3_peace_conference_family_preserves_compiled_paths() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("peace_conference")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("peace_conference")

    assert by_name["pdx"].kind == "pdx"
    assert by_name["pdx"].many is True
    assert by_name["pdx"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert family.pdx_path_template == "{source_path}"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"


def test_pihc3_general_history_family_preserves_compiled_paths() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("general_history")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("general_history")

    assert by_name["pdx"].kind == "pdx"
    assert by_name["pdx"].many is True
    assert by_name["pdx"].required is True
    assert family.pdx_path_template == "{source_path}"


def test_pihc3_state_family_preserves_compiled_paths() -> None:
    from paradev.build import BuildContext
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("state")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("state")

    assert by_name["def"].kind == "pdx"
    assert by_name["def"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert family.pdx_path_template == "history/states/{object_id}.txt"
    assert family.loc_path_template is None
    assert family.required_loc_keys == ("STATE_{object_id}",)
    assert family.title_loc_keys == ("STATE_{object_id}",)

    discovered = project.discover_modules(family="state")
    assert discovered.diagnostics == ()
    artifacts = family.emit(
        BuildContext(project_id="PIHC3"),
        discovered.modules,
        (),
    )
    localization = {str(artifact.path): artifact for artifact in artifacts if artifact.artifact_type == "loc"}
    assert set(localization) == {
        "localisation/english/state_names_l_english.yml",
        "localisation/english/victory_points_l_english.yml",
        "localisation/russian/state_names_l_russian.yml",
        "localisation/russian/victory_points_l_russian.yml",
        "localisation/simp_chinese/state_names_l_simp_chinese.yml",
        "localisation/simp_chinese/victory_points_l_simp_chinese.yml",
    }
    state_names = {path: artifact for path, artifact in localization.items() if "state_names_" in path}
    victory_points = {path: artifact for path, artifact in localization.items() if "victory_points_" in path}
    assert all(
        artifact.owner == "project:PIHC3"
        and artifact.metadata["family"] == "state"
        and len(artifact.metadata["module_ids"]) == 902
        and len(artifact.inputs) == 902
        and len(artifact.payload) == 902
        for artifact in state_names.values()
    )
    assert all(
        artifact.owner == "project:PIHC3"
        and artifact.metadata["family"] == "state"
        and len(artifact.metadata["module_ids"]) == 898
        and len(artifact.inputs) == 898
        and len(artifact.payload) == 910
        for artifact in victory_points.values()
    )


@PIHC3_FULL_BUILD_GROUP
def test_pihc3_states_are_native_without_copy_overlay(
    pihc3_full_build_result: BuildResult,
) -> None:
    result = pihc3_full_build_result

    for path in [
        "history/states/1.txt",
        "history/states/100.txt",
        "history/states/902.txt",
    ]:
        owners = sorted(artifact.owner for artifact in result.artifacts if str(artifact.path) == path)
        assert owners == [_state_owner(path)]


def test_pihc3_strategic_region_family_preserves_compiled_paths() -> None:
    from paradev.build import BuildContext
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("strategic_region")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("strategic_region")

    assert by_name["def"].kind == "pdx"
    assert by_name["def"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert family.pdx_path_template == "map/strategicregions/{object_id}.txt"
    assert family.loc_path_template is None
    assert family.required_loc_keys == ("STRATEGICREGION_{object_id}",)
    assert family.title_loc_keys == ("STRATEGICREGION_{object_id}",)

    discovered = project.discover_modules(family="strategic_region")
    assert discovered.diagnostics == ()
    artifacts = family.emit(
        BuildContext(project_id="PIHC3"),
        discovered.modules,
        (),
    )
    localization = {str(artifact.path): artifact for artifact in artifacts if artifact.artifact_type == "loc"}
    assert set(localization) == {
        "localisation/english/strategic_region_names_l_english.yml",
        "localisation/russian/strategic_region_names_l_russian.yml",
        "localisation/simp_chinese/strategic_region_names_l_simp_chinese.yml",
    }
    assert all(
        artifact.owner == "project:PIHC3"
        and artifact.metadata["family"] == "strategic_region"
        and len(artifact.metadata["module_ids"]) == 330
        and len(artifact.inputs) == 330
        and len(artifact.payload) == 330
        for artifact in localization.values()
    )


@PIHC3_FULL_BUILD_GROUP
def test_pihc3_strategic_regions_are_native_without_copy_overlay(
    pihc3_full_build_result: BuildResult,
) -> None:
    result = pihc3_full_build_result

    for path in [
        "map/strategicregions/1.txt",
        "map/strategicregions/100.txt",
        "map/strategicregions/99.txt",
    ]:
        owners = sorted(artifact.owner for artifact in result.artifacts if str(artifact.path) == path)
        assert owners == [_strategic_region_owner(path)]


def test_pihc3_game_asset_family_preserves_compiled_paths() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("game_asset")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("game_asset")

    assert by_name["pdx"].kind == "pdx"
    assert by_name["pdx"].many is True
    assert by_name["pdx"].required is False
    assert by_name["assets"].kind == "copy"
    assert by_name["assets"].many is True
    assert by_name["assets"].required is False
    assert family.pdx_path_template == "{source_path}"
    assert family.copy_path_template == "{source_path}"


@PIHC3_FULL_BUILD_GROUP
def test_pihc3_game_asset_owns_static_gfx_entities_without_copy_overlay(
    pihc3_full_build_result: BuildResult,
) -> None:
    result = pihc3_full_build_result
    expected_owners = {
        "gfx/entities/buildings.asset": "module:game_asset/GAME_ASSET_GFX_ENTITIES_BUILDINGS_ASSET",
        "gfx/entities/mapitems.asset": "module:game_asset/GAME_ASSET_GFX_ENTITIES_MAPITEMS_ASSET",
        "gfx/entities/mapitems.gfx": "module:game_asset/GAME_ASSET_GFX_ENTITIES_MAPITEMS_GFX",
        "gfx/entities/weather_entities.asset": "module:game_asset/GAME_ASSET_GFX_ENTITIES_WEATHER_ENTITIES_ASSET",
    }

    for path, owner in expected_owners.items():
        owners = sorted(artifact.owner for artifact in result.artifacts if str(artifact.path) == path)
        assert owners == [owner]


@PIHC3_FULL_BUILD_GROUP
def test_pihc3_game_asset_owns_static_gfx_fx_without_copy_overlay(
    pihc3_full_build_result: BuildResult,
) -> None:
    result = pihc3_full_build_result
    expected_owners = {
        "gfx/FX/constants.fxh": "module:game_asset/GAME_ASSET_GFX_FX_CONSTANTS",
        "gfx/FX/maparrow.shader": "module:game_asset/GAME_ASSET_GFX_FX_MAPARROW",
        "gfx/FX/pdxmap.shader": "module:game_asset/GAME_ASSET_GFX_FX_PDXMAP",
        "gfx/FX/river.shader": "module:game_asset/GAME_ASSET_GFX_FX_RIVER",
    }

    for path, owner in expected_owners.items():
        owners = sorted(artifact.owner for artifact in result.artifacts if str(artifact.path) == path)
        assert owners == [owner]


@PIHC3_FULL_BUILD_GROUP
def test_pihc3_game_asset_owns_static_gfx_models_without_copy_overlay(
    pihc3_full_build_result: BuildResult,
) -> None:
    result = pihc3_full_build_result
    expected_owners = {
        "gfx/models/border_bottom_mesh.mesh": "module:game_asset/GAME_ASSET_GFX_MODELS_BORDER_BOTTOM_MESH",
        "gfx/models/border_mesh.mesh": "module:game_asset/GAME_ASSET_GFX_MODELS_BORDER_MESH",
        "gfx/models/borderlp_bottom material_color.dds": "module:game_asset/GAME_ASSET_GFX_MODELS_BORDERLP_BOTTOM_MATERIAL_COLOR",
        "gfx/models/borderlp_bottom material_norm.dds": "module:game_asset/GAME_ASSET_GFX_MODELS_BORDERLP_BOTTOM_MATERIAL_NORM",
        "gfx/models/borderlp_bottom material_spec.dds": "module:game_asset/GAME_ASSET_GFX_MODELS_BORDERLP_BOTTOM_MATERIAL_SPEC",
        "gfx/models/borderlp_top material_color.dds": "module:game_asset/GAME_ASSET_GFX_MODELS_BORDERLP_TOP_MATERIAL_COLOR",
        "gfx/models/borderlp_top material_norm.dds": "module:game_asset/GAME_ASSET_GFX_MODELS_BORDERLP_TOP_MATERIAL_NORM",
        "gfx/models/borderlp_top material_spec.dds": "module:game_asset/GAME_ASSET_GFX_MODELS_BORDERLP_TOP_MATERIAL_SPEC",
    }

    for path, owner in expected_owners.items():
        owners = sorted(artifact.owner for artifact in result.artifacts if str(artifact.path) == path)
        assert owners == [owner]


@PIHC3_FULL_BUILD_GROUP
def test_pihc3_game_asset_owns_static_gfx_minimap_without_copy_overlay(
    pihc3_full_build_result: BuildResult,
) -> None:
    result = pihc3_full_build_result
    owners = sorted(artifact.owner for artifact in result.artifacts if str(artifact.path) == "gfx/minimap/minimap.dds")

    assert owners == ["module:game_asset/GAME_ASSET_GFX_MINIMAP_MINIMAP"]


@PIHC3_FULL_BUILD_GROUP
def test_pihc3_game_asset_owns_static_gfx_maparrows_without_copy_overlay(
    pihc3_full_build_result: BuildResult,
) -> None:
    result = pihc3_full_build_result

    for path in PIHC3_SUPPORT_MAPARROW_PATHS:
        owners = sorted(artifact.owner for artifact in result.artifacts if str(artifact.path) == path)
        assert owners == [_game_asset_owner(path)]


@PIHC3_FULL_BUILD_GROUP
def test_pihc3_game_asset_owns_static_gfx_particles_without_copy_overlay(
    pihc3_full_build_result: BuildResult,
) -> None:
    result = pihc3_full_build_result

    for path in PIHC3_SUPPORT_PARTICLE_PATHS:
        owners = sorted(artifact.owner for artifact in result.artifacts if str(artifact.path) == path)
        assert owners == [_game_asset_owner(path)]


@PIHC3_FULL_BUILD_GROUP
def test_pihc3_game_asset_owns_map_terrain_without_copy_overlay(
    pihc3_full_build_result: BuildResult,
) -> None:
    result = pihc3_full_build_result

    for path in [
        "map/terrain/Tree_tint.bmp",
        "map/terrain/atlas0.dds",
        "map/terrain/colormap_water_0.png",
        "map/terrain/colormap_rgb_cityemissivemask_a.dds",
    ]:
        owners = sorted(artifact.owner for artifact in result.artifacts if str(artifact.path) == path)
        assert owners == [_game_asset_owner(path)]


@PIHC3_FULL_BUILD_GROUP
def test_pihc3_game_asset_owns_root_map_files_without_copy_overlay(
    pihc3_full_build_result: BuildResult,
) -> None:
    result = pihc3_full_build_result

    for path in [
        "map/default.map",
        "map/definition.csv",
        "map/provinces.bmp",
        "map/cities.bmp",
        "map/states_labels.png",
    ]:
        owners = sorted(artifact.owner for artifact in result.artifacts if str(artifact.path) == path)
        assert owners == [_game_asset_owner(path)]


def test_pihc3_scripted_localisation_family_preserves_compiled_paths() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("scripted_localisation")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("scripted_localisation")

    assert by_name["pdx"].kind == "pdx"
    assert by_name["pdx"].many is True
    assert by_name["pdx"].required is True
    assert family.pdx_path_template == "{source_path}"

    object_id = "SCRIPTED_LOC_C02_RHODODENDRONS_SWITCH"
    discovery = project.discover_modules(
        profile=project.game,
        module_id=f"scripted_localisation/{object_id}",
    )
    assert discovery.diagnostics == ()
    assert len(discovery.modules) == 1
    assert discovery.modules[0].module_id == f"scripted_localisation/{object_id}"
    assert discovery.modules[0].source_slots == {"pdx": ("common/scripted_localisation/inventory_items_4EP_C02_RABID_RHODODENDRONS_NEUTRALIZER_o0_switch.txt",)}


def test_pihc3_audio_family_preserves_compiled_paths() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("audio")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("audio")

    assert by_name["pdx"].kind == "pdx"
    assert by_name["pdx"].many is True
    assert by_name["pdx"].required is True
    assert by_name["assets"].kind == "copy"
    assert by_name["assets"].many is True
    assert family.pdx_path_template == "{source_path}"
    assert family.copy_path_template == "{source_path}"


def test_pihc3_interface_family_preserves_compiled_paths() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("interface")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("interface")

    assert by_name["assets"].kind == "copy"
    assert by_name["assets"].many is True
    assert by_name["assets"].required is True
    assert family.copy_path_template == "{source_path}"


def test_pihc3_localization_family_preserves_compiled_paths() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("localization")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("localization")

    assert by_name["assets"].kind == "copy"
    assert by_name["assets"].many is True
    assert by_name["assets"].required is True
    assert family.copy_path_template == "{source_path}"


def test_pihc3_font_family_preserves_compiled_paths() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("font")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("font")

    assert by_name["assets"].kind == "copy"
    assert by_name["assets"].many is True
    assert by_name["assets"].required is True
    assert family.copy_path_template == "{source_path}"


def test_pihc3_doctrine_family_owns_shared_support_paths() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("doctrine")
    by_name = {slot.name: slot for slot in slots}

    assert by_name["shared_pdx"].kind == "pdx"
    assert by_name["shared_pdx"].many is True
    assert by_name["shared_pdx"].required is False
    assert by_name["shared_loc"].kind == "copy"
    assert by_name["shared_loc"].many is True
    assert by_name["shared_loc"].required is False
    with pytest.raises(ValueError, match="not registered"):
        registry.family("doctrine_definition")

    support_root = PIHC3_ROOT / "src/modules/doctrine" / "PIHC_DOCTRINE_SUPPORT - 教义共享支持"
    assert {path.relative_to(support_root).as_posix() for path in support_root.rglob("*.txt")} == {
        "common/doctrines/folders/doctrine_folders.txt",
        "common/doctrines/grand_doctrines/sea_grand_doctrines.txt",
        "common/doctrines/subdoctrines/sea/navy_capital_subdoctrines.txt",
        "common/doctrines/subdoctrines/sea/navy_carrier_doctrines.txt",
        "common/doctrines/subdoctrines/sea/navy_screen_doctrines.txt",
        "common/doctrines/subdoctrines/sea/navy_submarine_doctrines.txt",
        "common/doctrines/tracks/PIHC_air_doctrine_tracks.txt",
        "common/doctrines/tracks/PIHC_land_doctrine_tracks.txt",
        "common/doctrines/tracks/sea_doctrine_tracks.txt",
    }
    assert {path.relative_to(support_root).as_posix() for path in support_root.rglob("*.yml")} == {
        "localisation/english/PIHC_DOCTRINE_REWARDS_l_english.yml",
        "localisation/simp_chinese/PIHC_DOCTRINE_REWARDS_l_simp_chinese.yml",
    }


@PIHC3_FULL_BUILD_GROUP
def test_pihc3_grand_doctrine_artifacts_are_definition_owned(
    pihc3_full_build_result: BuildResult,
) -> None:
    result = pihc3_full_build_result
    grand_doctrine_artifacts = sorted(
        str(artifact.path)
        for artifact in result.artifacts
        if artifact.target_root == "output" and str(artifact.path).startswith("common/doctrines/grand_doctrines/")
    )

    grand_module_ids = sorted(
        path.parent.name.split(" - ", 1)[0]
        for path in (PIHC3_ROOT / "src/modules/doctrine").glob("*/def.txt")
        if re.search(
            r"^\s*folder\s*=\s*(?:air|land|navy)\s*$",
            path.read_text(encoding="utf-8"),
            flags=re.MULTILINE,
        )
    )
    assert len(grand_module_ids) == 8
    assert grand_doctrine_artifacts == sorted(
        [f"common/doctrines/grand_doctrines/{object_id}.txt" for object_id in grand_module_ids] + ["common/doctrines/grand_doctrines/sea_grand_doctrines.txt"]
    )


def test_pihc3_doctrines_infer_routes_and_keep_hidden_tree_state_complete() -> None:
    from paradev.sdk import Project

    modules_root = PIHC3_ROOT / "src/modules/doctrine"
    all_module_roots = sorted(path for path in modules_root.iterdir() if path.is_dir())
    module_roots = [path for path in all_module_roots if (path / "def.txt").is_file()]
    for module_root in module_roots:
        meta_path = module_root / ".paradev/meta.yaml"
        diagram = yaml.safe_load((module_root / ".paradev/diagram.yaml").read_text(encoding="utf-8"))

        assert not meta_path.exists()
        assert set(diagram) == {
            "schema",
            "position",
            "paths",
            "mutually_exclusive",
        }
        assert diagram["schema"] == "paradev.hoi4.doctrine-diagram-state.v1"
        assert (module_root / "def.txt").is_file()
        assert (module_root / "main.loc").is_file()
        assert (module_root / "icon.png").is_file()
        assert not (module_root / "legacy").exists()

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = {slot.name: slot for slot in registry.source_slots_for("doctrine")}
    projection = project.module_diagram("doctrine")

    assert len(module_roots) == 51
    assert len(all_module_roots) == 52
    assert slots["preview"].match == "icon.png"
    assert projection["editable"] is True
    assert projection["diagnostics"] == []
    assert len(projection["nodes"]) == 51
    assert projection["summary"]["module_count"] == 52
    assert projection["summary"]["support_module_count"] == 1
    assert projection["support_module_ids"] == ["doctrine/PIHC_DOCTRINE_SUPPORT"]
    assert len(projection["edges"]) == 79
    assert sum(edge["kind"] == "path" for edge in projection["edges"]) == 54
    assert sum(edge["kind"] == "mutually_exclusive" for edge in projection["edges"]) == 25
    assert all(node["editable"] is True for node in projection["nodes"])


def test_pihc3_idea_family_owns_shared_source_paths() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("idea")
    by_name = {slot.name: slot for slot in slots}

    assert by_name["shared_pdx"].kind == "pdx"
    assert by_name["shared_pdx"].many is True
    assert by_name["shared_pdx"].required is False
    with pytest.raises(ValueError, match="not registered"):
        registry.family("idea_support")


def test_pihc3_idea_family_uses_shared_definition_localization_and_asset_slots() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("idea")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("idea")

    assert by_name["def"].match == "def.txt"
    assert by_name["def"].many is False
    assert by_name["loc"].match == "**/*.loc"
    assert by_name["loc"].many is True
    assert by_name["preview"].match == "preview.png"
    assert by_name["compiled_assets"].kind == "copy"
    assert by_name["compiled_assets"].regex is True
    assert by_name["shared_pdx"].kind == "pdx"
    assert by_name["shared_pdx"].many is True
    assert family.pdx_path_template == "common/ideas/{object_id}.txt"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"
    assert family.copy_path_template == "{source_path}"


def test_pihc3_idea_family_emits_asset_only_resource_sets() -> None:
    from paradev.project import Project

    result = Project.load(PIHC3_ROOT).build(
        module_id="idea/HOI4_LAW_ICONS",
        strict_metadata=True,
    )
    owned_paths = {str(artifact.path) for artifact in result.artifacts if artifact.owner == "module:idea/HOI4_LAW_ICONS"}

    assert result.blocked is False
    assert not result.diagnostics
    assert len(owned_paths) == 17
    assert "gfx/interface/ideas/idea_war_economy.dds" in owned_paths
    assert "gfx/interface/ideas/idea_volunteer_only.dds" in owned_paths


def test_pihc3_technology_family_owns_def_loc_and_compiled_asset_slots() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("technology")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("technology")

    assert by_name["def"].kind == "pdx"
    assert by_name["def"].match == "def.txt"
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert by_name["compiled_assets"].kind == "copy"
    assert by_name["compiled_assets"].many is True
    assert by_name["preview"].kind is None
    assert family.pdx_path_template == "common/technologies/{object_id}.txt"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"
    assert family.copy_path_template == "{source_path}"
    assert by_name["shared_pdx"].kind == "pdx"
    assert by_name["shared_pdx"].many is True
    assert registry.publication_replacements_for("technology") == ()
    assert not hasattr(family, "retired_families")


def test_pihc3_technology_family_owns_shared_source_paths() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("technology")
    by_name = {slot.name: slot for slot in slots}

    assert by_name["shared_pdx"].kind == "pdx"
    assert by_name["shared_pdx"].many is True
    assert by_name["shared_pdx"].required is False
    with pytest.raises(ValueError, match="not registered"):
        registry.family("technology_support")
    with pytest.raises(ValueError, match="not registered"):
        registry.family("technology_component")


def test_pihc3_trait_family_uses_shared_routed_def_loc_and_icon_slots() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("trait")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("trait")

    assert by_name["def"].match == "def.txt"
    assert by_name["loc"].match == "**/*.loc"
    assert by_name["loc"].many is True
    assert by_name["icon"].match == "^(icon|goal|portrait|picture)\\.(png|dds|tga)$"
    assert family.settings_key == "subtype"
    assert family.default_route == "country_leader"
    assert set(family.routes) == {"country_leader", "scientist", "unit_leader"}
    assert family.routes["country_leader"].pdx_path_template == "common/country_leader/{object_id}.txt"
    assert family.routes["scientist"].pdx_path_template == "common/scientist_traits/{object_id}.txt"
    assert family.routes["unit_leader"].pdx_path_template == "common/unit_leader/{object_id}.txt"


def test_pihc3_trait_modules_need_no_repeated_compiler_routing_metadata() -> None:
    modules_root = PIHC3_ROOT / "src/modules/trait"
    for object_id in (
        "TRAIT_POPULAR",
        "TRAIT_GRASS_GREEN_AGRICULTURE",
        "TRAIT_KING_SOMBRA_KING_OF_MONSTERS",
        "TRAIT_ADVANCED_MEDICAL_EXPERT",
    ):
        module_root = next(modules_root.glob(f"{object_id} - *"))

        assert not (module_root / ".paradev/meta.yaml").exists()
        assert not (Path(module_root) / "meta.yaml").exists()
        assert not (module_root / "legacy").exists()
        assert (module_root / "def.txt").is_file()
        assert (module_root / "main.loc").is_file()


def test_pihc3_trait_family_exposes_definition_localization_and_optional_assets() -> None:
    from paradev.sdk import Project

    project = Project.load(PIHC3_ROOT)
    slots = {slot.name: slot for slot in project._build_registry(profile=project.game).source_slots_for("trait")}

    assert slots["def"].match == "def.txt"
    assert slots["loc"].match == "**/*.loc"
    assert slots["loc"].many is True
    assert slots["icon"].regex is True
    assert slots["copy"].many is True
    assert slots["assets"].many is True


def test_pihc3_unit_leader_family_preserves_compiled_paths() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("unit_leader")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("unit_leader")

    assert by_name["pdx"].kind == "pdx"
    assert by_name["pdx"].many is True
    assert by_name["pdx"].required is True
    assert family.pdx_path_template == "{source_path}"


def test_pihc3_leader_trait_family_preserves_compiled_paths() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("leader_trait")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("leader_trait")

    assert by_name["pdx"].kind == "pdx"
    assert by_name["pdx"].many is True
    assert by_name["pdx"].required is True
    assert family.pdx_path_template == "{source_path}"


def test_pihc3_character_family_uses_preview_and_compiled_portrait_slots() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("character")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("character")

    assert by_name["def"].kind == "pdx"
    assert by_name["def"].match == "def.txt"
    assert by_name["def"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].many is True
    assert by_name["preview"].kind is None
    assert by_name["preview"].match == "portrait.png"
    assert by_name["compiled_portraits"].kind == "copy"
    assert by_name["compiled_portraits"].many is True
    assert family.pdx_path_template == "common/characters/{object_id}.txt"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"
    assert family.copy_path_template == "{source_path}"


def test_pihc3_defines_family_preserves_compiled_lua_paths() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("defines")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("defines")

    assert by_name["files"].kind == "copy"
    assert by_name["files"].many is True
    assert by_name["files"].required is True
    assert family.copy_path_template == "{source_path}"


def test_pihc3_definess_keep_vanilla_initializer_merged() -> None:
    project_root = PIHC3_ROOT
    manifest = _pihc3_manifest(project_root)
    replace_paths = manifest["replace_path"]

    assert "common/defines" not in replace_paths


def test_pihc3_loading_screen_family_preserves_compiled_paths() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("loading_screen")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("loading_screen")

    assert "asset" not in by_name
    assert by_name["assets"].kind == "copy"
    assert by_name["assets"].many is True
    assert by_name["assets"].required is True
    assert family.copy_path_template == "{source_path}"


def test_pihc3_ui_family_preserves_compiled_paths_without_a_sidecar_family() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("ui")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("ui")

    assert tuple(by_name) == ("layout", "alert_images", "autonomy_images")
    assert by_name["layout"].kind == "pdx"
    assert by_name["layout"].match == "interface/alerts.gui"
    assert {by_name[name].kind for name in ("alert_images", "autonomy_images")} == {"copy"}
    assert all(by_name[name].many is True for name in ("alert_images", "autonomy_images"))
    assert all(by_name[name].required is False for name in ("alert_images", "autonomy_images"))
    assert {by_name[name].authoring_path for name in ("alert_images", "autonomy_images")} == {
        "gfx/interface/alerts/{filename}",
        "gfx/interface/autonomy/{filename}",
    }
    assert family.copy_path_template == "{source_path}"
    with pytest.raises(ValueError, match="is not registered"):
        registry.family("ui_asset_component")


def test_pihc3_balance_of_power_family_owns_def_loc_and_assets() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("balance_of_power")
    by_name = {slot.name: slot for slot in slots}
    asset_slots = [slot for slot in slots if slot.name == "assets"]
    family = registry.family("balance_of_power")

    assert tuple(by_name) == ("def", "loc", "assets")
    assert by_name["def"].match == "def.txt"
    assert by_name["def"].required is True
    assert by_name["loc"].match == "**/*.loc"
    assert by_name["loc"].many is True
    assert len(asset_slots) == 2
    assert {slot.match for slot in asset_slots} == {
        r"^gfx/interface/bop/.*\.dds$",
        r"^interface/bop/.*\.gfx$",
    }
    assert all(slot.kind == "copy" for slot in asset_slots)
    assert all(slot.many is True for slot in asset_slots)
    assert {slot.authoring_path for slot in asset_slots} == {
        "gfx/interface/bop/{filename}",
        "interface/bop/{filename}",
    }
    assert by_name["assets"].required is False
    assert family.pdx_path_template == "common/bop/{object_id}.txt"
    assert family.copy_path_template == "{source_path}"

    with pytest.raises(ValueError, match="is not registered"):
        registry.family("balance_of_power_asset_component")


def test_pihc3_balance_of_power_sources_match_reviewed_digest() -> None:
    import hashlib

    modules_root = PIHC3_ROOT / "src/modules/balance_of_power"
    rows: list[str] = []
    for module_root in sorted(modules_root.iterdir()):
        sources = sorted(path for path in module_root.rglob("*") if path.is_file() and path.name != "meta.yaml")
        rows.extend(f"{module_root.name}\0{path.relative_to(module_root).as_posix()}\0{hashlib.sha256(path.read_bytes()).hexdigest()}" for path in sources)

    assert len(rows) == 40
    assert hashlib.sha256("\n".join(rows).encode()).hexdigest() == "fb290b80cf37e36e5b71df25964d1cb45ed351ef3da373b063fc21c7ebf96fa5"


def test_pihc3_balance_of_power_target_owns_all_six_outputs() -> None:
    from paradev.project import Project

    object_id = "BOP_C01_COZY_GLOW_EXHAUSTION"
    owner = f"module:balance_of_power/{object_id}"
    project = Project.load(PIHC3_ROOT)
    result = project.build(
        family="balance_of_power",
        module_id=f"balance_of_power/{object_id}",
        strict_metadata=True,
    )
    owned_paths = {str(artifact.path) for artifact in result.artifacts if artifact.owner == owner}

    assert owned_paths == {
        f"common/bop/{object_id}.txt",
        f"gfx/interface/bop/{object_id}_LEFT_SIDE.dds",
        f"gfx/interface/bop/{object_id}_RIGHT_SIDE.dds",
        f"interface/bop/{object_id}.gfx",
        f"localisation/english/{object_id}_l_english.yml",
        f"localisation/simp_chinese/{object_id}_l_simp_chinese.yml",
    }
    assert not result.diagnostics


def test_pihc3_decision_family_uses_shared_definition_localization_and_asset_slots() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("decision")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("decision")

    assert by_name["def"].match == "def.txt"
    assert by_name["def"].many is False
    assert by_name["loc"].match == "**/*.loc"
    assert by_name["loc"].many is True
    assert by_name["preview"].match == "preview.png"
    assert by_name["compiled_assets"].kind == "copy"
    assert by_name["compiled_assets"].regex is True
    assert by_name["shared_pdx"].kind == "pdx"
    assert by_name["shared_pdx"].many is True
    assert family.pdx_path_template == "common/decisions/{collection_id}.txt"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"
    assert family.copy_path_template == "{source_path}"


def test_pihc3_decision_family_owns_shared_source_paths() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("decision")
    by_name = {slot.name: slot for slot in slots}

    assert by_name["shared_pdx"].kind == "pdx"
    assert by_name["shared_pdx"].many is True
    assert by_name["shared_pdx"].required is False
    with pytest.raises(ValueError, match="not registered"):
        registry.family("decision_support")


def test_pihc3_decision_family_owns_its_compiled_assets() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("decision")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("decision")

    assert by_name["compiled_assets"].kind == "copy"
    assert by_name["compiled_assets"].many is True
    assert family.copy_path_template == "{source_path}"
    with pytest.raises(ValueError, match="is not registered"):
        registry.family("decision_asset_component")


def test_pihc3_technology_owns_compiled_assets_without_retired_metadata() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("technology")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("technology")

    assert by_name["compiled_assets"].kind == "copy"
    assert by_name["compiled_assets"].many is True
    assert by_name["compiled_assets"].required is False
    assert family.copy_path_template == "{source_path}"
    assert registry.publication_replacements_for("technology") == ()
    assert not hasattr(family, "retired_families")
    with pytest.raises(ValueError, match="not registered"):
        registry.family("technology_asset_component")


def test_pihc3_focus_family_owns_tree_assets_without_retired_metadata() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("focus")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("focus")

    assert by_name["preview"].match == "preview.png"
    assert by_name["compiled_assets"].kind == "copy"
    assert by_name["compiled_assets"].many is True
    assert by_name["compiled_assets"].required is False
    assert family.copy_path_template == "{source_path}"
    assert registry.publication_replacements_for("focus") == ()
    assert not hasattr(family, "retired_families")
    with pytest.raises(ValueError, match="not registered"):
        registry.family("focus_asset_component")


def test_pihc3_focus_tree_preview_icons_are_module_owned() -> None:
    from paradev.sdk import Project

    project_root = PIHC3_ROOT
    payload = Project.load(project_root).module_diagram("focus")
    image_paths = [node.get("image_path") for node in payload["nodes"]]

    assert len(image_paths) == 738
    assert len(set(image_paths)) == 738
    assert all(
        isinstance(path, str) and path.startswith("src/modules/focus/") and path.endswith("/preview.png") and (project_root / path).is_file()
        for path in image_paths
    )
    assert not (project_root / "src/modules/focus_tree").exists()
    assert not (project_root / "src/modules/focus_asset_component").exists()


def test_pihc3_default_focus_tree_references_use_current_generic_focus_identity() -> None:
    source_files = [
        (PIHC3_ROOT / "src/collections/focus/generic - Generic/def.txt"),
        (PIHC3_ROOT / "src/modules/ai_config/AI_CONFIG_AI_STRATEGY_PLANS_GENERIC/common/ai_strategy_plans/GENERIC.txt"),
        (PIHC3_ROOT / "src/modules/on_action/PIHC_basic_on_actions__on_monthly/def.txt"),
        (PIHC3_ROOT / "src/modules/scripted_effect/C22_TIREK_DEAD/def.txt"),
    ]
    joined = "\n".join(path.read_text(encoding="utf-8") for path in source_files)
    generic_root = PIHC3_ROOT / "src/collections/focus/generic - Generic"

    assert "id = generic_focus" in source_files[0].read_text(encoding="utf-8")
    assert generic_root.name == "generic - Generic"
    assert not (generic_root / ".paradev").exists()
    assert "has_focus_tree = generic_focus" in joined
    assert "load_focus_tree = generic_focus" in joined
    assert "id = GENERIC" not in joined
    assert "has_focus_tree = GENERIC" not in joined
    assert "load_focus_tree = GENERIC" not in joined


def test_pihc3_checked_in_focus_tree_diagram_preserves_source_layout() -> None:
    from paradev.sdk import Project

    diagram = Project.load(PIHC3_ROOT).module_diagram("focus")
    nodes = {node["id"]: node for node in diagram["nodes"]}
    edges = {(edge["kind"], edge["source"], edge["target"]) for edge in diagram["edges"]}

    assert (PIHC3_ROOT / "src/collections/focus/C08_MAIN - C08 Main").is_dir()
    canterlot_pact = nodes["FOCUS_C08_CANTERLOT_PACT"]
    assert {key: canterlot_pact[key] for key in ("tree_id", "x", "y", "module_id", "collection_id")} == {
        "tree_id": "C08_MAIN",
        "x": 7,
        "y": 0,
        "module_id": "focus/FOCUS_C08_CANTERLOT_PACT",
        "collection_id": "C08_MAIN",
    }
    assert canterlot_pact["source_path"].startswith("src/modules/focus/")
    assert canterlot_pact["source_path"].endswith("/def.txt")
    assert canterlot_pact["image_path"].endswith("/preview.png")
    assert nodes["FOCUS_C08_CONTACT_COMRADES"]["x"] == 6
    assert nodes["FOCUS_C08_CONTACT_COMRADES"]["y"] == 3
    assert nodes["FOCUS_C08_ANOTHER_YEAR"]["x"] == 7
    assert nodes["FOCUS_C08_ANOTHER_YEAR"]["y"] == 4
    assert (
        "prerequisite",
        "FOCUS_C08_SPARKING_FIRE",
        "FOCUS_C08_CONTACT_COMRADES",
    ) in edges
    assert (
        "prerequisite",
        "FOCUS_C08_CONTACT_COMRADES",
        "FOCUS_C08_ANOTHER_YEAR",
    ) in edges
    assert (
        "prerequisite",
        "FOCUS_C08_THE_SITUATION",
        "FOCUS_C08_ANOTHER_YEAR",
    ) in edges


def test_pihc3_event_family_owns_its_compiled_assets() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("event")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("event")

    assert by_name["compiled_assets"].kind == "copy"
    assert by_name["compiled_assets"].many is True
    assert family.copy_path_template == "{source_path}"
    with pytest.raises(ValueError, match="is not registered"):
        registry.family("event_asset_component")


def test_pihc3_event_family_preserves_bce_definition_and_picture() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    result = project.build(module_id="event/BCE")
    owned_paths = {str(artifact.path) for artifact in result.artifacts if artifact.owner == "module:event/BCE"}

    assert result.blocked is False
    assert "events/BCE.txt" in owned_paths
    assert "gfx/event_pictures/border_war.dds" in owned_paths
    with pytest.raises(ValueError, match="is not registered"):
        registry.family("event_component")


def test_pihc3_portrait_family_consolidates_character_and_shared_sources() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    families = {str(spec.family): spec for spec in registry.families}

    assert "portrait_asset_component" not in families
    assert "portrait_component" not in families

    character_slots = {slot.name: slot for slot in registry.source_slots_for("character")}
    character_family = registry.family("character")
    assert character_slots["preview"].match == "portrait.png"
    assert character_slots["preview"].kind is None
    assert character_slots["compiled_portraits"].kind == "copy"
    assert character_slots["compiled_portraits"].many is True
    assert character_family.copy_path_template == "{source_path}"
    assert registry.publication_replacements_for("character") == ()
    assert not hasattr(character_family, "retired_families")

    portrait_slots = {slot.name: slot for slot in registry.source_slots_for("portrait")}
    portrait_family = registry.family("portrait")
    assert portrait_slots["definitions"].kind == "pdx"
    assert portrait_slots["definitions"].many is True
    assert portrait_slots["assets"].kind == "copy"
    assert portrait_slots["assets"].many is True
    assert portrait_family.pdx_path_template == "{source_path}"
    assert portrait_family.copy_path_template == "{source_path}"
    assert registry.publication_replacements_for("portrait") == ()
    assert not hasattr(portrait_family, "retired_families")

    modules_root = PIHC3_ROOT / "src/modules"
    character_roots = sorted(path for path in (modules_root / "character").iterdir() if path.is_dir())
    portrait_roots = sorted(path for path in (modules_root / "portrait").iterdir() if path.is_dir())
    assert len(character_roots) == 250
    assert len(portrait_roots) == 9
    assert not (modules_root / "portrait_asset_component").exists()
    assert not (modules_root / "portrait_component").exists()
    assert not any((root / "legacy").exists() for root in character_roots)
    assert not any((root / "meta.yaml").exists() for root in character_roots)

    character = modules_root / "character/CHARACTER_ABYSSINIA_KING_MEOWMEOW"
    assert (character / "interface/portraits/CHARACTER_ABYSSINIA_KING_MEOWMEOW.gfx").is_file()
    assert (character / "gfx/leaders/CHARACTER_ABYSSINIA_KING_MEOWMEOW_army_small.dds").is_file()
    assert (modules_root / "portrait/RANDOM_CHARACTER_PONY_MILITARY" / "interface/portraits/RANDOM_CHARACTER_pony_military.gfx").is_file()
    assert (modules_root / "portrait/PIHC_PORTRAIT_SUPPORT/portraits/pihc_portraits.txt").is_file()


def test_pihc3_country_family_owns_its_compiled_flags() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("country")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("country")

    assert by_name["flags"].kind == "copy"
    assert by_name["flags"].many is True
    assert family.copy_path_template == "{source_path}"
    assert registry.writer("pihc3_country_flag").artifact_type == "pihc3_country_flag"
    with pytest.raises(ValueError, match="is not registered"):
        registry.family("flag_asset_component")


def test_pihc3_country_preview_is_editor_only() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    writer = project._build_registry(profile=project.game).writer("pihc3_country_flag")
    preview = PIHC3_ROOT / "src/modules/country/C03 - 云中城公爵领/preview.png"
    result = project.build(
        family="country",
        module_id="country/C03",
    )
    artifacts = {str(artifact.path): artifact for artifact in result.artifacts if artifact.target_root == "output"}

    assert preview.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert artifacts["gfx/flags/30C.tga"].artifact_type == "copy"
    assert artifacts["gfx/flags/small/30C_anarchy.tga"].artifact_type == "copy"
    for variant, expected_size in {"medium": (41, 26), "small": (10, 7)}.items():
        artifact = artifacts[f"gfx/flags/{variant}/C03_anarchy.tga"]
        assert artifact.artifact_type == "pihc3_country_flag"
        assert isinstance(artifact.payload, bytes)
        assert writer.expected_sha256(artifact) == sha256(artifact.payload).hexdigest()
        assert int.from_bytes(artifact.payload[12:14], "little") == expected_size[0]
        assert int.from_bytes(artifact.payload[14:16], "little") == expected_size[1]
    assert "preview.png" not in artifacts


def test_pihc3_country_browser_exposes_integrated_preview_and_flags() -> None:
    from paradev.project import Project

    payload = Project.load(PIHC3_ROOT).browser(
        family="country",
        module_id="country/C03",
    )
    item = payload["items"][0]

    assert item["id"] == "module:country/C03"
    assert item["metadata"]["title"] == "云中城公爵领"
    assert "preview.png" in item["source_slots"]["preview"]
    assert "gfx/flags/30C.tga" in item["source_slots"]["flags"]


def test_pihc3_idea_family_owns_its_compiled_assets() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("idea")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("idea")

    assert by_name["compiled_assets"].kind == "copy"
    assert by_name["compiled_assets"].many is True
    assert family.copy_path_template == "{source_path}"
    with pytest.raises(ValueError, match="is not registered"):
        registry.family("idea_asset_component")


def test_pihc3_achievement_family_owns_preview_and_compiled_icons_without_component_families() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("achievement")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("achievement")
    family_names = {str(spec.family) for spec in registry.families}

    assert by_name["def"].kind == "pdx"
    assert by_name["def"].match == "def.txt"
    assert by_name["def"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].match == "**/*.loc"
    assert by_name["loc"].many is True
    assert by_name["preview"].kind is None
    assert by_name["preview"].match == "^icon\\.(png|dds|tga)$"
    assert by_name["compiled_icons"].kind == "copy"
    assert by_name["compiled_icons"].match == "^gfx/achievements/.*\\.dds$"
    assert by_name["compiled_icons"].many is True
    assert by_name["compiled_icons"].required is False
    assert family.pdx_path_template == "common/achievements/{object_id}.txt"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"
    assert family.copy_path_template == "{source_path}"
    assert "achievement_component" not in family_names
    assert "achievement_asset_component" not in family_names


def test_pihc3_texticon_family_preserves_compiled_paths_without_a_sidecar_family() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("texticon")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("texticon")

    assert tuple(by_name) == ("definitions", "images")
    assert by_name["definitions"].kind == "pdx"
    assert by_name["definitions"].many is True
    assert by_name["images"].kind == "copy"
    assert by_name["images"].many is True
    assert by_name["images"].required is False
    assert by_name["images"].authoring_path == "gfx/texticons/{filename}"
    assert family.copy_path_template == "{source_path}"
    with pytest.raises(ValueError, match="is not registered"):
        registry.family("texticon_asset_component")


def test_pihc3_special_project_family_uses_shared_def_loc_and_icon_slots() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("special_project")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("special_project")

    assert by_name["def"].kind == "pdx"
    assert by_name["def"].match == "def.txt"
    assert by_name["def"].required is False
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].match == "**/*.loc"
    assert by_name["loc"].many is True
    assert by_name["icon"].kind == "copy"
    assert by_name["icon"].match == "^icon\\.(png|dds|tga)$"
    assert by_name["shared_pdx"].kind == "pdx"
    assert by_name["shared_pdx"].many is True
    assert by_name["shared_assets"].kind == "copy"
    assert by_name["shared_assets"].many is True
    assert family.pdx_path_template == "common/special_projects/projects/{object_id}.txt"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"
    assert family.copy_path_template == "gfx/interface/special_project/project_icons/{object_id}{source_suffix}"
    assert family.sprite_slots == ("icon",)
    assert family.sprite_gfx_path_template == "interface/special_projects/{object_id}.gfx"
    assert family.sprite_name_template == "GFX_{object_id}"


def test_pihc3_special_project_reward_family_uses_shared_def_and_loc_slots() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("special_project_reward")
    by_name = {slot.name: slot for slot in slots}
    family = registry.family("special_project_reward")

    assert by_name["def"].kind == "pdx"
    assert by_name["def"].match == "def.txt"
    assert by_name["def"].required is True
    assert by_name["loc"].kind == "loc"
    assert by_name["loc"].match == "**/*.loc"
    assert by_name["loc"].many is True
    assert "icon" not in by_name
    assert family.pdx_path_template == "common/special_projects/prototype_rewards/{object_id}.txt"
    assert family.loc_path_template == "localisation/{language_folder}/{object_id}_{language}.yml"


def test_pihc3_special_project_family_owns_shared_source_paths() -> None:
    from paradev.project import Project

    project = Project.load(PIHC3_ROOT)
    registry = project._build_registry(profile=project.game)
    slots = registry.source_slots_for("special_project")
    by_name = {slot.name: slot for slot in slots}

    assert by_name["shared_pdx"].kind == "pdx"
    assert by_name["shared_pdx"].many is True
    assert by_name["shared_assets"].kind == "copy"
    assert by_name["shared_assets"].many is True
    with pytest.raises(ValueError, match="not registered"):
        registry.family("special_project_support")


def test_pihc3_special_projects_own_minimal_sources_and_generated_sprite_definitions() -> None:
    from paradev.project import Project

    project_root = PIHC3_ROOT
    module_root = next((project_root / "src/modules/special_project").glob("SP_ELEC_ARC*"))
    assert not (module_root / "meta.yaml").exists()
    assert not (module_root / "legacy").exists()
    assert {path.name for path in module_root.iterdir()} == {
        "def.txt",
        "icon.dds",
        "main.loc",
    }

    support_root = project_root / "src/modules/special_project/PIHC_SPECIAL_PROJECT_SUPPORT - PIHC Special Project Support"
    support_sources = [path for path in support_root.rglob("*") if path.is_file()]
    assert not (support_root / "meta.yaml").exists()
    assert len(support_sources) == 27
    assert not (project_root / "src/modules/special_project_asset_component").exists()
    assert not (project_root / "src/modules/special_project_component").exists()

    result = Project.load(project_root).build(module_id="special_project/SP_ELEC_ARC")
    assert result.blocked is False
    sprites = [artifact for artifact in result.artifacts if str(artifact.path) == "interface/special_projects/SP_ELEC_ARC.gfx"]
    assert len(sprites) == 1
    assert sprites[0].owner == "module:special_project/SP_ELEC_ARC"
    assert sprites[0].inputs == (module_root / "icon.dds",)
    assert sprites[0].payload[0].name == "GFX_SP_ELEC_ARC"
    assert sprites[0].payload[0].texturefile == "gfx/interface/special_project/project_icons/SP_ELEC_ARC.dds"
