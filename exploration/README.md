# Exploration scripts (not part of the pipeline)

Used on day one to test what the API returns before choosing an approach. Kept as a record; the pipeline in
`src/` does not depend on them, and they are not maintained.

- `check_connection.py`: first smoke test with Sayari's Python SDK. Needs `pip install sayari` (the SDK is not in
  `requirements.txt`, because the pipeline calls the REST API directly; see `src/sayari_api.py`).
- `probe_helpers.py`, `probe_deep.py`: ran every read-only endpoint on 10 sample companies across all three lists.
  `probe_helpers.py` is an early copy of the client that became `src/sayari_api.py`.

Findings are summarized in the main README under "What I learned about the API".
