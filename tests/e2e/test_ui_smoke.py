"""Browser smoke tests — the critical UI paths that unit tests can't reach.

These would have caught the recent regressions: the queue missing `kind` (reassign
button never appeared) and the broken onclick quoting (clicking a name did nothing).
Dev-only; requires `playwright install chromium`. Run: pytest tests/e2e -q

Seed (conftest): a person card (Mario, faceButtons flow) and a dog card (Scooby,
D-pad cross flow). Faces read "who is this?"; dogs/cars use the inverted-T cross.
"""
import json


def _open_pool(page, url, name):
    page.goto(url)
    page.wait_for_selector(".id")
    page.click(f".id:has-text('{name}')")
    page.wait_for_selector("#btns button")   # controls rendered (cross or face row)


def _expect_verdict(page):
    return page.expect_request(
        lambda r: r.url.endswith("/api/verdict") and r.method == "POST")


# --- dog/car: the inverted-T cross ------------------------------------------
def test_dog_pool_renders_cross(winnow, page):
    _open_pool(page, winnow["url"], "Scooby")
    for sel in (".dpad .no", ".dpad .skip", ".dpad .yes", ".dpad .undo"):
        assert page.locator(sel).is_visible()


# --- faces: "who is this?" --------------------------------------------------
def test_face_card_has_who_is_this_buttons(winnow, page):
    _open_pool(page, winnow["url"], "Mario")
    assert page.locator("#btns button.yes").is_visible()       # confirm the guess
    assert page.locator("#btns button.reassign").is_visible()  # identify (primary)
    assert page.locator("#btns button.no").is_visible()        # reject


def test_face_yes_posts_verdict(winnow, page):
    _open_pool(page, winnow["url"], "Mario")
    with _expect_verdict(page) as ri:
        page.click("#btns button.yes")
    body = json.loads(ri.value.post_data)
    assert body["cid"] == "face|Mario|t1" and body["verdict"] == "yes"


def test_face_reject_posts_reject(winnow, page):
    _open_pool(page, winnow["url"], "Mario")
    with _expect_verdict(page) as ri:
        page.click("#btns button.no")          # "✗ Reject" for faces
    assert json.loads(ri.value.post_data)["verdict"] == "reject"


def test_flag_posts(winnow, page):
    _open_pool(page, winnow["url"], "Mario")
    with page.expect_request(
        lambda r: r.url.endswith("/api/flag") and r.method == "POST") as ri:
        page.click("button.flag")              # debug mode on by default
    assert json.loads(ri.value.post_data)["cid"] == "face|Mario|t1"


# --- reassign typeahead (faces; reassign is in the main row) ----------------
def test_reassign_opens(winnow, page):
    _open_pool(page, winnow["url"], "Mario")
    page.click("#btns button.reassign")
    page.wait_for_selector("#reassign", state="visible")
    assert page.locator("#ratext").is_visible()


def test_reassign_typeahead_filters(winnow, page):
    _open_pool(page, winnow["url"], "Mario")
    page.click("#btns button.reassign")
    page.wait_for_selector("#reassign", state="visible")
    page.fill("#ratext", "Toad")
    page.wait_for_selector("#ralist .raitem")
    items = page.locator("#ralist .raitem").all_inner_texts()
    assert any("Toad" == t for t in items)
    assert any("Toadette" == t for t in items)
    assert not any("Mario" == t for t in items)


def test_reassign_arrow_then_enter_posts_assign(winnow, page):
    _open_pool(page, winnow["url"], "Mario")
    page.click("#btns button.reassign")
    page.wait_for_selector("#reassign", state="visible")
    page.fill("#ratext", "Toad")            # -> [Toad, Toadette], Toad highlighted
    page.wait_for_selector("#ralist .raitem")
    page.keyboard.press("ArrowDown")        # -> Toadette highlighted
    with _expect_verdict(page) as ri:
        page.keyboard.press("Enter")
    assert json.loads(ri.value.post_data)["verdict"] == "assign:Toadette"


def test_reassign_click_name_posts_assign(winnow, page):
    # the path the onclick-quoting bug broke: clicking a name must fire
    _open_pool(page, winnow["url"], "Mario")
    page.click("#btns button.reassign")
    page.wait_for_selector("#reassign", state="visible")
    page.fill("#ratext", "Pea")
    page.wait_for_selector("#ralist .raitem")
    with _expect_verdict(page) as ri:
        page.click("#ralist .raitem:has-text('Peach')")
    assert json.loads(ri.value.post_data)["verdict"] == "assign:Peach"


def test_blank_field_has_no_highlight(winnow, page):
    _open_pool(page, winnow["url"], "Mario")
    page.click("#btns button.reassign")
    page.wait_for_selector("#ralist .raitem")
    assert page.locator("#ralist .raitem.active").count() == 0
