"""Browser check of the whole workflow on SYNTHETIC data (needs Playwright + a running app on an EMPTY test database).

  pip install playwright && playwright install chromium
  python tests/browser_check.py synthetic_school
"""
from playwright.sync_api import sync_playwright
import glob

import os, sys
B = os.environ.get("SCHOOLOPS_URL", "http://127.0.0.1:8000") + "/app/"
S = (sys.argv[1] if len(sys.argv) > 1 else "synthetic_school").rstrip("/") + "/"
errs, out = [], []
def log(*a):
    out.append(" ".join(str(x) for x in a)); print(*a, flush=True)

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1366, "height": 900})
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: m.type == "error" and "fonts" not in m.text and "ERR_TUNNEL" not in m.text and errs.append(m.text))
    pg.goto(B); pg.fill("#caller", "Office"); pg.goto(B + "#students"); pg.wait_for_timeout(1000)
    log("tabs:", pg.locator("#nav a").all_inner_texts())
    # 1. Add Excel with all files (incl. converted xls)
    pg.click("#addExcel")
    pg.set_input_files("#impFiles", sorted(glob.glob(S + "*.xlsx")))
    pg.click("#impCheck"); pg.wait_for_selector("#impGo")
    pg.click("#impGo"); pg.click("#impYes"); pg.wait_for_selector("text=Import finished")
    log("import:", pg.inner_text(".modal-box").replace("\n", " | ")[:300])
    pg.click("[data-imp-close]"); pg.wait_for_timeout(1000)
    # 2. Confirm a sibling pair in Excel sync & review
    pg.click("[data-view=sync]"); pg.wait_for_timeout(800)
    log("reviews:", pg.locator("[data-act=approve]").count())
    pg.screenshot(path="browser-v2-sync.png")
    pg.locator("button:has-text('Yes, same parent')").first.click(); pg.wait_for_timeout(1000)
    # 3. Dashboard queue: find sibling family via search
    pg.goto(B + "#dashboard"); pg.wait_for_timeout(1500)
    log("dashboard cards:", pg.inner_text(".cards").replace("\n", " | "))
    pg.screenshot(path="browser-v2-dash.png", full_page=True)
    first = pg.inner_text("#qCurrent .famhead")
    log("queue top:", first.replace("\n", " | ")[:160])
    # call the first family: Will pay on date requires a date
    pg.click("#qCurrent [data-o=gave_date]"); pg.click("#qCurrent [data-save]"); pg.wait_for_timeout(300)
    log("date required shown:", "choose the date" in pg.inner_text("#toast").lower())
    pg.fill("#qCurrent [data-date]", "2099-01-15"); pg.click("#qCurrent [data-save]"); pg.wait_for_timeout(1500)
    log("after save toast:", pg.inner_text("#toast")[:60].replace("\n", " "))
    log("queue top changed:", pg.inner_text("#qCurrent .famhead") != first)
    pg.click("#undoBtn"); pg.wait_for_timeout(1500)
    log("after undo back to first:", pg.inner_text("#qCurrent .famhead") == first)
    # 4. Start a campaign
    pg.click("text=Start a campaign"); pg.click("#startCamp"); pg.wait_for_timeout(1500)
    log("campaign:", pg.inner_text(".campaign").replace("\n", " | ")[:200])
    # 5. Family with siblings: search for the confirmed pair via students
    sib = next(x for x in pg.request.get(B.replace("/app/", "") + "/students?page_size=100").json()["items"] if x["siblings"] > 0)
    pg.goto(B + "#students"); pg.wait_for_timeout(1000)
    pg.fill("[data-f=search]", sib["name"]); pg.wait_for_timeout(1000)
    pg.locator("tr:has(.pill:has-text('sibling'))").first.click(); pg.wait_for_timeout(800)
    pg.click("#drawer [data-family]"); pg.wait_for_timeout(1000)
    log("family drawer:", pg.inner_text("#drawer").replace("\n", " | ")[:260])
    log("kids checkboxes:", pg.locator("#drawer [data-kid]").count())
    pg.screenshot(path="browser-v2-family.png")
    pg.click("#drawer [data-o=part_payment]"); pg.fill("#drawer [data-amount]", "5000"); pg.click("#drawer [data-save]"); pg.wait_for_timeout(1500)
    log("family calls after part payment:", pg.inner_text("#drawer").count("Part payment"))
    # 6. Messages: consolidated draft
    pg.click("#drawer a:has-text('Prepare one message')"); pg.wait_for_timeout(1500)
    pg.select_option("#msgLang", "mr"); pg.click("#mkDraft"); pg.wait_for_timeout(1000)
    body = pg.input_value("#draftBody")
    log("draft has total line:", "एकूण" in body, "| lines:", body.count("\n- "))
    pg.fill("#draftBody", body + "\nकार्यालय वेळ: 9 ते 1"); pg.click("#dApprove"); pg.wait_for_timeout(1200)
    log("draft status:", pg.inner_text("#msgEditor .panel-head"))
    pg.screenshot(path="browser-v2-messages.png", full_page=True)
    # 7. Call records
    pg.goto(B + "#calls"); pg.wait_for_timeout(1000)
    log("call rows:", pg.locator("#callTable tbody tr").count(), "| sibling note:", "same call" in pg.inner_text("#callTable"))
    # 8. Follow-up filter in students
    pg.goto(B + "#students"); pg.wait_for_timeout(800)
    pg.select_option("[data-f=follow_up]", "upcoming"); pg.wait_for_timeout(800)
    log("students waiting for a date:", pg.inner_text(".pager"))
    # phone width
    m = b.new_page(viewport={"width": 390, "height": 844})
    m.on("pageerror", lambda e: errs.append(str(e)))
    for h in ["dashboard", "students", "students?view=sync", "calls", "messages"]:
        m.goto(B + "#" + h); m.wait_for_timeout(1200)
        wide = m.evaluate("""()=>{const W=innerWidth;return [...document.querySelectorAll('body *')].filter(e=>{const r=e.getBoundingClientRect();return r.right>W+1 && !e.closest('.tablewrap') && getComputedStyle(e).position!=='fixed' && !e.closest('.side')}).slice(0,3).map(e=>e.tagName+'.'+e.className)}""")
        log("phone", h, "too wide:", wide)
    m.goto(B + "#dashboard"); m.wait_for_timeout(1200); m.screenshot(path="browser-v2-mobile.png", full_page=True)
    b.close()
log("errors:", errs)
