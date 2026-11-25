# Integration API

The system is built to run as a subsystem: another system sends the documents, takes the
recommendation, lets its own user approve, and stores the result. The web pages are optional.

- Base path: `/api/v1`. Machine-readable schema: `GET /api/openapi.json`.
- Authentication: header `X-API-Key`. One key per integrating system, set at start:
  `FEECALC_API_KEYS="billing:<24+ characters>,crm:<another key>"`. Without keys the API answers 503.
  The key name becomes the author of everything that system does (audit log), optionally followed
  by the `actor` you pass: `billing: A. Karimov`.
- Errors: `{"detail": "..."}` with 400 (bad input), 401 (key), 404, 409 (not possible in this state), 422.
- The service makes no outbound calls: poll for status.

## Flow

| Step | Call | Notes |
|---|---|---|
| 1 | `POST /jobs` (multipart: `project` 0..n PDFs, `conclusion` PDF, `title`, `external_id`, `actor`) | 202; repeating an `external_id` returns the same job |
| 2 | `GET /jobs/{id}` | `status`: queued, running (`requests_done` / `requests_total`), ready, failed (`error`, `hint`), finalized |
| 3 | `GET /jobs/{id}/recommendation` | volumes, coefficients, fee, `expertise` (the conclusion checked against the project), `open`, `assumptions`, `exemptions`, `norms`, `ready_to_approve` |
| 4 | `GET /jobs/{id}/values` | every value with status and source (file, page, snippet); page image and highlight: `/jobs/{id}/pages/{file}/{page}.png`, `.../{page}/rects?snippet=` |
| 5 | `PUT /jobs/{id}/volumes/{code}` with `{value, reason, actor}` | optional; `code` is Hb, Hyu, Ha, Ht or Hu; the reason is mandatory |
| 5a | `PUT /jobs/{id}/differences/{key}` with `{actor, note}` | every entry of `differences` in the recommendation (a figure the project and the conclusion state differently; the project is used) must be checked by a person before approval |
| 6 | `POST /jobs/{id}/approve` with `{kt?, ko?, kz?, kz_value?, kj?, k_council?, bhm?, contract_number?, contract_date?, actor?}` | omitted fields take the recommendation; 409 says what is still open (unchecked differences included); returns the finalized sheet |
| 7 | `GET /jobs/{id}/sheet`, `/annex.docx`, `/contract.docx` | after approval |

Reference data: `GET /reference`, `PUT /reference/central-streets` with `{settlement, streets[], basis, actor}`.
Health: `GET /health`.

```bash
KEY="X-API-Key: <key>"; BASE=http://127.0.0.1:8000/api/v1
curl -H "$KEY" -F "project=@album.pdf" -F "conclusion=@xulosa.pdf" -F "external_id=ORD-17" $BASE/jobs
curl -H "$KEY" $BASE/jobs/5
curl -H "$KEY" $BASE/jobs/5/recommendation
curl -H "$KEY" -H "Content-Type: application/json" -d '{"kj": "other", "actor": "A. Karimov"}' $BASE/jobs/5/approve
```
