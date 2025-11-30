import os
import time
from io import BytesIO
import base64
import requests
from flask import Flask, request, jsonify, Response
from openai import OpenAI
from requests.auth import HTTPBasicAuth
import boto3
from botocore.exceptions import NoCredentialsError, ClientError
from fpdf import FPDF  # fpdf2

app = Flask(__name__)
client = OpenAI()

AWS_REGION = os.getenv("AWS_REGION")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME")

if not AWS_REGION or not S3_BUCKET_NAME:
    raise Exception("AWS_REGION and S3_BUCKET_NAME environment variables must be set")

s3_client = boto3.client('s3', region_name=AWS_REGION)

ORACLE_CPQ_USERNAME = os.getenv("ORACLE_CPQ_USERNAME")
ORACLE_CPQ_PASSWORD = os.getenv("ORACLE_CPQ_PASSWORD")

SERVICE_AUTH_USERNAME = os.getenv("API_AUTH_USERNAME")
SERVICE_AUTH_PASSWORD = os.getenv("API_AUTH_PASSWORD")

HEADERS = {"Accept": "application/json"}


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
            return Response("Unauthorized", 401, {"WWW-Authenticate": 'Basic realm="Login Required"'})
        return f(*args, **kwargs)
    decorated.__name__ = f.__name__
    return decorated


def fetch_transaction(base_url, process_var_name, transaction_id):
    api_base = f"https://{base_url}/rest/v19/commerceDocuments{process_var_name}Transaction"
    url = f"{api_base}/{transaction_id}"
    resp = requests.get(url, headers=HEADERS, auth=HTTPBasicAuth(ORACLE_CPQ_USERNAME, ORACLE_CPQ_PASSWORD))
    resp.raise_for_status()
    return resp.json()


def fetch_transaction_lines(base_url, process_var_name, transaction_id):
    api_base = f"https://{base_url}/rest/v19/commerceDocuments{process_var_name}Transaction"
    url = f"{api_base}/{transaction_id}/transactionLine"
    resp = requests.get(url, headers=HEADERS, auth=HTTPBasicAuth(ORACLE_CPQ_USERNAME, ORACLE_CPQ_PASSWORD))
    resp.raise_for_status()
    return resp.json()


def create_pdf(proposal_text, logo_path=None):
    pdf = FPDF(format='A4')
    pdf.set_left_margin(15)
    pdf.set_right_margin(15)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    font_path = os.path.join(os.path.dirname(__file__), 'DejaVuSans.ttf')
    pdf.add_font('DejaVu', '', font_path, uni=True)
    pdf.add_font('DejaVu', 'B', font_path, uni=True)

    # Add logo if exists
    if logo_path and os.path.isfile(logo_path):
        pdf.image(logo_path, x=15, y=15, w=40)
        pdf.ln(25)
    else:
        pdf.ln(20)

    # Split lines
    lines = proposal_text.split('\n')

    pdf.set_font('DejaVu', 'B', 18)
    # Title line (first line)
    if lines:
        pdf.multi_cell(0, 12, lines[0])
        pdf.ln(5)

    pdf.set_font('DejaVu', '', 12)

    bullet_indent = 10
    normal_indent = 5
    usable_width = pdf.w - pdf.l_margin - pdf.r_margin

    i = 1
    while i < len(lines):
        line = lines[i].strip()

        if not line:  # Blank line for spacing
            pdf.ln(8)
            i += 1
            continue

        if line.startswith("### "):  # Section header
            header_text = line[4:].strip()
            pdf.set_font('DejaVu', 'B', 14)
            pdf.ln(5)
            pdf.cell(0, 10, header_text, ln=True)
            pdf.ln(2)
            pdf.set_font('DejaVu', '', 12)
            i += 1
            continue

        if line.startswith("-"):  # Bullet list item
            pdf.set_x(pdf.l_margin + bullet_indent)
            pdf.multi_cell(usable_width - bullet_indent, 8, line)
            i += 1
            continue

        if "|" in line:  # Table starts
            table_lines = []
            while i < len(lines) and "|" in lines[i]:
                table_lines.append(lines[i].strip())
                i += 1
            render_simple_table(pdf, table_lines)
            pdf.ln(5)
            continue

        # Normal paragraph text
        pdf.set_x(pdf.l_margin + normal_indent)
        pdf.multi_cell(usable_width - normal_indent, 8, line)
        i += 1

    pdf_bytes = pdf.output(dest='S')
    return BytesIO(pdf_bytes)


def render_simple_table(pdf, table_lines):
    if not table_lines:
        return

    # Parse columns from first line (header)
    cols = [c.strip() for c in table_lines[0].split("|") if c.strip() != ""]
    col_count = len(cols)
    page_width = pdf.w - pdf.l_margin - pdf.r_margin
    col_width = page_width / col_count

    # Header row: gray fill and bold
    pdf.set_font('DejaVu', 'B', 12)
    pdf.set_fill_color(230, 230, 230)
    for col in cols:
        pdf.cell(col_width, 10, col, border=1, fill=True)
    pdf.ln()

    # Data rows
    pdf.set_font('DejaVu', '', 12)
    for line in table_lines[1:]:
        cells = [c.strip() for c in line.split("|") if c.strip() != ""]
        for cell in cells:
            pdf.cell(col_width, 8, cell, border=1)
        pdf.ln()


# Other functions (generate_proposal_with_retry, extract_string, compose_prompt, etc.)
# Use those from your existing code without changes.

# Be sure to update your upload_pdf_to_s3 function to:
def upload_pdf_to_s3(pdf_bytes_io, transaction_id):
    pdf_bytes_io.seek(0)
    s3_key = f"proposals/CPQ_Proposal_{transaction_id}.pdf"

    try:
        s3_client.upload_fileobj(
            pdf_bytes_io, 
            S3_BUCKET_NAME, 
            s3_key,
            ExtraArgs={"ContentType": "application/pdf"}  # no ACL param
        )
    except NoCredentialsError:
        raise Exception("AWS credentials not found or invalid")
    except ClientError as e:
        raise Exception(f"Failed to upload to S3: {e}")

    if AWS_REGION == "us-east-1":
        url = f"https://{S3_BUCKET_NAME}.s3.amazonaws.com/{s3_key}"
    else:
        url = f"https://{S3_BUCKET_NAME}.s3.{AWS_REGION}.amazonaws.com/{s3_key}"

    return url

# Flask route handler as before...

if __name__ == "__main__":
    app.run(debug=True)
