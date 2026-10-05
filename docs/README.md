# Documentation index

This folder describes the Intelligent Bill of Lading Extractor: a FastAPI service that reads shipping documents, extracts structured B/L and invoice data, and optionally writes Microsoft Dynamics 365 Dataverse records.

| Document | Contents |
| -------- | -------- |
| [Architecture](architecture.md) | Layered package layout, dependency rule, module map |
| [API](api.md) | HTTP endpoints, query/form fields, response shapes |
| [Extraction pipeline](extraction-pipeline.md) | PDF/Excel/OCR → parsers → Gemini → validation → CRM JSON |
| [Dataverse](dataverse.md) | Auth, entities, lookups, option sets, upload/dedup, invoices |
| [Configuration](configuration.md) | Environment variables and runtime toggles |
| [Development](development.md) | Local setup, tests, Docker, adding a parser |
| [Postman](postman/Dynamics.postman_collection.json) | Dynamics API collection |
| [Legacy requirements](legacy/requirements_pdf_excel_extractor.txt) | Older unpinned dependency list (not used by Docker) |

Start with architecture if you are changing code. Start with the API guide if you are calling the service. Start with configuration if you are deploying it.
