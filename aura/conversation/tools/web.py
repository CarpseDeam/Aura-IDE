"""Web search and fetching tools using Tavily and BeautifulSoup."""
from __future__ import annotations

from typing import Any

import httpx
from bs4 import BeautifulSoup

from aura.config import get_tavily_api_key


def web_search(query: str, max_results: int = 5) -> dict[str, Any]:
    """Search the web using Tavily. Returns a list of result objects."""
    api_key = get_tavily_api_key()
    if not api_key:
        return {
            "ok": False, 
            "error": "Tavily API key not found. Set the TAVILY_API_KEY environment variable."
        }

    try:
        url = "https://api.tavily.com/search"
        payload = {
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False
        }
        
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            
        results = data.get("results", [])
        return {"ok": True, "results": results}
    except Exception as exc:
        return {"ok": False, "error": f"Search failed: {exc}"}


def web_fetch(url: str) -> dict[str, Any]:
    """Fetch and scrape the text content of a URL."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        with httpx.Client(follow_redirects=True, timeout=15.0, headers=headers) as client:
            resp = client.get(url)
            resp.raise_for_status()
            
        soup = BeautifulSoup(resp.text, "html.parser")
        
        # Remove script and style elements
        for script_or_style in soup(["script", "style", "header", "footer", "nav"]):
            script_or_style.decompose()
            
        # Get text, clean up whitespace
        text = soup.get_text(separator="\n")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        clean_text = "\n".join(lines)
        
        # Cap to avoid context overflow (approx 10k chars)
        if len(clean_text) > 10000:
            clean_text = clean_text[:10000] + "... [truncated]"
            
        return {
            "ok": True, 
            "url": url, 
            "title": soup.title.string if soup.title else url,
            "content": clean_text
        }
    except Exception as exc:
        return {"ok": False, "error": f"Fetch failed: {exc}"}
