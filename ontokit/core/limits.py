"""Shared resource ceilings for ontology payloads and anonymous sessions."""

# Imported ontologies are capped at 50 MiB. Editor saves carry the same full
# Turtle document, so they use the same ceiling rather than inventing a second
# incompatible document-size contract.
MAX_TURTLE_PAYLOAD_BYTES = 50 * 1024 * 1024

# Anonymous sessions are intentionally finite even when individual documents
# are small. These caps bound Git history growth and cumulative request work.
MAX_ANONYMOUS_SESSION_COMMITS = 100
MAX_ANONYMOUS_SESSION_BYTES = 250 * 1024 * 1024
