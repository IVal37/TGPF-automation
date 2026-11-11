# imports from std lib
from __future__ import annotations

import base64
import logging
from typing import List

# imports from third party
from anthropic import Anthropic, APIError
from django.conf import settings
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_EXTRACTION_PROMPT = (
    "Extract every product line item from this invoice. For each product, fill in "
    "the fields needed to create it in webjoint: product_name, brand, product_type "
    "(e.g. Buds, Concentrate), lineage (Indica, Sativa, or Hybrid), strain, category "
    "(e.g. Flower, Cartridges, Concentrates, Prerolls), subcategory, seo_title, a short "
    "marketing description, sale_discount (e.g. '10%'), tags, for_sale ('Yes' or 'No'), "
    "weight_size (numeric), unit (e.g. 'g'), name_variant, and price (numeric)."
)


# what the dashboard accepts as an invoice
ALLOWED_CONTENT_TYPES = ("application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp")
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class InvoiceRejected(Exception):
    """The invoice cannot be used; the message says why and is shown to the dispatcher."""


def check_upload(content_type: str, size: int) -> None:
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise InvoiceRejected("The invoice must be a PDF or an image (PNG, JPEG, GIF or WebP).")
    if size > MAX_UPLOAD_BYTES:
        raise InvoiceRejected(f"The invoice is too large (the limit is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")


class Product(BaseModel):
    product_name: str
    brand: str
    product_type: str
    lineage: str
    strain: str
    category: str
    subcategory: str
    seo_title: str
    description: str
    sale_discount: str
    tags: str
    for_sale: str
    weight_size: float
    unit: str
    name_variant: str
    price: float


class _InvoiceProducts(BaseModel):
    products: List[Product]


def extract_products_from_invoice(file_bytes: bytes, media_type: str) -> list[dict]:
    client = Anthropic(api_key=settings.ANTHROPIC_API_KEY or None)
    block_type = "document" if media_type == "application/pdf" else "image"
    data = base64.standard_b64encode(file_bytes).decode("utf-8")

    try:
        response = client.messages.parse(
            model=settings.INVOICE_MODEL,
            max_tokens=16000,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": block_type,
                        "source": {"type": "base64", "media_type": media_type, "data": data},
                    },
                    {"type": "text", "text": _EXTRACTION_PROMPT},
                ],
            }],
            output_format=_InvoiceProducts,
        )
    except APIError as e:
        logger.error("invoice reading failed: %s: %s", type(e).__name__, e)
        raise InvoiceRejected(f"Could not read the invoice ({type(e).__name__}). Try again in a minute.") from e
    return [p.model_dump() for p in response.parsed_output.products]
