"""The script that makes the engine's limited Odoo account."""
import pytest

from scripts import create_odoo_engine_user as script


class Odoo:
    def __init__(self, users=()):
        self.users = list(users)
        self.created, self.written = [], []

    def search_read(self, model, domain, fields, **kwargs):
        if model == "ir.model.data":
            return [{"res_id": 91}]
        return [u for u in self.users if u["login"] == domain[0][2]]

    def create(self, model, values):
        self.created.append((model, values))
        return 7

    def write(self, model, ids, values):
        self.written.append((model, ids, values))
        return True


ENV = {"ODOO_ENGINE_LOGIN": "leadgate-engine", "ODOO_ENGINE_PASSWORD": "a-long-engine-password"}


def test_a_new_account_gets_the_engine_group_and_nothing_else_of_its_own():
    odoo = Odoo()
    assert script.ensure_engine_user(odoo, "leadgate-engine", "a-long-engine-password") == 7
    model, values = odoo.created[0]
    assert model == "res.users" and values["login"] == "leadgate-engine" and values["password"] == "a-long-engine-password"
    assert values["groups_id"] == [(6, 0, [91])]  # replaces the default groups; the group brings what it implies
    assert values["email"] == "leadgate-engine@leadgate.invalid"  # Odoo will not assign a lead for a sender with none


def test_a_sender_address_of_your_own_can_be_given():
    odoo = Odoo()
    script.ensure_engine_user(odoo, "leadgate-engine", "a-long-engine-password", "bot@shop.example")
    assert odoo.created[0][1]["email"] == "bot@shop.example"


def test_running_it_again_resets_the_password_and_groups_of_the_same_account():
    odoo = Odoo([{"id": 5, "login": "leadgate-engine"}])
    assert script.ensure_engine_user(odoo, "leadgate-engine", "another-long-password") == 5
    assert odoo.created == [] and odoo.written[0][1] == [5] and odoo.written[0][2]["password"] == "another-long-password"


@pytest.mark.parametrize("login, password", [("admin", "a-long-engine-password"), ("leadgate-engine", "short"), ("leadgate-engine", "")])
def test_it_refuses_the_admin_account_and_weak_passwords(login, password):
    with pytest.raises(SystemExit):
        script.ensure_engine_user(Odoo(), login, password)


def test_the_engines_own_settings_name_the_account_to_create():
    env = {"ODOO_USER": "leadgate-engine", "ODOO_PASSWORD": "from-the-engine-settings"}
    assert script.engine_credentials(env) == ("leadgate-engine", "from-the-engine-settings")


def test_explicit_engine_settings_win():
    env = {"ODOO_USER": "admin", "ODOO_PASSWORD": "admins-password", "ODOO_ENGINE_LOGIN": "bot", "ODOO_ENGINE_PASSWORD": "bots-long-password"}
    assert script.engine_credentials(env) == ("bot", "bots-long-password")


def test_the_admins_password_is_never_given_to_the_engine_account():
    assert script.engine_credentials({"ODOO_USER": "admin", "ODOO_PASSWORD": "admins-password"}) == ("leadgate-engine", "")


def test_a_module_that_is_not_upgraded_is_reported():
    odoo = Odoo()
    odoo.search_read = lambda model, domain, fields, **kwargs: []
    with pytest.raises(SystemExit, match="upgrade"):
        script.ensure_engine_user(odoo, "leadgate-engine", "a-long-engine-password")
