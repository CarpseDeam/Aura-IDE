"""Web-search execution support.

Search runs when the model calls the ``web_search`` tool. There is no request
classifier here deciding whether research is needed — that machinery is gone.
"""

from aura.research.adapter import WEB_RESEARCH_DRONE_ID, ResearchAdapterCall
from aura.research.native import execute_native_web_search
from aura.research.result import ResearchResult, format_research_answer

__all__ = [
    "WEB_RESEARCH_DRONE_ID",
    "ResearchAdapterCall",
    "ResearchResult",
    "execute_native_web_search",
    "format_research_answer",
]
