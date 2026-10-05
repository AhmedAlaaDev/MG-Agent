"""FastAPI application factory and ASGI app."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routers import audit, dynamics, extract, health, invoices, tariffs, ui, vendor_invoices

app = FastAPI(
    title="Professional PDF + OCR + Excel Bill of Lading Extractor",
    version="4.0.0",
    description="Extracts native/OCR PDF text or Excel sheet data, then extracts structured B/L JSON.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ui.router)
app.include_router(health.router)
app.include_router(audit.router)
app.include_router(extract.router)
app.include_router(dynamics.router)
app.include_router(invoices.router)
app.include_router(vendor_invoices.router)
app.include_router(tariffs.router)
