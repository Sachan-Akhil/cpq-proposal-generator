import os
import time
from io import BytesIO

import requests
from flask import Flask, request, send_file, jsonify
from fpdf import FPDF
from openai import OpenAI
from requests.auth import HTTPBasicAuth

app = Flask(__name__)
client = OpenAI()

# Read Basic Auth credentials from environment variables
ORACLE_CPQ_USERNAME = os.getenv("ORACLE_CPQ_USERNAME")
ORACLE_CPQ_PASSWORD = os.getenv("ORACLE_CPQ_PASSWORD")

HEADERS = {
    "Accept": "application/json"
}

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

def create_pdf(text):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)
    for line in text.split("\n"):
        pdf.multi_cell(0, 10, line)
    pdf_output = pdf.output(dest='S').encode('latin1')
    pdf_bytes = BytesIO(pdf_output)
    pdf_bytes.seek(0)
    return pdf_bytes

def generate_proposal_with_retry(prompt_text, max_retries=3, backoff=2):
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {"role": "system", "content": "You are a helpful sales assistant."},
                    {"role": "user", "content": prompt_text}
                ],
                max_tokens=1500,
                temperature=0.7,
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

def compose_prompt(transaction):
    customer_name = transaction.get("_customer_t_company_name", "Unknown Customer")
    transaction_name = transaction.get("transactionName_t", "N/A")
    total_value = transaction.get("totalContractValue_t", "N/A")
    currency = transaction.get("currency_t", "USD")

    """
    lines_text = ""
    for i, line in enumerate(lines, start=1):
        desc = line.get("displayedItemName_l") or line.get("_part_desc", "N/A")
        qty = line.get("requestedQuantity_l", "1")
        unit_price = line.get("_price_unit_price_each", "0")
        try:
            line_total = float(qty) * float(unit_price)
        except (ValueError, TypeError):
            line_total = 0
        lines_text += f"{i}. {desc} - Quantity: {qty}, Unit Price: {unit_price} {currency}, Line Total: {line_total:.2f} {currency}\n"
    """
    prompt = (
        f"Write a professional sales proposal for the customer {customer_name}.\n"
        f"Transaction name: {transaction_name}\n"
        f"Total Contract Value: {total_value} {currency}\n"
        f"Here are the line items:\n{lines_text}\n"
        "Include an introduction, overview of the items, pricing details, and terms and conditions suitable for a sales proposal."
    )
    return prompt

@app.route('/generate_proposal_document', methods=['POST'])
def generate_proposal_document():
    if not request.is_json:
        return jsonify({"error": "Content-Type must be application/json"}), 415

    data = request.get_json()
    required_fields = ["transaction_id", "base_url", "process_var_name"]
    if not data or not all(field in data for field in required_fields):
        return jsonify({"error": f"Missing one of {required_fields} in JSON body"}), 400

    transaction_id = data["transaction_id"]
    base_url = data["base_url"].rstrip("/")  # remove trailing slash if present
    process_var_name = data["process_var_name"]

    try:
        transaction = fetch_transaction(base_url, process_var_name, transaction_id)
       # transaction_lines = fetch_transaction_lines(base_url, process_var_name, transaction_id)

        # prompt = compose_prompt(transaction, transaction_lines)
        prompt = compose_prompt(transaction)
        proposal_text_or_response = generate_proposal_with_retry(prompt)

        if isinstance(proposal_text_or_response, tuple):
            return proposal_text_or_response  # error response from OpenAI wrapper

        pdf_file = create_pdf(proposal_text_or_response)
        return send_file(
            pdf_file,
            mimetype='application/pdf',
            as_attachment=True,
            download_name=f"Proposal_{transaction_id}.pdf"
        )
    except requests.HTTPError as http_err:
        return jsonify({"error": f"HTTP error when fetching transaction data: {http_err}"}), 502
    except Exception as err:
        return jsonify({"error": f"Unexpected error: {err}"}), 500

if __name__ == "__main__":
    app.run(debug=True)
