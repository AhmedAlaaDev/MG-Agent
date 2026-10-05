# Dynamics 365 / Dataverse

The extractor writes MESCO operations into Dataverse using OData v9.2.

## Authentication

`app.infrastructure.dataverse.client_service.DataverseClientService` is a process-wide singleton.

- OAuth2 client credentials against `https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token`
- Scope: `{BASE_URL}/.default`
- API root: `AZURE_APP_API_URL` or `{BASE_URL}/api/data/v9.2`
- Retries on network errors, 5xx, and 429 (not on other 4xx)
- A failed constructor does **not** cache a broken singleton (serverless-safe)

Env loading: `env_service.EnvService` (`python-dotenv`). Errors are normalized by `error_service.DataverseErrorService`.

## Entities

| OData set | Logical name | Role |
| --------- | ------------ | ---- |
| `mesco_operations` | `mesco_operation` | Master or house B/L |
| `mesco_containers` | `mesco_container` | Equipment |
| `mesco_cargos` | `mesco_cargo` | Goods lines |
| `xollsp_quotecostlines` | (invoice) | Vendor cost lines |

House operations link to the master via `mesco_Operation@odata.bind` / `_mesco_operation_value`. Cargo and containers bind to master and/or house depending on B/L type.

`mesco_bltype`: Direct `886150000`, Master `886150001`, House `886150002`.

## Upload path

`app.infrastructure.dataverse.uploader.upload_crm_json`:

1. `custom_business_rules.prepare_crm_payload_for_upload`
2. `_preprocess_payload` — drop invalid columns, coerce numbers/dates, build `@odata.bind` lookups
3. Option-set labels → integers via `metadata.resolve_option_value` (live EntityDefinitions, then `dataverse_optionsets_cache.json`, then bundled defaults)
4. String caps from `field_limits.py`
5. Dedup existing master/house by BL number + type (+ parent for houses)
6. Create or patch operations, containers, cargo
7. Link houses to master; inherit shipping line / ATA POD where configured
8. Sync operation totals from cargo when needed

Dedup helpers and cleanup utilities: `dedup_cleanup.py`. Schema field lists can be refreshed from generated TypeScript in the MG Operation / OperationBackend repos (`schema_sync.py`).

## Lookups

Unbounded lists (accounts, ports, countries, units, vendors, logistic services) are **not** cached as option sets. The uploader searches Dataverse per value (`_resolve_lookup`, `_lookup_search_variants`). MESCO account matching has dedicated variants so "Marine and Engineering Services" resolves consistently.

## Invoices

`app.application.invoice_dataverse_mapper` builds a mapping plan: vendor, currency, transport type, load type, import/export, logistic service, container type. `app.api.routers.invoices` then:

- Resolves master/house by MBL and HBL
- Reuses or creates a container on the operation
- Ensures house cargo exists for measurements
- Posts cost lines idempotently (signature = vendor invoice + service + amounts)
- Can skip LLM when `reviewed_json` is supplied

WE-CAN `.xls` invoices are parsed by `wecan_proxy_bill_extractor` (FINAL DN reconciliation, HBL groups, local agreement lines excluded from posted cost names).

## Read-back and compare

The dynamics router fetches expanded operations for the `/operation` HTML page (keeps OData formatted values) and can compare a sent payload to what Dataverse stored (`POST /dynamics/compare`).

## Audit

Successful and failed B/L uploads are stored under `upload_audit/` (or `BL_AUDIT_DIR` / `/tmp/bl_upload_audit`). SQLite holds metadata and a truncated response; original files go in `upload_audit/files/`. The dashboard is `GET /audit`.

## Credentials

Never commit `.env`. Required for upload:

```
TENANT_ID=
CLIENT_ID=
CLIENT_SECRET=
BASE_URL=https://your-org.crm4.dynamics.com
AZURE_APP_API_URL=https://your-org.crm4.dynamics.com/api/data/v9.2
```

A Postman collection is in `docs/postman/Dynamics.postman_collection.json`. TypeScript mirrors of the client live in `integrations/typescript/` for the Node backends; Python is the source of truth for this service.
