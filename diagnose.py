"""
Diagnostic script — navigates to each bookmaker's tennis page,
waits for full render, then saves the page HTML to a local file.
Run once so scanner.py can be updated with correct selectors.
"""

import asyncio
from playwright.async_api import async_playwright

SITES = [
    ("sportsbet",  "https://www.sportsbet.com.au/betting/tennis"),
    ("neds",       "https://www.neds.com.au/sports/tennis"),
    ("ladbrokes",  "https://www.ladbrokes.com.au/sports/tennis"),
    ("bet365",     "https://www.bet365.com.au/#/AC/B13/C1/D1002/F2/"),
    ("betfair",    "https://www.betfair.com.au/sport/tennis"),
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctx = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 900},
            locale="en-AU",
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        for name, url in SITES:
            print(f"Visiting {name} ...")
            page = await ctx.new_page()
            try:
                await page.goto(url, timeout=60_000, wait_until="networkidle")
                await asyncio.sleep(5)  # let late JS render finish
                html = await page.content()
                out = f"html_{name}.html"
                with open(out, "w", encoding="utf-8") as f:
                    f.write(html)
                print(f"  Saved {len(html):,} bytes → {out}")
            except Exception as e:
                print(f"  ERROR: {e}")
            finally:
                await page.close()

        await ctx.close()
        await browser.close()
    print("\nDone. HTML files saved.")

asyncio.run(main())
