"""Execute the authored temperature arithmetic and check its scheduling/GUI contracts.

The small evaluator implements only the Clausewitz subset used by this system.
It is not an engine substitute: native monthly/GUI checks remain part of release QA.
"""

from collections import Counter
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1] / "projects/PIHC3/src/modules"


def source(family, pattern):
    return next((ROOT / family).glob(pattern)).read_text(encoding="utf-8")


def parse(text):
    tokens = iter(re.findall(r'"[^"\n]*"|[{}=<>]|[^\s{}=<>]+', re.sub(r"#[^\n]*", "", text)))

    def body():
        result = []
        for key in tokens:
            if key == "}":
                return result
            op = next(tokens)
            value = next(tokens)
            result.append((key, op, body() if value == "{" else value))
        return result

    return body()


class Temperature:
    def __init__(self):
        self.effects = {k: v for k, _, v in parse(source("scripted_effect", "PIHC_STATE_TEMPERATURE - */def.txt"))}
        self.globals = {"PIHC_var_current_month": 12, "threat": 0, "num_days": 366064}
        self.flags = set()
        self.countries = []
        self.states = []
        self.calls = Counter()

    def address(self, name, current, previous):
        if name.startswith("global."):
            return self.globals, name[7:]
        if name.startswith("PREV."):
            return previous, name[5:]
        return current, name.removeprefix("THIS.")

    def value(self, value, current, previous):
        try:
            return float(value)
        except ValueError:
            scope, key = self.address(value, current, previous)
            return scope.get(key, 0)

    def assign(self, name, value, current, previous):
        # A deliberately conservative bound catches unsafe accumulated magnitudes.
        assert abs(value) < 2147483, (name, value)
        scope, key = self.address(name, current, previous)
        scope[key] = round(value, 3)

    def matches(self, body, current, previous):
        checks = []
        for key, op, val in body:
            if key in ("OR", "AND", "NOT"):
                items = [self.matches([item], current, previous) for item in val]
                checks.append(any(items) if key == "OR" else not all(items) if key == "NOT" else all(items))
            elif key == "check_variable":
                data = {k: v for k, _, v in val}
                name, compare, expected = next(t for t in val if t[0] != "compare")
                left, right = self.value(name, current, previous), self.value(expected, current, previous)
                compare = data.get("compare", compare)
                checks.append(
                    {
                        "=": left == right,
                        "<": left < right,
                        ">": left > right,
                        "greater_than": left > right,
                        "less_than": left < right,
                        "less_than_or_equals": left <= right,
                        "greater_than_or_equals": left >= right,
                    }[compare]
                )
            elif key in ("has_idea", "has_tech"):
                checks.append(val in current.get(key, set()))
            elif key == "has_global_flag":
                checks.append(val in self.flags)
            elif key == "tag":
                checks.append(current.get("tag") == val)
            elif key == "generator_complex":
                checks.append(current.get("building_level@generator_complex", 0) > float(val))
            else:
                raise AssertionError(("unsupported trigger", key))
        return all(checks)

    def run(self, name, current=None, previous=None):
        current = self.globals if current is None else current
        self.calls[name] += 1
        self.execute(self.effects[name], current, previous)

    def execute(self, body, current, previous):
        matched = False
        for key, _, val in body:
            if key in ("if", "else_if", "else"):
                limit = next((v for k, _, v in val if k == "limit"), [])
                if key == "if":
                    matched = False
                if not matched and self.matches(limit, current, previous):
                    matched = True
                    self.execute([t for t in val if t[0] != "limit"], current, previous)
            elif key in self.effects:
                self.run(key, current, previous)
            elif key == "random_country":
                self.execute(val, self.countries[0], current)
            elif key.startswith("every_"):
                scopes = {
                    "every_country": self.countries,
                    "every_state": self.states,
                    "every_controlled_state": current.get("states", []),
                    "every_neighbor_state": current.get("neighbors", []),
                }[key]
                limit = next((v for k, _, v in val if k == "limit"), [])
                for scope in scopes:
                    if self.matches(limit, scope, current):
                        self.execute([t for t in val if t[0] != "limit"], scope, current)
            elif key in ("set_variable", "add_to_variable", "subtract_from_variable", "multiply_variable", "divide_variable", "modulo_variable"):
                name, _, raw = val[0]
                right = self.value(raw, current, previous)
                left = self.value(name, current, previous)
                if key == "set_variable":
                    result = right
                elif key == "add_to_variable":
                    result = left + right
                elif key == "subtract_from_variable":
                    result = left - right
                elif key == "multiply_variable":
                    result = left * right
                elif key == "divide_variable":
                    result = left / right
                else:
                    result = left % right
                self.assign(name, result, current, previous)
            elif key == "clamp_variable":
                data = {k: v for k, _, v in val}
                result = max(float(data["min"]), min(float(data["max"]), self.value(data["var"], current, previous)))
                self.assign(data["var"], result, current, previous)
            elif key == "round_variable":
                self.assign(val, round(self.value(val, current, previous)), current, previous)
            elif key == "set_variable_to_random":
                data = {k: v for k, _, v in val}
                # Midpoint makes results independent of traversal order for the scheduling test.
                result = (self.value(data["min"], current, previous) + self.value(data["max"], current, previous)) / 2
                self.assign(data["var"], int(result) if data.get("integer") == "yes" else result, current, previous)
            elif key == "set_global_flag":
                self.flags.add(val)
            elif key == "add_ideas":
                current.setdefault("has_idea", set()).clear()
                current["has_idea"].add(val)
            elif key == "force_update_dynamic_modifier":
                self.calls[key] += 1
            else:
                raise AssertionError(("unsupported effect", key))


@pytest.mark.parametrize(
    "temperature,frame,score",
    [
        (-50, 1, 1),
        (-40, 2, 3),
        (-39.9, 2, 3),
        (-20, 3, 7),
        (-19.9, 3, 7),
        (-10, 4, 8),
        (-9.9, 4, 8),
        (0, 5, 10),
        (0.1, 5, 10),
        (29.9, 5, 10),
        (30, 6, 9),
        (30.1, 6, 9),
        (40, 6, 9),
        (40.1, 7, 1),
        (50, 7, 1),
    ],
)
def test_comfort_thresholds(temperature, frame, score):
    system = Temperature()
    state = {"PIHC_var_state_indoor_temperature": temperature}
    system.run("PIHC_update_state_temperature_buff", state)
    assert state["PIHC_var_state_indoor_temperature_frame"] == frame
    assert state["PIHC_var_state_indoor_temperature_comfort_score"] == score


def test_heating_and_burn_override():
    system = Temperature()
    state = {
        "PIHC_var_state_outdoor_temperature": -40,
        "building_level@generator_complex": 2,
        "PIHC_temperature_neighbor_heat": 5,
        "PIHC_var_country_tec_COLD_level": 2,
        "PIHC_var_country_ERA_COLD_level": 3,
        "coal_subsidies": 1,
        "logs_subsidies": 1,
    }
    system.run("PIHC_update_state_indoor_temperature", state)
    assert state["PIHC_var_state_indoor_temperature"] == 37  # -40 + 30 + 5 + 6 + 21 + 10 + 5
    state["PIHC_var_country_ERA_COLD_level"] = -1
    system.run("PIHC_update_state_indoor_temperature", state)
    assert state["PIHC_var_state_indoor_temperature"] == 20


@pytest.mark.parametrize("temperature,y", [(-50, 285), (-39, 285), (-30, 249), (-10, 169), (-1, 133), (0, 129), (10, 89), (30, 9), (42, -39), (50, -39)])
def test_mercury_position_is_continuous_and_clipped(temperature, y):
    system = Temperature()
    state = {"PIHC_var_state_outdoor_temperature": temperature, "PIHC_var_state_indoor_temperature": 4}
    system.run("PIHC_update_state_outdoor_temperature_frame", state)
    assert state["PIHC_temperature_mercury_y"] == pytest.approx(y)
    state["PIHC_var_state_outdoor_temperature"] = 1.25
    system.run("PIHC_update_state_outdoor_temperature_frame", state)
    assert state["PIHC_temperature_mercury_y"] == pytest.approx(124)


def test_two_years_one_update_per_state_and_bounded_population_weighting():
    system = Temperature()
    a = {"state_population_k": 1_000_000, "building_level@generator_complex": 2}
    b = {"state_population_k": 500_000}
    a["neighbors"], b["neighbors"] = [b], [a]
    system.states = [a, b]
    country = {"states": system.states, "tag": "C08", "num_controlled_states": 2}
    empty = {"states": [], "tag": "C67"}
    system.countries = [country, empty]
    for month in range(24):
        system.globals["num_days"] = 1003 * 365 + (month // 12) * 365 + [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334][month % 12]
        system.run("PIHC_update_current_month")
        assert a["PIHC_temperature_neighbor_heat"] == 0
        assert b["PIHC_temperature_neighbor_heat"] == 5
        assert country["PIHC_var_country_temperature_population_sum"] == 1500
        expected = (2 * a["PIHC_var_state_indoor_temperature_comfort_score"] + b["PIHC_var_state_indoor_temperature_comfort_score"]) / 3
        assert country["PIHC_var_country_temperature_comfort_score"] == pytest.approx(expected, abs=0.001)
        assert empty["PIHC_var_country_temperature_comfort_score"] == 10
    assert system.globals["PIHC_var_current_month"] == 12
    assert system.globals["PIHC_temperature_revision"] == 24
    assert system.calls["PIHC_update_state_indoor_temperature"] == 48
    assert system.calls["PIHC_update_state_indoor_temperature_frame"] == 48
    assert system.calls["PIHC_prepare_temperature_season"] == 24
    # Removing a tower clears cached neighbor heat at the next monthly update.
    a["building_level@generator_complex"] = 0
    system.run("PIHC_update_current_month")
    assert b["PIHC_temperature_neighbor_heat"] == 0


def test_ui_and_schedule_use_one_refresh_and_no_weekly_work():
    gui = source("scripted_gui", "pihc_state_temperature - */def.txt")
    assert gui.count("dirty =") == 1
    assert "y = THIS.PIHC_temperature_mercury_y" in gui
    gfx = source("interface", "*TEMPERATURE*/interface/PIHC_state_temperature.gfx")
    assert "mercury_indicator_strip" not in gfx
    assert "mercury.dds" in gfx
    actions = source("on_action", "PIHC_STATE_TEMPERATURE - */def.txt")
    assert "on_weekly" not in actions
    assert actions.count("PIHC_update_current_month = yes") == 1
    assert "NOT = { has_global_flag = PIHC_temperature_initialized }" in actions


def test_country_monthly_callbacks_advance_world_only_once():
    system = Temperature()
    system.countries = [{"states": [], "tag": "C08"}]
    actions = parse(source("on_action", "PIHC_STATE_TEMPERATURE - */def.txt"))[0][2]
    monthly = next(v for k, _, v in actions if k == "on_monthly")
    effect = next(v for k, _, v in monthly if k == "effect")
    system.globals["PIHC_temperature_last_update_month"] = 0
    for month, day in enumerate([0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334], 1):
        system.globals["num_days"] = 1003 * 365 + day
        for callback in range(68):
            system.globals["num_days"] = 1003 * 365 + day + callback % 20
            system.execute(effect, system.countries[0], None)
        assert system.globals["PIHC_temperature_revision"] == month
        assert system.globals["PIHC_var_current_month"] == month
    assert system.calls["PIHC_update_monthly_global_variables"] == 12


@pytest.mark.parametrize(
    "policy,costs", [("1_COAL", (32, 0, 0)), ("2_WOOD", (0, 48, 0)), ("3_SUBSIDY", (0, 0, 12)), ("4_SELF", (0, 0, 0)), ("5_BURN", (0, 0, 0))]
)
def test_only_selected_heating_policy_charges_resources(policy, costs):
    system = Temperature()
    country = {"num_controlled_states": 96, "tag": "C08", "has_idea": {"IDEA_ERA_COLD_" + policy}, "resource@coal": 100, "resource@logs": 100}
    system.run("PIHC_prepare_country_temperature", country)
    assert tuple(country["PIHC_var_country_modifier_cost_" + res] for res in ["coal", "logs", "civ"]) == costs


def test_wood_shortage_falls_back_even_when_coal_is_available():
    system = Temperature()
    country = {"has_idea": {"IDEA_ERA_COLD_2_WOOD"}, "resource@coal": 10, "resource@logs": -1}
    system.run("PIHC_change_ERA_COLD_level", country)
    assert country["has_idea"] == {"IDEA_ERA_COLD_3_SUBSIDY"}
