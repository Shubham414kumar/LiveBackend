"""Superseded. This file is intentionally empty of tests.

The legacy tests here imported ``server.py`` (now a deprecation shim, and
importing it is fatal under ``filterwarnings = ["error", ...]``) and replaced the
whole ``google`` package with a ``MagicMock`` in ``sys.modules`` — a fake package
left installed process-wide for the rest of the session, so any module that later
imported ``google.*`` silently got a mock.

The AI routes need coverage of two paths, and the legacy file exercised neither
honestly: the Gemini-backed path, and the deterministic fallback used when
``GEMINI_API_KEY`` is unset (``settings.has_gemini`` is false). The fallback is
what production serves by default, so it is the more important of the two. Both
belong in ``test_ai.py``, with Gemini stubbed at the transport layer via the
``upstream`` fixture so the real request, retry and error-mapping code runs.

This file is a tombstone only because the environment this rewrite was performed
in cannot delete files. Remove it:

    git rm backend/tests/test_ai_endpoints.py
"""
