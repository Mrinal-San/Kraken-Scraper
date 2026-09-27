from playwright.sync_api import sync_playwright
from datetime import date, timedelta

pw = sync_playwright().start()
browser = pw.firefox.launch(headless = False, slow_mo = 2000)
page = browser.new_page()
page.goto("https://www.yatra.com", wait_until="domcontentloaded", timeout=60000)

# ==========================================
# 1. HANDLE DEPARTURE (Origin)
# ==========================================

departure_field = page.locator('[role="Combobox"][aria-label^="Departure From"]')
departure_field.click()
city_input = page.locator("#input-with-icon-adornment")
city_input.fill("Mumbai")
page.get_by_text("Mumbai", exact=True).first.click()
print("✅ Departure changed to Mumbai successfully!")




# ==========================================
# 2. HANDLE ARRIVAL (Destination)
# ==========================================
city_input = page.locator("#input-with-icon-adornment")
city_input.fill("New Delhi")
page.get_by_text("New Delhi", exact=True).first.click()
print("✅ Destination changed to New Delhi successfully!")


# ==========================================
# 3. HANDLE DEPARTURE DATE
# ==========================================
tomorrow = date.today() + timedelta(days=1)
day = tomorrow.day
if 10 <= day % 100 <= 20:
	ordinal = "th"
else:
	ordinal = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
date_label = f"Choose {tomorrow.strftime('%A, %B')} {day}{ordinal}, {tomorrow.year}"
calendar_day = page.locator(
	f'.react-datepicker__day:not(.react-datepicker__day--outside-month)'
	f'[aria-label="{date_label}"][aria-disabled="false"]'
)
calendar_day.wait_for(state="visible", timeout=10000)
calendar_day.click()
page.wait_for_timeout(500)

# ==========================================
# 4. SELECT PREMIUM ECONOMY
# ==========================================
page.locator('[aria-label="Travellers class inputbox"]').click()
premium_economy = page.locator('label[role="radio"][aria-label="Premium Economy"]')
premium_economy.focus()
premium_economy.press("Space")
if not page.locator('input[type="radio"][value="Special"]').is_checked():
	raise RuntimeError("Premium Economy was not selected")
page.get_by_role("button", name="Done", exact=True).click()

# ==========================================
# 5. HANDLE SEARCH BUTTON & REDIRECT
# ==========================================
search_button = page.get_by_role("button", name="Search", exact=True)
search_button.click()
page.wait_for_timeout(1000)

print(page.title())
page.screenshot(path="screenshot.png")

browser.close()
print("Browser closed successfully.")