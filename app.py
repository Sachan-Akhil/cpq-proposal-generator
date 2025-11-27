import os
import time
from io import BytesIO
import base64
import tempfile

import requests
from flask import Flask, request, jsonify, Response
from fpdf import FPDF
from openai import OpenAI
from requests.auth import HTTPBasicAuth

app = Flask(__name__)
client = OpenAI()

# Credentials for external API Basic Auth (Oracle CPQ)
ORACLE_CPQ_USERNAME = os.getenv("ORACLE_CPQ_USERNAME")
ORACLE_CPQ_PASSWORD = os.getenv("ORACLE_CPQ_PASSWORD")

# Credentials for this service Basic Auth
SERVICE_AUTH_USERNAME = os.getenv("API_AUTH_USERNAME")
SERVICE_AUTH_PASSWORD = os.getenv("API_AUTH_PASSWORD")

HEADERS = {
    "Accept": "application/json"
}


def check_auth():
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Basic "):
        return False
    try:
        encoded = auth.split(" ", 1)[1]
        decoded = base64.b64decode(encoded).decode('utf-8')
        username, password = decoded.split(":", 1)
    except Exception:
        return False
    return username == SERVICE_AUTH_USERNAME and password == SERVICE_AUTH_PASSWORD


def require_basic_auth(f):
    def decorated(*args, **kwargs):
        if not check_auth():
            return Response(
                "Unauthorized", 401,
                {"WWW-Authenticate": 'Basic realm="Login Required"'})
        return f(*args, **kwargs)
    decorated.__name__ = f.__name__
    return decorated


def fetch_transaction(base_url, process_var_name, transaction_id):
    api_base = f"https://{base_url}/rest/v19/commerceDocuments{process_var_name}Transaction"
    url = f"{api_base}/{transaction_id}"
    resp = requests.get(url, headers=HEADERS,
                        auth=HTTPBasicAuth(ORACLE_CPQ_USERNAME, ORACLE_CPQ_PASSWORD))
    resp.raise_for_status()
    return resp.json()


def fetch_transaction_lines(base_url, process_var_name, transaction_id):
    api_base = f"https://{base_url}/rest/v19/commerceDocuments{process_var_name}Transaction"
    url = f"{api_base}/{transaction_id}/transactionLine"
    resp = requests.get(url, headers=HEADERS,
                        auth=HTTPBasicAuth(ORACLE_CPQ_USERNAME, ORACLE_CPQ_PASSWORD))
    resp.raise_for_status()
    return resp.json()


def create_pdf(text, logo_path=None):
    pdf = FPDF()
    pdf.add_page()
    if logo_path:
        pdf.image(logo_path, x=10, y=8, w=33)
        pdf.ln(30)
    pdf.set_font("Arial", size=12)
    for line in text.split("\n"):
        pdf.multi_cell(0, 10, line)

    # Write PDF to a temp file then read bytes
    with tempfile.NamedTemporaryFile(delete=True) as tmpfile:
        pdf.output(tmpfile.name)
        tmpfile.seek(0)
        pdf_bytes = BytesIO(tmpfile.read())
    pdf_bytes.seek(0)
    return pdf_bytes


def generate_proposal_with_retry(prompt_text, max_retries=3, backoff=2):
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {"role": "system", "content": "You are a helpful sales assistant who writes professional and detailed sales proposals."},
                    {"role": "user", "content": prompt_text}
                ],
                max_tokens=700,
                temperature=0.5,
            )
            return response.choices[0].message.content
        except Exception as e:
            if hasattr(e, "status_code") and e.status_code == 429:
                if attempt < max_retries - 1:
                    time.sleep(backoff ** attempt)
                else:
                    return jsonify({"error": "OpenAI API rate limit exceeded. Please try again later."}), 429
            else:
                return jsonify({"error": f"OpenAI API error: {str(e)}"}), 500


def extract_string(field_value):
    if isinstance(field_value, str):
        return field_value.strip()
    elif isinstance(field_value, dict):
        for key in ["displayValue", "value", "name"]:
            if key in field_value and isinstance(field_value[key], str):
                return field_value[key].strip()
        return str(field_value)
    elif field_value is None:
        return ""
    else:
        return str(field_value)


def compose_prompt(transaction, transaction_lines):
    customer_name = extract_string(transaction.get("_customer_t_company_name", "Unknown Customer"))
    customer_contact_name = " ".join(filter(None, [
        extract_string(transaction.get("_customer_t_first_name", "")),
        extract_string(transaction.get("_customer_t_last_name", ""))
    ])).strip()

    customer_address_parts = []
    for field in ["_customer_t_address", "_customer_t_city", "_customer_t_state", "_customer_t_zip", "_customer_t_country"]:
        value = extract_string(transaction.get(field, ""))
        if value:
            customer_address_parts.append(value)
    customer_address = ", ".join(customer_address_parts)

    contact_email = extract_string(transaction.get("_customer_t_email", "N/A"))
    contact_phone = extract_string(transaction.get("_customer_t_phone", "N/A"))

    transaction_name = extract_string(transaction.get("transactionName_t", "N/A"))
    total_value = extract_string(transaction.get("totalContractValue_t", "N/A"))
    currency = extract_string(transaction.get("currency_t", "USD"))
    payment_terms = extract_string(transaction.get("paymentTerms_t", "N/A"))
    owner = extract_string(transaction.get("owner_t", "Sales Team"))
    sales_email = extract_string(transaction.get("_owner_email_t", "sales@example.com"))
    sales_phone = extract_string(transaction.get("_owner_phone_t", "N/A"))

    proposal_date = time.strftime("%B %d, %Y")

    lines_text = ""
    for i, line in enumerate(transaction_lines.get("items", []), start=1):
        part_number = extract_string(line.get("_part_number", "N/A"))
        desc = extract_string(line.get("_part_desc")) or extract_string(line.get("displayedItemName_l")) or "N/A"

        qty_raw = line.get("requestedQuantity_l", 1)
        try:
            qty = float(qty_raw)
        except (ValueError, TypeError):
            qty = 1

        price_unit = line.get("_price_unit_price_each", {}).get("value", 0)
        currency_local = line.get("_price_unit_price_each", {}).get("currency", currency)

        lead_time = extract_string(line.get("_part_lead_time", "N/A"))
        shipping_date = extract_string(line.get("oRCL_ERP_RequestShipDate_l", "N/A"))

        line_total = qty * price_unit

        lines_text += (
            f"{i}. Product: {desc} (Part #: {part_number})\n"
            f"   Quantity: {qty}\n"
            f"   Unit Price: {price_unit:.2f} {currency_local}\n"
            f"   Line Total: {line_total:.2f} {currency_local}\n"
            f"   Lead Time: {lead_time}\n"
            f"   Estimated Shipping Date: {shipping_date}\n\n"
        )

    prompt = (
        f"Generate a detailed and professional sales proposal document.\n"
        f"Proposal Date: {proposal_date}\n"
        f"Prepared by: {owner}\n"
        f"Sales Representative Contact:\n"
        f"Email: {sales_email}\n"
        f"Phone: {sales_phone}\n\n"
        f"Customer Information:\n"
        f"Name: {customer_name}\n"
        f"Contact Person: {customer_contact_name}\n"
        f"Address: {customer_address}\n"
        f"Email: {contact_email}\n"
        f"Phone: {contact_phone}\n\n"
        f"Transaction Details:\n"
        f"Transaction Name: {transaction_name}\n"
        f"Total Contract Value: {total_value} {currency}\n"
        f"Payment Terms: {payment_terms}\n\n"
        f"Line Items:\n{lines_text}\n"
        f"Please include these sections:\n"
        f"1. Introduction with appreciation.\n"
        f"2. Summary of offered products and services.\n"
        f"3. Pricing and payment terms.\n"
        f"4. Delivery expectations.\n"
        f"5. Terms and conditions.\n"
        f"6. Next steps and contact info.\n"
        f"Use a professional and persuasive tone suitable for a business proposal."
    )
    return prompt


def upload_pdf_to_fileio(pdf_bytes):
    pdf_bytes.seek(0)
    files = {
        'file': ('proposal.pdf', pdf_bytes, 'application/pdf'),
    }
    resp = requests.post("https://file.io/?expires=15m", files=files)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise Exception("file.io upload failed")
    return data.get("link")


@app.route('/generate_proposal_document', methods=['POST'])
@require_basic_auth
def generate_proposal_document():
    if not request.is_json:
        return jsonify({"error": "Content-Type must be application/json"}), 415

    data = request.get_json()
    required_fields = ["transaction_id", "base_url", "process_var_name"]
    if not data or not all(field in data for field in required_fields):
        return jsonify({"error": f"Missing one of {required_fields} in JSON body"}), 400

    transaction_id = data["transaction_id"]
    base_url = data["base_url"].rstrip("/")
    process_var_name = data["process_var_name"]

    try:
        transaction = fetch_transaction(base_url, process_var_name, transaction_id)
        transaction_lines = fetch_transaction_lines(base_url, process_var_name, transaction_id)

        prompt = compose_prompt(transaction, transaction_lines)
        proposal_text_or_response = generate_proposal_with_retry(prompt)

        if isinstance(proposal_text_or_response, tuple):
            return proposal_text_or_response

        pdf_file = create_pdf(proposal_text_or_response)

        try:
            download_url = upload_pdf_to_fileio(pdf_file)
        except Exception as e:
            return jsonify({"error": f"Uploading to file.io failed: {e}"}), 500

        return jsonify({"download_url": download_url})

    except requests.HTTPError as http_err:
        return jsonify({"error": f"HTTP error when fetching transaction data: {http_err}"}), 502
    except Exception as err:
        return jsonify({"error": f"Unexpected error: {err}"}), 500


if __name__ == "__main__":
    app.run(debug=True)
