"""Tests for aura.research.extract.

This fixture derives from Playwright MCP's published report format,
which predates this parser — it is a guard, not a mirror.
"""

from __future__ import annotations

from aura.research.extract import ParsedPage, parse_call_result, parse_report

SAMPLE_REPORT = """\
### Result
Some result summary text here.

### Ran Playwright code
```javascript
async function run() {
  console.log("should not appear");
  await page.goto("https://example.com/research");
  const title = await page.title();
  console.log("Page title:", title);
  return title;
}
```

### Page state
URL: https://example.com/research
Title: Research Test Page

- link "External Link" [ref=e1]
/url: https://external.org/doc
- link "No URL Link" [ref=e2]
- heading "Section Title" [ref=e3]
- paragraph "Some descriptive text" [ref=e4]
- link "Another Link" [ref=e5]
/url: https://example.org/page
"""


class TestParseReport:
    """Verify parse_report extracts URL, title, links, and clean text."""

    def test_url_and_title(self) -> None:
        result = parse_report(SAMPLE_REPORT)
        assert result.url == "https://example.com/research"
        assert result.title == "Research Test Page"

    def test_real_href_link(self) -> None:
        result = parse_report(SAMPLE_REPORT)
        assert ("External Link", "https://external.org/doc") in result.links

    def test_no_url_link(self) -> None:
        result = parse_report(SAMPLE_REPORT)
        assert ("No URL Link", "") in result.links

    def test_js_code_absent(self) -> None:
        result = parse_report(SAMPLE_REPORT)
        assert "should not appear" not in result.clean_text
        assert "console.log" not in result.clean_text

    def test_clean_text_includes_content(self) -> None:
        result = parse_report(SAMPLE_REPORT)
        assert "Section Title" in result.clean_text
        assert "Some descriptive text" in result.clean_text

    def test_error_result(self) -> None:
        result = parse_call_result({"ok": False, "error": "boom", "content": []})
        assert result.clean_text.startswith("MCP error:")
        assert "boom" in result.clean_text
