# imports from std lib
from __future__ import annotations

import base64
import logging
from typing import List

# imports from third party
from anthropic import Anthropic
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_MODEL = "claude-opus-4-8"

_EXTRACTION_PROMPT = (
    "Extract every product line item from this invoice. For each product, fill in "
    "the fields needed to create it in webjoint: product_name, brand, product_type "
    "(e.g. Buds, Concentrate), lineage (Indica, Sativa, or Hybrid), strain, category "
    "(e.g. Flower, Cartridges, Concentrates, Prerolls), subcategory, seo_title, a short "
    "marketing description, sale_discount (e.g. '10%'), tags, for_sale ('Yes' or 'No'), "
    "weight_size (numeric), unit (e.g. 'g'), name_variant, and price (numeric)."
)


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
    client = Anthropic()
    block_type = "document" if media_type == "application/pdf" else "image"
    data = base64.standard_b64encode(file_bytes).decode("utf-8")

    response = client.messages.parse(
        model=_MODEL,
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
    return [p.model_dump() for p in response.parsed_output.products]
