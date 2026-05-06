import json
import logging
from ddgs import DDGS
import httpx
from bs4 import BeautifulSoup

def web_search(query: str, max_results: int = 5) -> dict:
    """Search the web for a query and return structured results."""
    try:
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append(r)
        return {"ok": True, "results": results}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def web_fetch(url: str) -> dict:
    """Fetch a URL and extract readable text."""
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
        
        soup = BeautifulSoup(resp.content, "html.parser")
        for script in soup(["script", "style", "nav", "footer", "header"]):
            script.extract()
            
        text = soup.get_text(separator="\n")
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        text = "\n".join(chunk for chunk in chunks if chunk)
        
        if len(text) > 20000:
            text = text[:20000] + "\n...[truncated]"
            
        return {"ok": True, "content": text, "url": url}
    except Exception as e:
        return {"ok": False, "error": str(e)}
