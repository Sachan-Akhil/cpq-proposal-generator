from flask import Flask, request, send_file, jsonify
from io import BytesIO
from fpdf import FPDF
import os
import time
from openai import OpenAI

app = Flask(__name__)
client = OpenAI()

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
                    time.sleep(backoff ** attempt)  # exponential backoff
                else:
                    return jsonify({"error": "OpenAI API rate limit exceeded. Please try again later."}), 429
            else:
                return jsonify({"error": f"OpenAI API error: {str(e)}"}), 500

@app.route('/generate_proposal_document', methods=['POST'])
def generate_proposal_document():
    if not request.is_json:
        return jsonify({"error": "Content-Type must be application/json"}), 415

    data = request.get_json()
    if not data or "transaction_id" not in data:
        return jsonify({"error": "Missing 'transaction_id' in JSON body"}), 400

    transaction_id = data["transaction_id"]
    prompt = f"Write a professional sales proposal for the CPQ quote transaction ID: {transaction_id}."

    proposal_text_or_response = generate_proposal_with_retry(prompt)
    if isinstance(proposal_text_or_response, tuple):
        # means it's a Flask response (error), just return that
        return proposal_text_or_response

    pdf_file = create_pdf(proposal_text_or_response)

    return send_file(pdf_file,
                     mimetype='application/pdf',
                     as_attachment=True,
                     download_name=f"Proposal_{transaction_id}.pdf")

if __name__ == "__main__":
    app.run()
