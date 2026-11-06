"""Step 5: turn report/report.md into report/report.html, and into report/report.pdf if Chrome/Chromium is
installed (headless print). The finished report.pdf is already included, so this step is optional.

Run from take_home_code/:  .venv/bin/python src/build_report.py
"""
import shutil
import subprocess
from pathlib import Path

import markdown

CHROME_CANDIDATES = ["google-chrome", "chromium", "chromium-browser", "chrome",
                     "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]


def find_chrome():
    """First Chrome/Chromium binary found on PATH or at the usual macOS location, else None."""
    for name in CHROME_CANDIDATES:
        path = shutil.which(name) or (name if Path(name).exists() else None)
        if path:
            return path
    return None


CSS = """
@page { size: Letter; margin: 0.55in 0.6in; }
body { font-family: -apple-system, Helvetica, Arial, sans-serif; font-size: 9.6pt; line-height: 1.34; color: #222; }
h1 { font-size: 16pt; margin: 0 0 2px; color: #1f4e79; }
h2 { font-size: 11pt; margin: 12px 0 4px; color: #1f4e79; border-bottom: 1px solid #d5d8dc; padding-bottom: 2px; }
p, li { margin: 3px 0; }
ul, ol { padding-left: 18px; margin: 3px 0; }
table { border-collapse: collapse; width: 100%; font-size: 8.8pt; margin: 6px 0; }
th, td { border-bottom: 1px solid #e5e7e9; padding: 2px 5px; text-align: left; vertical-align: top; }
th { background: #f4f6f7; }
img { display: block; margin: 4px auto; max-width: 92%; }
code { font-size: 8.5pt; background: #f4f6f7; padding: 0 2px; }
td code { font-size: 7pt; }
table:first-of-type td:nth-child(3) { white-space: nowrap; }  /* headline table: keep trade dates on one line */
table:first-of-type td:nth-child(1), table:first-of-type td:nth-child(4) { white-space: nowrap; }  /* supplier, listing */
h2.appendix { page-break-before: always; }
tr { break-inside: avoid; }  /* a table may split between rows, never inside one */
table:first-of-type + p { font-size: 8.2pt; color: #555; }  /* the headline table's notes read as a caption */
"""


def main():
    """Turn report/report.md into report.html, then print that to report.pdf with headless Chrome if found."""
    report = Path("report")
    body = markdown.markdown((report / "report.md").read_text(encoding="utf-8"), extensions=["tables"])
    html = report / "report.html"
    html.write_text(f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head>"
                    f"<body>{body}</body></html>", encoding="utf-8")
    chrome = find_chrome()
    if chrome is None:
        print("wrote report/report.html (no Chrome/Chromium found, so the PDF was not rebuilt)")
        return
    subprocess.run([chrome, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={report.resolve() / 'report.pdf'}", html.resolve().as_uri()],
                   check=True, capture_output=True)
    print("wrote report/report.html and report/report.pdf")


if __name__ == "__main__":
    main()
