# Third-Party Notices

**Date:** 2026-07-07

This document lists the third-party components bundled or depended upon by
**ontokit-api** and their open-source licenses. It is provided for
attribution and license-compliance purposes.

`ontokit-api` itself is licensed under the **MIT License** (see `LICENSE`).

The components below are the **direct runtime dependencies** declared in
`pyproject.toml`, with versions resolved from the local virtual environment
(`.venv`, Python 3.13) and `uv.lock`. Licenses were read from each package's
installed distribution metadata (`*.dist-info/METADATA` — `License` /
`License-Expression` / `Classifier: License ::` fields), not inferred.

**Total direct runtime dependencies: 24**

License breakdown: MIT (10), BSD family (7), Apache-2.0 (4), LGPL-3.0 (2),
GPL-2.0-with-linking-exception (1).

---

## MIT License (10)

| Package | Version | License | Purpose |
|---|---|---|---|
| fastapi | 0.138.1 | MIT | ASGI web framework — the HTTP API layer |
| pydantic | 2.13.4 | MIT | Data validation and settings models |
| pydantic-settings | 2.14.2 | MIT | Environment/config loading for Pydantic |
| SQLAlchemy | 2.0.51 | MIT | SQL ORM / database toolkit |
| alembic | 1.18.5 | MIT | Database schema migrations |
| redis | 5.3.1 | MIT | Redis client (cache / queue backend) |
| arq | 0.28.0 | MIT | Async task queue on Redis |
| PyJWT | 2.13.0 | MIT | JSON Web Token encode/decode for auth |
| slowapi | 0.1.10 | MIT | Rate limiting for FastAPI/Starlette |
| pgvector | 0.4.2 | MIT | pgvector type support for embeddings in Postgres |

## BSD Family (7)

| Package | Version | License | Purpose |
|---|---|---|---|
| uvicorn | 0.49.0 | BSD-3-Clause | ASGI server (with `[standard]` extras) |
| rdflib | 7.6.0 | BSD-3-Clause | RDF graph parsing/serialization |
| httpx | 0.28.1 | BSD-3-Clause | Async HTTP client |
| GitPython | 3.1.50 | BSD-3-Clause | Git repository access from Python |
| websockets | 16.0 | BSD-3-Clause | WebSocket protocol implementation |
| numpy | 2.4.6 | BSD-3-Clause | Numeric arrays for embeddings/vectors |
| passlib | 1.7.4 | BSD-2-Clause | Password hashing (with `[bcrypt]` extra) |

> `numpy`'s full SPDX expression is `BSD-3-Clause AND 0BSD AND MIT AND Zlib
> AND CC0-1.0` (bundled vendored components); the primary license is
> BSD-3-Clause. All constituents are permissive.

## Apache License 2.0 (4)

| Package | Version | License | Purpose |
|---|---|---|---|
| asyncpg | 0.31.0 | Apache-2.0 | Async PostgreSQL driver |
| python-multipart | 0.0.32 | Apache-2.0 | Multipart/form-data parsing (file uploads) |
| minio | 7.2.20 | Apache-2.0 | S3/MinIO object-storage client |
| sentence-transformers | 5.6.0 | Apache-2.0 | Text embedding models |

---

## Copyleft / Attention (3)

The following dependencies carry **copyleft** licenses. None are AGPL. All
three are weak/library copyleft (LGPL, or GPL with a linking exception) and are
consumed as unmodified, dynamically-linked/imported libraries — the standard
LGPL/linking-exception usage that does not impose copyleft on `ontokit-api`'s
own MIT-licensed source. If any of these were to be **modified and
redistributed**, their respective source-availability terms would apply.

| Package | Version | License | Purpose | Note |
|---|---|---|---|---|
| owlready2 | 0.51 | **LGPL-3.0-or-later** | OWL 2.0 ontology load/reason/manipulate | Weak copyleft; used as an imported library |
| PyGithub | 2.9.1 | **LGPL-3.0** | GitHub REST API client | Weak copyleft; used as an imported library |
| pygit2 | 1.19.3 | **GPL-2.0 WITH linking exception** | libgit2 bindings for Git operations | GPLv2 *with* a linking exception permitting use in non-GPL software |

**AGPL check: none found.** No direct dependency is licensed under AGPL. (An
`AGPL`/`Affero` string appears only inside the verbatim LGPL license text
bundled in the transitive dependency `scipy`'s metadata — `scipy` itself is
BSD-3-Clause; it is a false positive, not an AGPL dependency.)

---

## FOLIO Ontology Data (CC-BY-4.0)

No FOLIO ontology data is **vendored** in this repository — there are no
`.ttl`, `.owl`, or `.rdf` data files checked in. FOLIO is instead consumed at
runtime via a separate companion service (`folio-api`, referenced as a sibling
repository), not embedded here. If FOLIO data is later vendored or
redistributed, note that FOLIO is published under **CC-BY-4.0** and requires
attribution to the Open Legal Standard / SALI Alliance.

---

## Note on Transitive Dependencies

This document enumerates **direct** runtime dependencies only. Transitive
dependencies pulled in through the packages above inherit
compatible **permissive** licenses (predominantly MIT, BSD, Apache-2.0,
PSF/Python-2.0) unless explicitly listed in the *Copyleft / Attention* section
above. The full resolved dependency tree is recorded in `uv.lock`.
