"""Gloss extraction service — stub pending OpenGloss availability.

TOOL-02 requires OpenGloss for definition/gloss extraction from reference texts.
OpenGloss does not exist as a PyPI package or ALEA GitHub repository as of 2026-04-06.
This stub will be replaced when the package becomes available.
"""


class GlossExtractionService:
    """Extract definitions/glosses from reference texts. STUB — awaiting OpenGloss."""

    def extract_glosses(self, text: str, entity_label: str) -> list[dict]:
        """Extract glosses for entity_label from reference text.

        Returns list of dicts with keys: gloss, source, confidence.

        Raises NotImplementedError until OpenGloss becomes available.
        """
        raise NotImplementedError(
            "GlossExtractionService requires OpenGloss (TOOL-02), "
            "which is not yet available as a Python package. "
            "See .planning/phases/12-*/12-RESEARCH.md Open Questions #1."
        )
