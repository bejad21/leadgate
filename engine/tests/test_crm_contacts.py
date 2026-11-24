

def test_a_price_typed_without_separators_is_not_a_phone_number():
    from engine.core.crm_contacts import parse_contact

    assert parse_contact("Beatriz, beatriz@example.com, la casa de Madison a 1995000") == ("beatriz@example.com", None)
    assert parse_contact("call me on 859-555-0116")[1] == "8595550116"
    assert parse_contact("my number is 8595550116")[1] == "8595550116"
