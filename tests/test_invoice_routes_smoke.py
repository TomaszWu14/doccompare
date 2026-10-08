import app as appmod


def test_home_requires_login():
    client = appmod.app.test_client()
    r = client.get("/invoices/")
    assert r.status_code in (302, 401)          # redirect do logowania


def test_blueprint_registered():
    rules = {r.rule for r in appmod.app.url_map.iter_rules()}
    assert "/invoices/" in rules
    assert "/invoices/upload" in rules
    assert "/invoices/coverage" in rules
