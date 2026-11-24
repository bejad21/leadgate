import json

import pytest

from eval import cases

KINDS = [(domain, kind) for domain in ("cars", "real_estate") for kind in ("dev", "heldout")]


@pytest.mark.parametrize(("domain", "kind"), KINDS)
def test_every_dataset_is_valid_and_has_unique_ids(domain, kind):
    path = cases.dataset_path(domain, kind)
    _, items = cases.load_dataset(path)
    assert items
    assert cases.validate_cases(items, path.name) == []


def test_ids_are_unique_across_every_dataset():
    seen: dict[str, str] = {}
    for domain, kind in KINDS:
        path = cases.dataset_path(domain, kind)
        for case in cases.load_dataset(path)[1]:
            assert case["id"] not in seen, f"{case['id']} is in {seen[case['id']]} and {path.name}"
            seen[case["id"]] = path.name


def test_the_red_team_set_has_unique_ids():
    attacks = json.loads((cases.DATASETS_DIR / "redteam_set.json").read_text(encoding="utf-8"))
    assert len({a["id"] for a in attacks}) == len(attacks) == 38


@pytest.mark.parametrize("domain", ["cars", "real_estate"])
def test_the_dev_sets_cover_holds_viewings_multi_turn_and_out_of_scope(domain):
    categories = {c["category"] for c in cases.load_dataset(cases.dataset_path(domain, "dev"))[1]}
    assert {"hold_request", "hold_missing_contact", "hold_unseen_item", "viewing_request", "multi_turn_refine", "multi_turn_lead",
            "ambiguous_ask", "out_of_scope", "no_match_genuine"} <= categories


def test_the_cars_dev_set_has_a_case_for_every_cars_filter():
    items = cases.load_dataset(cases.dataset_path("cars", "dev"))[1]
    used = {key for c in items for key in (c.get("expected_args") or {})}
    assert {"condition", "location", "year_min", "mileage_max", "sort_by", "price_min", "price_max", "make", "model"} <= used
    sorts = {c["expected_args"].get("sort_by") for c in items if c["expected_args"].get("sort_by")}
    assert sorts == {"price_asc", "price_desc", "mileage_asc", "year_desc"}
    assert any(
        (c["expected_args"].get("location") or {}).get("$any_of") == ["KY", "Kentucky"] for c in items if isinstance(c["expected_args"].get("location"), dict)
    )


def test_every_corrected_label_is_listed_with_before_and_after():
    info, items = cases.load_dataset(cases.dataset_path("real_estate", "dev"))
    corrections = {c["id"]: c for c in info["phase_6"]["label_corrections"]}
    assert {"r03", "r06", "r41", "r28"} <= set(corrections)
    for entry in corrections.values():
        assert entry["before"] and "after" in entry and entry["reason"]
    by_id = {c["id"]: c for c in items}
    assert by_id["r41"]["expected_tool"] is None and by_id["r41"]["accept_tools"] == [None, "search_listings"]
    assert "bedrooms" not in json.dumps([c["expected_args"] for c in items])
    assert "property_type" not in json.dumps([c["expected_args"] for c in items])


def test_the_original_dev_ids_are_all_still_there():
    for domain, prefix, count in (("cars", "c", 45), ("real_estate", "r", 48)):
        ids = {c["id"] for c in cases.load_dataset(cases.dataset_path(domain, "dev"))[1]}
        assert {f"{prefix}{i:02d}" for i in range(1, count + 1)} <= ids
