# RCE OpenInvoice Bulk PDF Downloader

## What this is
Streamlit app for accounting team to bulk download OpenInvoice PDFs for audit purposes.
Deployed on RCEUTIL02 at http://rceutil02:8502 as Windows service RockcliffOI.

## Database
- Server: RCESQL02 (Windows auth)
- Database: foundation
- Key table: bronze_openinvoice.receipt
- Key columns: itemID, receiptNumber, status (APPROVED/SUBMITTED/DISPUTED/CANCELLED),
  submittedDatetime, serviceDateFrom, totalAmount, supplierParty__name,
  lineItems__afe__number, lineItems__costCenter__number,
  lineItems__major__code, lineItems__major__description,
  lineItems__minor__code, lineItems__minor__description,
  attachments__links, referencingInvoices__invoiceID

## OpenInvoice API
- Base: https://api.openinvoice.com
- Auth: mTLS (cert.pem + rockcliff.key) + HMAC header (mac)
- PDF endpoints:
  - GET /docp/supply-chain/v1/receipts/{itemID}/snapshot
  - GET /docp/supply-chain/v1/receipts/{itemID}/attachments/{attachmentId}
  - GET /docp/supply-chain/v1/invoices/{invoiceId}/snapshot

## Secrets (never commit)
- .streamlit/secrets.toml
- cert.pem
- rockcliff.key

## Deploy
git push origin main → auto-pulls within 5 min via RockcliffApps-AutoPull task
Force: git -C C:\dev\rockcliff-oi reset --hard origin/main
Restart: nssm restart RockcliffOI
