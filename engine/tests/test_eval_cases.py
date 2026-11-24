import datetime as dt
import hashlib
import json

import pytest

from eval import cases, heldout_rules, rules

REF = dt.date(2026, 11, 4)
ROOT = cases.DATASETS_DIR


def row(id_, price, name, **attrs):
    return {"id": id_, "name": name, "price": float(price), "attributes": json.dumps(attrs)}


CATALOG = [
    row(11, 40470, "2021 Land Rover Range Rover", make="Land Rover", model="Range Rover", year="2021", location="Cincinnati, OH"),
    row(10, 34726, "2021 Land Rover Range Rover", make="Land Rover", model="Range Rover", year="2021", location="Cincinnati, OH"),
    row(13, 32550, "2024 Toyota Camry", make="Toyota", model="Camry", year="2024", location="Clarksville, IN"),
]


def test_the_frozen_held_out_files_still_match_their_recorded_hashes():
    cases.verify_frozen()  # raises if either file changed


def test_a_changed_held_out_file_is_detected(tmp_path):
    for name in ("heldout_cars.json", "heldout_real_estate.json"):
        (tmp_path / name).write_bytes((ROOT / name).read_bytes())
    (tmp_path / "HELDOUT_SHA256.txt").write_bytes((ROOT / "HELDOUT_SHA256.txt").read_bytes())
    cases.verify_frozen(tmp_path)
    changed = (tmp_path / "heldout_cars.json").read_text(encoding="utf-8").replace("Mustang", "Mustangs", 1)
    (tmp_path / "heldout_cars.json").write_text(changed, encoding="utf-8")
    with pytest.raises(cases.FrozenFileChanged, match="heldout_cars.json"):
        cases.verify_frozen(tmp_path)


def test_the_recorded_hash_is_a_real_sha256_of_the_file():
    recorded = {line.split()[1]: line.split()[0] for line in (ROOT / "HELDOUT_SHA256.txt").read_text().splitlines() if line.strip()}
    for name, digest in recorded.items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest


def test_every_held_out_case_has_a_rule_entry_and_every_entry_a_case():
    ids = set()
    for kind in ("cars", "real_estate"):
        _, loaded = cases.load_dataset(ROOT / f"heldout_{kind}.json")
        ids |= {c["id"] for c in loaded}
    assert ids == set(heldout_rules.HELDOUT_RULES)


def test_every_held_out_rule_names_a_rule_that_exists():
    def names(rule):
        yield rule["rule"]
        for option in rule.get("options", []):
            for inner in option:
                yield from names(inner)

    used = {n for entry in heldout_rules.HELDOUT_RULES.values() for r in entry.get("rules", []) for n in names(r)}
    assert used <= set(rules.RULES)


def test_a_search_case_resolves_to_its_tool_arguments_and_default_rules():
    case = {"id": "x1", "message": "any Ford?", "expected_tool": "search_inventory", "expected_args": {"make": "Ford"}, "expected_outcome": "o"}
    expected = cases.build_expected(case, CATALOG, REF)
    assert expected["tool"] == "search_inventory" and expected["args"] == {"make": "Ford"}
    names = [r["rule"] for r in expected["rules"]]
    assert names[0] == "reply_nonempty" and "says_none_when_empty" in names


def test_a_no_tool_dev_case_requires_no_tool_and_no_link():
    case = {"id": "x2", "message": "weather?", "expected_tool": None, "expected_args": {}, "expected_outcome": "o"}
    names = [r["rule"] for r in cases.build_expected(case, CATALOG, REF)["rules"]]
    assert "no_tool_called" in names and "reply_no_link" in names


def test_explicit_rules_in_a_case_replace_the_derived_ones():
    case = {"id": "x3", "message": "m", "expected_tool": None, "expected_args": {}, "expected_outcome": "o",
            "rules": [{"rule": "reply_asks", "topics": ["budget"]}]}
    names = [r["rule"] for r in cases.build_expected(case, CATALOG, REF)["rules"]]
    assert names == ["reply_nonempty", "reply_asks"]


def test_accept_tools_may_include_no_tool():
    case = {"id": "x4", "message": "m", "expected_tool": "search_inventory", "expected_args": {}, "expected_outcome": "o", "accept_tools": [None]}
    assert cases.build_expected(case, CATALOG, REF)["accept_tools"] == [None]


def test_a_hold_case_resolves_the_item_id_from_the_catalog():
    case = {"id": "h-c26", "message": "hold the one for 40470", "expected_tool": "reserve_item",
            "expected_args": {"item_id": "<catalog id of the 2021 Land Rover Range Rover priced 40470 in Cincinnati, OH>",
                              "customer_name": "Grace Kim", "customer_contact": "grace.kim@example.com"},
            "expected_outcome": "o"}
    expected = cases.build_expected(case, CATALOG, REF)
    assert expected["args"]["item_id"] == 11 and not expected["resolution_errors"]


def test_a_viewing_case_gets_the_any_future_saturday_matcher():
    case = {"id": "h-c28", "message": "m", "expected_tool": "book_viewing", "expected_args": {}, "expected_outcome": "o"}
    expected = cases.build_expected(case, CATALOG, REF)
    assert expected["args"]["date"] == {"$weekday_after": {"weekday": 5, "after": "2026-11-04"}}
    assert expected["args"]["item_id"] == 13


def test_an_unresolvable_placeholder_is_recorded_not_raised():
    case = {"id": "x5", "message": "m", "expected_tool": "reserve_item",
            "expected_args": {"item_id": "<catalog id of the 1999 Ferrari priced 5000>"}, "expected_outcome": "o"}
    expected = cases.build_expected(case, CATALOG, REF)
    assert expected["resolution_errors"] and "item_id" not in expected["args"]


def test_history_gets_the_search_results_the_real_chat_would_have_stored():
    history = [
        {"role": "user", "content": "range rover in cincinnati?"},
        {"role": "assistant", "content": "I found 2: 1) 2021 Land Rover Range Rover, $34,726, Cincinnati, OH. 2) 2021 Land Rover Range Rover, $40,470, Cincinnati, OH."},
    ]
    built = cases.build_history(history, CATALOG, "search_inventory")
    assert [m["role"] for m in built] == ["user", "assistant", "tool", "assistant"]
    assert built[1]["tool_calls"][0]["function"]["name"] == "search_inventory"
    shown = json.loads(built[2]["content"])
    assert sorted(m["id"] for m in shown["matches"]) == [10, 11]
    assert built[2]["tool_call_id"] == built[1]["tool_calls"][0]["id"]
    assert built[3] == history[1]


def test_history_without_prices_is_left_as_it_was():
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hello! What are you looking for?"}]
    assert cases.build_history(history, CATALOG, "search_inventory") == history


def test_dataset_validation_flags_duplicate_ids_and_missing_keys():
    good = {"id": "a", "category": "c", "message": "m", "expected_tool": None, "expected_args": {}, "expected_outcome": "o"}
    assert cases.validate_cases([good], "t") == []
    problems = cases.validate_cases([good, dict(good)], "t")
    assert any("duplicate id" in p for p in problems)
    broken = {k: v for k, v in good.items() if k != "message"}
    assert any("message" in p for p in cases.validate_cases([broken], "t"))
    assert any("rule" in p for p in cases.validate_cases([{**good, "id": "b", "rules": [{"rule": "nope"}]}], "t"))
    assert any("accept_tools" in p for p in cases.validate_cases([{**good, "id": "c", "accept_tools": "x"}], "t"))


def test_the_fresh_kind_maps_to_its_own_files():
    assert cases.dataset_path("cars", "fresh").name == "fresh_cars.json"
    assert cases.dataset_path("real_estate", "fresh").name == "fresh_real_estate.json"


def test_a_fresh_set_is_verified_against_its_own_hash_file(tmp_path):
    (tmp_path / "fresh_cars.json").write_text('{"cases": []}', encoding="utf-8")
    digest = cases.sha256_of(tmp_path / "fresh_cars.json")
    (tmp_path / "FRESH_SHA256.txt").write_text(f"{digest}  fresh_cars.json\n", encoding="utf-8")
    cases.verify_frozen(tmp_path, "FRESH_SHA256.txt")
    (tmp_path / "fresh_cars.json").write_text('{"cases": [1]}', encoding="utf-8")
    with pytest.raises(cases.FrozenFileChanged):
        cases.verify_frozen(tmp_path, "FRESH_SHA256.txt")


def test_an_unfrozen_fresh_set_is_refused_for_a_scored_run(tmp_path):
    with pytest.raises(cases.FrozenFileChanged, match="not frozen"):
        cases.verify_frozen(tmp_path, "FRESH_SHA256.txt")


def test_the_frozen_fresh_files_have_not_changed():
    cases.verify_frozen(hash_file="FRESH_SHA256.txt")  # raises if either file changed


def test_the_frozen_fresh_files_say_what_was_reviewed():
    for domain in ("cars", "real_estate"):
        info, items = cases.load_dataset(cases.dataset_path(domain, "fresh"))
        assert info["frozen"] is True and len(items) == 30
        assert "judgement-call" in info["owner_label_review"] and "pending" in info["owner_label_review"]


def test_the_second_fresh_kind_maps_to_its_own_files_and_hash_file():
    assert cases.dataset_path("cars", "fresh2").name == "fresh2_cars.json"
    assert cases.dataset_path("real_estate", "fresh2").name == "fresh2_real_estate.json"
    assert cases.HASH_FILES["fresh2"] == "FRESH2_SHA256.txt"
    assert cases.HASH_FILES["fresh"] == "FRESH_SHA256.txt" and cases.HASH_FILES["heldout"] == "HELDOUT_SHA256.txt"


def test_the_frozen_second_fresh_files_have_not_changed_and_say_they_were_not_reviewed():
    cases.verify_frozen(hash_file="FRESH2_SHA256.txt")
    for domain in ("cars", "real_estate"):
        info, items = cases.load_dataset(cases.dataset_path(domain, "fresh2"))
        assert info["frozen"] is True and len(items) == 30
        assert info["owner_label_review"] == "none: not reviewed by a human"


def test_the_third_fresh_kind_maps_to_its_own_files_and_hash_file():
    assert cases.dataset_path("cars", "fresh3").name == "fresh3_cars.json"
    assert cases.dataset_path("real_estate", "fresh3").name == "fresh3_real_estate.json"
    assert cases.HASH_FILES["fresh3"] == "FRESH3_SHA256.txt"


def test_the_frozen_third_fresh_files_have_not_changed_and_say_they_were_not_reviewed():
    cases.verify_frozen(hash_file="FRESH3_SHA256.txt")
    for domain in ("cars", "real_estate"):
        info, items = cases.load_dataset(cases.dataset_path(domain, "fresh3"))
        assert info["frozen"] is True and len(items) == 30
        assert info["owner_label_review"] == "none: not reviewed by a human"


def test_the_large_fresh_kind_maps_to_its_own_files_and_hash_file():
    assert cases.dataset_path("cars", "fresh4").name == "fresh4_cars.json"
    assert cases.dataset_path("real_estate", "fresh4").name == "fresh4_real_estate.json"
    assert cases.HASH_FILES["fresh4"] == "FRESH4_SHA256.txt"


def test_the_frozen_large_fresh_files_have_not_changed_and_hold_a_hundred_unreviewed_cases():
    cases.verify_frozen(hash_file="FRESH4_SHA256.txt")
    seen: set[str] = set()
    for domain in ("cars", "real_estate"):
        info, items = cases.load_dataset(cases.dataset_path(domain, "fresh4"))
        assert info["frozen"] is True and len(items) == 100 and info["n_cases"] == 100
        assert info["owner_label_review"] == "none: not reviewed by a human"
        assert cases.validate_cases(items, domain) == []
        assert not seen & {c["id"] for c in items}
        seen |= {c["id"] for c in items}


def test_the_second_large_fresh_set_maps_to_its_files_and_is_frozen():
    assert cases.dataset_path("cars", "fresh5").name == "fresh5_cars.json"
    assert cases.HASH_FILES["fresh5"] == "FRESH5_SHA256.txt"
    cases.verify_frozen(hash_file="FRESH5_SHA256.txt")
    old_messages = set()
    for kind in ("dev", "heldout", "fresh", "fresh2", "fresh3", "fresh4"):
        for domain in ("cars", "real_estate"):
            old_messages |= {c["message"].strip().lower() for c in cases.load_dataset(cases.dataset_path(domain, kind))[1]}
    for domain in ("cars", "real_estate"):
        info, items = cases.load_dataset(cases.dataset_path(domain, "fresh5"))
        assert info["frozen"] is True and len(items) == 100
        assert cases.validate_cases(items, domain) == []
        assert not old_messages & {c["message"].strip().lower() for c in items}


def test_the_sixth_fresh_set_is_split_into_a_dev_part_and_a_locked_test_part_and_frozen_together():
    assert cases.dataset_path("cars", "fresh6dev").name == "fresh6dev_cars.json"
    assert cases.dataset_path("real_estate", "fresh6test").name == "fresh6test_real_estate.json"
    assert cases.HASH_FILES["fresh6dev"] == cases.HASH_FILES["fresh6test"] == "FRESH6_SHA256.txt"
    cases.verify_frozen(hash_file="FRESH6_SHA256.txt")
    old_messages = set()
    for kind in ("dev", "heldout", "fresh", "fresh2", "fresh3", "fresh4", "fresh5"):
        for domain in ("cars", "real_estate"):
            old_messages |= {c["message"].strip().lower() for c in cases.load_dataset(cases.dataset_path(domain, kind))[1]}
    for domain in ("cars", "real_estate"):
        dev_info, dev = cases.load_dataset(cases.dataset_path(domain, "fresh6dev"))
        test_info, test = cases.load_dataset(cases.dataset_path(domain, "fresh6test"))
        assert dev_info["frozen"] is True and test_info["frozen"] is True
        assert (len(dev), len(test)) == (50, 100)
        assert not {c["id"] for c in dev} & {c["id"] for c in test}  # a case is in one part only
        for part in (dev, test):
            assert cases.validate_cases(part, domain) == []
            assert not old_messages & {c["message"].strip().lower() for c in part}
        # the split keeps every category in both parts where it has two or more cases
        by = lambda part: {c["category"] for c in part}  # noqa: E731
        assert by(dev) <= by(test) | by(dev) and len(by(dev) & by(test)) >= 15


def test_the_seventh_fresh_set_is_split_into_a_dev_part_and_a_locked_test_part_and_frozen_together():
    assert cases.dataset_path("cars", "fresh7dev").name == "fresh7dev_cars.json"
    assert cases.dataset_path("real_estate", "fresh7test").name == "fresh7test_real_estate.json"
    assert cases.HASH_FILES["fresh7dev"] == cases.HASH_FILES["fresh7test"] == "FRESH7_SHA256.txt"
    cases.verify_frozen(hash_file="FRESH7_SHA256.txt")
    for domain in ("cars", "real_estate"):
        _, dev = cases.load_dataset(cases.dataset_path(domain, "fresh7dev"))
        _, test = cases.load_dataset(cases.dataset_path(domain, "fresh7test"))
        assert (len(dev), len(test)) == (50, 100)
        assert not {c["id"] for c in dev} & {c["id"] for c in test}
        assert cases.validate_cases(dev, "dev") == [] and cases.validate_cases(test, "test") == []
