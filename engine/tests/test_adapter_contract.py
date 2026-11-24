"""What every catalog adapter must declare, so a lead can never quietly lose its catalog tag."""
import pytest

from engine.core import actions
from engine.core.adapter_base import DomainAdapter, create_verified_lead
from engine.core.crm_contacts import lead_contact_values
from engine.tests.fakes import FakeOdoo, item

LEAD_ARGS = {"name": "2024 Toyota Corolla", "customer_name": "Sarah", "customer_contact": "sarah@example.com"}
HOLD_ARGS = {"item_id": 7, "customer_name": "Sarah", "customer_contact": "sarah@example.com"}


def _concrete(**attributes):
    """Define a complete adapter class with the given class attributes."""
    body = {"tools": lambda self: [], "execute_tool": lambda self, name, args: {}, **attributes}
    return type("SomeAdapter", (DomainAdapter,), body)


def test_an_adapter_without_a_domain_type_or_crm_tag_fails_when_the_class_is_defined():
    with pytest.raises(TypeError, match="SomeAdapter must set a non-empty domain_type and crm_tag"):
        _concrete()


@pytest.mark.parametrize("attributes, missing", [
    ({"domain_type": "boats"}, "crm_tag"),
    ({"crm_tag": "Boats"}, "domain_type"),
    ({"domain_type": "boats", "crm_tag": "  "}, "crm_tag"),
    ({"domain_type": "", "crm_tag": "Boats"}, "domain_type"),
    ({"domain_type": "boats", "crm_tag": None}, "crm_tag"),
])
def test_each_missing_or_blank_attribute_is_named(attributes, missing):
    with pytest.raises(TypeError, match=f"must set a non-empty {missing}$"):
        _concrete(**attributes)


def test_a_complete_adapter_is_accepted():
    assert _concrete(domain_type="boats", crm_tag="Boats").crm_tag == "Boats"


def test_an_abstract_intermediate_base_does_not_need_them():
    class PartialBase(DomainAdapter):
        def execute_tool(self, name, args):
            return {}

    assert PartialBase.__abstractmethods__ == frozenset({"tools"})


@pytest.mark.parametrize("call", [
    lambda odoo: create_verified_lead(odoo, "cars", dict(LEAD_ARGS)),  # pyright: ignore[reportCallIssue]
    lambda odoo: lead_contact_values(odoo, dict(LEAD_ARGS)),  # pyright: ignore[reportCallIssue]
    lambda odoo: actions.reserve_item(odoo, "cars", dict(HOLD_ARGS)),  # pyright: ignore[reportCallIssue]
    lambda odoo: actions.book_viewing(odoo, "cars", dict(HOLD_ARGS)),  # pyright: ignore[reportCallIssue]
    lambda odoo: actions.execute_action_tool(odoo, "cars", "reserve_item", dict(HOLD_ARGS)),  # pyright: ignore[reportCallIssue]
], ids=["create_verified_lead", "lead_contact_values", "reserve_item", "book_viewing", "execute_action_tool"])
def test_the_catalog_tag_has_to_be_passed_explicitly(call):
    with pytest.raises(TypeError, match="crm_tag"):
        call(FakeOdoo(items={7: item(7)}))
