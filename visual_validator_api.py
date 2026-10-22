"""
Visual Validator API -- entry point kept for backwards compatibility.
===========================================================================

This file used to be a stand-alone copy of the Visual Validator (DOCX Viewer V2) endpoints, with
their own SharePoint-backed session handling and a hard-coded Windows path. Keeping a second copy
of that logic could only drift out of sync with the real one, so it is gone: every Visual
Validator endpoint now lives in pipeline_api.py and stores its data (validation sessions, PDFs,
Excel files, draft revisions) in the S3 bucket, like everything else.

Anything that still starts this module keeps working -- it serves the very same application:

    uvicorn visual_validator_api:app --host 0.0.0.0 --port 8000

The endpoints (all under /api):
    POST /search                                     PXML fuzzy search (click a paragraph -> locate it)
    POST /convert-to-pdf                             DOCX -> PDF with tracked-change markup
    POST /validation/create-session                  new validation session (files stored in S3)
    GET  /validation/sessions                        list sessions
    POST /validation/resume                          resume a session (any pod, any time)
    GET  /validation/session/{id}                    session progress record
    GET  /validation/session/{id}/file/{name}        session file (HTTP Range supported)
    POST /validation/session/{id}/upload-file        add a file to a session
    POST /validation/save-pxml                       save edited PXML
    POST /validation/update-revision-status          mark a revision complete / skipped
    GET  /draft-revisions, POST /draft-revisions/upload
"""
from pipeline_api import app  # noqa: F401  (re-exported for ASGI servers)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, workers=1)
