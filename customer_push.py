"""
Customer push to QuickBooks: REST API and mapping.
- POST /push-from-site: accept Magento-style payload, queue for QB.
- POST /sync-from-oms: fetch all customers from OMS API, queue new ones for QB sync.
"""
import logging
import re
import uuid
from typing import Optional, List, Tuple

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _map_magento_item_to_qb_payload(item: dict) -> dict:
    """Map one Magento customers/search item to QB CustomerAdd payload."""
    first = (item.get("firstname") or "").strip()
    last = (item.get("lastname") or "").strip()
    email = (item.get("email") or "").strip()
    name = f"{first} {last}".strip() or email
    addr = None
    addrs = item.get("addresses") or []
    if addrs:
        addr = addrs[0] if isinstance(addrs[0], dict) else None
    company = ""
    state = ""
    if addr:
        company = (addr.get("company") or "").strip()
        reg = addr.get("region") or {}
        if isinstance(reg, dict):
            state = (reg.get("region_code") or reg.get("region") or "").strip()
    street = addr.get("street") if addr else []
    addr1 = (street[0] if isinstance(street, list) and street else street or "").strip() if street else ""
    return {
        "name": name,
        "company": company,
        "first_name": first,
        "last_name": last,
        "email": email,
        "phone": (addr.get("telephone") or "").strip() if addr else "",
        "addr1": addr1,
        "city": (addr.get("city") or "").strip() if addr else "",
        "state": state,
        "postal": (addr.get("postcode") or "").strip() if addr else "",
        "country": (addr.get("country_id") or "").strip() if addr else "",
    }


async def fetch_oms_customers(
    oms_base_url: str,
    oms_bearer_token: str,
    page_size: int,
    log_lines: List[str],
) -> Tuple[List[dict], List[str]]:
    """
    Fetch all customers from OMS REST (paginated). Each returned dict has QB payload + email.
    Appends progress/error lines to log_lines.
    """
    results: List[dict] = []
    url_base = (oms_base_url.rstrip("/") + "/rest/V1/customers/search") or ""
    params_base = {
        "searchCriteria[sortOrders][0][field]": "created_at",
        "searchCriteria[sortOrders][0][direction]": "DESC",
        "searchCriteria[pageSize]": page_size,
    }
    headers = {"Authorization": f"Bearer {oms_bearer_token}", "Content-Type": "application/json"}
    log_lines.append("Starting OMS customer fetch.")
    if not oms_bearer_token:
        log_lines.append("ERROR: OMS_BEARER_TOKEN is not set.")
        return results, log_lines
    current_page = 1
    total_count = None
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            params = {**params_base, "searchCriteria[currentPage]": current_page}
            try:
                resp = await client.get(url_base, params=params, headers=headers)
                log_lines.append(f"OMS API page {current_page}: status={resp.status_code}.")
                if resp.status_code != 200:
                    log_lines.append(f"OMS API error: status={resp.status_code} body={resp.text[:500]}")
                    logger.warning("OMS customer search failed: %s %s", resp.status_code, resp.text[:300])
                    break
                data = resp.json()
            except Exception as e:
                log_lines.append(f"Parse/request error: {e!s}")
                logger.exception("OMS fetch error")
                break
            items = data.get("items") or []
            if total_count is None:
                total_count = data.get("total_count")
                log_lines.append(f"OMS total_count={total_count}.")
            for it in items:
                if not it.get("email"):
                    continue
                try:
                    payload = _map_magento_item_to_qb_payload(it)
                    if payload.get("email"):
                        results.append(payload)
                except Exception as e:
                    log_lines.append(f"Skip map error for {it.get('email', '?')}: {e!s}")
                    logger.warning("Map Magento item failed: %s", e)
            log_lines.append(f"Fetched page {current_page}: items={len(items)} running_total={len(results)}.")
            if len(items) < page_size or (total_count is not None and len(results) >= total_count):
                break
            current_page += 1
    log_lines.append(f"OMS fetch done: total_fetched={len(results)}.")
    return results, log_lines


def get_customer_router(
    job_queue: list,
    transaction_map: dict,
    oms_base_url: str,
    oms_bearer_token: str,
):
    """
    Returns an APIRouter for customer push endpoints.
    job_queue: list to append new jobs.
    transaction_map: customer:email -> ListID for "already in QB" check.
    oms_base_url / oms_bearer_token: used by sync-from-oms.
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

    @router.post("/sync-from-oms")
    async def api_sync_customers_from_oms(
        page_size: int = 50,
    ):
        """
        Fetch all customers from OMS (Magento) API and queue new ones for QuickBooks sync.
        Returns summary and logs for process tracking.
        """
        log_lines: List[str] = []
        try:
            payloads, log_lines = await fetch_oms_customers(
                oms_base_url,
                oms_bearer_token,
                page_size=page_size,
                log_lines=log_lines,
            )
        except Exception as e:
            log_lines.append(f"Fetch failed: {e!s}")
            logger.exception("sync-from-oms fetch failed")
            raise HTTPException(status_code=502, detail=f"OMS fetch failed: {e!s}")
        queued = 0
        skipped_in_qb = 0
        skipped_queued = 0
        errors: List[str] = []
        for p in payloads:
            email = (p.get("email") or "").strip()
            if not email:
                continue
            key = f"customer:{email}"
            if transaction_map.get(key):
                skipped_in_qb += 1
                log_lines.append(f"Skipped (already in QB): {email}")
                continue
            in_queue = any(
                j.get("operation") == "push_customer"
                and j.get("k365_id") == email
                and j.get("status") in ("pending", "processing")
                for j in job_queue
            )
            if in_queue:
                skipped_queued += 1
                log_lines.append(f"Skipped (already queued): {email}")
                continue
            job_id = f"job_rest_{uuid.uuid4().hex[:8]}"
            job = {
                "id": job_id,
                "client_id": "qbuser",
                "operation": "push_customer",
                "priority": 3,
                "source": "customer_flow",
                "status": "pending",
                "k365_id": email,
                "linked_order": None,
                "retry_count": 0,
                "qb_id": None,
                "payload": p,
            }
            job_queue.append(job)
            queued += 1
            log_lines.append(f"Queued: {email}")
        logger.info(
            "sync-from-oms done: total_fetched=%s queued=%s skipped_qb=%s skipped_queued=%s",
            len(payloads), queued, skipped_in_qb, skipped_queued,
        )
        return {
            "total_fetched": len(payloads),
            "queued": queued,
            "skipped_in_qb": skipped_in_qb,
            "skipped_queued": skipped_queued,
            "errors": errors,
            "logs": log_lines,
        }

    return router
