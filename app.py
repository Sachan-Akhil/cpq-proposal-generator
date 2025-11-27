from flask import Flask, request, send_file, jsonify
from io import BytesIO
from fpdf import FPDF

app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "Hello from CPQ Proposal Generator!", 200

def create_sample_pdf():
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=16)
    pdf.cell(200, 10, txt="Sample Proposal Document", ln=True, align="C")
    pdf.set_font("Arial", size=12)
    pdf.cell(200, 10, txt="This is a dummy proposal. Replace with actual content later.", ln=True, align="L")
    pdf_bytes = BytesIO()
    pdf.output(pdf_bytes)
    pdf_bytes.seek(0)
    return pdf_bytes

@app.route('/generate_proposal_document', methods=['POST'])
def generate_proposal_document():
    if not request.is_json:
        return jsonify({"error": "Content-Type must be application/json"}), 415

    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid or missing JSON body"}), 400

    transaction_id = data.get("transaction_id")
    if not transaction_id:
        return jsonify({"error": "Missing 'transaction_id' in JSON body"}), 400

    pdf_file = create_sample_pdf()
    
    return send_file(pdf_file,
                     mimetype='application/pdf',
                     as_attachment=True,
                     download_name=f"Proposal_{transaction_id}.pdf")

if __name__ == "__main__":
    app.run()
