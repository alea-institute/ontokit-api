"""Prompt template package for LLM-based ontology suggestion generation.

Exports PROMPT_BUILDERS — a dispatch dict mapping SuggestionType string values
to build_messages functions. Each builder accepts (context: dict, batch_size: int)
and returns a list of {"role": str, "content": str} messages for LLMProvider.chat().

Usage:
    from ontokit.services.llm.prompts import PROMPT_BUILDERS

    msgs = PROMPT_BUILDERS["children"](context, batch_size=5)
    text, in_tokens, out_tokens = await provider.chat(msgs)
"""

from ontokit.services.llm.prompts import annotations as annotations_module
from ontokit.services.llm.prompts import children, edges, parents, siblings

PROMPT_BUILDERS: dict[str, object] = {
    "children": children.build_messages,
    "siblings": siblings.build_messages,
    "annotations": annotations_module.build_messages,
    "parents": parents.build_messages,
    "edges": edges.build_messages,
}

__all__ = [
    "PROMPT_BUILDERS",
    "children",
    "siblings",
    "annotations_module",
    "parents",
    "edges",
]
