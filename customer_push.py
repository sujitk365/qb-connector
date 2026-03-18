"""
Customer push to QuickBooks: REST API and mapping.
Accepts same payload as Postman (Magento-style customer + addresses).
Mount in main.py so POST /api/customers/push-from-site works with validation.
"""
import re
import uuid
from typing import Optional, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def get_customer_router(job_queue: list, transaction_map: dict):
    """
    Returns an APIRouter for customer push endpoints.
    job_queue: list to append new jobs.
    transaction_map: used to detect "already in QB" (customer:email -> ListID).
    """
    router = APIRouter(prefix="/api/customers", tags=["customers"])

    # ── QB-style payload (direct) ───────────────────────────────────────────
    class CustomerPushPayload(BaseModel):
        name: str
        company: Optional[str] = ""
        first_name: Optional[str] = ""
        last_name: Optional[str] = ""
        email: str
        phone: Optional[str] = ""
        addr1: Optional[str] = ""
        city: Optional[str] = ""
        state: Optional[str] = ""
        postal: Optional[str] = ""
        country: Optional[str] = ""

    class CustomerPushRequest(BaseModel):
        client_id: Optional[str] = "qbuser"
        k365_id: Optional[str] = None
        customer: CustomerPushPayload

    # ── API site payload (Magento/Kitchen365 style) ─────────────────────────
    class RegionInAddress(BaseModel):
        region_code: Optional[str] = None
        region_id: Optional[int] = None
        region: Optional[str] = None

    class AddressInCustomer(BaseModel):
        default_billing: Optional[bool] = False
        default_shipping: Optional[bool] = False
        firstname: Optional[str] = ""
        company: Optional[str] = ""
        lastname: Optional[str] = ""
        country_id: Optional[str] = ""
        postcode: Optional[str] = ""
        city: Optional[str] = ""
        street: Optional[List[str]] = []
        telephone: Optional[str] = ""
        region: Optional[RegionInAddress] = None

    class CustomerFromSite(BaseModel):
        firstname: Optional[str] = ""
        lastname: Optional[str] = ""
        email: str
        store_id: Optional[int] = None
        website_id: Optional[int] = None
        addresses: Optional[List[AddressInCustomer]] = []

    class CompanyDetails(BaseModel):
        has_showroom: Optional[str] = None
        company_name: Optional[str] = ""
        dedicated_designer: Optional[int] = None
        comment: Optional[str] = None
        enroll: Optional[int] = None
        region_code: Optional[str] = None
        country_id: Optional[str] = None
        business_type: Optional[int] = None

    class CustomerPushFromSiteRequest(BaseModel):
        customer: CustomerFromSite
        company_details: Optional[CompanyDetails] = None
        password: Optional[str] = None

    def map_site_customer_to_qb_payload(body: CustomerPushFromSiteRequest) -> dict:
        c = body.customer
        addr = (c.addresses or [None])[0] if (c.addresses and len(c.addresses) > 0) else None
        comp = body.company_details or CompanyDetails()
        first = (c.firstname or "").strip()
        last = (c.lastname or "").strip()
        name = f"{first} {last}".strip() or c.email
        company = ""
        if addr and (addr.company or "").strip():
            company = (addr.company or "").strip()
        elif (comp.company_name or "").strip():
            company = (comp.company_name or "").strip()
        state = ""
        if addr and addr.region:
            state = (addr.region.region_code or addr.region.region or "").strip()
        elif (comp.region_code or "").strip():
            state = (comp.region_code or "").strip()
        return {
            "name": name,
            "company": company,
            "first_name": first,
            "last_name": last,
            "email": (c.email or "").strip(),
            "phone": (addr.telephone or "").strip() if addr else "",
            "addr1": (addr.street[0] if addr and addr.street else "").strip(),
            "city": (addr.city or "").strip() if addr else "",
            "state": state,
            "postal": (addr.postcode or "").strip() if addr else "",
            "country": (addr.country_id or comp.country_id or "").strip() if addr or comp.country_id else "",
        }

    def _validate_and_check_duplicate(email: str) -> None:
        """Validate email and raise HTTPException if duplicate (queued or already in QB)."""
        email = (email or "").strip()
        if not email:
            raise HTTPException(status_code=400, detail="Email is required.")
        if not EMAIL_RE.match(email):
            raise HTTPException(status_code=400, detail="Invalid email address format.")
        key = f"customer:{email}"
        if transaction_map.get(key):
            raise HTTPException(
                status_code=409,
                detail="A customer with the same email address already exists in QuickBooks.",
            )
        for j in job_queue:
            if (
                j.get("operation") == "push_customer"
                and j.get("k365_id") == email
                and j.get("status") in ("pending", "processing")
            ):
                raise HTTPException(
                    status_code=409,
                    detail="A customer with the same email address is already queued for sync.",
                )

    @router.post("/push-from-site")
    async def api_customer_push_from_site(req: CustomerPushFromSiteRequest):
        """
        Accept same Postman payload (Magento-style: customer + addresses).
        Validates email and duplicates; queues job for QB Web Connector.
        """
        email = (req.customer.email or "").strip()
        _validate_and_check_duplicate(email)
        payload = map_site_customer_to_qb_payload(req)
        k365_id = email
        job_id = f"job_rest_{uuid.uuid4().hex[:8]}"
        job = {
            "id": job_id,
            "client_id": "qbuser",
            "operation": "push_customer",
            "priority": 3,
            "source": "customer_flow",
            "status": "pending",
            "k365_id": k365_id,
            "linked_order": None,
            "retry_count": 0,
            "qb_id": None,
            "payload": payload,
        }
        job_queue.append(job)
        return {
            "job_id": job_id,
            "k365_id": k365_id,
            "status": "pending",
            "message": "Customer queued for QuickBooks. Run QB Web Connector to sync.",
        }

    @router.post("/push")
    async def api_customer_push(req: CustomerPushRequest):
        """Submit customer in QB-style JSON; queue job (customer_flow)."""
        payload = req.customer.model_dump()
        k365_id = req.k365_id or f"cust_rest_{uuid.uuid4().hex[:12]}"
        job_id = f"job_rest_{uuid.uuid4().hex[:8]}"
        job = {
            "id": job_id,
            "client_id": req.client_id,
            "operation": "push_customer",
            "priority": 3,
            "source": "customer_flow",
            "status": "pending",
            "k365_id": k365_id,
            "linked_order": None,
            "retry_count": 0,
            "qb_id": None,
            "payload": payload,
        }
        job_queue.append(job)
        return {
            "job_id": job_id,
            "k365_id": k365_id,
            "status": "pending",
            "message": "Customer queued for QuickBooks. Run QB Web Connector to sync.",
        }

    return router
